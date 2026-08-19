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

长任务的静默心跳默认每 300 秒汇报一次；工作流产生新阶段、完成或失败事件时仍即时通知。

## 配置文件

```text
bot/.env
workflow/env/global.env
workflow/env/cdp.env
workflow/env/chats.env
workflow/env/bases.env
```

这些文件可能包含敏感信息，不得提交、打印或对外发送。发布副本不包含任何代码内置的私人 Base、表、视图或字段标识；业务资源必须通过环境配置提供。

## CDP 浏览器

项目使用独立 Profile：

```text
workflow/assets/chrome-cdp-profile
```

不要替换为个人 Chrome Profile。首次激活时通过 `--start-cdp-profile`打开浏览器并重新登录所需平台。
