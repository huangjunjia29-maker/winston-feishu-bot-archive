---
name: feishu-workflow
description: Feishu/Lark business workflow library and runner for jielong export, social public metric scraping, XLSX preservation, Base backfill, all-platform 72h refresh, workflow registry, env files, exports, logs, and Chrome CDP assets.
---

# Feishu Workflow

This skill owns the business work. The bot listener should call this runner as a child process and forward JSONL progress messages.

## Runtime Files

- Runner: `scripts/feishu_workflow.py`
- Modules:
  - `modules/export_jielong.py`
  - `modules/social_metrics.py`
  - `modules/base_backfill.py`
  - `modules/weekly_account_posts.py`
  - `modules/x_timeline.py`
  - `modules/account_timeline.py`
- Env:
  - `env/global.env`
  - `env/chats.env`
  - `env/bases.env`
  - `env/cdp.env`
- CDP profile: `assets/chrome-cdp-profile`
- Logs: `tmp/feishu_workflow_logs`
- XLSX outputs: workspace `exports`

## Function Directory

```text
F001 export_jielong_xlsx
F002 scrape_social_metrics_to_xlsx
F003 backfill_xlsx_to_base
F004 full_social_scrape
F005 refresh_72h
F006 confirm_push
F007 secondary_push
F008 today_publish_count
F009 weekly_market_accounts
F010 cdp_cache_cleanup
```

Workflow registry:

```text
workflow_id=full_social_scrape  -> 抓取
workflow_id=refresh_72h         -> 72h
workflow_id=confirm_push        -> 确认推送
workflow_id=secondary_push      -> 二次推送
workflow_id=today_publish_count -> 今日发布数
workflow_id=weekly_market_accounts -> 海外市场账号
```

The runner receives `--workflow-id` from `feishu-bot`; it does not scan available skills or auto-discover commands at runtime.

F010 is an internal workflow only. It has no Feishu bot trigger and must stay absent from the bot command registry. Direct local runs emit a normal workflow report.

## Commands

Full social scrape:

```powershell
python <skill-root>\scripts\feishu_workflow.py "话题" --workflow-id full_social_scrape
```

72h:

```powershell
python <skill-root>\scripts\feishu_workflow.py "话题" --workflow-id refresh_72h
```

确认推送:

```powershell
python <skill-root>\scripts\feishu_workflow.py "Project" --workflow-id confirm_push
```

二次推送:

```powershell
python <skill-root>\scripts\feishu_workflow.py "话题" --workflow-id secondary_push
```

今日发布数:

```powershell
python <skill-root>\scripts\feishu_workflow.py "dummy" --workflow-id today_publish_count
```

海外市场账号:

```powershell
python <skill-root>\scripts\feishu_workflow.py "20260605-20260609" --workflow-id weekly_market_accounts
python <skill-root>\scripts\feishu_workflow.py "20260605-20260609" --workflow-id weekly_market_accounts --limit-per-platform 1 --latest-per-account 3
python <skill-root>\scripts\feishu_workflow.py "20260629-20260629" --workflow-id weekly_market_accounts --platforms x,tk --latest-per-account 2
python <skill-root>\scripts\feishu_workflow.py "20260622-20260628" --workflow-id weekly_market_accounts --platforms linkedin --source-indexes 3,5,10-12
```

Internal CDP cache cleanup:

```powershell
python <skill-root>\scripts\feishu_workflow.py "manual-F010" --workflow-id cdp_cache_cleanup
```

F010 clears allowlisted Chrome CDP cache artifacts while preserving cookies, local storage, IndexedDB, login data, and other session state. Social scraping workflows append it silently after normal completion/failure and must not emit Feishu reports for the appended cleanup step.

Standalone LinkedIn slow-browse pressure probe:

```powershell
python <skill-root>\scripts\linkedin_pressure_browse.py "https://www.linkedin.com/company/xpeng-%C3%B6sterreich/" --read-min 45 --read-max 90 --action-min 20 --action-max 45 --between-url-min 90 --between-url-max 180
```

This probe is intentionally not a bot command. It reuses the project CDP profile, browses slowly, and writes lightweight anti-bot state logs under `logs/linkedin_pressure`; it must not backfill Base/XLSX or extract post text/metrics.

Standalone LinkedIn feed probe:

```powershell
python <skill-root>\scripts\linkedin_feed_probe.py "https://www.linkedin.com/company/xpengbenelux/" --account "XPENG Benelux" --limit 2
```

This probe is intentionally not a bot command. It uses the logged-in project CDP profile, enters one account's posts feed, expands visible post cards, reads small-batch DOM text/metrics, and writes local JSONL/XLSX outputs only. It must not write Base records.

Compatibility modes are still accepted:

