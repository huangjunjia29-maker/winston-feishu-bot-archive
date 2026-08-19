#!/usr/bin/env python3
"""Send or reply to a Feishu/Lark text message from a UTF-8 file.

This helper is intentionally small. It avoids two Windows hazards that have
already bitten manual reports:

1. npm-generated lark-cli.CMD can reinterpret characters such as '&'.
2. PowerShell pipelines can replace non-ASCII script literals with '?'.

Prefer `--text-file` for Chinese or multiline reports.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def configure_utf8_runtime() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def lark_cli_node_argv(npm_dir: Path) -> list[str] | None:
    script = npm_dir / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
    if not script.exists():
        return None
    node_candidates = [
        npm_dir / "node.exe",
        shutil.which("node"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe",
    ]
    for candidate in node_candidates:
        if candidate and Path(candidate).exists():
            return [str(Path(candidate)), str(script)]
    return None


def lark_cli_argv() -> list[str]:
    configured = os.environ.get("LARK_CLI_PATH")
    if configured:
        configured_path = Path(configured)
        if configured_path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            direct = lark_cli_node_argv(configured_path.parent)
            if direct:
                return direct
        if configured_path.exists():
            return [str(configured_path)]

    npm_dirs: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        npm_dirs.append(Path(appdata) / "npm")
    for name in ("lark-cli.cmd", "lark-cli.CMD", "lark-cli.ps1"):
        resolved = shutil.which(name)
        if resolved:
            npm_dirs.append(Path(resolved).parent)

    seen: set[Path] = set()
    for npm_dir in npm_dirs:
        npm_dir = npm_dir.resolve()
        if npm_dir in seen:
            continue
        seen.add(npm_dir)
        direct = lark_cli_node_argv(npm_dir)
        if direct:
            return direct

    resolved = shutil.which("lark-cli")
    if resolved:
        return [resolved]
    raise FileNotFoundError("lark-cli was not found.")


def read_text(args: argparse.Namespace) -> str:
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8-sig")
    if args.text is not None:
        return args.text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --text-file, --text, or stdin.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send Feishu text safely from UTF-8 input.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat-id", help="Send a new message to this chat id.")
    target.add_argument("--message-id", help="Reply to this message id.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text-file", help="UTF-8 text file to send.")
    source.add_argument("--text", help="Inline text. Prefer --text-file for non-ASCII content.")
    parser.add_argument("--as", dest="identity", default="bot", choices=["bot", "user"])
    parser.add_argument("--cwd", default=str(PROJECT_ROOT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-verify", action="store_true", help="Skip reading the sent message back.")
    return parser


def verify_sent_message(command_prefix: list[str], message_id: str, expected_text: str, identity: str, cwd: str) -> bool:
    verify_command = [
        *command_prefix,
        "im",
        "+messages-mget",
        "--message-ids",
        message_id,
        "--no-reactions",
        "--as",
        identity,
        "--format",
        "json",
    ]
    result = subprocess.run(
        verify_command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        return False
    try:
        payload = json.loads(result.stdout)
        messages = payload.get("data", {}).get("messages", [])
        actual_text = messages[0].get("content", "") if messages else ""
    except Exception as exc:
        print(f"[verify-error] failed to parse message fetch result: {exc}", file=sys.stderr)
        return False
    if actual_text != expected_text:
        print("[verify-error] sent message content does not match source text.", file=sys.stderr)
        print(json.dumps({"expected": expected_text, "actual": actual_text}, ensure_ascii=False), file=sys.stderr)
        return False
    print("[verify] sent message content matches source text.")
    return True


def main(argv: list[str] | None = None) -> int:
    configure_utf8_runtime()
    args = build_parser().parse_args(argv)
    text = read_text(args)
    command_prefix = lark_cli_argv()
    if args.message_id:
        command = [
            *command_prefix,
            "im",
            "+messages-reply",
            "--message-id",
            args.message_id,
            "--text",
            text,
            "--as",
            args.identity,
        ]
    else:
        command = [
            *command_prefix,
            "im",
            "+messages-send",
            "--chat-id",
            args.chat_id,
            "--text",
            text,
            "--as",
            args.identity,
        ]

    if args.dry_run:
        print(json.dumps({"argv_prefix": command[:4], "chars": len(text), "preview": text[:120]}, ensure_ascii=False))
        return 0

    result = subprocess.run(
        command,
        cwd=args.cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0 or args.no_verify:
        return result.returncode
    try:
        payload = json.loads(result.stdout)
        message_id = payload.get("data", {}).get("message_id", "")
    except Exception as exc:
        print(f"[verify-error] failed to parse send result: {exc}", file=sys.stderr)
        return 4
    if not message_id:
        print("[verify-error] send result did not include message_id.", file=sys.stderr)
        return 4
    return 0 if verify_sent_message(command_prefix, message_id, text, args.identity, args.cwd) else 4


if __name__ == "__main__":
    raise SystemExit(main())
