"""One-request sudo authorization. Never poll Telegram separately from the gateway.

The administrator explicitly accepts Bot API (not E2EE) password transport.
Only IDs and status are returned before execution; secrets never enter tool args.
"""
import asyncio
import ctypes
import json
import os
import resource
import secrets
import subprocess
import threading
import time

# Operator configuration, never inferred from a model request or first Telegram visitor.
def configured_owner():
    value = os.environ.get('HERMES_TELEGRAM_ROOT_OWNER_ID', '')
    return int(value) if value.isascii() and value.isdigit() and int(value) > 0 else None


OWNER = configured_owner()
AUTH_TTL = 3600
EXEC_TIMEOUT = 1800
PROMPT_PREFIX = '⚠️ 單次完整 root 操作授權'
_lock = threading.Lock()
_active = None
_loop = None
_bot = None
# Prompt IDs only, including retired requests, so late password replies stay out of models.
_prompts = set()
_revoked_updates = set()
_tasks = set()


def harden():
    # Credential retention is RAM-only; prevent ordinary core dump disclosure.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        raise RuntimeError('privacy_setup_failed')


class Pending:
    def __init__(self, command, reason):
        self.command = command
        self.reason = reason
        self.nonce = secrets.token_urlsafe(18)
        self.deadline = time.monotonic() + AUTH_TTL
        self.prompt_id = None
        self.claimed = False
        self.done = threading.Event()
        self.accepted = threading.Event()
        self.result = None
        self.process = None


def finish(pending, result):
    with _lock:
        if pending.result is None:
            pending.result = result
            pending.done.set()


