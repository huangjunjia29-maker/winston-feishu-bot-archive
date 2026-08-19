# ACTIVATE FIRST — 重新激活指南

> **最高优先级文件。** 新智能体进入本项目后，必须先完整阅读本文件，再执行其他操作。

## 0. 当前状态

这是不含 Git/Trellis/开发历史和浏览器登录态的干净发布副本。默认状态是：

```text
NOT ACTIVATED
```

不要因为代码可以解析或 `--help` 能运行，就把项目视为已经激活。只有完成本文全部检查后，才可以启动长期监听器或执行真实业务工作流。

## 1. 激活前的禁止事项

在完成配置和 dry-run 前：

- 不要启动长期 Bot 监听器。
- 不要执行 Base 写入。
- 不要执行真实社交平台批量抓取。
- 不要打印 `bot/.env` 或 `workflow/env/*.env` 的值。
- 不要把普通 Chrome 的个人用户目录作为 CDP Profile。
- 不要把旧项目的绝对路径、Base Token、表 ID、群 ID 或账号登录态恢复进来。

## 2. 确认项目根目录

所有命令都从本文件所在目录执行。PowerShell 示例：

```powershell
Set-Location -LiteralPath $PSScriptRoot
$ProjectRoot = (Get-Location).Path
```

不要假设项目仍位于原始开发路径。

## 3. 环境文件人工处理门禁

以下文件是从源工作区复制的配置载体，项目所有者会手动清理和重新填写：

```text
bot/.env
workflow/env/global.env
workflow/env/cdp.env
workflow/env/chats.env
workflow/env/bases.env
```

智能体必须先向项目所有者确认：

1. 旧凭证已经清除。
2. 新飞书 App、群聊、Base 和表配置已经填写。
3. 旧绝对路径已经改为当前项目路径。

只允许检查键是否存在、值是否为空、路径是否指向当前根目录；不得回显具体值。

至少要确认这些路径配置指向当前项目：

```text
JIELONG_BOT_WORKDIR=<current-project-root>
JIELONG_SOCIAL_WORKDIR=<current-project-root>
SOCIAL_CDP_PROFILE=<current-project-root>\workflow\assets\chrome-cdp-profile
BACKFILL_XLSX_DIR=<current-project-root>\exports
```

业务资源必须通过环境配置提供，不得写回代码。发布副本已移除原项目的私人 Base/Table/View/Field 默认标识。

## 4. 安装本机依赖

推荐 Python 3.12、Node.js、Google Chrome。

```powershell
python --version
node --version

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

安装 lark-cli：

```powershell
npm install -g @larksuite/cli
lark-cli --version
```

## 5. 初始化飞书 Bot 身份

lark-cli 身份保存在本机，不随项目目录迁移。使用新的飞书应用重新初始化：

```powershell
lark-cli config init
lark-cli auth status --json --verify
```

要求：Bot identity 必须显示 ready/available。Bot 身份不需要执行用户 `auth login`。

同时在飞书开发者后台确认：

- 已启用机器人。
- 已配置并发布所需权限。
- 已启用长连接事件。
- 已订阅 `im.message.receive_v1`。
- 机器人已加入允许执行命令的群聊。

## 6. 初始化空白 CDP 浏览器

本项目只允许使用：

```text
workflow/assets/chrome-cdp-profile
```

确认目录中没有旧账号状态后运行：

```powershell
python -X utf8 workflow/modules/social_metrics.py --start-cdp-profile
```

在打开的独立 Chrome 中，按业务需要重新登录 Instagram、Facebook、TikTok、LinkedIn、X 等平台。完成后正常关闭 Chrome。

## 7. 本地无写入验证

依次执行：

```powershell
python -X utf8 bot/scripts/feishu_bot.py parse "@机器人 帮助"
python -X utf8 workflow/scripts/feishu_workflow.py --help
python -m compileall -q bot workflow
```

配置好 Base 后，先执行相应工作流的 `--dry-run`。任何缺少配置、权限、字段或视图的问题都必须先修复，不得用默认值绕过。

## 8. 前台监听验证

```powershell
python -X utf8 bot/scripts/feishu_bot.py listen
```

在允许的飞书群发送：

```text
@机器人 帮助
```

确认机器人能够收到消息并正确回复。使用 `Ctrl+C` 停止前台测试。

## 9. 正式后台启动

前台验证成功后：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\bot\scripts\run_listener.ps1
```

长期运行可将该命令配置到 Windows 任务计划程序。

## 10. 激活完成记录

只有以下全部通过后，才创建本地激活记录：

- 环境路径指向当前项目。
- lark-cli Bot identity ready。
- CDP 使用项目空白 Profile 并已完成所需平台登录。
- Python compile 通过。
- Bot 命令解析通过。
- 工作流 CLI help 通过。
- 飞书群前台监听收发通过。
- 至少一个目标工作流 dry-run 通过。

创建不含密钥的本地记录：

```powershell
@"
activated_at=$(Get-Date -Format o)
project_root=$((Get-Location).Path)
bot_identity=verified
cdp_profile=verified
parse_smoke=passed
workflow_help=passed
listener_smoke=passed
dry_run=passed
"@ | Set-Content -LiteralPath .\ACTIVATED.local -Encoding UTF8
```

`ACTIVATED.local`只代表该机器、该配置在记录时间点通过验证；迁移到另一台机器或更换飞书应用后必须重新激活。
