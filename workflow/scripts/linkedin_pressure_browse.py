#!/usr/bin/env python3
"""Slow LinkedIn CDP browsing probe.

This script is intentionally separate from F009. It does not backfill Base,
does not write XLSX, and does not extract post text or metrics. It opens or
reuses the project CDP Chrome profile and slowly browses LinkedIn pages while
logging lightweight page state so we can observe anti-bot behavior.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


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
MODULE_DIR = WORKFLOW_ROOT / "modules"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs" / "linkedin_pressure"
DEFAULT_CDP_PROFILE = WORKFLOW_ROOT / "assets" / "chrome-cdp-profile"
DEFAULT_CDP_PORT = 9222


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


for env_file in (WORKFLOW_ROOT / "env" / "global.env", WORKFLOW_ROOT / "env" / "cdp.env"):
    load_env_file(env_file)


def import_social_metrics() -> Any:
    module_path = MODULE_DIR / "social_metrics.py"
    spec = importlib.util.spec_from_file_location("linkedin_pressure_social_metrics", module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import social_metrics module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def compact_spaces(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def random_delay(min_s: float, max_s: float) -> float:
    low = max(0.0, min_s)
    high = max(low, max_s)
    return random.uniform(low, high)


def sleep_human(label: str, min_s: float, max_s: float) -> float:
    delay = random_delay(min_s, max_s)
    print(f"[wait] {label} {delay:.1f}s", file=sys.stderr, flush=True)
    time.sleep(delay)
    return delay


def normalize_linkedin_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        raise ValueError("empty URL")
    if not re.match(r"https?://", value, flags=re.I):
        value = "https://" + value.lstrip("/")
    parsed = urlparse(value)
    if "linkedin.com" not in parsed.netloc.lower():
        raise ValueError(f"not a LinkedIn URL: {url}")
    return value


def read_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = []
    for value in args.urls or []:
        urls.append(normalize_linkedin_url(value))
    if args.input:
        for raw_line in args.input.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(normalize_linkedin_url(line))
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(url)
    if not deduped:
        raise SystemExit("Provide at least one LinkedIn URL or --input file.")
    return deduped


LIGHT_PAGE_STATE_SCRIPT = r"""
() => {
  const text = [
    document.title || '',
    location.href || '',
    Array.from(document.querySelectorAll('iframe[src]')).map(node => node.src || '').join(' '),
  ].join('\n').toLowerCase();
  const markers = [
    'li.protechts.net',
    'recaptcha',
    'captcha',
    'hcaptcha',
    'checkpoint',
    'security check',
    'unusual activity',
    'verify you are human',
    'automated requests',
    'automated access',
  ];
  const marker = markers.find(item => text.includes(item)) || '';
  const iframeSrcs = Array.from(document.querySelectorAll('iframe[src]'))
    .map(node => node.src || '')
    .filter(Boolean)
    .slice(0, 12);
  return {
    title: document.title || '',
    url: location.href || '',
    marker,
    iframeSrcs,
    readyState: document.readyState || '',
    bodyChildCount: document.body ? document.body.childElementCount : 0,
    linkCount: document.querySelectorAll('a[href]').length,
  };
}
"""


def safe_page_state(page: Any) -> dict[str, Any]:
    try:
        data = page.evaluate(LIGHT_PAGE_STATE_SCRIPT)
        if isinstance(data, dict):
            data["ok"] = True
            return data
    except Exception as exc:
        return {"ok": False, "error": str(exc).splitlines()[0]}
    return {"ok": False, "error": "unexpected page state"}


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class JsonlLogger:
    path: Path

    def write(self, event: str, **payload: Any) -> None:
        record = {"ts": timestamp(), "event": event, **payload}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)


def page_wait(page: Any, label: str, min_s: float, max_s: float) -> float:
    delay = random_delay(min_s, max_s)
    print(f"[wait] {label} {delay:.1f}s", file=sys.stderr, flush=True)
    page.wait_for_timeout(int(delay * 1000))
    return delay


def browse_url(
    page: Any,
    logger: JsonlLogger,
    url: str,
    *,
    index: int,
    total: int,
    read_min_s: float,
    read_max_s: float,
    between_action_min_s: float,
    between_action_max_s: float,
    scrolls: int,
    stop_on_antibot: bool,
    timeout_ms: int,
) -> bool:
    logger.write("url_start", index=index, total=total, url=url)
    started = time.perf_counter()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        logger.write("navigation_error", index=index, url=url, error=str(exc).splitlines()[0])
        return True

    page_wait(page, "initial_read", read_min_s, read_max_s)
    state = safe_page_state(page)
    logger.write("state", index=index, url=url, stage="after_goto", state=state)
    if stop_on_antibot and state.get("marker"):
        logger.write("anti_bot_stop", index=index, url=url, stage="after_goto", marker=state.get("marker"))
        return False

    for scroll_index in range(1, max(0, scrolls) + 1):
        try:
            if scroll_index % 3 == 0:
                page.mouse.wheel(0, -450)
                direction = "up"
            else:
                page.mouse.wheel(0, random.randint(420, 980))
                direction = "down"
            page_wait(page, f"read_scroll_{scroll_index}_{direction}", between_action_min_s, between_action_max_s)
            state = safe_page_state(page)
            logger.write(
                "state",
                index=index,
                url=url,
                stage=f"scroll_{scroll_index}_{direction}",
                state=state,
            )
            if stop_on_antibot and state.get("marker"):
                logger.write(
                    "anti_bot_stop",
                    index=index,
                    url=url,
                    stage=f"scroll_{scroll_index}_{direction}",
                    marker=state.get("marker"),
                )
                return False
        except Exception as exc:
            logger.write("scroll_error", index=index, url=url, scroll_index=scroll_index, error=str(exc).splitlines()[0])
            return True

    logger.write("url_done", index=index, total=total, url=url, elapsed_s=round(time.perf_counter() - started, 1))
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Slowly browse LinkedIn pages through the project CDP profile and log anti-bot state.",
    )
    parser.add_argument("urls", nargs="*", help="LinkedIn company or post URLs to browse slowly.")
    parser.add_argument("--input", type=Path, help="UTF-8 text file with one LinkedIn URL per line.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help=f"Default: {DEFAULT_LOG_DIR}")
    parser.add_argument("--log-file", type=Path, help="Optional exact JSONL output path.")
    parser.add_argument(
        "--cdp-profile",
        type=Path,
        default=Path(os.getenv("SOCIAL_CDP_PROFILE", str(DEFAULT_CDP_PROFILE))),
        help="Chrome user-data directory to reuse.",
    )
    parser.add_argument("--cdp-port", type=int, default=int(os.getenv("SOCIAL_CDP_PORT", str(DEFAULT_CDP_PORT))))
    parser.add_argument("--timeout-ms", type=int, default=60_000)
    parser.add_argument("--read-min", type=float, default=25.0, help="Initial read dwell lower bound per page.")
    parser.add_argument("--read-max", type=float, default=55.0, help="Initial read dwell upper bound per page.")
    parser.add_argument("--action-min", type=float, default=12.0, help="Between scroll/action dwell lower bound.")
    parser.add_argument("--action-max", type=float, default=30.0, help="Between scroll/action dwell upper bound.")
    parser.add_argument("--between-url-min", type=float, default=45.0)
    parser.add_argument("--between-url-max", type=float, default=120.0)
    parser.add_argument("--scrolls", type=int, default=4, help="Small human-like scroll count per URL.")
    parser.add_argument("--visible", action=argparse.BooleanOptionalAction, default=True, help="Start CDP visibly.")
    parser.add_argument("--stop-on-antibot", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--keep-open", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    urls = read_urls(args)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = args.log_file or (args.log_dir / f"linkedin_pressure_{run_id}.jsonl")
    logger = JsonlLogger(log_path)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Missing playwright dependency.") from exc

    social_metrics = import_social_metrics()
    first_url = urls[0]
    session = social_metrics.ensure_cdp_profile(
        args.cdp_profile,
        args.cdp_port,
        first_url,
        start_minimized=not bool(args.visible),
        auto_close=not bool(args.keep_open),
    )
    logger.write(
        "run_start",
        urls=len(urls),
        cdp_url=session.url,
        cdp_profile=str(args.cdp_profile),
        log_file=str(log_path),
        stop_on_antibot=bool(args.stop_on_antibot),
    )

    keep_running = True
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(session.url)
        context = browser.contexts[0] if browser.contexts else browser.new_context(locale="en-US")
        page = context.new_page()
        page.set_default_timeout(args.timeout_ms)
        page.set_default_navigation_timeout(args.timeout_ms)
        try:
            for index, url in enumerate(urls, 1):
                keep_running = browse_url(
                    page,
                    logger,
                    url,
                    index=index,
                    total=len(urls),
                    read_min_s=args.read_min,
                    read_max_s=args.read_max,
                    between_action_min_s=args.action_min,
                    between_action_max_s=args.action_max,
                    scrolls=args.scrolls,
                    stop_on_antibot=bool(args.stop_on_antibot),
                    timeout_ms=args.timeout_ms,
                )
                if not keep_running or index >= len(urls):
                    break
                sleep_human("between_urls", args.between_url_min, args.between_url_max)
        finally:
            try:
                page.close()
            except Exception:
                pass
            if not args.keep_open:
                session.close()

    logger.write("run_done", completed=keep_running, log_file=str(log_path))
    return 0 if keep_running else 2


if __name__ == "__main__":
    raise SystemExit(main())