```powershell
python <skill-root>\scripts\feishu_workflow.py "话题" --mode full
python <skill-root>\scripts\feishu_workflow.py "话题" --mode 72h
python <skill-root>\scripts\feishu_workflow.py "Project" --mode confirm-push
python <skill-root>\scripts\feishu_workflow.py "话题" --mode secondary-push
python <skill-root>\scripts\feishu_workflow.py "dummy" --mode today-publish-count
python <skill-root>\scripts\feishu_workflow.py "20260605-20260609" --mode weekly-market-accounts
```

Common options: `--dry-run`, `--no-backfill`, `--chat-id`, `--base-url`, `--base-token`, `--target-base-url`, `--target-base-token`, `--target-table-id`, `--overwrite`, `--start-row`, `--limit-per-platform`, `--platforms`, `--source-indexes`, `--job-id`, `--workdir`, `--log-dir`.

F009 diagnostic option: `--latest-per-account N` writes to a `-latestN` result table and takes each account's latest N discovered posts without date filtering.

F009 platform filter: `--platforms x,tk` limits execution to specific platforms. Aliases include `tk`/`tiktok`, `twitter`/`x`, `ins`/`instagram`, `fb`/`facebook`, `yt`/`youtube`, and `li`/`linkedin`.

F009 repair filter: `--source-indexes 3,5,10-12` retries exact source rows from the overseas account Base while preserving source order.

`--base-url` / `--base-token` are direct jielong source Base inputs. `--target-base-url` / `--target-base-token` / `--target-table-id` are backfill targets.

## JSONL Contract

Stdout is JSON Lines:

```json
{"event":"progress","stage":"scrape","message":"...","data":{}}
{"event":"final","stage":"complete","message":"...","data":{}}
{"event":"error","stage":"backfill","message":"...","data":{}}
```

Encoding and reporting rules:

- Workflow code must emit UTF-8 JSONL only. Do not send Feishu IM messages from
  workflow modules directly.
- Feishu IM delivery is owned by `feishu-bot`, which uses the shared direct
  `node.exe ... @larksuite/cli/scripts/run.js` transport and reads messages back
  for verification.
- Do not pipe Chinese report content through inline PowerShell here-strings into
  `python -` or `lark-cli`. For manual reports, write a UTF-8 text file and use
  `feishu-bot/scripts/send_text.py --text-file`.

## Current Workflows

`full_social_scrape` runs:

```text
F001 导出接龙 XLSX -> F002 抓取社媒并回填 XLSX -> F003 XLSX 回填 Base
抓取本地: exports/<话题>.xlsx -> F002 -> F003
```

Rules:

- A bad link must be skipped and reported, not block the whole batch.
- Scraper retries up to 3 attempts before marking manual review.
- X/Twitter links must use the reusable Chrome CDP profile from the first
  attempt (`--use-cdp-for-x`) in workflow runs.
- Metrics scraping must not use a one-hour total batch timeout. Use the
  per-page/playwright timeout and a 3600s no-output guard for a genuinely
  stalled child process.
- Metrics logs include `[item-time] ... elapsed_s=...` for per-link timing.
- Excel metrics mode saves each scraped row immediately (`[excel-write n/N]`)
  so failed jobs can resume without redoing completed rows.
- Base matching failures are skipped and reported.
- Same Owner with multiple links is valid; match by platform link first, then an empty same-owner slot.
- Empty XLSX values do not clear Base cells.
- Existing Base values are not overwritten unless `--overwrite` is set.

`refresh_72h` runs:

```text
72h: F001 export -> all-platform CDP scrape -> XLSX 72h writeback -> Base backfill
72h本地: exports/<话题>.xlsx -> all-platform CDP scrape -> XLSX 72h writeback -> Base backfill
```

Rules:

- Read all supported platform links from wide columns (`INS链接`, `X链接`, `TK链接`, `FB链接`) or the generic jielong link column.
- Use CDP-backed scraping for Instagram, X, TikTok, and Facebook links.
- Preserve duplicate URLs by XLSX row and map results back by URL/status id when needed.
- Write new 72h play/exposure fields: `INS播放(72h)`, `X浏览(72h)`, `TK播放(72h)`, and `FB曝光(72h)`.
- Never overwrite original play/exposure fields: `INS播放`, `X浏览`, `TK播放`, and `FB曝光`.
- Overwrite interaction fields with the freshly scraped values: likes, comments, saves, and shares where the platform has those fields.
- Base matching remains Project + Owner + platform link slot.
- Final wording must include `完成【话题】72h`.

`confirm_push` runs:

```text
read target Base records by Project -> select owner non-empty and 推送通知 empty -> write 确认推送 -> wait -> verify 通知状态
```

Rules:

- Match Project exactly by record value.
- Skip Owner blank rows.
- Never overwrite non-empty `推送通知`.
- Verify `通知状态` after writing.

