#!/usr/bin/env python3
"""Passive LinkedIn CDP anti-bot watcher.

This watcher does not attach to pages, execute JavaScript, scroll, click, or
scrape content. It only polls Chrome's CDP /json endpoint and records target
metadata so manual browsing can be observed with minimal browser interaction.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


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


configure_utf8_runtime()

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = WORKFLOW_ROOT.parent
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs" / "linkedin_pressure"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"

MARKERS = (
    "li.protechts.net",
    "recaptcha",
    "captcha",
    "checkpoint",
    "security check",
    "security verification",
    "unusual activity",
    "verify you are human",
    "automated requests",
    "automated access",
    "challenge",
    "checkpoint/challenge",
)


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fetch_targets(cdp_url: str, timeout_s: float) -> list[dict[str, Any]]:
    endpoint = cdp_url.rstrip("/") + "/json"
    with urlopen(endpoint, timeout=timeout_s) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def relevant_targets(targets: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for target in targets:
        target_type = str(target.get("type") or "")
        if target_type not in {"page", "iframe", "worker"}:
            continue
        title = str(target.get("title") or "")
        url = str(target.get("url") or "")
        text = f"{title} {url}".lower()
        if target_type == "page" or "linkedin." in text or "licdn." in text or "protechts" in text or "recaptcha" in text:
            rows.append({"type": target_type, "title": title, "url": url})
    return rows


def detect_markers(rows: list[dict[str, str]]) -> list[str]:
    text = "\n".join(f"{row.get('title', '')} {row.get('url', '')}" for row in rows).lower()
    return [marker for marker in MARKERS if marker in text]


def signature(rows: list[dict[str, str]], markers: list[str]) -> str:
    compact = [{"type": r["type"], "title": r["title"], "url": r["url"]} for r in rows]
    return json.dumps({"markers": markers, "targets": compact}, ensure_ascii=False, sort_keys=True)


class JsonlLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **payload: Any) -> None:
        record = {"ts": timestamp(), "event": event, **payload}
        line = json.dumps(record, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Passively monitor LinkedIn CDP target metadata for anti-bot markers.")
    parser.add_argument("--cdp-url", default=os.getenv("SOCIAL_CDP_URL", DEFAULT_CDP_URL))
    parser.add_argument("--interval", type=float, default=10.0, help="Polling interval in seconds.")
    parser.add_argument("--timeout", type=float, default=3.0, help="HTTP timeout per CDP poll.")
    parser.add_argument("--duration", type=float, default=0.0, help="Optional max runtime seconds; 0 means forever.")
    parser.add_argument("--heartbeat", type=int, default=6, help="Emit unchanged heartbeat every N polls.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--log-file", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = args.log_file or args.log_dir / f"linkedin_cdp_watch_{run_id}.jsonl"
    logger = JsonlLogger(log_path)
    logger.write(
        "watch_start",
        cdp_url=args.cdp_url,
        interval_s=args.interval,
        duration_s=args.duration,
        log_file=str(log_path),
        mode="passive_cdp_json_only",
    )

    started = time.monotonic()
    last_signature = ""
    poll_count = 0
    last_error = ""
    try:
        while True:
            poll_count += 1
            if args.duration > 0 and time.monotonic() - started >= args.duration:
                logger.write("watch_done", reason="duration_elapsed", polls=poll_count)
                return 0
            try:
                targets = fetch_targets(args.cdp_url, args.timeout)
                rows = relevant_targets(targets)
                markers = detect_markers(rows)
                sig = signature(rows, markers)
                changed = sig != last_signature
                heartbeat = args.heartbeat > 0 and poll_count % args.heartbeat == 0
                if changed or heartbeat or markers:
                    logger.write(
                        "snapshot",
                        poll=poll_count,
                        changed=changed,
                        markers=markers,
                        target_count=len(rows),
                        targets=rows,
                    )
                    last_signature = sig
                last_error = ""
            except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                error = str(exc).splitlines()[0]
                if error != last_error or poll_count % max(1, args.heartbeat) == 0:
                    logger.write("poll_error", poll=poll_count, error=error)
                last_error = error
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        logger.write("watch_done", reason="keyboard_interrupt", polls=poll_count)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
