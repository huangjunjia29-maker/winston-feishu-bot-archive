# Highest-Priority Activation Gate

This is a clean release copy of Feishu Bot Workflow.

## Winston runtime contract

Before performing a Feishu task from any Codex conversation using this project,
read `WINSTON_RUNTIME.md`. It defines the stable App ID/Secret to
tenant-access-token flow, task routing, approval boundaries, and verification
requirements. Preserve the existing runtime implementation; do not create a
conversation-specific substitute.

## Mandatory first action

Before reading code, starting services, installing dependencies, opening Chrome, or running any workflow, read `ACTIVATE_FIRST.md` in full and follow it in order.

## Safety boundaries

- Treat the project as **not activated** unless a current local `ACTIVATED.local` record exists and its checks still pass.
- Never print, summarize, commit, or send values from `bot/.env` or `workflow/env/*.env`.
- Do not run live Base writes or social scraping before configuration, bot identity, CDP login, and dry-run checks are complete.
- The reusable browser profile is `workflow/assets/chrome-cdp-profile`; do not substitute a personal Chrome profile.
- Use paths relative to this project root. Do not restore old absolute source-project paths.
- Prefer explicit failure when configuration is missing. Do not invent fallback tokens, table IDs, chat IDs, or credentials.

## Runtime ownership

- `bot/` owns Feishu event listening, command parsing, one-active-job enforcement, workflow launch, and reply delivery.
- `workflow/` owns workflow orchestration, Base operations, XLSX handling, social collection, and CDP browser use.
- `exports/`, `logs/`, `tmp/`, `bot/tmp/`, and `workflow/tmp/` are runtime-only directories.