`secondary_push` runs:

```text
F001 export jielong XLSX -> backfill social links by Project+Owner -> mark 二次推送 rows
二次推送本地: exports/<话题>.xlsx -> backfill social links -> mark 二次推送 rows
```

Standard `72h` uses the exact local XLSX first and falls back to F001 export only when it is absent. All `本地` variants require the exact local XLSX and fail explicitly when it is missing; they never fall back to F001 export.

Rules:

- Reuse the mature normal workbook-loading path for Feishu XLSX headers.
- Do not overwrite existing social links or existing `二次推送`.
- Mark only rows with Owner non-empty, empty `发布状态`, and empty `二次推送`.

`today_publish_count` runs:

```text
read 账号活跃度 views -> count 账号编号 -> locate 素材推送表 Date last row + 2 -> write 账号编号
```

Rules:

- Source views: 回头草, 地区代表, 新手村, 30日活跃度.
- Write order: 回头草 -> 地区代表 -> 新手村 -> 30日活跃度.
- Target field is `素材推送表.账号编号`.
- Locate by `自动素材编号` ascending and the last row whose `Date` field is non-empty; leave one separator row and write from N+2.
- If a target row already has `账号编号`, treat it as a manual entry, skip that row, and continue writing from the next empty target row.
- If the target range does not have enough empty rows, non-dry-run creates the missing records with `base +record-batch-create`, then writes account IDs into the newly available range. Dry-run reports the shortage without creating rows.
- Normal report cadence is exactly: read count, precheck range, fill complete.

`weekly_market_accounts` runs:

```text
read 海外账号表 -> parse explicit date range -> create/reuse same-Base range result table -> scrape all supported account timelines -> write matching post records
```

Rules:

