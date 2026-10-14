import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('telegram_root_broker', Path(__file__).with_name('broker.py'))
broker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(broker)


def register(ctx):
    ctx.register_telegram_handler(broker.install)
    ctx.register_tool(
        name='telegram_root', toolset='telegram_root',
        schema={
            'name': 'telegram_root',
            'description': '向指定管理者的 Telegram 私訊要求單次 root 指令授權。必須先說明影響；command 會完整顯示給管理者。密碼只能回覆專用提示，不能傳入工具參數。刪除密碼訊息後才執行。每次重新授權，不提供持續 root shell。逾時不代表執行成功；不要重複執行不可冪等操作。禁止用來擷取／持久保存 root 或 sudo 密碼、停用此授權防護。',
            'parameters': {
                'type': 'object',
                'properties': {'command': {'type': 'string'}, 'reason': {'type': 'string'}},
                'required': ['command', 'reason'], 'additionalProperties': False,
            },
        }, handler=broker.request,
    )
