# Winston Runtime Contract

This project uses the existing stable Winston runtime. Do not replace its
authentication or workflow implementation with a new provider or token store.

## Feishu authentication

For direct Feishu API calls, load `FEISHU_APP_ID` and `FEISHU_APP_SECRET` from
the local project environment, request an internal `tenant_access_token`, cache
it only for the current process, and refresh it on expiry or authorization
failure. Never print, commit, upload, or place either credential in chat,
skill instructions, logs, or generated reports.

The existing implementations are:

- `bot/scripts/feishu_bot.py` for IM reads, replies, and message delivery.
- `workflow/modules/base_backfill.py` for direct Base API operations.
- `workflow/modules/feishu_auth.py` as the shared workflow authentication helper.

## Codex task routing

When a Codex task asks to read, write, or message through Feishu, use this
project as the execution root and reuse the existing modules and environment
files. Do not infer resource IDs, credentials, or permissions from another
conversation. Read the live resource first, preserve unrelated fields, and
verify writes by reading the affected range or record back.

Use the existing lark-cli path only where the current listener or workflow
explicitly requires it. Do not reconfigure the CLI or change the listener just
because another conversation cannot see this project's context.

## Safe execution rules

- Read-only inspection may run without confirmation.
- Any Feishu write, message, permission change, or external publication needs
  explicit user approval in the current task unless that exact operation was
  already approved.
- Prefer dry-run for business workflows.
- Never expose Secret, token, cookies, or local `.env` contents.
- Keep source tables unchanged unless the task explicitly names them as a
  write target.
- Report the exact target workbook/table/sheet and verification result after a
  write.

## Stable workflow entry points

- Bot listener: `bot/scripts/feishu_bot.py listen`
- Background listener: `bot/scripts/run_listener.ps1`
- Business workflows: `workflow/scripts/feishu_workflow.py`
- Social/CDP workflows: use existing modules under `workflow/modules` and the
  project CDP profile configured in `workflow/env/cdp.env`.

This contract standardizes how Codex tasks use the existing runtime. It does
not make separate Codex conversations share chat history, process state, or
browser sessions.