def execute(pending, password):
    """No password in argv, environment, exception text, disk or sudo timestamp cache."""
    try:
        process = subprocess.Popen(
            ['/usr/bin/sudo', '-k', '-S', '-p', '', '--', '/usr/bin/env', '-i',
             'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
             'HOME=/root', '/bin/bash', '-c', pending.command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, cwd='/',
        )
        with _lock:
            pending.process = process
        try:
            out, err = process.communicate((password + '\n').encode(), timeout=EXEC_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Do not kill a live deployment during rollback. Preserve no secret input/output.
            finish(pending, {'ok': False, 'status': 'execution_still_running', 'pid': process.pid,
                             'warning': 'Do not retry blindly; investigate completion/rollback.'})
            process.communicate()
            return
        # Commands can echo stdin or diagnostics; redact exact secret before returning any output.
        secret = password.encode()
        out = out.replace(secret, b'[REDACTED]')
        err = err.replace(secret, b'[REDACTED]')
        finish(pending, {'ok': process.returncode == 0, 'status': 'completed',
                         'exit_code': process.returncode,
                         'stdout': out[:16000].decode(errors='replace'),
                         'stderr': err[:4000].decode(errors='replace')})
    except Exception:
        finish(pending, {'ok': False, 'status': 'execution_failed'})
    finally:
        if 'process' in locals():
            # communicate() retains its input on Popen; do not pin a password via Pending.
            process._input = None
        with _lock:
            pending.process = None
        password = None


async def prompt(pending):
    from telegram import ForceReply
    msg = await _bot.send_message(
        chat_id=OWNER,
        text=(PROMPT_PREFIX + '\n理由：' + pending.reason +
              '\n\n即將執行的完整指令：\n' + pending.command +
              '\n\nBot 聊天不是端到端加密。接受風險才回覆這則訊息，輸入執行 Hermes 的主機帳號 sudo 密碼。'
              '\n不要加上指令或引號；回覆「取消」拒絕。最多 1 小時內有效，密碼訊息刪除失敗不執行。'),
        reply_markup=ForceReply(selective=True, input_field_placeholder='sudo 密碼／取消'),
        protect_content=True,
    )
    with _lock:
        pending.prompt_id = msg.message_id
        _prompts.add((OWNER, msg.message_id))
    return msg.message_id


def request(params, **kwargs):
    global _active
    if OWNER is None:
        return json.dumps({'ok': False, 'status': 'administrator_not_configured'})
    if (not isinstance(params, dict) or set(params) != {'command', 'reason'}
            or not isinstance(params['command'], str) or not 0 < len(params['command']) <= 2400
            or not isinstance(params['reason'], str) or not 0 < len(params['reason']) <= 400
            or '\x00' in params['command'] or '\x00' in params['reason']):
        return json.dumps({'ok': False, 'status': 'invalid_arguments'})
    if _loop is None or not _loop.is_running() or _bot is None:
        return json.dumps({'ok': False, 'status': 'telegram_gateway_unavailable'})
    try:
        if asyncio.get_running_loop() is _loop:
            return json.dumps({'ok': False, 'status': 'tool_must_run_off_event_loop'})
    except RuntimeError:
        pass
    pending = Pending(params['command'], params['reason'])
    with _lock:
        if _active is not None:
            return json.dumps({'ok': False, 'status': 'another_request_active'})
        _active = pending
    try:
        future = asyncio.run_coroutine_threadsafe(prompt(pending), _loop)
        try:
            future.result(timeout=20)
        except Exception:
            future.cancel()
            return json.dumps({'ok': False, 'status': 'prompt_failed'})
        if not pending.accepted.wait(max(0, pending.deadline - time.monotonic())):
            with _lock:
                pending.claimed = True
            finish(pending, {'ok': False, 'status': 'authorization_expired'})
        elif not pending.done.wait(EXEC_TIMEOUT + 30):
            finish(pending, {'ok': False, 'status': 'authorization_or_execution_timeout'})
        return json.dumps(pending.result, ensure_ascii=False)
    finally:
        with _lock:
            # Close admission even when sending/waiting fails. Late replies remain intercepted.
            pending.claimed = True
            if _active is pending:
                _active = None


async def receive(update, context):
    from telegram.ext import ApplicationHandlerStop
    msg = update.effective_message
    if msg is None:
        return
    chat = msg.chat
    reply = getattr(msg, 'reply_to_message', None)
    reply_key = (chat.id, reply.message_id) if reply else None
    text = getattr(msg, 'text', None) or ''
    if text.split('@', 1)[0] == '/root_test':
        try:
            if chat.type == 'private' and chat.id == OWNER and msg.from_user is not None and msg.from_user.id == OWNER:
                async def identity_test():
                    try:
                        result = json.loads(await asyncio.to_thread(request, {
                            'command': '/usr/bin/id -u',
                            'reason': '唯讀測試：只確認 sudo UID，沒有部署或修改 Ymir。',
                        }))
                        ok = result.get('ok') and result.get('stdout', '').strip() == '0'
                        text = ('✅ 單次 sudo 測試成功，UID 0；沒有重新部署或修改 Ymir。'
                                if ok else '⚠️ 單次 sudo 測試未成功。狀態：' + str(result.get('status', 'failed')))
                        await context.bot.send_message(chat_id=OWNER, text=text)
                    except Exception:
                        pass  # Never expose an update, exception or credential to a log.
                task = asyncio.create_task(identity_test())
                _tasks.add(task)
                task.add_done_callback(_tasks.discard)
        finally:
            raise ApplicationHandlerStop
    with _lock:
        # Identify replies to retired prompts even after gateway restart, without storing secrets.
        retired_prompt = reply is not None and (getattr(reply, 'text', None) or '').startswith(PROMPT_PREFIX)
        recognized = reply_key in _prompts or retired_prompt or text.startswith('/sudo_password')
        if not recognized:
            return
        # All matched messages must stop before core transcript, observer, or model handlers.
        key = getattr(update, 'update_id', None)
        duplicate = key is not None and key in _revoked_updates
        if key is not None:
            _revoked_updates.add(key)
        pending = _active
        authorized = (not duplicate and chat.type == 'private' and chat.id == OWNER
                      and msg.from_user is not None and msg.from_user.id == OWNER
                      and pending is not None and reply_key == (OWNER, pending.prompt_id)
                      and not pending.claimed and time.monotonic() < pending.deadline)
        if authorized:
            pending.claimed = True
            pending.accepted.set()
    try:
        # Delete BEFORE handing even a valid password to sudo; fail closed if deletion fails.
        try:
            deleted = await context.bot.delete_message(chat_id=chat.id, message_id=msg.message_id)
        except Exception:
            deleted = False
        if not authorized:
            return
        if deleted is not True:
            finish(pending, {'ok': False, 'status': 'password_message_deletion_failed'})
            return
        if text.strip().lower() in ('取消', 'cancel', '/cancel'):
            finish(pending, {'ok': False, 'status': 'cancelled'})
            return
        if not text or len(text) > 1024 or '\n' in text or '\r' in text or '\x00' in text:
            finish(pending, {'ok': False, 'status': 'invalid_password_reply'})
            return
        worker = threading.Thread(target=execute, args=(pending, text), daemon=True)
        worker.start()
    finally:
        # Never stringify exceptions/update objects containing the password.
        text = None
        raise ApplicationHandlerStop


def install(native, adapter):
    global _loop, _bot
    from telegram import Update
    from telegram.ext import TypeHandler
    harden()
    _loop = asyncio.get_running_loop()
    _bot = native.bot
    native.add_handler(TypeHandler(Update, receive), group=-100)
