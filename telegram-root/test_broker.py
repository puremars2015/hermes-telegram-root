import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class Stop(Exception):
    pass


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('test_root_broker', Path(__file__).with_name('broker.py'))
        self.b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.b)
        self.b.OWNER = 123456789
        self.modules = patch.dict(sys.modules, {'telegram.ext': types.SimpleNamespace(ApplicationHandlerStop=Stop)})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.pending = self.b.Pending('/usr/bin/id -u', 'test')
        self.pending.prompt_id = 10
        self.b._active = self.pending
        self.b._prompts.add((self.b.OWNER, 10))
        self.deleted = []

    def update(self, text='fixture-password', owner=None, private=True, prompt=10, update_id=1):
        owner = self.b.OWNER if owner is None else owner
        msg = types.SimpleNamespace(text=text, chat=types.SimpleNamespace(id=owner, type='private' if private else 'group'),
            from_user=types.SimpleNamespace(id=owner), message_id=11,
            reply_to_message=types.SimpleNamespace(message_id=prompt, text=self.b.PROMPT_PREFIX))
        return types.SimpleNamespace(effective_message=msg, update_id=update_id)

    async def handle(self, update, deletion=True):
        async def delete_message(**kwargs):
            self.deleted.append(kwargs)
            return deletion
        context = types.SimpleNamespace(bot=types.SimpleNamespace(delete_message=delete_message))
        with self.assertRaises(Stop):
            await self.b.receive(update, context)

    async def test_valid_reply_deleted_before_execution(self):
        calls = []
        def execute(pending, password):
            self.assertEqual(len(self.deleted), 1)
            calls.append(password)
        with patch.object(self.b, 'execute', execute):
            await self.handle(self.update())
            await asyncio.sleep(.02)
        self.assertEqual(calls, ['fixture-password'])
        self.assertTrue(self.pending.claimed)

    async def test_delete_failure_never_executes(self):
        with patch.object(self.b, 'execute') as execute:
            await self.handle(self.update(), deletion=False)
            execute.assert_not_called()
        self.assertEqual(self.pending.result['status'], 'password_message_deletion_failed')
        self.assertNotIn('fixture-password', str(self.pending.result))

    async def test_wrong_owner_group_expired_and_wrong_prompt_are_swallowed(self):
        for update in [self.update(owner=123), self.update(private=False), self.update(prompt=99)]:
            with patch.object(self.b, 'execute') as execute:
                await self.handle(update)
                execute.assert_not_called()
        self.pending.deadline = 0
        with patch.object(self.b, 'execute') as execute:
            await self.handle(self.update(update_id=2))
            execute.assert_not_called()

    async def test_retired_prompt_after_restart_never_enters_chat(self):
        self.b._active = None
        self.b._prompts.clear()
        await self.handle(self.update())
        self.assertEqual(len(self.deleted), 1)

    async def test_cancel(self):
        with patch.object(self.b, 'execute') as execute:
            await self.handle(self.update('取消'))
            execute.assert_not_called()
        self.assertEqual(self.pending.result['status'], 'cancelled')

    async def test_invalid_multiline_password(self):
        await self.handle(self.update('fixture\npassword'))
        self.assertEqual(self.pending.result['status'], 'invalid_password_reply')

    async def test_duplicate_does_not_execute_twice(self):
        with patch.object(self.b, 'execute') as execute:
            await self.handle(self.update())
            await asyncio.sleep(.02)
            await self.handle(self.update())
            self.assertEqual(execute.call_count, 1)

    async def test_normal_message_not_intercepted(self):
        update = self.update('hello')
        update.effective_message.reply_to_message = None
        await self.b.receive(update, types.SimpleNamespace())

    async def test_identity_command_denies_wrong_owner(self):
        with patch.object(self.b, 'request') as request:
            await self.handle(self.update('/root_test', owner=123))
            request.assert_not_called()

    async def test_identity_command_stays_out_of_models(self):
        sent = []
        async def send_message(**kwargs):
            sent.append(kwargs)
        context = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=send_message))
        with patch.object(self.b, 'request', return_value='{"ok":true,"status":"completed","stdout":"0\\n"}') as request:
            with self.assertRaises(Stop):
                await self.b.receive(self.update('/root_test'), context)
            await asyncio.sleep(.05)
            request.assert_called_once_with({'command': '/usr/bin/id -u', 'reason': '唯讀測試：只確認 sudo UID，沒有部署或修改 Ymir。'})
        self.assertEqual(sent[0]['chat_id'], self.b.OWNER)

    def test_execute_redacts_secret_and_uses_stdin_only(self):
        process = types.SimpleNamespace(pid=1, returncode=0, _input=b'fixture-password',
            communicate=lambda value=None, timeout=None: (b'0 fixture-password', b'fixture-password'))
        with patch.object(self.b.subprocess, 'Popen', return_value=process) as popen:
            self.b.execute(self.pending, 'fixture-password')
            args, kwargs = popen.call_args
            self.assertNotIn('fixture-password', str(args) + str(kwargs))
            self.assertIn('-k', args[0])
        self.assertNotIn('fixture-password', str(self.pending.result))
        self.assertIsNone(process._input)
        self.assertIsNone(self.pending.process)

    def test_bad_tool_arguments(self):
        self.assertIn('invalid_arguments', self.b.request({'command': 'id', 'reason': 'test', 'password': 'bad'}))

    def test_missing_owner_fails_closed(self):
        self.b.OWNER = None
        self.assertIn('administrator_not_configured', self.b.request({'command': 'id', 'reason': 'test'}))

    def test_owner_config_rejects_invalid_values(self):
        for value in ['', '-1', '0', 'all', '１２３']:
            with patch.dict(self.b.os.environ, {'HERMES_TELEGRAM_ROOT_OWNER_ID': value}):
                self.assertIsNone(self.b.configured_owner())
        with patch.dict(self.b.os.environ, {'HERMES_TELEGRAM_ROOT_OWNER_ID': '123456789'}):
            self.assertEqual(self.b.configured_owner(), 123456789)


if __name__ == '__main__':
    unittest.main()
