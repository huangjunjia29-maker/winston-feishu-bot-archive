# 温斯顿新设备恢复指南

此仓库保存温斯顿的代码、工作流、配置模板和恢复工具。它不会保存任何真实密钥、飞书群聊历史、浏览器 Cookie、导出数据或运行日志。

## 1. 克隆并初始化

```powershell
git clone <private-repository-url>
cd winston-feishu-bot-archive
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup_new_device.ps1 -InstallDependencies
npm install -g @larksuite/cli
```

脚本会从 `敏感配置模板` 创建本机 `.env`，并自动填写与当前克隆目录相关的路径。已有配置不会被覆盖。

## 2. 恢复飞书身份和业务坐标

在飞书开放平台确认应用已启用机器人、长连接和 `im.message.receive_v1`，然后运行：

```powershell
lark-cli config init
lark-cli auth status --json --verify
```

在本机填写以下文件中的 App ID、群 ID、Base、表和视图坐标：

```text
bot/.env
bot/chat.env
workflow/env/chats.env
workflow/env/bases.env
```

App Secret 不要写进命令行或聊天。使用隐藏输入脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure_feishu_secret.ps1
```

## 3. 恢复灵算聊天

在灵算控制台重新创建或取回 API Key，然后通过隐藏输入窗口配置：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure_lingsuan_chat.ps1
```

默认模型为 `gpt-5.6-terra`，接口为 `https://lingsuan.org/v1/responses`。温斯顿的人格与说话风格保存在代码默认配置中；本地聊天历史不会跨设备恢复。

在 `bot/.env` 分开设置业务命令群与聊天群：

```text
JIELONG_BOT_ALLOWED_CHAT_IDS=允许业务命令的群
WINSTON_CHAT_ALLOWED_CHAT_IDS=仅开放自由聊天的群
```

聊天群中的业务命令会被静默忽略；两类消息都必须明确 `@温斯顿`。

## 4. 恢复社媒登录

CDP Profile 不进入 Git。新设备必须重新打开专用浏览器并登录所需平台：

```powershell
.\.venv\Scripts\python.exe -X utf8 workflow\modules\social_metrics.py --start-cdp-profile
```

只在这个专用浏览器中登录，不要复制个人 Chrome Profile。

## 5. 无写入验证

```powershell
.\.venv\Scripts\python.exe -X utf8 bot\scripts\feishu_bot.py parse "@温斯顿 帮助"
.\.venv\Scripts\python.exe -X utf8 workflow\scripts\feishu_workflow.py --help
.\.venv\Scripts\python.exe -X utf8 -m compileall -q bot workflow
```

先在飞书群执行带 `--dry-run` 的业务命令。确认群、Base、字段、权限和 CDP 登录均正确后，再进行真实写入。

`海外市场账号` 使用独立来源 Base。必须在本机 `workflow/env/bases.env`
恢复以下三项，真实坐标不得进入仓库：

```text
WEEKLY_ACCOUNT_BASE_TOKEN=
WEEKLY_ACCOUNT_SOURCE_TABLE=
WEEKLY_ACCOUNT_SOURCE_VIEW=
```

恢复后先执行一次对应日期范围的 `--dry-run`，确认能读取并校验来源账号，
且没有创建表、抓取、写入或生成实际 JSONL/XLSX 文件。

## 6. 启动

```powershell
.\start_bot.cmd
```

需要在每次 Windows 登录后自动唤醒温斯顿时，运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

计划任务会在当前用户登录 3 分钟后隐藏启动监听器。飞书 CLI 凭据属于当前用户，因此不要将监听器改为登录前运行的系统服务。

在允许的群里测试：

```text
@温斯顿 状态
@温斯顿 介绍一下你自己
```

常规静默心跳每 5 分钟汇报一次；新阶段、完成和失败仍即时通知。

## 不在 GitHub 中的内容

- `*.env` 中的 App Secret、灵算 Key、群 ID 与 Base 坐标
- `workflow/assets/chrome-cdp-profile/` 的 Cookie 和登录态
- `exports/`、`logs/`、`tmp/` 与聊天历史
- `.venv/`、Playwright 浏览器和其他可重新安装依赖
- `ACTIVATED.local`，因为它只对原设备有效

迁移后必须重新执行激活检查，不能把旧设备的激活状态直接视为有效。
