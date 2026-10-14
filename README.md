# Hermes Telegram Root

Hermes Agent 的 Telegram **單次 root 指令授權插件**。Agent 先展示完整指令與理由，指定管理者在私人對話回覆 sudo 密碼；插件在訊息進入模型前攔截回覆，刪除成功後才執行指令。

> **高風險功能：Telegram Bot 聊天不是端到端加密。** 使用此插件表示接受密碼經過 Telegram 伺服器與 Bot API 的風險。刪除訊息不保證 Telegram、客戶端或其他第三方未留存副本。優先考慮本機遮蔽密碼表單或受限部署授權，而不是傳送密碼。

## 功能

- 僅接受預先配置的 Telegram 管理者私人對話。
- 每次顯示完整指令，120 秒內回覆授權或取消；每次指令都需重新授權。
- 在 Hermes 核心聊天／模型／觀察器處理前攔截授權回覆。
- 密碼訊息刪除失敗時拒絕執行；重複、過期、非管理者及群組回覆不授權。
- 使用 stdin 傳入 `sudo -k -S`；密碼不在 argv、環境變數或插件檔案中。
- 不設定 `NOPASSWD`，不儲存 `SUDO_PASSWORD`，不開啟持續 root shell。
- `/root_test` 僅執行 `id -u`，確認單次 sudo 是否成功。

## 必要條件

- Linux 主機、Python 3.10+、`/usr/bin/sudo`、`/bin/bash`。
- 執行 Hermes 的主機帳號具有既有 sudo 權限；插件不自動修改 sudoers。
- 已設定 Telegram 的 Hermes Gateway。
- Hermes 插件 API 支援 `register_telegram_handler`、`register_tool`，Telegram adapter 使用 python-telegram-bot handler 與 `ApplicationHandlerStop`。

不同 Hermes 版本的插件 API 可能不同；升級後需重跑測試並核對攔截 handler 已載入。

## 安裝

1. 將 `telegram-root/` 複製到 `${HERMES_HOME:-$HOME/.hermes}/plugins/telegram-root/`。
2. 在 Hermes 的私有 `.env` 加入你自己的 **數字 Telegram user ID**：

   ```dotenv
   HERMES_TELEGRAM_ROOT_OWNER_ID=YOUR_NUMERIC_TELEGRAM_USER_ID
   ```

   必須替換為正整數；未配置／無效時工具拒絕授權。不配置任何 sudo 密碼或 Bot token 到本倉庫。

3. 在 Hermes `config.yaml` 的既有 `plugins.enabled` 清單追加，**不要覆蓋其他插件**：

   ```yaml
   plugins:
     enabled:
       - telegram-root
   ```

4. 重啟 Hermes Gateway。若使用 user systemd service：

   ```bash
   systemctl --user restart hermes-gateway.service
   ```

   重啟會中斷 Hermes 正在進行的工作，請先確認沒有執行中的授權或部署。

## 使用

先在管理者與 Bot 的私人對話輸入 `/root_test`。收到 root 操作提示後，**回覆該提示**輸入執行 Hermes 的主機帳號 sudo 密碼，或回覆「取消」。不要把密碼直接送進一般聊天，也不要將密碼放入工具參數。

Agent 的工具介面：

```json
{
  "command": "/usr/bin/id -u",
  "reason": "唯讀確認 sudo UID，不修改系統。"
}
```

工具名稱是 `telegram_root`。成功的唯讀測試回傳 UID `0`；部署應另行取得你對實際部署指令的授權，不能把測試成功當成部署成功。

## 安全限制

詳見 [SECURITY.md](SECURITY.md)。此插件允許**任意 root 指令**，不是沙箱或受限部署服務。核准前務必檢閱完整指令。指令可能建立永久權限、讀取機密或停用保護，因此「單次」不能保證核准指令不留下持續影響。

密碼僅在程序記憶體短期使用，但 Python 不保證所有副本立即清除。插件停用普通 core dump，不防止已入侵主機、root debugger、swap 或上游 Telegram／HTTP debug logging。

指令 stdout／stderr 會傳回 Agent，**不要核准輸出機密的指令**。執行環境是乾淨 root 環境，工作目錄 `/`；需要的非機密環境、路徑與 rootless Podman 身份需在指令中明確指定。

執行超過 30 分鐘會回報仍在執行，不強殺部署或 rollback。不要盲目重試，請先檢查執行狀態。Gateway 重啟後不得回覆舊提示。

## 測試

```bash
python3 telegram-root/test_broker.py
```

測試只使用假 Telegram 訊息與 subprocess，不呼叫 Telegram、不需要真密碼、不執行 sudo。真實 Bot 刪除、sudo 和 Hermes 工具載入仍需在安裝主機測試。

## 檔案

```text
telegram-root/
  plugin.yaml       插件 metadata
  __init__.py       註冊工具與 Telegram handler
  broker.py         授權、訊息攔截與單次 sudo 執行
  test_broker.py    離線回歸測試
```

本倉庫不包含個人 Telegram ID、Bot token、密碼、Hermes 設定檔或執行紀錄，也不包含特定產品的部署腳本。
