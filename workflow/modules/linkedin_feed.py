#!/usr/bin/env python3
"""Slow LinkedIn feed-card collection for F009.

The collector intentionally starts at the account home page, enters the posts
feed, expands visible cards, and reads one card at a time through the logged-in
CDP profile. It does not open a fast detail-page loop.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_CDP_PORT = 9222
LOCAL_TIMEZONE = timezone(timedelta(hours=8))


def import_social_metrics() -> Any:
    module_name = "weekly_account_linkedin_feed_social_metrics"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = MODULE_DIR / "social_metrics.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import social_metrics module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


SOCIAL_METRICS = import_social_metrics()


class LinkedInFeedError(RuntimeError):
    def __init__(self, message: str, *, records: list["LinkedInFeedPost"] | None = None, stats: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.records = records or []
        self.stats = stats or {}


class LinkedInHardAntiBot(LinkedInFeedError):
    pass


class LinkedInUnavailablePage(LinkedInFeedError):
    pass


class LinkedInDateUnreadable(LinkedInFeedError):
    pass


@dataclass
class LinkedInFeedPost:
    account: str
    account_url: str
    post_url: str = ""
    text: str = ""
    published_at: str = ""
    raw_date: str = ""
    metrics: dict[str, int | None] = field(default_factory=dict)
    soft_markers: list[str] = field(default_factory=list)
    hard_markers: list[str] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        if self.post_url:
            return self.post_url
        raw = f"{self.account_url}\n{self.text[:500]}\n{self.raw_date}"
        return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "account_url": self.account_url,
            "post_url": self.post_url,
            "text": self.text,
            "published_at": self.published_at,
            "raw_date": self.raw_date,
            "metrics": self.metrics,
            "soft_markers": self.soft_markers,
            "hard_markers": self.hard_markers,
            "debug": self.debug,
        }


class JsonlLogger:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **payload: Any) -> None:
        line = json.dumps({"ts": timestamp(), "event": event, **payload}, ensure_ascii=False, default=str)
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        print(line, file=sys.stderr, flush=True)


def timestamp() -> str:
    return datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def compact_spaces(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def clean_linkedin_text(text: str | None) -> str:
    value = compact_spaces(text)
    if not value:
        return ""
    value = re.sub(r"\bhashtag\s+#", "#", value, flags=re.IGNORECASE)
    value = re.sub(r"(?:话题标签|話題標籤)\s*#", "#", value)
    return compact_spaces(value)


def parse_count_token(token: str | None) -> int | None:
    return SOCIAL_METRICS.parse_count_token(token)


def parse_datetime_text(raw: str | None) -> str:
    parsed = SOCIAL_METRICS.parse_social_datetime_text(raw)
    if parsed:
        return parsed
    text = compact_spaces(raw).lower()
    match = re.search(r"(?P<count>\d+)\s*(?:个|個)?\s*(?P<unit>秒|分钟|分鐘|分|小时|小時|天|日|周|週|星期|月|年)\s*前", text)
    if match:
        count = int(match.group("count"))
        unit = match.group("unit")
        if unit == "秒":
            delta = timedelta(seconds=count)
        elif unit in {"分钟", "分鐘", "分"}:
            delta = timedelta(minutes=count)
        elif unit in {"小时", "小時"}:
            delta = timedelta(hours=count)
        elif unit in {"天", "日"}:
            delta = timedelta(days=count)
        elif unit in {"周", "週", "星期"}:
            delta = timedelta(weeks=count)
        elif unit == "月":
            delta = timedelta(days=count * 30)
        else:
            delta = timedelta(days=count * 365)
        return (datetime.now(LOCAL_TIMEZONE) - delta).isoformat()
    if text in {"now", "just now", "刚刚"}:
        return datetime.now(LOCAL_TIMEZONE).isoformat()
    if text in {"yesterday", "昨天"}:
        return (datetime.now(LOCAL_TIMEZONE) - timedelta(days=1)).isoformat()
    match = re.search(r"(?P<count>\d+)\s*(?P<unit>s|m|h|d|w|mo|mon|mos|y|yr|yrs)\b", text, flags=re.I)
    if match:
        count = int(match.group("count"))
        unit = match.group("unit").lower()
        if unit == "s":
            delta = timedelta(seconds=count)
        elif unit == "m":
            delta = timedelta(minutes=count)
        elif unit == "h":
            delta = timedelta(hours=count)
        elif unit == "d":
            delta = timedelta(days=count)
        elif unit == "w":
            delta = timedelta(weeks=count)
        elif unit in {"mo", "mon", "mos"}:
            delta = timedelta(days=count * 30)
        else:
            delta = timedelta(days=count * 365)
        return (datetime.now(LOCAL_TIMEZONE) - delta).isoformat()
    return compact_spaces(raw)


def parse_local_date(value: str | None) -> date | None:
    text = compact_spaces(value)
    if not text:
        return None
    clean = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(LOCAL_TIMEZONE).date()


def date_state(value: str | None, *, date_start: date | None, date_end_exclusive: date | None) -> str:
    parsed = parse_local_date(value)
    if not parsed:
        return "unknown"
    if date_start and parsed < date_start:
        return "before-start"
    if date_end_exclusive and parsed >= date_end_exclusive:
        return "after-end"
    return "in-range"


def random_delay(min_s: float, max_s: float) -> float:
    low = max(0.0, min_s)
    high = max(low, max_s)
    return random.uniform(low, high)


def page_wait_human(page: Any, label: str, min_s: float, max_s: float) -> float:
    delay = random_delay(min_s, max_s)
    print(f"[linkedin-feed-wait] {label} {delay:.1f}s", file=sys.stderr, flush=True)
    page.wait_for_timeout(int(delay * 1000))
    return delay


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


PAGE_STATE_SCRIPT = r"""
() => {
  const frames = Array.from(document.querySelectorAll('iframe[src]')).map(node => node.src || '').filter(Boolean);
  const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
  const bodyText = body.replace(/\s+/g, ' ').trim();
  const links = Array.from(document.querySelectorAll('a[href]')).map(node => node.href || node.getAttribute('href') || '').filter(Boolean);
  const postLikeLinks = links.filter(href => /\/(?:feed\/update|posts|pulse)\//i.test(href)).length;
  const currentUrl = location.href || '';
  const text = [document.title || '', currentUrl, bodyText.slice(0, 5000), ...frames].join(' ').toLowerCase();
  const softMarkers = ['li.protechts.net', 'recaptcha'].filter(marker => text.includes(marker));
  const rawHardMarkers = [
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
  const linkedinLinkCount = links.filter(href => /linkedin\.com/i.test(href)).length;
  const contentSample = bodyText.slice(Math.max(0, bodyText.length - 4000));
  let contentHash = 0;
  for (let i = 0; i < contentSample.length; i += 1) {
    contentHash = ((contentHash * 31) + contentSample.charCodeAt(i)) >>> 0;
  }
  const contentUsable = Boolean(document.querySelector('main'))
    && bodyText.length >= 1200
    && linkedinLinkCount >= 15;
  const hardMarkers = [];
  const unavailable = /\/company\/unavailable\/?/i.test(currentUrl)
    || /(page not found|this page doesn't exist|this company page doesn't exist|company page doesn't exist|profile not found|content isn't available|此公司主页不存在|您搜索的主页不存在|你搜索的主页不存在|您搜尋的主頁不存在|页面不存在|主页不存在|主頁不存在|无法访问|內容不可用|内容不可用|找不到|搜素的主页不存在|搜索的主页不存在)/i.test(bodyText);
  const loginWall = /\/(?:login|uas\/login|authwall)/i.test(currentUrl)
    || /(sign in to view|log in to linkedin|join linkedin to view|加入 linkedin 即可|登录即可查看|登入即可查看|註冊即可查看|注册即可查看)/i.test(bodyText);
  return {
    title: document.title || '',
    url: location.href || '',
    readyState: document.readyState || '',
    scrollY: Math.round(window.scrollY || 0),
    scrollHeight: Math.round(document.documentElement?.scrollHeight || document.body?.scrollHeight || 0),
    viewportHeight: Math.round(window.innerHeight || 0),
    bodyTextLength: bodyText.length,
    contentHash,
    samePlatformLinks: linkedinLinkCount,
    postLikeLinks,
    hasMain: Boolean(document.querySelector('main')),
    unavailable,
    loginWall,
    softMarkers,
    hardMarkers,
    rawHardMarkers,
    contentUsable,
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
  const labels = /(see more|show more|展开|顯示更多|查看更多|显示更多)/i;
  let clicked = 0;
  const nodes = Array.from(card.querySelectorAll('button, [role="button"]'));
  for (const node of nodes) {
    if (node.closest('a[href]')) continue;
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
  const allText = compact(card.innerText || '');
  const datePattern = /(\d+\s*(?:个|個)?\s*(秒|分钟|分鐘|分|小时|小時|天|日|周|週|星期|月|年|s|m|h|d|w|mo|mon|mos|yr|yrs)\s*(?:前|ago)?|now|just now|刚刚|昨天|yesterday|ago|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?|\d{1,2}\s*月\s*\d{1,2}\s*日)/i;
  const dateCandidates = [];
  const dateNodes = [
    ...Array.from(card.querySelectorAll('.update-components-actor__sub-description, [class*="actor__sub-description"], time')),
    ...Array.from(card.querySelectorAll('span, a, div')),
  ];
  for (const node of dateNodes) {
    const parts = [
      node.getAttribute?.('datetime') || '',
      visibleText(node),
      node.getAttribute?.('aria-label') || '',
      node.getAttribute?.('title') || '',
    ].map(compact).filter(Boolean);
    for (const text of parts) {
      if (text.length <= 140 && datePattern.test(text) && !dateCandidates.includes(text)) {
        dateCandidates.push(text);
      }
    }
  }
  let rawDate = compact(dateCandidates[0] || '');
  if (!rawDate) {
    const fallbackMatch = allText.match(datePattern);
    rawDate = compact(fallbackMatch?.[0] || '');
  }
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
  const text = textParts.sort((a, b) => b.length - a.length)[0] || '';
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


def page_state(page: Any) -> dict[str, Any]:
    try:
        data = page.evaluate(PAGE_STATE_SCRIPT)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        return {"error": str(exc).splitlines()[0]}


def hard_marker_present(state: dict[str, Any]) -> bool:
    markers = state.get("hardMarkers")
    return isinstance(markers, list) and bool(markers)


def unavailable_page_present(state: dict[str, Any]) -> bool:
    return bool(state.get("unavailable")) or "/company/unavailable" in str(state.get("url") or "").lower()


def valid_linkedin_page_state(state: dict[str, Any]) -> bool:
    if state.get("unavailable") or state.get("loginWall") or state.get("error"):
        return False
    if hard_marker_present(state):
        return False
    if state.get("contentUsable"):
        return True
    if int(state.get("postLikeLinks") or 0) > 0:
        return True
    if int(state.get("bodyTextLength") or 0) >= 300 and int(state.get("samePlatformLinks") or 0) >= 5:
        return True
    return bool(state.get("hasMain")) and int(state.get("bodyTextLength") or 0) >= 500


def linkedin_path_key(url: str | None) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return ""
    return re.sub(r"/+", "/", parsed.path or "/").rstrip("/").lower() or "/"


def page_is_account_home(page: Any, account_home: str) -> bool:
    current_key = linkedin_path_key(getattr(page, "url", ""))
    account_key = linkedin_path_key(account_home)
    return bool(current_key and account_key and current_key == account_key)


def page_progress_signature(state: dict[str, Any], card_count: int, seen_count: int) -> tuple[int, int, int, int, int, int]:
    return (
        int(state.get("bodyTextLength") or 0),
        int(state.get("contentHash") or 0),
        int(state.get("samePlatformLinks") or 0),
        int(state.get("scrollHeight") or 0),
        int(card_count or 0),
        int(seen_count or 0),
    )


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
        likes = parse_count_token(str(raw.get("reactionText") or ""))
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


def extract_record_from_raw(account: str, account_url: str, raw: dict[str, Any], state: dict[str, Any]) -> LinkedInFeedPost | None:
    text = clean_linkedin_text(str(raw.get("text") or ""))
    post_url = normalize_linkedin_post_url(str(raw.get("postUrl") or ""), str(raw.get("urn") or ""))
    raw_date = compact_spaces(str(raw.get("rawDate") or ""))
    metrics = parse_metrics(raw)
    if not text and not post_url:
        return None
    return LinkedInFeedPost(
        account=account,
        account_url=account_url,
        post_url=post_url,
        text=text,
        published_at=parse_datetime_text(raw_date),
        raw_date=raw_date,
        metrics=metrics,
        soft_markers=[str(item) for item in state.get("softMarkers") or []],
        hard_markers=[str(item) for item in state.get("hardMarkers") or []],
        debug={
            "urn": raw.get("urn"),
            "top": raw.get("top"),
            "height": raw.get("height"),
            "raw_hard_markers": [str(item) for item in state.get("rawHardMarkers") or []],
            "content_usable": bool(state.get("contentUsable")),
            "reaction_text": raw.get("reactionText"),
            "comment_text": raw.get("commentText"),
            "repost_text": raw.get("repostText"),
            "view_text": raw.get("viewText"),
            "metric_candidates": raw.get("metricCandidates") or [],
        },
    )


def write_discovery_snapshot(path: Path | None, records: list[LinkedInFeedPost]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([record.to_jsonable() for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )


@dataclass(frozen=True)
class LinkedInFeedOptions:
    timeout_ms: int = 60_000
    max_scrolls: int = 5
    idle_scrolls: int = 2
    post_limit: int = 3
    home_read_min_s: float = 4.0
    home_read_max_s: float = 8.0
    posts_read_min_s: float = 5.0
    posts_read_max_s: float = 10.0
    card_read_min_s: float = 3.0
    card_read_max_s: float = 7.0
    expand_read_min_s: float = 2.0
    expand_read_max_s: float = 5.0
    scroll_delay_min_s: float = 4.0
    scroll_delay_max_s: float = 8.0
    scroll_min_px: int = 650
    scroll_max_px: int = 1300
    cdp_start_minimized: bool = True
    manual_challenge_wait: bool = False
    manual_challenge_timeout_s: float = 0.0
    manual_challenge_poll_s: float = 10.0
    date_unknown_text_limit: int = 3


def wait_for_manual_challenge_clear(
    page: Any,
    logger: JsonlLogger,
    options: LinkedInFeedOptions,
    stage: str,
    state: dict[str, Any],
    *,
    records: list["LinkedInFeedPost"] | None = None,
    stats: dict[str, int] | None = None,
    on_challenge: Callable[[str, str, dict[str, Any], float], None] | None = None,
) -> dict[str, Any]:
    if not hard_marker_present(state):
        return state
    markers = state.get("hardMarkers")
    if not options.manual_challenge_wait:
        raise LinkedInHardAntiBot(f"LinkedIn hard anti-bot state on {stage}: {markers}", records=records, stats=stats)

    started = time.monotonic()
    timeout_s = max(0.0, float(options.manual_challenge_timeout_s or 0.0))
    poll_s = max(3.0, float(options.manual_challenge_poll_s or 10.0))
    url = str(state.get("url") or "")
    logger.write(
        "manual_challenge_wait_start",
        stage=stage,
        markers=markers,
        url=url,
        timeout_s=timeout_s,
        poll_s=poll_s,
    )
    if on_challenge:
        on_challenge("start", stage, state, 0.0)
    timeout_text = "no-timeout" if timeout_s <= 0 else f"{timeout_s:.0f}s"
    print(
        f"[linkedin-challenge] stage={stage} markers={markers} url={url} "
        f"waiting_for_manual_verification timeout={timeout_text}",
        file=sys.stderr,
        flush=True,
    )

    while True:
        elapsed = time.monotonic() - started
        if timeout_s > 0 and elapsed >= timeout_s:
            latest_state = page_state(page)
            logger.write(
                "manual_challenge_wait_timeout",
                stage=stage,
                elapsed_s=round(elapsed, 1),
                state=latest_state,
            )
            if on_challenge:
                on_challenge("timeout", stage, latest_state, elapsed)
            raise LinkedInHardAntiBot(
                f"LinkedIn hard anti-bot state on {stage} after manual wait timeout {timeout_s:.0f}s: "
                f"{latest_state.get('hardMarkers') or markers}",
                records=records,
                stats=stats,
            )
        time.sleep(poll_s)
        latest_state = page_state(page)
        logger.write(
            "manual_challenge_wait_poll",
            stage=stage,
            elapsed_s=round(time.monotonic() - started, 1),
            state=latest_state,
        )
        if hard_marker_present(latest_state):
            print(
                f"[linkedin-challenge] stage={stage} still_waiting "
                f"elapsed_s={time.monotonic() - started:.1f} markers={latest_state.get('hardMarkers')}",
                file=sys.stderr,
                flush=True,
            )
            continue
        if unavailable_page_present(latest_state):
            logger.write(
                "manual_challenge_wait_done_unavailable",
                stage=stage,
                elapsed_s=round(time.monotonic() - started, 1),
                state=latest_state,
            )
            if on_challenge:
                on_challenge("cleared_unavailable", stage, latest_state, time.monotonic() - started)
            return latest_state
        if not valid_linkedin_page_state(latest_state):
            print(
                f"[linkedin-challenge] stage={stage} hard_markers_cleared_waiting_valid_page "
                f"elapsed_s={time.monotonic() - started:.1f} url={latest_state.get('url')}",
                file=sys.stderr,
                flush=True,
            )
            continue
        try:
            page.keyboard.press("Escape")
            page.evaluate(DISMISS_OVERLAYS_SCRIPT)
        except Exception:
            pass
        settled_state = page_state(page)
        logger.write(
            "manual_challenge_wait_done",
            stage=stage,
            elapsed_s=round(time.monotonic() - started, 1),
            state=settled_state,
        )
        if on_challenge:
            on_challenge("done", stage, settled_state, time.monotonic() - started)
        print(
            f"[linkedin-challenge] stage={stage} cleared elapsed_s={time.monotonic() - started:.1f}",
            file=sys.stderr,
            flush=True,
        )
        return settled_state


def click_posts_tab_or_fallback(
    page: Any,
    account_home: str,
    logger: JsonlLogger,
    options: LinkedInFeedOptions,
    on_challenge: Callable[[str, str, dict[str, Any], float], None] | None = None,
) -> str:
    posts_url = posts_url_from_home(account_home)
    if page_is_account_home(page, account_home):
        logger.write("initial_account_page_reused", url=str(page.url or ""), account_home=account_home)
    else:
        logger.write("initial_account_page_navigate", from_url=str(page.url or ""), to_url=account_home)
        page.goto(account_home, wait_until="domcontentloaded", timeout=options.timeout_ms)
    page_wait_human(page, "home_read", options.home_read_min_s, options.home_read_max_s)
    try:
        page.keyboard.press("Escape")
        page.evaluate(DISMISS_OVERLAYS_SCRIPT)
    except Exception:
        pass
    state = page_state(page)
    logger.write("state", stage="home", state=state)
    if hard_marker_present(state):
        state = wait_for_manual_challenge_clear(page, logger, options, "home", state, on_challenge=on_challenge)
    if unavailable_page_present(state):
        raise LinkedInUnavailablePage(f"LinkedIn company page unavailable on home: {state.get('url') or state.get('title')}")

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
        page.goto(posts_url, wait_until="domcontentloaded", timeout=options.timeout_ms)
    page_wait_human(page, "posts_read", options.posts_read_min_s, options.posts_read_max_s)
    state = page_state(page)
    logger.write("state", stage="posts", state=state)
    if hard_marker_present(state):
        state = wait_for_manual_challenge_clear(page, logger, options, "posts", state, on_challenge=on_challenge)
    if unavailable_page_present(state):
        raise LinkedInUnavailablePage(f"LinkedIn company page unavailable on posts: {state.get('url') or state.get('title')}")
    return str(page.url or "")


def page_url_is_target(url: str | None, target_url: str) -> bool:
    try:
        current = normalize_linkedin_url(str(url or ""))
        target = normalize_linkedin_url(target_url)
    except Exception:
        return False
    return linkedin_path_key(current) == linkedin_path_key(target)


def select_initial_account_page(context: Any, account_home: str, *, prefer_existing: bool, timeout_ms: int) -> Any:
    if prefer_existing:
        for candidate in list(getattr(context, "pages", []) or []):
            try:
                if page_url_is_target(getattr(candidate, "url", ""), account_home):
                    return candidate
            except Exception:
                continue
    page = context.new_page()
    page.goto(account_home, wait_until="domcontentloaded", timeout=timeout_ms)
    return page


def collect_visible_feed_posts(
    page: Any,
    account: str,
    account_home: str,
    logger: JsonlLogger,
    options: LinkedInFeedOptions,
    *,
    latest_mode: bool,
    date_start: date | None,
    date_end_exclusive: date | None,
    discovery_path: Path | None = None,
    on_challenge: Callable[[str, str, dict[str, Any], float], None] | None = None,
) -> tuple[list[LinkedInFeedPost], dict[str, int]]:
    records: list[LinkedInFeedPost] = []
    seen: set[str] = set()
    idle_scrolls = 0
    consecutive_date_unknown = 0
    last_progress_signature: tuple[int, int, int, int, int, int] | None = None
    reloaded_after_no_content = False
    stats: dict[str, int] = {
        "cards_seen": 0,
        "candidates": 0,
        "filtered": 0,
        "skipped_no_text": 0,
        "skipped_no_date": 0,
        "valid_account_page": 0,
    }

    for scroll_index in range(options.max_scrolls + 1):
        state = page_state(page)
        logger.write("state", stage=f"feed_scroll_{scroll_index}", state=state)
        if hard_marker_present(state):
            state = wait_for_manual_challenge_clear(
                page,
                logger,
                options,
                f"feed scroll {scroll_index}",
                state,
                records=records,
                stats=stats,
                on_challenge=on_challenge,
            )
        if valid_linkedin_page_state(state):
            stats["valid_account_page"] = 1
        if unavailable_page_present(state):
            raise LinkedInUnavailablePage(
                f"LinkedIn company page unavailable on feed scroll {scroll_index}: {state.get('url') or state.get('title')}",
                records=records,
                stats=stats,
            )

        try:
            cards = page.query_selector_all(POST_CARD_SELECTOR)
        except Exception as exc:
            logger.write("card_query_error", error=str(exc).splitlines()[0])
            break

        before_seen = len(seen)
        stats["cards_seen"] += len(cards)
        if cards:
            stats["valid_account_page"] = 1
        logger.write("cards_seen", scroll_index=scroll_index, card_count=len(cards), collected=len(records))
        stop_before_start = False
        for card_index, card in enumerate(cards):
            if len(records) >= options.post_limit:
                break
            try:
                card.scroll_into_view_if_needed(timeout=options.timeout_ms)
                page_wait_human(page, f"card_{len(records) + 1}_read_before_expand", options.card_read_min_s, options.card_read_max_s)
                before_expand_url = str(page.url or "")
                clicked = card.evaluate(EXPAND_CARD_SCRIPT)
                if clicked:
                    logger.write("card_expand", scroll_index=scroll_index, card_index=card_index, clicked=clicked)
                    page_wait_human(page, f"card_{len(records) + 1}_read_after_expand", options.expand_read_min_s, options.expand_read_max_s)
                    after_expand_url = str(page.url or "")
                    if after_expand_url and after_expand_url != before_expand_url:
                        logger.write(
                            "card_expand_navigation",
                            scroll_index=scroll_index,
                            card_index=card_index,
                            before_url=before_expand_url,
                            after_url=after_expand_url,
                        )
                        try:
                            page.go_back(wait_until="domcontentloaded", timeout=options.timeout_ms)
                            page_wait_human(page, "return_after_expand_navigation", options.posts_read_min_s, options.posts_read_max_s)
                        except Exception as nav_exc:
                            logger.write("card_expand_navigation_recover_error", error=str(nav_exc).splitlines()[0])
                            try:
                                page.goto(posts_url_from_home(account_home), wait_until="domcontentloaded", timeout=options.timeout_ms)
                                page_wait_human(page, "reload_posts_after_expand_navigation", options.posts_read_min_s, options.posts_read_max_s)
                            except Exception as goto_exc:
                                logger.write("card_expand_navigation_reload_error", error=str(goto_exc).splitlines()[0])
                        continue
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
            stats["candidates"] += 1
            if not record.text:
                stats["skipped_no_text"] += 1
                continue
            if not latest_mode:
                state_name = date_state(record.published_at, date_start=date_start, date_end_exclusive=date_end_exclusive)
                if state_name == "unknown":
                    stats["skipped_no_date"] += 1
                    stats["date_unknown_text_cards"] = stats.get("date_unknown_text_cards", 0) + 1
                    consecutive_date_unknown += 1
                    logger.write(
                        "date_unknown",
                        scroll_index=scroll_index,
                        card_index=card_index,
                        raw_date=record.raw_date,
                        published_at=record.published_at,
                        post_url=record.post_url,
                        consecutive=consecutive_date_unknown,
                    )
                    if consecutive_date_unknown >= max(1, int(options.date_unknown_text_limit or 1)):
                        stats["date_unreadable_stop"] = 1
                        logger.write(
                            "date_unreadable_stop",
                            scroll_index=scroll_index,
                            card_index=card_index,
                            limit=options.date_unknown_text_limit,
                            collected=len(records),
                            seen=len(seen),
                            raw_date=record.raw_date,
                            published_at=record.published_at,
                            post_url=record.post_url,
                            text_sample=record.text[:180],
                        )
                        raise LinkedInDateUnreadable(
                            "LinkedIn date unreadable on text-bearing feed cards; stopped this account to avoid crossing the requested date range",
                            records=records,
                            stats=stats,
                        )
                    continue
                consecutive_date_unknown = 0
                if state_name == "after-end":
                    stats["filtered"] += 1
                    logger.write(
                        "date_after_end",
                        scroll_index=scroll_index,
                        card_index=card_index,
                        raw_date=record.raw_date,
                        published_at=record.published_at,
                        post_url=record.post_url,
                    )
                    continue
                if state_name == "before-start":
                    stats["filtered"] += 1
                    stop_before_start = True
                    logger.write(
                        "date_before_start",
                        scroll_index=scroll_index,
                        card_index=card_index,
                        raw_date=record.raw_date,
                        published_at=record.published_at,
                        post_url=record.post_url,
                        collected=len(records),
                    )
                    break
            records.append(record)
            write_discovery_snapshot(discovery_path, records)
            logger.write(
                "record_collected",
                index=len(records),
                post_url=record.post_url,
                text_len=len(record.text),
                raw_date=record.raw_date,
                published_at=record.published_at,
                metrics=record.metrics,
            )
        if len(records) >= options.post_limit:
            break
        if stop_before_start:
            stats["early_stop_before_start"] = stats.get("early_stop_before_start", 0) + 1
            logger.write("early_stop_before_start", scroll_index=scroll_index, collected=len(records))
            break
        progress_signature = page_progress_signature(page_state(page), len(cards), len(seen))
        content_progressed = last_progress_signature is None or progress_signature != last_progress_signature or len(seen) > before_seen
        logger.write(
            "page_progress",
            scroll_index=scroll_index,
            progressed=content_progressed,
            idle_scrolls=idle_scrolls,
            signature=progress_signature,
            previous_signature=last_progress_signature,
        )
        if not content_progressed:
            idle_scrolls += 1
        else:
            idle_scrolls = 0
        last_progress_signature = progress_signature
        if idle_scrolls >= options.idle_scrolls:
            if not stats.get("valid_account_page") and not reloaded_after_no_content:
                reloaded_after_no_content = True
                stats["reload_after_no_readable_content"] = 1
                logger.write(
                    "reload_after_no_readable_content",
                    scroll_index=scroll_index,
                    idle_scrolls=idle_scrolls,
                    state=page_state(page),
                )
                try:
                    page.reload(wait_until="domcontentloaded", timeout=options.timeout_ms)
                    page_wait_human(page, "reload_after_no_readable_content", options.posts_read_min_s, options.posts_read_max_s)
                except Exception as exc:
                    logger.write("reload_after_no_readable_content_error", error=str(exc).splitlines()[0])
                idle_scrolls = 0
                last_progress_signature = None
                continue
            if not stats.get("valid_account_page"):
                stats["blocked_no_readable_content"] = 1
                logger.write(
                    "blocked_no_readable_content",
                    idle_scrolls=idle_scrolls,
                    collected=len(records),
                    seen=len(seen),
                    state=page_state(page),
                )
                raise LinkedInHardAntiBot(
                    "LinkedIn anti-bot/no readable content after reload and scroll attempts",
                    records=records,
                    stats=stats,
                )
            stats["idle_no_new_content"] = stats.get("idle_no_new_content", 0) + 1
            logger.write("idle_stop_no_new_content", idle_scrolls=idle_scrolls, collected=len(records), seen=len(seen))
            break
        page.mouse.wheel(0, random.randint(options.scroll_min_px, options.scroll_max_px))
        page_wait_human(page, f"scroll_{scroll_index + 1}_settle", options.scroll_delay_min_s, options.scroll_delay_max_s)
    write_discovery_snapshot(discovery_path, records)
    return records, stats


def collect_linkedin_feed_posts(
    account_url: str,
    account: str,
    *,
    cdp_profile: Path,
    cdp_port: int = DEFAULT_CDP_PORT,
    options: LinkedInFeedOptions | None = None,
    latest_mode: bool = False,
    date_start: date | None = None,
    date_end_exclusive: date | None = None,
    log_path: Path | None = None,
    discovery_path: Path | None = None,
    on_challenge: Callable[[str, str, dict[str, Any], float], None] | None = None,
) -> tuple[list[LinkedInFeedPost], dict[str, int]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LinkedInFeedError("playwright is not installed") from exc

    opts = options or LinkedInFeedOptions()
    account_home = account_home_from_url(account_url)
    logger = JsonlLogger(log_path)
    session = SOCIAL_METRICS.ensure_cdp_profile(
        cdp_profile,
        cdp_port,
        account_home,
        start_minimized=opts.cdp_start_minimized,
        auto_close=False,
    )
    logger.write(
        "run_start",
        account=account,
        account_home=account_home,
        latest_mode=latest_mode,
        date_start=str(date_start or ""),
        date_end_exclusive=str(date_end_exclusive or ""),
        post_limit=opts.post_limit,
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(session.url, timeout=opts.timeout_ms)
        context = browser.contexts[0] if browser.contexts else browser.new_context(locale="en-US")
        page = select_initial_account_page(
            context,
            account_home,
            prefer_existing=bool(session.started_by_tool),
            timeout_ms=opts.timeout_ms,
        )
        page.set_default_timeout(opts.timeout_ms)
        page.set_default_navigation_timeout(opts.timeout_ms)
        try:
            feed_url = click_posts_tab_or_fallback(page, account_home, logger, opts, on_challenge=on_challenge)
            logger.write("feed_ready", url=feed_url)
            records, stats = collect_visible_feed_posts(
                page,
                account,
                account_home,
                logger,
                opts,
                latest_mode=latest_mode,
                date_start=date_start,
                date_end_exclusive=date_end_exclusive,
                discovery_path=discovery_path,
                on_challenge=on_challenge,
            )
            logger.write("run_done", records=len(records), stats=stats)
            return records, stats
        except LinkedInFeedError:
            raise
        except Exception as exc:
            raise LinkedInFeedError(str(exc)) from exc
        finally:
            try:
                page.close()
            except Exception:
                pass
            time.sleep(0.2)
