#!/usr/bin/env python3
"""Small-scale LinkedIn feed collection probe.

This script is intentionally separate from F009. It uses the project CDP
profile, opens a LinkedIn account home page, enters the posts feed, slowly
expands visible posts, reads one card at a time, and writes local JSONL/XLSX
outputs. It does not write Base records.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse


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
DEFAULT_EXPORT_DIR = PROJECT_ROOT / "exports"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs" / "linkedin_feed_probe"
DEFAULT_CDP_PROFILE = WORKFLOW_ROOT / "assets" / "chrome-cdp-profile"
DEFAULT_CDP_PORT = 9222
LOCAL_HEADERS = ["账号", "发帖链接", "正文内容", "发布日期", "曝光", "点赞", "收藏", "评论", "转发"]

SOFT_MARKERS = ("li.protechts.net", "recaptcha")
HARD_MARKERS = (
    "checkpoint",
    "security check",
    "security verification",
    "unusual activity",
    "verify you are human",
    "automated requests",
    "automated access",
    "challenge",
    "captcha challenge",
)


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
    spec = importlib.util.spec_from_file_location("linkedin_feed_probe_social_metrics", module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import social_metrics module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SOCIAL_METRICS = import_social_metrics()


def compact_spaces(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def parse_count_token(token: str | None) -> int | None:
    return SOCIAL_METRICS.parse_count_token(token)


def random_delay(min_s: float, max_s: float) -> float:
    low = max(0.0, min_s)
    high = max(low, max_s)
    return random.uniform(low, high)


def wait_human(label: str, min_s: float, max_s: float) -> float:
    delay = random_delay(min_s, max_s)
    print(f"[wait] {label} {delay:.1f}s", file=sys.stderr, flush=True)
    time.sleep(delay)
    return delay


def page_wait_human(page: Any, label: str, min_s: float, max_s: float) -> float:
    delay = random_delay(min_s, max_s)
    print(f"[wait] {label} {delay:.1f}s", file=sys.stderr, flush=True)
    page.wait_for_timeout(int(delay * 1000))
    return delay


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def safe_stem(value: str, fallback: str = "linkedin") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("._-")
    return cleaned[:80] or fallback


def normalize_linkedin_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if not re.match(r"https?://", value, flags=re.I):
        value = "https://" + value.lstrip("/")
    parsed = urlparse(value)
    if "linkedin.com" not in parsed.netloc.lower():
        raise ValueError(f"not a LinkedIn URL: {url}")
    path = re.sub(r"/+", "/", parsed.path or "/")
    return urlunparse(("https", "www.linkedin.com", path.rstrip("/") or "/", "", parsed.query, ""))


def account_home_from_url(url: str) -> str:
    normalized = normalize_linkedin_url(url)
    parsed = urlparse(normalized)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"company", "in", "school", "showcase"}:
        return f"https://www.linkedin.com/{parts[0]}/{parts[1]}/"
    if "/feed/update/" in parsed.path:
        return "https://www.linkedin.com/"
    return normalized


def posts_url_from_home(account_home: str) -> str:
    parsed = urlparse(account_home)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "company":
        return f"https://www.linkedin.com/company/{parts[1]}/posts/?feedView=all"
    if len(parts) >= 2 and parts[0] == "showcase":
        return f"https://www.linkedin.com/showcase/{parts[1]}/posts/?feedView=all"
    if len(parts) >= 2 and parts[0] == "school":
        return f"https://www.linkedin.com/school/{parts[1]}/posts/?feedView=all"
    if len(parts) >= 2 and parts[0] == "in":
        return f"https://www.linkedin.com/in/{parts[1]}/recent-activity/all/"
    return account_home


def account_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return parts[1]
    return parsed.netloc or "linkedin"


@dataclass
class ProbeRecord:
    account: str
    account_url: str
    post_url: str = ""
    text: str = ""
    published_at: str = ""
    raw_date: str = ""
    exposure: int | None = None
    likes: int | None = None
    saves: int | None = None
    comments: int | None = None
    reposts: int | None = None
    collected_at: str = field(default_factory=timestamp)
    soft_markers: list[str] = field(default_factory=list)
    hard_markers: list[str] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        if self.post_url:
            return self.post_url
        base = f"{self.account_url}\n{self.text[:500]}\n{self.raw_date}"
        return hashlib.sha1(base.encode("utf-8", errors="replace")).hexdigest()

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "account_url": self.account_url,
            "post_url": self.post_url,
            "text": self.text,
            "published_at": self.published_at,
            "raw_date": self.raw_date,
            "metrics": {
                "exposure": self.exposure,
                "likes": self.likes,
                "saves": self.saves,
                "comments": self.comments,
                "reposts": self.reposts,
            },
            "collected_at": self.collected_at,
            "soft_markers": self.soft_markers,
            "hard_markers": self.hard_markers,
            "debug": self.debug,
        }

    def xlsx_row(self) -> list[Any]:
        return [
            self.account,
            self.post_url,
            self.text,
            self.published_at or self.raw_date,
            self.exposure,
            self.likes,
            self.saves,
            self.comments,
            self.reposts,
        ]


class JsonlLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **payload: Any) -> None:
        line = json.dumps({"ts": timestamp(), "event": event, **payload}, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        print(line, flush=True)


PAGE_STATE_SCRIPT = r"""
() => {
  const frames = Array.from(document.querySelectorAll('iframe[src]')).map(node => node.src || '').filter(Boolean);
  const text = [document.title || '', location.href || '', ...frames].join(' ').toLowerCase();
  const softMarkers = ['li.protechts.net', 'recaptcha'].filter(marker => text.includes(marker));
  const hardMarkers = [
    'checkpoint',
    'security check',
    'security verification',
    'unusual activity',
    'verify you are human',
    'automated requests',
    'automated access',
    'challenge',
    'captcha challenge',
  ].filter(marker => text.includes(marker));
  return {
    title: document.title || '',
    url: location.href || '',
    readyState: document.readyState || '',
    softMarkers,
    hardMarkers,
    iframeSrcs: frames.slice(0, 8),
  };
}
"""


DISMISS_OVERLAYS_SCRIPT = r"""
() => {
  const words = /(close|dismiss|not now|no thanks|skip|关闭|關閉|取消|暂不|稍后|×)/i;
  const selectors = [
    'button[aria-label*="Dismiss" i]',
    'button[aria-label*="Close" i]',
    'button[title*="Dismiss" i]',
    'button[title*="Close" i]',
    'button.contextual-sign-in-modal__modal-dismiss',
    '.contextual-sign-in-modal__modal-dismiss',
    'button.modal__dismiss',
    '[data-tracking-control-name*="dismiss" i]',
  ];
  let clicked = 0;
  for (const selector of selectors) {
    for (const node of Array.from(document.querySelectorAll(selector))) {
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      try { node.click(); clicked += 1; if (clicked >= 2) return clicked; } catch {}
    }
  }
  for (const node of Array.from(document.querySelectorAll('button, [role="button"]'))) {
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const text = [node.innerText || '', node.getAttribute('aria-label') || '', node.getAttribute('title') || '']
      .join(' ').replace(/\s+/g, ' ').trim();
    if (!words.test(text)) continue;
    try { node.click(); clicked += 1; if (clicked >= 2) break; } catch {}
  }
  return clicked;
}
"""


POST_CARD_SELECTOR = (
    "div.feed-shared-update-v2, "
    "div[data-urn*='urn:li:activity'], "
    "article"
)


EXPAND_CARD_SCRIPT = r"""
(card) => {
  const labels = /(see more|show more|more|展开|顯示更多|查看更多|显示更多|更多)/i;
  let clicked = 0;
  const nodes = Array.from(card.querySelectorAll('button, [role="button"], span, a'));
  for (const node of nodes) {
    const text = [
      node.innerText || '',
      node.getAttribute('aria-label') || '',
      node.getAttribute('title') || '',
    ].join(' ').replace(/\s+/g, ' ').trim();
    if (!text || text.length > 80 || !labels.test(text)) continue;
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    try {
      node.click();
      clicked += 1;
      if (clicked >= 3) break;
    } catch {}
  }
  return clicked;
}
"""


EXTRACT_CARD_SCRIPT = r"""
(card) => {
  const compact = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const visibleText = (node) => {
    if (!node) return '';
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return '';
    return compact(node.innerText || node.textContent || '');
  };
  const aria = (node) => compact([
    node?.getAttribute?.('aria-label') || '',
    node?.getAttribute?.('title') || '',
  ].join(' '));
  const hrefs = Array.from(card.querySelectorAll('a[href]')).map(a => a.href || a.getAttribute('href') || '').filter(Boolean);
  const postUrl = hrefs.find(h => /\/feed\/update\//.test(h))
    || hrefs.find(h => /\/posts\//.test(h))
    || hrefs.find(h => /activity-\d+/.test(h))
    || '';
  const urn = card.getAttribute('data-urn') || card.getAttribute('data-id') || '';
  const timeNode = card.querySelector('time');
  const rawDate = compact(
    timeNode?.getAttribute('datetime')
    || timeNode?.innerText
    || Array.from(card.querySelectorAll('span, a'))
      .map(visibleText)
      .find(text => /(\d+\s*(秒|分钟|分|小时|天|周|月|年|s|m|h|d|w|mo|yr)|now|昨天|yesterday|ago)/i.test(text) && text.length <= 80)
    || ''
  );
  const textSelectors = [
    '.update-components-text',
    '.feed-shared-inline-show-more-text',
    '.feed-shared-update-v2__description',
    '.break-words',
    '[dir="ltr"]',
  ];
  const textParts = [];
  for (const selector of textSelectors) {
    for (const node of Array.from(card.querySelectorAll(selector))) {
      const text = visibleText(node);
      if (text && text.length > 5 && !textParts.includes(text)) textParts.push(text);
    }
  }
  let text = textParts.sort((a, b) => b.length - a.length)[0] || '';
  const reactionText = compact(card.querySelector('.social-details-social-counts__reactions-count')?.innerText || '');
  const commentText = compact(
    Array.from(card.querySelectorAll('.social-details-social-counts__comments, button, a, span'))
      .map(node => compact([visibleText(node), aria(node)].filter(Boolean).join(' | ')))
      .find(text => /(comment|评论|評論|回复|回覆)/i.test(text) && text.length <= 180)
    || ''
  );
  const repostText = compact(
    Array.from(card.querySelectorAll('.social-details-social-counts__reposts, button, a, span'))
      .map(node => compact([visibleText(node), aria(node)].filter(Boolean).join(' | ')))
      .find(text => /(repost|share|转发|轉發|分享)/i.test(text) && text.length <= 180)
    || ''
  );
  const viewText = compact(
    Array.from(card.querySelectorAll('button, a, span, div'))
      .map(node => compact([visibleText(node), aria(node)].filter(Boolean).join(' | ')))
      .find(text => /(impression|view|浏览|瀏覽|曝光|展示)/i.test(text) && !/(reaction|回应|赞|讚|点赞|喜欢)/i.test(text) && text.length <= 180)
    || ''
  );
  const metricCandidates = Array.from(card.querySelectorAll(
    '.social-details-social-counts__item, .social-details-social-counts__reactions-count, .social-details-social-counts__comments, .social-details-social-counts__reposts, button, span, a'
  )).map(node => compact([visibleText(node), aria(node)].filter(Boolean).join(' | '))).filter(Boolean);
  const allText = compact(card.innerText || '');
  const rect = card.getBoundingClientRect();
  return {
    postUrl,
    urn,
    rawDate,
    text,
    reactionText,
    commentText,
    repostText,
    viewText,
    metricCandidates: Array.from(new Set(metricCandidates)).slice(0, 120),
    allText: allText.slice(0, 5000),
    top: Math.round(rect.top + window.scrollY),
    height: Math.round(rect.height),
  };
}
"""


def page_state(page: Any) -> dict[str, Any]:
    try:
        data = page.evaluate(PAGE_STATE_SCRIPT)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        return {"error": str(exc).splitlines()[0]}


def hard_marker_present(state: dict[str, Any]) -> bool:
    markers = state.get("hardMarkers")
    return isinstance(markers, list) and bool(markers)


def parse_datetime_text(raw: str | None) -> str:
    parsed = SOCIAL_METRICS.parse_social_datetime_text(raw)
    return parsed or compact_spaces(raw)


def metric_from_sources(sources: list[str], patterns: list[str]) -> int | None:
    for source in sources:
        text = compact_spaces(source)
        if not text:
            continue
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                value = parse_count_token(match.group("count"))
                if value is not None:
                    return value
    return None


def metric_from_text(text: str | None, patterns: list[str]) -> int | None:
    return metric_from_sources([str(text or "")], patterns)


COUNT = r"(?P<count>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb]|万|萬|億|亿)?)"
METRIC_PATTERNS = {
    "likes": [
        rf"{COUNT}\s*(?:reactions?|likes?|赞|讚|点赞|點讚|喜欢|喜歡)",
        rf"(?:reactions?|likes?|赞|讚|点赞|點讚|喜欢|喜歡)[:：]?\s*{COUNT}",
    ],
    "comments": [
        rf"{COUNT}\s*(?:comments?|评论|評論|回复|回覆)",
        rf"(?:comments?|评论|評論|回复|回覆)[:：]?\s*{COUNT}",
    ],
    "reposts": [
        rf"{COUNT}\s*(?:reposts?|shares?|转发|轉發|分享)",
        rf"(?:reposts?|shares?|转发|轉發|分享)[:：]?\s*{COUNT}",
    ],
    "exposure": [
        rf"{COUNT}\s*(?:impressions?|views?|浏览|瀏覽|曝光|展示)",
        rf"(?:impressions?|views?|浏览|瀏覽|曝光|展示)[:：]?\s*{COUNT}",
    ],
}


def normalize_linkedin_post_url(post_url: str, urn: str) -> str:
    value = compact_spaces(post_url)
    if value:
        return normalize_linkedin_url(value)
    urn_value = compact_spaces(urn)
    match = re.search(r"urn:li:activity:(\d+)", urn_value)
    if match:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{match.group(1)}/"
    match = re.search(r"activity-(\d+)", urn_value)
    if match:
        return f"https://www.linkedin.com/feed/update/urn:li:activity:{match.group(1)}/"
    return ""


def parse_metrics(raw: dict[str, Any]) -> dict[str, int | None]:
    candidates = [str(item) for item in raw.get("metricCandidates") or []]
    candidates.append(str(raw.get("allText") or ""))
    likes = metric_from_text(raw.get("reactionText"), METRIC_PATTERNS["likes"])
    if likes is None:
        direct_reaction = parse_count_token(str(raw.get("reactionText") or ""))
        likes = direct_reaction
    comments = metric_from_text(raw.get("commentText"), METRIC_PATTERNS["comments"])
    reposts = metric_from_text(raw.get("repostText"), METRIC_PATTERNS["reposts"])
    exposure = metric_from_text(raw.get("viewText"), METRIC_PATTERNS["exposure"])
    return {
        "likes": likes if likes is not None else metric_from_sources(candidates, METRIC_PATTERNS["likes"]),
        "comments": comments if comments is not None else metric_from_sources(candidates, METRIC_PATTERNS["comments"]),
        "reposts": reposts if reposts is not None else metric_from_sources(candidates, METRIC_PATTERNS["reposts"]),
        "exposure": exposure if exposure is not None else metric_from_sources(candidates, METRIC_PATTERNS["exposure"]),
        "saves": None,
    }


def write_jsonl_record(path: Path, record: ProbeRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_jsonable(), ensure_ascii=False) + "\n")


def write_xlsx(path: Path, records: list[ProbeRecord]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "LinkedIn"
    ws.append(LOCAL_HEADERS)
    header_fill = PatternFill("solid", fgColor="D9EAF7")
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    for record in records:
        ws.append(record.xlsx_row())
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = wrap
    widths = {
        "A": 24,
        "B": 48,
        "C": 80,
        "D": 24,
        "E": 12,
        "F": 12,
        "G": 12,
        "H": 12,
        "I": 12,
    }
    for letter, width in widths.items():
        ws.column_dimensions[letter].width = width
    ws.freeze_panes = "A2"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.stem}.tmp{path.suffix}")
    wb.save(temp_path)
    temp_path.replace(path)


def click_posts_tab_or_fallback(page: Any, account_home: str, logger: JsonlLogger, args: argparse.Namespace) -> str:
    posts_url = posts_url_from_home(account_home)
    page.goto(account_home, wait_until="domcontentloaded", timeout=args.timeout_ms)
    page_wait_human(page, "home_read", args.home_read_min, args.home_read_max)
    try:
        page.keyboard.press("Escape")
        page.evaluate(DISMISS_OVERLAYS_SCRIPT)
    except Exception:
        pass
    state = page_state(page)
    logger.write("state", stage="home", state=state)
    if hard_marker_present(state):
        raise RuntimeError(f"LinkedIn hard anti-bot state on home: {state.get('hardMarkers')}")

    clicked = False
    click_selectors = [
        'a[href*="/posts/"]',
        'a:has-text("动态")',
        'a:has-text("Posts")',
        'a:has-text("Activity")',
    ]
    for selector in click_selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.click(timeout=5000)
            clicked = True
            logger.write("posts_nav_click", selector=selector)
            break
        except Exception:
            continue
    if not clicked:
        logger.write("posts_nav_fallback", url=posts_url)
        page.goto(posts_url, wait_until="domcontentloaded", timeout=args.timeout_ms)
    page_wait_human(page, "posts_read", args.posts_read_min, args.posts_read_max)
    state = page_state(page)
    logger.write("state", stage="posts", state=state)
    if hard_marker_present(state):
        raise RuntimeError(f"LinkedIn hard anti-bot state on posts: {state.get('hardMarkers')}")
    return page.url


def extract_record_from_raw(account: str, account_url: str, raw: dict[str, Any], state: dict[str, Any]) -> ProbeRecord | None:
    text = compact_spaces(str(raw.get("text") or ""))
    post_url = normalize_linkedin_post_url(str(raw.get("postUrl") or ""), str(raw.get("urn") or ""))
    raw_date = compact_spaces(str(raw.get("rawDate") or ""))
    metrics = parse_metrics(raw)
    if not text and not post_url:
        return None
    return ProbeRecord(
        account=account,
        account_url=account_url,
        post_url=post_url,
        text=text,
        published_at=parse_datetime_text(raw_date),
        raw_date=raw_date,
        exposure=metrics["exposure"],
        likes=metrics["likes"],
        saves=metrics["saves"],
        comments=metrics["comments"],
        reposts=metrics["reposts"],
        soft_markers=[str(item) for item in state.get("softMarkers") or []],
        hard_markers=[str(item) for item in state.get("hardMarkers") or []],
        debug={
            "urn": raw.get("urn"),
            "top": raw.get("top"),
            "height": raw.get("height"),
            "reaction_text": raw.get("reactionText"),
            "comment_text": raw.get("commentText"),
            "repost_text": raw.get("repostText"),
            "view_text": raw.get("viewText"),
            "metric_candidates": raw.get("metricCandidates") or [],
        },
    )


def collect_feed_posts(page: Any, account: str, account_home: str, logger: JsonlLogger, args: argparse.Namespace, jsonl_path: Path, xlsx_path: Path) -> list[ProbeRecord]:
    records: list[ProbeRecord] = []
    seen: set[str] = set()
    idle_scrolls = 0

    for scroll_index in range(args.max_scrolls + 1):
        state = page_state(page)
        logger.write("state", stage=f"feed_scroll_{scroll_index}", state=state)
        if hard_marker_present(state):
            logger.write("hard_antibot_stop", stage=f"feed_scroll_{scroll_index}", hard_markers=state.get("hardMarkers"))
            break

        try:
            cards = page.query_selector_all(POST_CARD_SELECTOR)
        except Exception as exc:
            logger.write("card_query_error", error=str(exc).splitlines()[0])
            break

        before_count = len(records)
        logger.write("cards_seen", scroll_index=scroll_index, card_count=len(cards), collected=len(records))
        for card_index, card in enumerate(cards):
            if len(records) >= args.limit:
                break
            try:
                card.scroll_into_view_if_needed(timeout=args.timeout_ms)
                page_wait_human(page, f"card_{len(records) + 1}_read_before_expand", args.card_read_min, args.card_read_max)
                clicked = card.evaluate(EXPAND_CARD_SCRIPT)
                if clicked:
                    logger.write("card_expand", scroll_index=scroll_index, card_index=card_index, clicked=clicked)
                    page_wait_human(page, f"card_{len(records) + 1}_read_after_expand", args.expand_read_min, args.expand_read_max)
                raw = card.evaluate(EXTRACT_CARD_SCRIPT)
            except Exception as exc:
                logger.write("card_extract_error", scroll_index=scroll_index, card_index=card_index, error=str(exc).splitlines()[0])
                continue
            if not isinstance(raw, dict):
                continue
            record = extract_record_from_raw(account, account_home, raw, page_state(page))
            if record is None:
                continue
            key = record.key()
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
            write_jsonl_record(jsonl_path, record)
            write_xlsx(xlsx_path, records)
            logger.write(
                "record_saved",
                index=len(records),
                post_url=record.post_url,
                text_len=len(record.text),
                raw_date=record.raw_date,
                metrics={
                    "exposure": record.exposure,
                    "likes": record.likes,
                    "saves": record.saves,
                    "comments": record.comments,
                    "reposts": record.reposts,
                },
            )
        if len(records) >= args.limit:
            break
        if len(records) <= before_count:
            idle_scrolls += 1
        else:
            idle_scrolls = 0
        if idle_scrolls >= args.idle_scrolls:
            logger.write("idle_stop", idle_scrolls=idle_scrolls, collected=len(records))
            break
        page.mouse.wheel(0, random.randint(args.scroll_min_px, args.scroll_max_px))
        page_wait_human(page, f"scroll_{scroll_index + 1}_settle", args.scroll_delay_min, args.scroll_delay_max)
    return records


def current_linkedin_page(context: Any) -> Any | None:
    pages = [page for page in context.pages if "linkedin.com" in (page.url or "").lower()]
    if pages:
        return pages[-1]
    return context.pages[-1] if context.pages else None


def resolve_account_url(page: Any | None, explicit_url: str) -> str:
    if explicit_url:
        return account_home_from_url(explicit_url)
    if page and "linkedin.com" in (page.url or "").lower():
        return account_home_from_url(page.url)
    raise SystemExit("Provide a LinkedIn account URL or open one in the CDP browser.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Small slow LinkedIn feed probe through the project CDP profile.")
    parser.add_argument("account_url", nargs="?", help="LinkedIn company/profile home URL. Defaults to the current LinkedIn CDP page.")
    parser.add_argument("--account", help="Display account name for XLSX column A.")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--max-scrolls", type=int, default=5)
    parser.add_argument("--idle-scrolls", type=int, default=2)
    parser.add_argument("--timeout-ms", type=int, default=60_000)
    parser.add_argument("--cdp-url", default=os.getenv("SOCIAL_CDP_URL", ""))
    parser.add_argument("--cdp-profile", type=Path, default=Path(os.getenv("SOCIAL_CDP_PROFILE", str(DEFAULT_CDP_PROFILE))))
    parser.add_argument("--cdp-port", type=int, default=int(os.getenv("SOCIAL_CDP_PORT", str(DEFAULT_CDP_PORT))))
    parser.add_argument("--start-minimized", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--reuse-current-page", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--jsonl", type=Path)
    parser.add_argument("--xlsx", type=Path)
    parser.add_argument("--home-read-min", type=float, default=25.0)
    parser.add_argument("--home-read-max", type=float, default=45.0)
    parser.add_argument("--posts-read-min", type=float, default=30.0)
    parser.add_argument("--posts-read-max", type=float, default=60.0)
    parser.add_argument("--card-read-min", type=float, default=18.0)
    parser.add_argument("--card-read-max", type=float, default=35.0)
    parser.add_argument("--expand-read-min", type=float, default=12.0)
    parser.add_argument("--expand-read-max", type=float, default=25.0)
    parser.add_argument("--scroll-delay-min", type=float, default=20.0)
    parser.add_argument("--scroll-delay-max", type=float, default=40.0)
    parser.add_argument("--scroll-min-px", type=int, default=650)
    parser.add_argument("--scroll-max-px", type=int, default=1300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    args.export_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id()
    logger = JsonlLogger(args.log_dir / f"linkedin_feed_probe_{rid}.events.jsonl")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Missing playwright dependency.") from exc

    session = None
    if args.cdp_url:
        cdp_url = args.cdp_url
    else:
        session = SOCIAL_METRICS.ensure_cdp_profile(
            args.cdp_profile,
            args.cdp_port,
            "https://www.linkedin.com/",
            start_minimized=bool(args.start_minimized),
            auto_close=False,
        )
        cdp_url = session.url

    logger.write("run_start", cdp_url=cdp_url, limit=args.limit, mode="linkedin_feed_probe")

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context(locale="en-US")
        page = current_linkedin_page(context) if args.reuse_current_page else None
        if page is None:
            page = context.new_page()
        page.set_default_timeout(args.timeout_ms)
        page.set_default_navigation_timeout(args.timeout_ms)

        account_home = resolve_account_url(page, args.account_url or "")
        account = args.account or account_name_from_url(account_home)
        stem = safe_stem(f"linkedin_{account}_{rid}")
        jsonl_path = args.jsonl or args.export_dir / f"{stem}.jsonl"
        xlsx_path = args.xlsx or args.export_dir / f"{stem}.xlsx"
        logger.write("outputs", account=account, account_home=account_home, jsonl=str(jsonl_path), xlsx=str(xlsx_path))

        try:
            final_feed_url = click_posts_tab_or_fallback(page, account_home, logger, args)
            logger.write("feed_ready", url=final_feed_url)
            records = collect_feed_posts(page, account, account_home, logger, args, jsonl_path, xlsx_path)
            if not records:
                write_xlsx(xlsx_path, records)
            logger.write("run_done", records=len(records), jsonl=str(jsonl_path), xlsx=str(xlsx_path))
            print(f"records={len(records)}")
            print(f"jsonl={jsonl_path}")
            print(f"xlsx={xlsx_path}")
            return 0
        finally:
            try:
                if not args.reuse_current_page:
                    page.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