- Bot trigger is `海外市场账号 <YYYYMMDD-YYYYMMDD>`; the date range argument is required.
- Source Base is the fixed overseas account Base. Source table is `海外账号表`, view `全部账号`.
- Result table name is the requested date range: `YYYY.MM.DD-YYYY.MM.DD`.
- Result fields are exactly, in order: `账号`, `发帖链接`, `正文内容`, `发布日期`, `曝光`, `点赞`, `收藏`, `评论`, `转发`.
- `曝光`, `点赞`, `收藏`, `评论`, and `转发` are number fields. `曝光` normalizes platform-specific view/play/browse/impression metrics into one column.
- During scraping F009 must persist every account result to timestamped local files such as `exports/<range>-20260703-110045.jsonl` and `exports/<range>-20260703-110045.xlsx`. Base writes happen only after scraping completes, reconstructed from that trigger's JSONL ledger.
- F009 result rows must be appended account by account in the source Base view row order, not grouped by platform. `--platforms` and `--limit-per-platform` only select accounts; the selected account list still preserves source order before scraping, local export, and Base backfill.
- Repeated triggers with the same range create separate local JSONL/XLSX files, so one trigger cannot append into or overwrite another trigger's local ledger. Base backfill still skips existing same-link posts and existing same-account no-post placeholders.
- `--no-backfill` generates/updates that trigger's local JSONL/XLSX and skips Base writes.
- F009 Feishu reporting cadence is start, 25%, 50%, 75%, and finish, using the selected full account count as the denominator. The finish report must include 回填数目, 无内容账号数, and 失效数; the bot then sends the generated XLSX file to the configured report chat.
- F009 treats readable `正文内容` plus usable publish date as the post success signal. Missing social metrics are left blank by default and must not turn a post/account into failed or keep retrying the batch. X may keep stronger detail retries because its metrics are CDP-stable and low-volume.
- In normal date-range mode, if an account has no post正文 inside the requested range and the account scrape did not fail, F009 writes one placeholder row with only `账号` filled and all other fields empty. This keeps inactive accounts visible. Do not add these placeholders in `--latest-per-account` diagnostic mode or for failed accounts.
- F009 must not write the legacy `社交媒体数据` field, even if an old result table still contains that column.
- Dry-run must not create tables, scrape accounts, or write records.
- F009 date-range mode must use reliable dates as scan boundaries where safe. X/Twitter stops after reliable older tweets, TikTok skips decoded video-id dates newer than the range and stops when a newly observed video is older than the range start, YouTube RSS stops at the first entry older than the range start, LinkedIn stops its slow feed scan after an older feed card beyond the first viewport, and Instagram/Facebook detail scraping stops the account once a scroll-discovered detail page is older than the range start. Do not apply discovery-stage early stop to platforms whose discovery dates are unreliable.
- X/Twitter account timeline discovery is copied into this project as `modules/x_timeline.py`; it must not call the external `x-tweet-metrics` skill script.
- X/Twitter detail scraping reuses project-local `modules/social_metrics.py` with CDP from `workflow/assets/chrome-cdp-profile` / `SOCIAL_CDP_PROFILE`.
- Instagram, Facebook, and TikTok account-page discovery lives in `modules/account_timeline.py` and uses the same project-local CDP profile.
- LinkedIn account collection lives in `modules/linkedin_feed.py`: start from the logged-in account home page, enter `/posts/?feedView=all` or profile activity, expand visible cards, read DOM card by card, and do not use the generic `SocialScraper` detail-page loop.
- F009 must directly use the project-local Chrome CDP profile for Instagram, Facebook, and LinkedIn. Do not fall back to non-CDP page scraping for those platforms in this workflow.
- F009 preserves Instagram source links as `/p/...` or `/reel/...`; do not enable generic `/p -> /reel` URL normalization for this account-report workflow.
- F009 filters obvious Instagram off-profile/tagged candidates before detail scraping, skips Instagram candidates whose discovered date is clearly outside the requested range, and stops Instagram/Facebook detail scraping after a scroll-discovered detail page is older than the requested range. For explicit Instagram/Facebook Reel links, F009 should use the existing Reels-grid fallback by default: after detail parsing, open the author Reels grid, match the target Reel thumbnail by content ID, and read the thumbnail overlay view count into `曝光`. Ordinary image/photo posts must not enter this fallback, and unmatched Reels should leave `曝光` empty. Disable only with `WEEKLY_ACCOUNT_ENABLE_REEL_GRID_VIEWS=0` or the platform-specific override.
- F009 parses platform text dates for non-X details where structured timestamps are absent: Instagram caption dates, Facebook text dates such as `6月20日12:22`, and LinkedIn relative/feed-card dates including `1w`, `2d`, `昨天`, and `now`.
- LinkedIn slow feed collection must preserve randomized waits for home read, posts feed read, card read before/after expansion, and scroll settle. Hard checkpoint/security/captcha states keep the current CDP page open without refresh/navigation and notify that manual verification is needed. The workflow polls the current page until hard markers disappear and `valid_linkedin_page_state` is true, then continues the same account. If verification is not cleared before `WEEKLY_ACCOUNT_LINKEDIN_MANUAL_CHALLENGE_TIMEOUT_S` (default 3600 seconds), only that account fails; LinkedIn is not platform-halted. Soft `li.protechts.net`/invisible recaptcha markers are logged only.
- F009 filters TikTok candidates by decoding the video id timestamp before detail scraping when possible, uses that timestamp as a publish-date fallback, and avoids full account scans by stopping after a newly observed video is older than the requested range.
- F009 does not wait for manual social-platform login by default; login walls are recorded and the batch continues. Set `WEEKLY_ACCOUNT_ALLOW_MANUAL_LOGIN=1` only for attended repair runs.
- YouTube account timelines use channel Atom RSS when a channel id can be resolved; date-range mode stops at the first RSS entry older than the requested range. YouTube records may additionally fill `曝光` from a lightweight video-page view-count parse; failure to read views must not block RSS records.
- Account results are persisted account by account after detail filtering, so a long run preserves completed local data even if a later account fails.
- Posts without a readable publish date are skipped and reported; only posts whose `发布日期` falls inside the requested date range are written.
- `--limit-per-platform N` limits F009 live/dry-run execution to the first N accounts per platform from the source view; this is intended for controlled cross-platform tests.
- `--platforms x,tk` limits F009 live/dry-run execution to specific platforms; use this for targeted runs such as latest X + TikTok refreshes.
- `--latest-per-account N` is a controlled diagnostic/latest mode. It creates/reuses the Base table `YYYY.MM.DD-YYYY.MM.DD-latestN`, writes timestamped local files such as `YYYY.MM.DD-YYYY.MM.DD-latestN-20260703-110045.*`, disables date filtering and date-based early stop, and writes the latest N discovered posts per selected account where detail extraction returns enough data.
- CDP-backed scraping must reuse an already running responsive CDP endpoint for the same `workflow/assets/chrome-cdp-profile` before starting Chrome. If the same profile has a stuck CDP process, fail with a clear profile/port message instead of starting a second browser against the locked profile.
- F009 may request quiet CDP startup by default, but CDP windows must remain manually previewable. The shared launcher may do one-shot minimization after launch; it must not use Chrome `--start-minimized` or a background minimizer thread that keeps forcing the profile window down.

## Adding Features

1. Add a workflow handler or module in `scripts/feishu_workflow.py` or `modules/`.
2. Register it in `FUNCTION_DIRECTORY` and `WORKFLOW_REGISTRY`.
3. Add the command trigger in `feishu-bot/scripts/feishu_bot.py` `COMMAND_REGISTRY`.
4. Emit only JSONL `progress`, `final`, and `error` events for bot reporting.
