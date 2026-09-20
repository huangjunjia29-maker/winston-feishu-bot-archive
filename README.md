# Feishu Bot Workflow

一个本地运行的飞书命令机器人与工作流编排项目。机器人负责接收群聊命令、管理任务和发送回复；工作流负责 Base、XLSX、社交数据采集和 CDP 浏览器操作。

首次使用必须先阅读并执行 [`ACTIVATE_FIRST.md`](ACTIVATE_FIRST.md)。

从 GitHub 在新设备恢复时，按 [`RESTORE_ON_NEW_DEVICE.md`](RESTORE_ON_NEW_DEVICE.md) 操作。

## 目录结构

```text
bot/                         飞书事件监听、命令解析、任务管理和回复
workflow/                    工作流编排与业务模块
workflow/assets/             项目专用 CDP 浏览器 Profile
workflow/env/                工作流环境配置
exports/                     运行时导出文件
logs/                        运行日志
bot/tmp/                     Bot 运行状态和服务日志
workflow/tmp/                工作流临时状态
ACTIVATE_FIRST.md            最高优先级重新激活说明
requirements.txt             Python 依赖
```

## 软件要求

- Windows PowerShell
- Python 3.12
- Node.js
- Google Chrome
- lark-cli

安装 Python 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

安装 lark-cli：

```powershell
npm install -g @larksuite/cli
```

## 主要入口

解析命令：

```powershell
python -X utf8 bot/scripts/feishu_bot.py parse "@机器人 帮助"
```

前台启动监听器：

```powershell
python -X utf8 bot/scripts/feishu_bot.py listen
```

后台守护运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\bot\scripts\run_listener.ps1
```

安装当前用户登录自启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_autostart.ps1
```

查看工作流参数：

```powershell
python -X utf8 workflow/scripts/feishu_workflow.py --help
```

打开项目专用 CDP 浏览器：

```powershell
python -X utf8 workflow/modules/social_metrics.py --start-cdp-profile
```

## Bot 命令

```text
抓取 <话题> [--dry-run]
抓取本地 <话题> [--dry-run]
72h <话题> [--dry-run]
72h本地 <话题> [--dry-run]
确认推送 <话题> [--dry-run]
二次推送 <话题> [--dry-run]
二次推送本地 <话题> [--dry-run]
今日发布数 [--dry-run]
海外市场账号 <YYYYMMDD-YYYYMMDD> [--dry-run]
状态
停止工作
帮助
```

普通 `72h` 会优先使用 `exports/<话题>.xlsx`；仅当本地文件不存在时才从飞书重新导出。带“本地”后缀的三个指令严格要求对应本地 XLSX 存在，缺失时直接失败，不会回退到飞书导出。
真实写入前必须先运行相应命令的 `--dry-run`。

明确 `@温斯顿` 后发送未注册为业务命令的文本，会进入灵算自由聊天。聊天使用按“群 + 用户”隔离的本地短期上下文；发送 `清空聊天`、`清空对话` 或 `忘记对话` 可清除当前上下文。聊天回答不能触发抓取或 Base 写入。

`WINSTON_CHAT_ALLOWED_CHAT_IDS` 可配置仅开放自由聊天的群聊。此类群聊中的注册业务命令会被静默忽略，不会启动抓取或 Base 工作流。

如需在温斯顿私聊中下达业务指令或聊天，将该私聊会话 ID 写入
`WINSTON_CHAT_ALLOWED_P2P_CHAT_IDS`。这里只允许显式列出的 1:1 会话，不开放
所有私聊；私聊不需要 `@温斯顿`，群聊仍必须明确 @。

长任务的静默心跳默认每 300 秒汇报一次；工作流产生新阶段、完成或失败事件时仍即时通知。

## 配置文件

```text
bot/.env
bot/chat.env
workflow/env/global.env
workflow/env/cdp.env
workflow/env/chats.env
workflow/env/bases.env
```

这些文件可能包含敏感信息，不得提交、打印或对外发送。发布副本不包含任何代码内置的私人 Base、表、视图或字段标识；业务资源必须通过环境配置提供。

`海外市场账号` 必须使用独立来源配置：`WEEKLY_ACCOUNT_BASE_TOKEN`、
`WEEKLY_ACCOUNT_SOURCE_TABLE`、`WEEKLY_ACCOUNT_SOURCE_VIEW`。不要误用目标
Base；真实坐标只保存在本机 `workflow/env/bases.env`，不得提交 GitHub。

## CDP 浏览器

项目使用独立 Profile：

```text
workflow/assets/chrome-cdp-profile
```

不要替换为个人 Chrome Profile。首次激活时通过 `--start-cdp-profile`打开浏览器并重新登录所需平台。

## 恢复 Codex 全局温斯顿能力

仓库的 `codex/winston-feishu/` 保存不含凭证的技能模板。新电脑下载项目后，先按 `ACTIVATE_FIRST.md` 安装依赖、恢复本机配置和所需登录，再从项目目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 install_codex_skill.py
```

脚本会按当前项目路径安装到用户的 `$CODEX_HOME/skills/winston-feishu`（未设置时为 `~/.codex/skills/winston-feishu`），并向用户级 `AGENTS.md` 添加入口。原有指引保留；覆盖前会备份已有文件，重复安装会更新带标记的入口。

如果原全局指引中已有手写的旧温斯顿路径，迁移时须核对并去掉过期入口，避免新旧路径冲突。项目移动后重新运行安装脚本。

重新打开 Codex 后，在新聊天中说：`调用温斯顿，读取这个飞书表格：[链接]`，或使用 `$winston-feishu` 指定技能。

安装后可用项目 Python 执行已安装技能的 `scripts/check_runtime.py --online`，只读验证认证与机器人接口。该备份不包含 Secret、Token、环境文件、浏览器登录态或本机激活记录；仅下载仓库不能自动恢复这些配置，也不会增加飞书资源权限。
