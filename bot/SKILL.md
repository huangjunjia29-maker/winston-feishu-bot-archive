---
name: feishu-bot
description: Thin Feishu/Lark command listener that parses group @bot commands, enforces one active background job, supports stop/status/help, routes registered commands to feishu-workflow, and forwards workflow JSONL progress messages back to the command group.
---

# Feishu Bot

This skill is only the listener and router. Do not put export, scraping, XLSX, CDP, or Base write logic here.

## Runtime

Main script:

```powershell
python <skill-root>\scripts\feishu_bot.py listen
```

It loads `<skill-root>\.env`. Keep command-group allowlist and listener-only settings there.

## Command Registry

The bot maintains one command table:

```text
抓取        -> workflow_id=full_social_scrape
72h         -> workflow_id=refresh_72h
确认推送    -> workflow_id=confirm_push
二次推送    -> workflow_id=secondary_push
今日发布数  -> workflow_id=today_publish_count
海外市场账号 -> workflow_id=weekly_market_accounts
停止工作    -> control=stop
状态        -> control=status
帮助        -> control=help
```

F010/CDP cache cleanup is intentionally not a bot command. It is a workflow-layer maintenance step that can be run locally with a workflow report, and is appended silently after social scraping workflows.

Supported group commands:

```text
@机器人 抓取 <话题>
@机器人 抓取本地 <话题>
@机器人 72h <话题>
@机器人 72h本地 <话题>
@机器人 确认推送 <Project>
@机器人 二次推送 <接龙主题>
@机器人 二次推送本地 <接龙主题>
@机器人 今日发布数
@机器人 海外市场账号 <YYYYMMDD-YYYYMMDD> [--latest-per-account N]
@机器人 停止工作
@机器人 状态
@机器人 帮助
```

Standard `72h` is local-first: use `exports/<话题>.xlsx` when present, otherwise export from Feishu. The `本地` variants require the exact local XLSX, never export when it is missing, and keep the existing downstream stages.

`今日发布数` is implemented in the workflow layer. User-visible behavior: it counts the four configured account-activity views in order (`回头草`, `地区代表`, `新手村`, `30日活跃度`), writes account IDs to `素材推送表`, skips target rows where `账号编号` is already filled, and can auto-create missing target rows before writing. Dry-run does not create rows.

`海外市场账号 <YYYYMMDD-YYYYMMDD>` is implemented in the workflow layer. It reads the fixed overseas account Base and writes records with fields in this exact order: `账号`, `发帖链接`, `正文内容`, `发布日期`, `曝光`, `点赞`, `收藏`, `评论`, `转发`. During scraping it persists every account result to timestamped local files such as `exports\<range>-20260703-110045.jsonl` and `exports\<range>-20260703-110045.xlsx`, so repeated triggers for the same date range do not overwrite or append to the same local files; only after scraping completes does it create/reuse the same-Base result table named `YYYY.MM.DD-YYYY.MM.DD` and batch backfill Base in source account order. Result rows are appended account by account in the source Base view row order, not grouped by platform; `--platforms`, `--source-indexes`, and `--limit-per-platform` only select accounts and the selected output still preserves source order. A repeated trigger uses a new local JSONL/XLSX pair, while Base backfill still skips existing same-link posts and existing same-account no-post placeholders. `--source-indexes 3,5,10-12` is a repair-only workflow option for retrying exact source rows from the overseas account Base. `--no-backfill` generates/updates the local JSONL/XLSX and skips Base writes. F009 Feishu reporting cadence is start, 25%, 50%, 75%, and finish, using the selected full account count as the denominator; the finish report includes 回填数目, 无内容账号数, and 失效数, then the bot sends the generated XLSX file to the configured report chat. F009 treats readable `正文内容` plus usable publish date as the post success signal; missing social metrics are left blank by default and must not fail the post/account or keep retrying the batch. If an account has no post正文 in the requested date range, date-range mode writes one placeholder row with only `账号` filled and all other fields empty; failed accounts do not get no-post placeholders. `曝光` normalizes platform-specific view/play/browse/impression metrics into one numeric column. X timeline discovery is project-local; Instagram, Facebook, TikTok, and LinkedIn use project-local CDP account-page discovery; YouTube uses channel RSS plus lightweight video-page view-count parsing. LinkedIn challenge handling keeps the current CDP page open without refresh/navigation, reports that manual verification is needed, and continues only after hard challenge markers disappear and a valid LinkedIn content page is visible; timeout defaults to 3600 seconds and fails only that account. Date-range runs use reliable dates as scan boundaries where safe: X stops after reliable older tweets, TikTok skips video-id dates newer than the range and stops when a newly observed video is older than the range start, YouTube RSS stops at the first entry older than the range start, and Instagram/Facebook/LinkedIn detail scraping stops the account once a scroll-discovered detail page reads older than the range start. Batch runs do not wait for manual social login unless `WEEKLY_ACCOUNT_ALLOW_MANUAL_LOGIN=1` is set. `--latest-per-account N` is a diagnostic/latest mode that uses `YYYY.MM.DD-YYYY.MM.DD-latestN` result table names and timestamped local files such as `YYYY.MM.DD-YYYY.MM.DD-latestN-20260703-110045.*`, and takes each account's latest N discovered posts without date filtering, date-based early stop, or no-post placeholders. Dry-run only reads and reports the plan.

Useful options for workflow commands:

```text
--dry-run
--no-backfill
--chat-id oc_xxx / --source <chat-alias>
source=<chat-alias>
--base <base-alias>
base=<base-alias>
--base-url <url>
--base-token <token>
--overwrite
--start-row N
--limit-per-platform N
--latest-per-account N
--source-indexes 3,5,10-12
```

## Routing

Route categories are separate. Do not reuse one alias table for different roles:

```text
JIELONG_BOT_COMMAND_CHAT_ALIASES=指令群=oc_xxx
JIELONG_BOT_SOURCE_CHAT_ALIASES=接龙群=oc_yyy;colombia=oc_zzz
JIELONG_BOT_DEFAULT_REPORT_CHAT=发送群=oc_zzz
JIELONG_BOT_SOURCE_BASE_URL_ALIASES=某接龙Base=https://.../base/...
JIELONG_BOT_DEFAULT_SOURCE_CHAT=接龙群

JIELONG_BOT_TARGET_BASE_URL_ALIASES=素材推送表=https://.../base/...?table=...
JIELONG_BOT_TARGET_BASE_TOKEN_ALIASES=归档表=bascn...
JIELONG_BOT_DEFAULT_TARGET_BASE=素材推送表
```

Then a command can route explicitly:

```text
@机器人 抓取 登陆哥伦比亚 source=colombia base=素材推送表
@机器人 72h 登陆哥伦比亚 --source colombia --base 素材推送表
@机器人 抓取 登陆哥伦比亚 source-base=某接龙Base base=素材推送表
@机器人 确认推送 登陆哥伦比亚
@机器人 二次推送 登陆哥伦比亚
@机器人 今日发布数
@机器人 海外市场账号 20260605-20260609 --dry-run
@机器人 海外市场账号 20260605-20260609 --limit-per-platform 1 --latest-per-account 3
```

Rules:

- Command groups are only allowlisted trigger locations.
- Listener jobs require an explicit bot mention when
  `JIELONG_BOT_REQUIRE_AT=true`. Do not trigger workflow commands from plain
  group text or from messages that mention someone else.
- Set `JIELONG_BOT_MENTION_MATCHES` to accepted bot mention text/open ids, for
  example `机器人;ou_xxx`. If no matches are configured, require a Feishu
  `<at ...>` tag and reject bare text commands.
- Source chats are only Feishu groups scanned for jielong messages.
- The report chat is only where generated review files such as F009 XLSX exports are sent.
- Source Bases are direct jielong Base inputs and skip source-chat scanning.
- Target Bases are only Base tables written during backfill.
- If no source is specified, the bot uses `JIELONG_BOT_DEFAULT_SOURCE_CHAT` or `JIELONG_BOT_SOURCE_CHAT_ID`.
- If no target Base is specified, the bot uses `JIELONG_BOT_DEFAULT_TARGET_BASE`; workflow can still fall back to `env/bases.env`.
- Unknown aliases fail fast and are reported to the command group.

## Boundary

- Parse command text and map it to a `workflow_id`.
- Enforce one active job. Reject overlapping workflow commands.
- Stop the active workflow process tree when receiving `停止工作` / `取消` / `中止`.
- Forward workflow stdout JSONL events with `event=progress|final|error`.
- Forward only workflow progress/final/error reports by default. `JIELONG_BOT_HEARTBEAT_SECONDS=0` disables silent-time heartbeat reports.
- Send all Feishu IM text through the shared Python sender path, which invokes
  the lark-cli JS entrypoint via `node.exe`; do not call `lark-cli.CMD` from
  workflow or report code.
- Verify sent replies by reading the Feishu message back and comparing content.
  This is on by default through `JIELONG_BOT_VERIFY_REPLIES=true`; only disable
  it deliberately with `--no-verify-reply` / `JIELONG_BOT_VERIFY_REPLIES=false`.
- Do not inspect or modify XLSX, social metrics, Base records, or CDP profile.

## Direct Tests

```powershell
python <skill-root>\scripts\feishu_bot.py parse "@机器人 抓取 登陆哥伦比亚 --dry-run"
python <skill-root>\scripts\feishu_bot.py parse "@机器人 72h 登陆哥伦比亚 --dry-run"
python <skill-root>\scripts\feishu_bot.py parse "@机器人 确认推送 登陆哥伦比亚 --dry-run"
python <skill-root>\scripts\feishu_bot.py parse "@机器人 二次推送 登陆哥伦比亚 --dry-run"
python <skill-root>\scripts\feishu_bot.py parse "@机器人 今日发布数"
python <skill-root>\scripts\feishu_bot.py parse "@机器人 海外市场账号 20260605-20260609 --dry-run"
python <skill-root>\scripts\feishu_bot.py parse "@机器人 海外市场账号 20260605-20260609 --latest-per-account 3"
python <skill-root>\scripts\feishu_bot.py run "登陆哥伦比亚" --workflow-id full_social_scrape --dry-run
python <skill-root>\scripts\feishu_bot.py run "登陆哥伦比亚" --workflow-id refresh_72h --dry-run
python <skill-root>\scripts\feishu_bot.py run "dummy" --workflow-id today_publish_count --dry-run
python <skill-root>\scripts\feishu_bot.py run "20260605-20260609" --workflow-id weekly_market_accounts --dry-run
```

## Manual Reports

For manual Chinese or multiline reports, do not pipe an inline PowerShell
here-string into Python and do not call `lark-cli.CMD` directly. Put the report
in a UTF-8 text file and send it through the safe helper:

```powershell
python -X utf8 <skill-root>\scripts\send_text.py --chat-id oc_xxx --text-file report.txt --as bot
```

The helper reads the text file as UTF-8 and runs the lark-cli JS entrypoint
through `node.exe`, avoiding Windows `.CMD` argument expansion and PowerShell
pipeline encoding loss. It also reads the sent Feishu message back and fails if
the stored message content does not match the source text.

## Background Service

Use the wrapper for Windows Scheduled Task autostart:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File <skill-root>\scripts\run_listener.ps1
```

Recommended task name:

```text
CodexFeishuBot
```
