#!/usr/bin/env python3
"""Collect public metrics from social media links and write Markdown reports."""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


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

URL_RE = re.compile(r"https?://[^\s<>\")']+")
X_STATUS_RE = re.compile(r"https?://(?:www\.)?x\.com/((?:i/status|[^/\s<>\")']+/status)/(\d+))", re.I)
TIKTOK_VIDEO_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/@[^/\s<>\")']+/video/\d+", re.I)
TIKTOK_SHORT_RE = re.compile(r"https?://(?:vt\.tiktok\.com|www\.tiktok\.com/t)/[A-Za-z0-9_-]+/?", re.I)
INSTAGRAM_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+/?", re.I)
FACEBOOK_RE = re.compile(r"https?://(?:www\.)?facebook\.com/(?:share/r/[A-Za-z0-9_-]+|reel/\d+)/?", re.I)
FB_WATCH_RE = re.compile(r"https?://fb\.watch/[A-Za-z0-9_-]+/?", re.I)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CDP_PROFILE = Path(os.getenv("SOCIAL_CDP_PROFILE", str(PROJECT_ROOT / "assets" / "chrome-cdp-profile")))
DEFAULT_CDP_PORT = int(os.getenv("SOCIAL_CDP_PORT", "9222"))
CDP_PORT_SCAN_LIMIT = int(os.getenv("SOCIAL_CDP_PORT_SCAN_LIMIT", "20"))
CHROME_CDP_BROWSER_MARKERS = ("chrome", "chromium", "edge")
LOCAL_TIMEZONE = timezone(timedelta(hours=8))

METRIC_LABELS = {
    "comments": "评论",
    "likes": "点赞/反应",
    "shares": "分享/转发",
    "saves": "收藏",
    "views": "浏览/播放",
}

EXCEL_METRIC_COLUMNS = [
    ("likes", "点赞/反应"),
    ("comments", "评论"),
    ("shares", "分享/转发"),
    ("saves", "收藏"),
    ("views", "浏览/播放量"),
]
OWNER_HEADER_CANDIDATES = ("接龙人", "Owner", "owner")
OWNER_FALLBACK_COLUMN = 3
EXCEL_METRIC_FALLBACK_COLUMNS = {
    "likes": 14,
    "comments": 15,
    "shares": 16,
    "saves": 17,
    "views": 18,
}
PAGE_OPEN_FAILURE_MARKERS = (
    "net::err_",
    "timeout",
    "timed out",
    "err_connection",
    "err_timed_out",
    "err_name_not_resolved",
    "econn",
    "enotfound",
    "connection reset",
    "connection refused",
    "name resolution",
)
ANTI_BOT_URL_MARKERS = (
    "recaptcha",
    "captcha",
    "hcaptcha",
    "/checkpoint/",
    "/challenge/",
    "li.protechts.net",
    "cf-chl",
)
ANTI_BOT_TEXT_MARKERS = (
    "recaptcha",
    "captcha",
    "hcaptcha",
    "security check",
    "unusual activity",
    "automated requests",
    "automated access",
    "automated behavior",
    "verify you are human",
    "are you a robot",
    "browser challenge",
    "just a moment",
    "checking your browser",
    "complete the security check",
    "li.protechts.net",
    "checkpoint",
)

COUNT_RE = r"(?P<count>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb]|万|萬|億|亿)?)"
ZH_COUNTER_RE = r"(?:\s*(?:次|个|個|条|條))?"
METRIC_PATTERNS = {
    "comments": [
        rf"{COUNT_RE}{ZH_COUNTER_RE}\s*(?:comments?|replies|评论|評論|回复|回覆)\b",
        rf"(?:comments?|replies|评论|評論|回复|回覆)[:：]?\s*{COUNT_RE}",
    ],
    "likes": [
        rf"{COUNT_RE}{ZH_COUNTER_RE}\s*(?:likes?|reactions?|赞|讚|点赞|點讚|喜欢|喜歡)\b",
        rf"(?:likes?|reactions?|赞|讚|点赞|點讚|喜欢|喜歡)[:：]?\s*{COUNT_RE}",
    ],
    "shares": [
        rf"{COUNT_RE}{ZH_COUNTER_RE}\s*(?:shares?|reposts?|retweets?|分享|转发|轉發|转帖|轉帖|转贴|轉貼|转推|轉推)\b",
        rf"(?:shares?|reposts?|retweets?|分享|转发|轉發|转帖|轉帖|转贴|轉貼|转推|轉推)[:：]?\s*{COUNT_RE}",
    ],
    "saves": [
        rf"{COUNT_RE}{ZH_COUNTER_RE}\s*(?:saves?|saved|bookmarks?|收藏|书签|書籤)\b",
        rf"(?:saves?|saved|bookmarks?|收藏|书签|書籤)[:：]?\s*{COUNT_RE}",
    ],
    "views": [
        rf"{COUNT_RE}{ZH_COUNTER_RE}\s*(?:views?|plays?|impressions?|观看|觀看|查看|浏览量|瀏覽量|浏览|瀏覽|播放量|播放|展示次数|展示|曝光量|曝光|阅读量|閱讀量|阅读|閱讀)\b",
        rf"(?:views?|plays?|impressions?|观看|觀看|查看|浏览量|瀏覽量|浏览|瀏覽|播放量|播放|展示次数|展示|曝光量|曝光|阅读量|閱讀量|阅读|閱讀)[:：]?\s*{COUNT_RE}",
    ],
}


@dataclass
class MetricValue:
    raw: str | None = None
    value: int | None = None


@dataclass
class SocialResult:
    source_url: str
    platform: str
    final_url: str | None = None
    title: str | None = None
    author_name: str | None = None
    author_handle: str | None = None
    posted_at: str | None = None
    text: str | None = None
    media_type: str | None = None
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    error: str | None = None
    debug: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "platform": self.platform,
            "final_url": self.final_url,
            "title": self.title,
            "author_name": self.author_name,
            "author_handle": self.author_handle,
            "posted_at": self.posted_at,
            "text": self.text,
            "media_type": self.media_type,
            "metrics": {key: value.__dict__ for key, value in self.metrics.items()},
            "error": self.error,
            "debug": self.debug,
        }


@dataclass
class CdpProfileSession:
    url: str
    process: subprocess.Popen[Any] | None = None
    started_by_tool: bool = False
    auto_close: bool = False
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.auto_close or not self.started_by_tool or self.process is None:
            return
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                self.process.kill()


def compact_spaces(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def markdown_escape_cell(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("|", "\\|").replace("\n", "<br>")


def markdown_quote(value: str | None) -> str:
    if not value:
        return "_未读取到正文_"
    return "\n".join(f"> {line}" if line else ">" for line in value.splitlines())


def parse_count_token(token: str | None) -> int | None:
    if not token:
        return None
    clean = (
        token.strip()
        .replace(",", "")
        .replace("\u202f", "")
        .replace("\xa0", "")
        .replace(" ", "")
    )
    match = re.search(r"(?P<num>\d+(?:\.\d+)?)(?P<suffix>[KkMmBb万萬億亿]?)", clean)
    if not match:
        return None
    number = float(match.group("num"))
    multipliers = {
        "": 1,
        "K": 1_000,
        "k": 1_000,
        "M": 1_000_000,
        "m": 1_000_000,
        "B": 1_000_000_000,
        "b": 1_000_000_000,
        "万": 10_000,
        "萬": 10_000,
        "億": 100_000_000,
        "亿": 100_000_000,
    }
    return int(round(number * multipliers.get(match.group("suffix"), 1)))


def instagram_url_kind(url: str | None) -> str:
    path = urlparse(str(url or "")).path.lower()
    if re.search(r"/p/[^/?#]+", path):
        return "post"
    if re.search(r"/(?:reel|reels)/[^/?#]+", path):
        return "reel"
    return "unknown"


def facebook_url_kind(url: str | None) -> str:
    path = urlparse(str(url or "")).path.lower()
    if re.search(r"/reel/\d+", path):
        return "reel"
    if re.search(r"/(?:posts|photo|photos|share/v|videos)/", path):
        return "post"
    return "unknown"


def social_url_kind(platform: str, url: str | None) -> str:
    if platform == "instagram":
        return instagram_url_kind(url)
    if platform == "facebook":
        return facebook_url_kind(url)
    return "unknown"


def format_metric(metric: MetricValue | None) -> str:
    if not metric or metric.value is None:
        return "未知"
    return f"{metric.value:,}"


def extract_metric(metric: str, sources: list[str]) -> MetricValue:
    zero_labels = {
        "comments": {"reply", "comment", "replies", "comments", "回复", "回覆", "评论", "評論"},
        "shares": {"repost", "retweet", "share", "转发", "轉發", "转帖", "轉帖", "转贴", "轉貼", "转推", "轉推", "分享"},
        "likes": {"like", "喜欢", "喜歡", "点赞", "點讚", "赞", "讚"},
        "saves": {"bookmark", "save", "书签", "書籤", "收藏"},
    }.get(metric, set())

    for source in sources:
        text = compact_spaces(source)
        if not text:
            continue
        for pattern in METRIC_PATTERNS[metric]:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return MetricValue(raw=text[:500], value=parse_count_token(match.group("count")))
    if any(compact_spaces(source).lower() in zero_labels for source in sources):
        return MetricValue(raw="0", value=0)
    return MetricValue()


def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in {"x.com", "twitter.com"}:
        return "x"
    if host.endswith("facebook.com") or host == "fb.watch":
        return "facebook"
    if host.endswith("tiktok.com") or host in {"vt.tiktok.com", "vm.tiktok.com"}:
        return "tiktok"
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith("linkedin.com"):
        return "linkedin"
    return "unknown"


def is_instagram_post_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host.endswith("instagram.com") and re.search(r"/p/[^/?#]+", parsed.path) is not None


def normalize_scrape_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith("instagram.com"):
        return re.sub(r"/p/([^/?#]+)", r"/reel/\1", url, count=1)
    return url


def tweet_id_from_url(url: str) -> str | None:
    match = re.search(r"/status(?:es)?/(\d+)", url)
    return match.group(1) if match else None


def collect_urls(values: list[str], input_files: list[Path], *, keep_duplicates: bool = False) -> list[str]:
    urls: list[str] = []
    for value in values:
        urls.extend(extract_urls_from_text(value))
    for input_file in input_files:
        text = input_file.read_text(encoding="utf-8-sig")
        urls.extend(extract_urls_from_text(text))

    cleaned = [url.rstrip(".,;)") for url in urls]
    if keep_duplicates:
        return cleaned
    unique: list[str] = []
    seen: set[str] = set()
    for clean in cleaned:
        if clean not in seen:
            unique.append(clean)
            seen.add(clean)
    return unique


def extract_urls_from_text(text: str) -> list[str]:
    urls: list[tuple[int, int, str]] = []
    specific_starts: set[int] = set()
    platform_patterns = [
        (X_STATUS_RE, lambda match: "https://x.com/" + match.group(1)),
        (TIKTOK_VIDEO_RE, lambda match: match.group(0)),
        (TIKTOK_SHORT_RE, lambda match: match.group(0)),
        (INSTAGRAM_RE, lambda match: match.group(0)),
        (FACEBOOK_RE, lambda match: match.group(0)),
        (FB_WATCH_RE, lambda match: match.group(0)),
    ]
    for pattern, build_url in platform_patterns:
        for match in pattern.finditer(text):
            specific_starts.add(match.start())
            urls.append((match.start(), 0, build_url(match)))
    for match in URL_RE.finditer(text):
        if match.start() in specific_starts:
            continue
        urls.append((match.start(), 1, match.group(0)))
    return [url for _, _, url in sorted(urls, key=lambda item: (item[0], item[1]))]


def default_documents_dir() -> Path:
    if sys.platform == "win32":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Personal")
            return Path(os.path.expandvars(value)).expanduser()
        except Exception:
            pass
    return Path.home() / "Documents"


def dated_default_output_dir() -> Path:
    return default_documents_dir() / datetime.now().strftime("%Y-%m-%d")


def filename_part(value: str | None, fallback: str = "social") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("._-")
    return cleaned or fallback


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def default_output_path(urls: list[str]) -> Path:
    output_dir = dated_default_output_dir()
    if len(urls) == 1:
        platform = detect_platform(urls[0])
        parsed = urlparse(urls[0])
        raw_id = parsed.path.strip("/").split("/")[-1] or parsed.netloc
        stem = f"social_public_metrics_{filename_part(platform)}_{filename_part(raw_id)}"
    else:
        platforms = "-".join(sorted({detect_platform(url) for url in urls}))
        stem = f"social_public_metrics_batch_{len(urls)}_{filename_part(platforms)}"
    return unique_path(output_dir / f"{stem}.md")


def default_text_output_path(markdown_output: Path) -> Path:
    return markdown_output.with_name(f"{markdown_output.stem}_text.md")


def default_json_output_path(markdown_output: Path) -> Path:
    return markdown_output.with_suffix(".json")


def reel_id_from_url(platform: str, url: str | None) -> str | None:
    if not url:
        return None
    if platform == "facebook":
        match = re.search(r"/reel/(\d+)", url)
        return match.group(1) if match else None
    if platform == "instagram":
        match = re.search(r"/(?:p|reel|reels)/([^/?#]+)", url)
        return match.group(1) if match else None
    return None


def is_reel_grid_candidate_url(platform: str, url: str | None) -> bool:
    if not url:
        return False
    if platform == "facebook":
        return re.search(r"/reel/\d+", url) is not None
    if platform == "instagram":
        return re.search(r"/(?:reel|reels)/[^/?#]+", url) is not None
    return False


def first_known_url(*urls: str | None) -> str | None:
    for url in urls:
        if url and url.startswith("http"):
            return url
    return None


def http_fetch(url: str, timeout_s: float = 20.0) -> tuple[str, str]:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        },
    )
    with urlopen(request, timeout=timeout_s) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read()
        return response.geturl(), raw.decode(charset, errors="replace")


def parse_tag_attrs(tag: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in re.finditer(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(['\"])(.*?)\2", tag, re.DOTALL):
        attrs[match.group(1).lower()] = html.unescape(match.group(3))
    return attrs


def extract_meta(html_text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for tag in re.findall(r"<meta\b[^>]*>", html_text, flags=re.IGNORECASE):
        attrs = parse_tag_attrs(tag)
        key = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        content = attrs.get("content")
        if key and content and key.lower() not in meta:
            meta[key.lower()] = content
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        meta.setdefault("html:title", compact_spaces(title_match.group(1)))
    canonical_match = re.search(
        r"<link\b[^>]*rel\s*=\s*['\"]canonical['\"][^>]*>",
        html_text,
        flags=re.IGNORECASE,
    )
    if canonical_match:
        attrs = parse_tag_attrs(canonical_match.group(0))
        if attrs.get("href"):
            meta["canonical"] = attrs["href"]
    return meta


def extract_json_scripts(html_text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for match in re.finditer(
        r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        attrs = parse_tag_attrs(match.group("attrs"))
        body = html.unescape(match.group("body")).strip()
        if not body:
            continue
        key = attrs.get("id") or attrs.get("type") or f"script_{len(data)}"
        if attrs.get("type") == "application/ld+json" or key in {
            "SIGI_STATE",
            "__UNIVERSAL_DATA_FOR_REHYDRATION__",
            "__NEXT_DATA__",
        }:
            try:
                data[key] = json.loads(body)
            except Exception:
                data[key] = body[:2000]
    return data


def values_from_nested(data: Any, target_keys: set[str]) -> list[Any]:
    found: list[Any] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in target_keys:
                    found.append(value)
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return found


def first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and compact_spaces(value):
            return compact_spaces(value)
    return None


def format_timestamp(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = int(value)
        if number > 10_000_000_000:
            number = number // 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return str(value)
    if isinstance(value, str) and compact_spaces(value):
        return compact_spaces(value)
    return None


MONTH_INDEX = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def iso_local_datetime(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> str | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=LOCAL_TIMEZONE).isoformat()
    except ValueError:
        return None


def infer_year_for_month_day(month: int, day: int, now: datetime) -> int:
    year = now.astimezone(LOCAL_TIMEZONE).year
    try:
        candidate = datetime(year, month, day, tzinfo=LOCAL_TIMEZONE)
    except ValueError:
        return year
    # Social sites often omit the year for recent posts. If the date would be
    # far in the future, it belongs to the previous year.
    if candidate.date() > now.astimezone(LOCAL_TIMEZONE).date() + timedelta(days=7):
        return year - 1
    return year


def parse_social_datetime_text(text: str | None, *, now: datetime | None = None) -> str | None:
    value = compact_spaces(text)
    if not value:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TIMEZONE)

    english_month = "|".join(sorted(MONTH_INDEX, key=len, reverse=True))
    match = re.search(
        rf"\b(?P<month>{english_month})\s+(?P<day>\d{{1,2}})(?:,\s*(?P<year>\d{{4}}))?"
        rf"(?:\s+(?:at\s+)?(?P<hour>\d{{1,2}}):(?P<minute>\d{{2}})\s*(?P<ampm>AM|PM|am|pm)?)?",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        month = MONTH_INDEX[match.group("month").lower()]
        day = int(match.group("day"))
        year = int(match.group("year") or infer_year_for_month_day(month, day, now))
        hour = int(match.group("hour") or 0)
        minute = int(match.group("minute") or 0)
        ampm = (match.group("ampm") or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        parsed = iso_local_datetime(year, month, day, hour, minute)
        if parsed:
            return parsed

    match = re.search(
        r"(?:(?P<year>\d{4})\s*年\s*)?(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"
        r"(?:\s*(?P<hour>\d{1,2})[:：](?P<minute>\d{2}))?",
        value,
    )
    if match:
        month = int(match.group("month"))
        day = int(match.group("day"))
        year = int(match.group("year") or infer_year_for_month_day(month, day, now))
        parsed = iso_local_datetime(year, month, day, int(match.group("hour") or 0), int(match.group("minute") or 0))
        if parsed:
            return parsed

    relative_patterns = [
        (r"(?P<count>\d+)\s*(?:秒|秒钟)\s*前", "seconds"),
        (r"(?P<count>\d+)\s*(?:分钟|分)\s*前", "minutes"),
        (r"(?P<count>\d+)\s*(?:小时|小時)\s*前", "hours"),
        (r"(?P<count>\d+)\s*(?:天|日)\s*前", "days"),
        (r"(?P<count>\d+)\s*(?:周|星期|週)\s*前?", "weeks"),
        (r"(?P<count>\d+)\s*(?:months?|mos?)\s+ago", "months"),
        (r"(?P<count>\d+)\s*(?:weeks?|w)\s+ago", "weeks"),
        (r"(?P<count>\d+)\s*(?:days?|d)\s+ago", "days"),
        (r"(?P<count>\d+)\s*(?:hours?|hrs?|h)\s+ago", "hours"),
        (r"(?P<count>\d+)\s*(?:minutes?|mins?|m)\s+ago", "minutes"),
    ]
    for pattern, unit in relative_patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        count = int(match.group("count"))
        if unit == "months":
            delta = timedelta(days=count * 30)
        elif unit == "weeks":
            delta = timedelta(weeks=count)
        elif unit == "days":
            delta = timedelta(days=count)
        elif unit == "hours":
            delta = timedelta(hours=count)
        elif unit == "minutes":
            delta = timedelta(minutes=count)
        else:
            delta = timedelta(seconds=count)
        return (now - delta).isoformat()

    return None


def first_social_datetime(*values: Any) -> str | None:
    for value in values:
        parsed = parse_social_datetime_text(str(value or ""))
        if parsed:
            return parsed
    return None


def metric_from_number(value: Any) -> MetricValue:
    if isinstance(value, bool) or value is None:
        return MetricValue()
    if isinstance(value, (int, float)):
        return MetricValue(raw=str(value), value=int(value))
    if isinstance(value, str):
        return MetricValue(raw=value, value=parse_count_token(value))
    return MetricValue()


def set_metric_if_known(metrics: dict[str, MetricValue], key: str, value: Any) -> None:
    metric = metric_from_number(value)
    if metric.value is not None:
        metrics[key] = metric


def metric_value_from_pattern(text: str | None, pattern: str) -> MetricValue:
    value = compact_spaces(text)
    if not value:
        return MetricValue()
    match = re.search(pattern, value, flags=re.IGNORECASE)
    if not match:
        return MetricValue()
    raw = match.group("count")
    return MetricValue(raw=raw, value=parse_count_token(raw))


def apply_platform_metric_fallbacks(platform: str, metrics: dict[str, MetricValue], page_data: dict[str, Any]) -> None:
    text = "\n".join(
        compact_spaces(str(value or ""))
        for value in [
            page_data.get("generic_article_text"),
            page_data.get("body_text"),
            (page_data.get("meta") or {}).get("og:description"),
            (page_data.get("meta") or {}).get("description"),
        ]
        if value
    )
    if platform == "facebook":
        summary_match = re.search(
            r"(?:所有心情|all reactions?)[:：]?\s*(?P<likes>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb])?)\s+"
            r"(?P<comments>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb])?)\s*(?:条|則)?\s*(?:评论|評論|comments?)\s+"
            r"(?P<shares>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb])?)\s*(?:次|則)?\s*(?:分享|shares?)",
            text,
            flags=re.IGNORECASE,
        )
        if summary_match:
            for key in ("likes", "comments", "shares"):
                parsed = parse_count_token(summary_match.group(key))
                if parsed is not None:
                    metrics[key] = MetricValue(raw=summary_match.group(key), value=parsed)
            return
        reactions = metric_value_from_pattern(text, r"(?:所有心情|all reactions?)[:：]?\s*(?P<count>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb])?)")
        if reactions.value is not None:
            metrics["likes"] = reactions
    elif platform == "linkedin":
        match = re.search(
            r"(?P<likes>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb])?)\s+"
            r"(?:(?P<comments>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb])?)\s*(?:条|則)?\s*(?:评论|評論|comments?)\s+)?"
            r"(?:(?P<shares>[\d,.]+(?:\.\d+)?\s*(?:[KkMmBb])?)\s*(?:次|則)?\s*(?:转发|轉發|reposts?|shares?))",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            likes = parse_count_token(match.group("likes"))
            comments = parse_count_token(match.group("comments"))
            shares = parse_count_token(match.group("shares"))
            if likes is not None:
                metrics["likes"] = MetricValue(raw=match.group("likes"), value=likes)
            if comments is not None:
                metrics["comments"] = MetricValue(raw=match.group("comments"), value=comments)
            if shares is not None:
                metrics["shares"] = MetricValue(raw=match.group("shares"), value=shares)


def clear_ambiguous_detail_views(platform: str, metrics: dict[str, MetricValue]) -> None:
    if platform not in {"instagram", "facebook"}:
        return
    metric = metrics.get("views")
    if not metric or metric.value is None:
        return
    raw = compact_spaces(metric.raw)
    reliable_words = (
        "profile reels grid",
        "views",
        "plays",
        "impressions",
        "观看",
        "觀看",
        "播放",
        "播放量",
        "浏览量",
        "瀏覽量",
        "曝光",
    )
    if any(word in raw.lower() for word in reliable_words[:4]) or any(word in raw for word in reliable_words[4:]):
        return
    metrics["views"] = MetricValue()


def extract_quoted_caption(description: str | None) -> str | None:
    if not description:
        return None
    text = html.unescape(description)
    quoted = re.findall(r"[\"“](.+?)[\"”]", text, flags=re.DOTALL)
    if quoted:
        return compact_spaces(quoted[-1])
    if ":" in text:
        return compact_spaces(text.split(":", 1)[1])
    return compact_spaces(text)


def instagram_handle_from_description(description: str | None) -> str | None:
    if not description:
        return None
    text = html.unescape(description)
    match = re.search(
        r"\s-\s(?P<handle>[A-Za-z0-9._]{2,30})(?=\s+on\s+|\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|[，,?])",
        text,
    )
    if match:
        return f"@{match.group('handle')}"
    return None


def instagram_caption_datetime(description: str | None) -> str | None:
    if not description:
        return None
    text = html.unescape(description)
    match = re.search(
        r"\s-\s+[A-Za-z0-9._]{2,30}\s*[，,]\s*(?P<date>[A-Za-z]+\s+\d{1,2},\s*\d{4})",
        text,
    )
    if match:
        return parse_social_datetime_text(match.group("date"))
    return parse_social_datetime_text(text)


def looks_like_instagram_handle(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"@?[A-Za-z0-9._]+", value.strip()))


def parse_tiktok_json(json_data: dict[str, Any]) -> dict[str, Any]:
    item_modules = values_from_nested(json_data, {"ItemModule", "itemStruct"})
    items: list[dict[str, Any]] = []
    for item_module in item_modules:
        if isinstance(item_module, dict):
            if "desc" in item_module:
                items.append(item_module)
            else:
                items.extend(value for value in item_module.values() if isinstance(value, dict))
    for item in items:
        if not isinstance(item, dict):
            continue
        stats = item.get("stats") or item.get("statsV2") or {}
        author = item.get("author") or {}
        return {
            "text": item.get("desc"),
            "author_name": author.get("nickname") if isinstance(author, dict) else None,
            "author_handle": f"@{author.get('uniqueId')}" if isinstance(author, dict) and author.get("uniqueId") else None,
            "posted_at": item.get("createTime"),
            "metrics": {
                "likes": stats.get("diggCount"),
                "comments": stats.get("commentCount"),
                "shares": stats.get("shareCount"),
                "saves": stats.get("collectCount"),
                "views": stats.get("playCount"),
            }
            if isinstance(stats, dict)
            else {},
        }
    return {}


def parse_json_ld(json_data: dict[str, Any]) -> dict[str, Any]:
    scripts = [value for key, value in json_data.items() if key == "application/ld+json"]
    if not scripts:
        scripts = values_from_nested(json_data, {"@type"})
    nodes: list[Any] = []
    for script in scripts:
        if isinstance(script, list):
            nodes.extend(script)
        else:
            nodes.append(script)
    for node in nodes:
        if not isinstance(node, dict):
            continue
        stats = {}
        for interaction in node.get("interactionStatistic", []) or []:
            if not isinstance(interaction, dict):
                continue
            interaction_type = str(interaction.get("interactionType", "")).lower()
            count = interaction.get("userInteractionCount")
            if "comment" in interaction_type:
                stats["comments"] = count
            elif "like" in interaction_type:
                stats["likes"] = count
            elif "share" in interaction_type:
                stats["shares"] = count
            elif "watch" in interaction_type or "view" in interaction_type:
                stats["views"] = count
        return {
            "title": node.get("name") or node.get("headline"),
            "text": node.get("description") or node.get("caption"),
            "author_name": node.get("author", {}).get("name") if isinstance(node.get("author"), dict) else None,
            "posted_at": node.get("uploadDate") or node.get("datePublished"),
            "metrics": stats,
        }
    return {}


def extract_x_from_page(page_data: dict[str, Any]) -> dict[str, Any]:
    article = page_data.get("x_article") or {}
    meta = page_data.get("meta") or {}
    description = first_string(meta.get("og:description"), meta.get("description"))
    text = first_string(article.get("text"), extract_quoted_caption(description), description)
    author_handle = article.get("handle")
    title = first_string(meta.get("og:title"), page_data.get("title"))
    metrics = {
        "comments": extract_metric("comments", [article.get("reply_label", ""), page_data.get("body_text", "")]),
        "shares": extract_metric("shares", [article.get("retweet_label", ""), page_data.get("body_text", "")]),
        "likes": extract_metric("likes", [article.get("like_label", ""), page_data.get("body_text", "")]),
        "saves": extract_metric("saves", [article.get("bookmark_label", ""), page_data.get("body_text", "")]),
        "views": extract_metric("views", [article.get("analytics_text", ""), page_data.get("body_text", "")]),
    }
    return {
        "title": title,
        "text": text,
        "author_handle": author_handle,
        "posted_at": article.get("time"),
        "metrics": metrics,
    }


def x_has_text_and_time(result: SocialResult) -> bool:
    return bool(compact_spaces(result.text) and result.posted_at)


def default_missing_x_metrics_to_zero(result: SocialResult) -> None:
    if result.platform != "x" or not x_has_text_and_time(result):
        return
    defaulted: list[str] = []
    for key in METRIC_LABELS:
        metric = result.metrics.get(key)
        if metric is None or metric.value is None:
            result.metrics[key] = MetricValue(raw="default_zero_when_hidden", value=0)
            defaulted.append(key)
    if defaulted:
        result.debug["x_defaulted_zero_metrics"] = defaulted


def build_result_from_data(url: str, platform: str, page_data: dict[str, Any]) -> SocialResult:
    meta = page_data.get("meta") or {}
    json_scripts = page_data.get("json_scripts") or {}
    json_ld = parse_json_ld(json_scripts)
    tiktok_data = parse_tiktok_json(json_scripts) if platform == "tiktok" else {}
    page_kind = social_url_kind(platform, page_data.get("final_url") or url)
    if page_kind == "unknown":
        page_kind = social_url_kind(platform, url)

    sources = [
        meta.get("og:description", ""),
        meta.get("description", ""),
        meta.get("twitter:description", ""),
        meta.get("html:title", ""),
        page_data.get("body_text", ""),
        json.dumps(json_ld, ensure_ascii=False),
        json.dumps(tiktok_data, ensure_ascii=False),
        page_data.get("generic_article_text", ""),
    ]
    title = first_string(
        tiktok_data.get("title"),
        json_ld.get("title"),
        meta.get("og:title"),
        meta.get("twitter:title"),
        meta.get("html:title"),
        page_data.get("title"),
    )
    text = first_string(
        tiktok_data.get("text"),
        json_ld.get("text"),
        extract_quoted_caption(meta.get("og:description")),
        meta.get("description"),
        title,
    )
    author_name = first_string(
        tiktok_data.get("author_name"),
        json_ld.get("author_name"),
        meta.get("author"),
    )
    author_handle = first_string(tiktok_data.get("author_handle"))
    posted_at = format_timestamp(tiktok_data.get("posted_at")) or format_timestamp(json_ld.get("posted_at"))
    posted_at = posted_at or first_string(page_data.get("generic_time"))
    posted_at = posted_at or first_social_datetime(
        meta.get("article:published_time"),
        meta.get("og:description"),
        meta.get("description"),
        meta.get("twitter:description"),
        page_data.get("generic_article_text"),
    )
    metrics = {key: extract_metric(key, sources) for key in METRIC_LABELS}

    for key, value in (json_ld.get("metrics") or {}).items():
        set_metric_if_known(metrics, key, value)
    for key, value in (tiktok_data.get("metrics") or {}).items():
        set_metric_if_known(metrics, key, value)
    apply_platform_metric_fallbacks(platform, metrics, page_data)
    clear_ambiguous_detail_views(platform, metrics)

    if platform == "x":
        x_data = extract_x_from_page(page_data)
        title = first_string(x_data.get("title"), title)
        text = first_string(x_data.get("text"), text)
        author_handle = first_string(x_data.get("author_handle"), author_handle)
        posted_at = first_string(x_data.get("posted_at"), posted_at)
        for key, value in x_data.get("metrics", {}).items():
            if isinstance(value, MetricValue) and value.value is not None:
                metrics[key] = value
    elif platform == "facebook" and title:
        if " | " in title:
            title_text, title_author = title.rsplit(" | ", 1)
            author_name = first_string(author_name, title_author)
            if title_text and (not text or len(title_text) > len(text) or text.endswith("...")):
                text = compact_spaces(title_text)
    elif platform == "instagram" and title:
        posted_at = posted_at or instagram_caption_datetime(meta.get("og:description"))
        posted_at = posted_at or instagram_caption_datetime(meta.get("description"))
        posted_at = posted_at or instagram_caption_datetime(meta.get("twitter:description"))
        author_handle = first_string(
            author_handle,
            instagram_handle_from_description(meta.get("og:description")),
            instagram_handle_from_description(meta.get("description")),
            instagram_handle_from_description(meta.get("twitter:description")),
            instagram_handle_from_description(page_data.get("body_text")),
        )
        match = re.search(r"^(.+?)\s+on Instagram:\s*[\"“](.*?)[\"”]\s*$", title, flags=re.DOTALL)
        if match:
            title_author = match.group(1).strip()
            if looks_like_instagram_handle(title_author):
                author_handle = first_string(author_handle, f"@{title_author.lstrip('@')}")
            else:
                author_name = first_string(author_name, title_author)
            title_caption = compact_spaces(match.group(2))
            if title_caption and (not text or len(title_caption) >= len(text)):
                text = title_caption
        author_handle = first_string(
            author_handle,
            *(instagram_handle_from_description(metric.raw) for metric in metrics.values() if metric.raw),
        )
    elif platform == "linkedin":
        text = first_string(page_data.get("generic_article_text"), text)
        posted_at = posted_at or first_social_datetime(text)

    if platform in {"instagram", "facebook"}:
        media_type = "reel/video" if page_kind == "reel" else "post"
    else:
        media_type = "reel/video" if platform == "tiktok" else "post"

    result = SocialResult(
        source_url=url,
        platform=platform,
        final_url=page_data.get("final_url") or meta.get("canonical") or url,
        title=title,
        author_name=author_name,
        author_handle=author_handle,
        posted_at=posted_at,
        text=text,
        media_type=media_type,
        metrics=metrics,
        debug={
            "meta_keys": sorted(meta.keys()),
            "json_script_keys": sorted(json_scripts.keys()),
            "page_kind": page_kind,
            "source": page_data.get("source"),
        },
    )
    default_missing_x_metrics_to_zero(result)
    return result


PLAYWRIGHT_EXTRACT_SCRIPT = r"""
() => {
  const attrs = (node) => {
    const out = {};
    for (const attr of Array.from(node.attributes || [])) out[attr.name.toLowerCase()] = attr.value;
    return out;
  };
  const meta = {};
  for (const node of Array.from(document.querySelectorAll('meta'))) {
    const a = attrs(node);
    const key = (a.property || a.name || a.itemprop || '').toLowerCase();
    if (key && a.content && !meta[key]) meta[key] = a.content;
  }
  const canonical = document.querySelector('link[rel="canonical"]');
  if (canonical?.href) meta.canonical = canonical.href;
  const jsonScripts = {};
  for (const node of Array.from(document.querySelectorAll('script'))) {
    const id = node.id || node.type || `script_${Object.keys(jsonScripts).length}`;
    const body = (node.textContent || '').trim();
    if (!body) continue;
    if (node.type === 'application/ld+json' || ['SIGI_STATE', '__UNIVERSAL_DATA_FOR_REHYDRATION__', '__NEXT_DATA__'].includes(id)) {
      try { jsonScripts[id] = JSON.parse(body); } catch { jsonScripts[id] = body.slice(0, 2000); }
    }
  }
  let xArticle = null;
  const article = document.querySelector('article[data-testid="tweet"]');
  if (article) {
    const text = Array.from(article.querySelectorAll('[data-testid="tweetText"]')).map(n => n.innerText).join('\n\n').trim();
    const getLabel = (id) => article.querySelector(`[data-testid="${id}"]`)?.getAttribute('aria-label') || '';
    const time = article.querySelector('time')?.getAttribute('datetime') || '';
    const statusLink = Array.from(article.querySelectorAll('a[href]')).map(a => a.href).find(h => /\/status\/\d+/.test(h));
    const handleMatch = statusLink?.match(/(?:x|twitter)\.com\/([^/]+)\/status\/(\d+)/);
    const analytics = Array.from(article.querySelectorAll('a[href*="/analytics"], span, div'))
      .map(n => n.innerText || n.getAttribute('aria-label') || '')
      .find(t => /\bViews?\b|查看|浏览/.test(t)) || '';
      xArticle = {
      text,
      time,
      handle: handleMatch ? `@${handleMatch[1]}` : '',
      reply_label: getLabel('reply'),
      retweet_label: getLabel('retweet') || getLabel('unretweet'),
      like_label: getLabel('like') || getLabel('unlike'),
      bookmark_label: getLabel('bookmark') || getLabel('removeBookmark'),
      analytics_text: analytics,
    };
  }
  const instagramReservedPaths = new Set([
    'accounts', 'about', 'api', 'developer', 'direct', 'explore', 'legal',
    'p', 'reel', 'reels', 'stories', 'terms', 'web',
  ]);
  const instagramProfileHandle = (href) => {
    try {
      const url = new URL(href);
      const host = url.hostname.replace(/^www\./, '').toLowerCase();
      if (!host.endsWith('instagram.com')) return '';
      const parts = url.pathname.split('/').filter(Boolean);
      if (parts.length !== 1) return '';
      const handle = parts[0];
      if (instagramReservedPaths.has(handle.toLowerCase())) return '';
      if (!/^[A-Za-z0-9._]{2,30}$/.test(handle)) return '';
      return handle;
    } catch {
      return '';
    }
  };
  const reelOwnerLinks = Array.from(document.querySelectorAll('a[href]')).map(a => {
    const href = a.href;
    const text = (a.innerText || '').replace(/\s+/g, ' ').trim();
    const label = (a.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    const title = (a.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
    const handle = instagramProfileHandle(href);
    const haystack = `${href} ${text} ${label} ${title}`.toLowerCase();
    const inArticle = Boolean(a.closest('article'));
    const inMain = Boolean(a.closest('main'));
    const imageAlt = Array.from(a.querySelectorAll('img'))
      .map(img => img.getAttribute('alt') || '')
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();
    let score = 0;
    if (inArticle) score += 100;
    if (inMain) score += 40;
    if (handle) score += 30;
    if (handle && [text, label, title].some(value => value === handle || value === `@${handle}`)) score += 40;
    if (handle && imageAlt && imageAlt.toLowerCase().includes(handle.toLowerCase())) score += 80;
    if (haystack.includes('owner profile')) score += 80;
    if (haystack.includes('profile.php') || haystack.includes('sk=reels_tab')) score += 80;
    if (haystack.includes('/reels/')) score += 50;
    if (/^(profile|个人主页|主页)$/i.test(text) || /^(profile|个人主页|主页)$/i.test(label)) score -= 80;
    const rect = a.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) score += 5;
    return {
      href,
      text,
      label,
      title,
      image_alt: imageAlt,
      instagram_handle: handle,
      score,
      in_article: inArticle,
      top: Math.round(rect.top),
    };
  }).filter(link => {
    const haystack = `${link.href} ${link.text} ${link.label} ${link.title}`.toLowerCase();
    return link.instagram_handle
      || haystack.includes('owner profile')
      || haystack.includes('profile.php')
      || haystack.includes('sk=reels_tab')
      || haystack.includes('/reels/');
  }).sort((a, b) => b.score - a.score || a.top - b.top);
  const genericRoot = document.querySelector('article')
    || document.querySelector('[data-urn*="activity"]')
    || document.querySelector('main')
    || document.body;
  const genericTimeNode = genericRoot?.querySelector('time[datetime]')
    || document.querySelector('time[datetime]');
  const genericArticleText = (genericRoot?.innerText || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 5000);
  const iframeSrcs = Array.from(document.querySelectorAll('iframe[src]'))
    .map(node => node.src || '')
    .filter(Boolean)
    .slice(0, 30);
  return {
    final_url: location.href,
    title: document.title || '',
    body_text: document.body?.innerText?.slice(0, 25000) || '',
    iframe_srcs: iframeSrcs,
    meta,
    json_scripts: jsonScripts,
    x_article: xArticle,
    reel_owner_links: reelOwnerLinks,
    generic_time: genericTimeNode?.getAttribute('datetime') || '',
    generic_article_text: genericArticleText,
    source: 'playwright',
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
      try {
        node.click();
        clicked += 1;
        if (clicked >= 3) return clicked;
      } catch {}
    }
  }
  for (const node of Array.from(document.querySelectorAll('button, [role="button"]'))) {
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const text = [
      node.innerText || '',
      node.getAttribute('aria-label') || '',
      node.getAttribute('title') || '',
    ].join(' ').replace(/\s+/g, ' ').trim();
    if (!words.test(text)) continue;
    try {
      node.click();
      clicked += 1;
      if (clicked >= 3) break;
    } catch {}
  }
  return clicked;
}
"""


X_EXTRACT_TWEET_SCRIPT = r"""
(tweetId) => {
  const articles = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
  const scoreArticle = (article) => {
    const html = article.innerHTML || '';
    let score = 0;
    if (tweetId && html.includes(`/status/${tweetId}`)) score += 20;
    if (article.querySelector('time')) score += 5;
    if (article.querySelector('[data-testid="tweetText"]')) score += 5;
    if (article.querySelector('[data-testid="reply"]')) score += 3;
    return score;
  };
  const ranked = articles
    .map((article, index) => ({ article, index, score: scoreArticle(article) }))
    .sort((a, b) => b.score - a.score || a.index - b.index);
  const target = ranked[0]?.article || null;
  if (!target) {
    return {
      found: false,
      pageText: document.body?.innerText?.slice(0, 3000) || '',
      title: document.title || '',
    };
  }

  const textNodes = Array.from(target.querySelectorAll('[data-testid="tweetText"]'));
  const tweetText = textNodes.map((node) => node.innerText).join('\n\n').trim();
  const labels = Array.from(target.querySelectorAll('[aria-label]'))
    .map((node) => node.getAttribute('aria-label') || '')
    .filter(Boolean);
  const byTestId = {};
  for (const id of ['reply', 'retweet', 'unretweet', 'like', 'unlike', 'bookmark', 'removeBookmark']) {
    const node = target.querySelector(`[data-testid="${id}"]`);
    byTestId[id] = node ? (node.getAttribute('aria-label') || node.innerText || '') : '';
  }

  const links = Array.from(target.querySelectorAll('a[href]')).map((node) => {
    const href = node.href || '';
    return {
      href,
      text: node.innerText || '',
      label: node.getAttribute('aria-label') || '',
    };
  });
  const isExactStatusLink = (href) => {
    const match = href.match(/(?:x|twitter)\.com\/([^/]+)\/status\/(\d+)(?:[?#]|$)/);
    return Boolean(match && (!tweetId || match[2] === tweetId));
  };
  const statusLink = links.find((link) => isExactStatusLink(link.href))
    || links.find((link) => /\/status\/\d+/.test(link.href));
  const analyticsLink = links.find((link) => /\/analytics(?:\?|$)/.test(link.href));
  const handleMatch = statusLink?.href?.match(/x\.com\/([^/]+)\/status\/(\d+)/)
    || statusLink?.href?.match(/twitter\.com\/([^/]+)\/status\/(\d+)/);
  const handle = handleMatch ? `@${handleMatch[1]}` : null;
  const idFromLink = handleMatch ? handleMatch[2] : null;
  const canonicalStatusUrl = handleMatch ? `https://x.com/${handleMatch[1]}/status/${handleMatch[2]}` : null;
  const timeNode = target.querySelector('time');
  const timeParent = timeNode?.closest('a') || null;

  const textLines = (target.innerText || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  let authorName = null;
  if (handle) {
    const handleIndex = textLines.findIndex((line) => line === handle || line.startsWith(`${handle}\n`));
    if (handleIndex > 0) authorName = textLines[handleIndex - 1];
  }
  if (!authorName && textLines.length > 0) authorName = textLines[0];

  return {
    found: true,
    articleIndex: ranked[0]?.index ?? 0,
    pageTitle: document.title || '',
    articleText: target.innerText || '',
    tweetText,
    authorName,
    authorHandle: handle,
    tweetId: idFromLink || tweetId || null,
    postedAt: timeNode?.getAttribute('datetime') || null,
    finalUrl: window.location.href,
    statusUrl: canonicalStatusUrl || statusLink?.href || timeParent?.href || null,
    analyticsText: analyticsLink ? (analyticsLink.label || analyticsLink.text || '') : '',
    labels,
    byTestId,
    links,
  };
}
"""


def classify_x_page_block(page_text: str) -> str:
    text = page_text.lower()
    if "log in" in text or "sign in" in text or "登录" in page_text:
        return "页面要求登录后查看"
    if "this post is unavailable" in text or "此帖子不可用" in page_text:
        return "推文不可用或权限受限"
    if "something went wrong" in text:
        return "X/Twitter 页面加载失败"
    return "未找到推文内容"


def x_metric_words(metric: str) -> set[str]:
    return {
        "comments": {"reply", "replies", "comment", "comments", "回复", "回覆", "评论", "評論"},
        "shares": {"repost", "reposts", "retweet", "retweets", "转发", "轉發", "转帖", "轉帖", "转贴", "轉貼", "转推", "轉推", "分享"},
        "likes": {"like", "likes", "喜欢", "喜歡", "点赞", "點讚", "赞", "讚"},
        "saves": {"bookmark", "bookmarks", "save", "saves", "书签", "書籤", "收藏"},
        "views": {"view", "views", "impression", "impressions", "观看", "觀看", "查看", "浏览", "瀏覽", "播放", "展示", "曝光", "阅读", "閱讀"},
    }[metric]


def x_metric_sources(raw: dict[str, Any], metric: str) -> list[str]:
    by_test_id = raw.get("byTestId") or {}
    article_text = raw.get("articleText") or ""
    labels = raw.get("labels") or []
    analytics_text = raw.get("analyticsText") or ""
    sources: list[str] = []

    if metric == "comments":
        sources.append(by_test_id.get("reply", ""))
    elif metric == "shares":
        sources.extend([by_test_id.get("retweet", ""), by_test_id.get("unretweet", "")])
    elif metric == "likes":
        sources.extend([by_test_id.get("like", ""), by_test_id.get("unlike", "")])
    elif metric == "saves":
        sources.extend([by_test_id.get("bookmark", ""), by_test_id.get("removeBookmark", "")])
    elif metric == "views":
        sources.append(analytics_text)

    words = x_metric_words(metric)
    sources.extend(label for label in labels if any(word in label.lower() for word in words))
    sources.append(article_text)
    return [source for source in sources if source]


def apply_x_raw_to_result(result: SocialResult, raw: dict[str, Any], tweet_id: str | None, page_url: str) -> None:
    result.final_url = raw.get("statusUrl") or raw.get("finalUrl") or page_url
    result.title = raw.get("pageTitle")
    result.author_name = raw.get("authorName")
    result.author_handle = raw.get("authorHandle")
    result.posted_at = raw.get("postedAt")
    result.text = raw.get("tweetText")
    result.debug.update(
        {
            "source": "x_full_parser",
            "tweet_id": raw.get("tweetId") or tweet_id,
            "page_title": raw.get("pageTitle"),
            "action_labels": raw.get("byTestId"),
            "analytics_text": raw.get("analyticsText"),
        }
    )
    for metric in METRIC_LABELS:
        parsed = extract_metric(metric, x_metric_sources(raw, metric))
        if parsed.value is not None or metric not in result.metrics:
            result.metrics[metric] = parsed
    default_missing_x_metrics_to_zero(result)


REELS_GRID_EXTRACT_SCRIPT = r"""
(target) => {
  const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const visibleRect = (node) => {
    const rect = node.getBoundingClientRect();
    return {
      visible: rect.width > 0 && rect.height > 0,
      top: Math.round(rect.top),
      left: Math.round(rect.left),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    };
  };
  const cardText = (node) => {
    const values = [];
    let current = node;
    for (let depth = 0; current && depth < 5; depth += 1, current = current.parentElement) {
      values.push(clean(current.innerText || current.textContent || ''));
      values.push(clean(current.getAttribute?.('aria-label') || ''));
      values.push(clean(current.getAttribute?.('title') || ''));
    }
    return values.filter(Boolean).join(' | ').slice(0, 1200);
  };
  const links = Array.from(document.querySelectorAll('a[href]')).map(a => {
    const rect = visibleRect(a);
    const text = clean(a.innerText || a.textContent || '');
    const label = clean(a.getAttribute('aria-label') || '');
    const title = clean(a.getAttribute('title') || '');
    return {
      href: a.href,
      text,
      label,
      title,
      card_text: cardText(a),
      visible: rect.visible,
      rect,
    };
  }).filter(link => link.visible);
  const matching = links.find(link => link.href && link.href.includes(target));
  const reelLinks = links.filter(link => /\/(?:reel|reels)\//.test(link.href));
  const postLikeLinks = links.filter(link => /\/(?:reel|reels|p)\//.test(link.href));
  const media = Array.from(document.querySelectorAll('img, video')).map(node => {
    const rect = visibleRect(node);
    const tag = (node.tagName || '').toLowerCase();
    let loaded = false;
    if (tag === 'img') loaded = Boolean(node.complete && node.naturalWidth > 0);
    if (tag === 'video') loaded = Number(node.readyState || 0) >= 2;
    return {tag, visible: rect.visible, loaded, rect};
  }).filter(item => item.visible);
  const iframeSrcs = Array.from(document.querySelectorAll('iframe[src]'))
    .map(node => node.src || '')
    .filter(Boolean)
    .slice(0, 30);
  return {
    final_url: location.href,
    title: document.title || '',
    body_text: document.body?.innerText?.slice(0, 5000) || '',
    iframe_srcs: iframeSrcs,
    sample_links: links
      .filter(link => /\/(?:reel|reels|p)\//.test(link.href))
      .slice(0, 24),
    matching,
    load_state: {
      ready_state: document.readyState || '',
      scroll_height: Math.round(document.scrollingElement?.scrollHeight || document.body?.scrollHeight || 0),
      viewport_height: Math.round(window.innerHeight || 0),
      visible_link_count: links.length,
      post_like_link_count: postLikeLinks.length,
      reel_link_count: reelLinks.length,
      visible_media_count: media.length,
      loaded_media_count: media.filter(item => item.loaded).length,
      matching_seen: Boolean(matching),
    },
  };
}
"""


def find_chrome_executable() -> str:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "chrome"


def cdp_url_from_port(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def cdp_json_version_url(cdp_url: str) -> str:
    return cdp_url.rstrip("/") + "/json/version"


def cdp_endpoint_info(cdp_url: str, timeout_s: float = 1.0) -> dict[str, Any] | None:
    try:
        with urlopen(cdp_json_version_url(cdp_url), timeout=timeout_s) as response:
            if not (200 <= response.status < 300):
                return None
            data = json.loads(response.read().decode("utf-8", errors="replace"))
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def is_chrome_cdp_info(info: dict[str, Any] | None) -> bool:
    if not info:
        return False
    browser = str(info.get("Browser") or "").lower()
    websocket_url = str(info.get("webSocketDebuggerUrl") or "")
    return bool(websocket_url) and any(marker in browser for marker in CHROME_CDP_BROWSER_MARKERS)


def cdp_endpoint_label(info: dict[str, Any] | None) -> str:
    if not info:
        return "unavailable"
    return str(info.get("Browser") or "unknown")


def is_cdp_available(cdp_url: str, timeout_s: float = 1.0) -> bool:
    return is_chrome_cdp_info(cdp_endpoint_info(cdp_url, timeout_s=timeout_s))


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_cdp_port(preferred_port: int, scan_limit: int = CDP_PORT_SCAN_LIMIT) -> int:
    preferred_url = cdp_url_from_port(preferred_port)
    preferred_info = cdp_endpoint_info(preferred_url)
    if preferred_info is None and port_is_free(preferred_port):
        return preferred_port
    preferred_label = cdp_endpoint_label(preferred_info)
    if preferred_info is None:
        preferred_label = "non-CDP service"
    for port in range(preferred_port + 1, preferred_port + max(1, scan_limit) + 1):
        url = cdp_url_from_port(port)
        info = cdp_endpoint_info(url)
        if info is None and port_is_free(port):
            print(
                f"CDP port {preferred_port} is occupied by non-Chrome endpoint ({preferred_label}); "
                f"using {url}",
                file=sys.stderr,
            )
            return port
    raise SystemExit(
        f"CDP port {preferred_port} is occupied by {preferred_label}, "
        f"and no free CDP port was found in the next {max(1, scan_limit)} ports."
    )


def chrome_startup_info(start_minimized: bool) -> Any | None:
    if not start_minimized or sys.platform != "win32":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 7
    return startupinfo


def windows_no_window_creationflags() -> int:
    if sys.platform != "win32":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def windows_process_tree_pids(root_pid: int) -> set[int]:
    if sys.platform != "win32":
        return {root_pid}
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return {root_pid}

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return {root_pid}

    parents: dict[int, int] = {}
    try:
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return {root_pid}
        while True:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)

    tree = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent_pid in parents.items():
            if parent_pid in tree and pid not in tree:
                tree.add(pid)
                changed = True
    return tree


def chrome_profile_pids(profile: Path) -> set[int]:
    if sys.platform != "win32":
        return set()
    env = os.environ.copy()
    env["_SOCIAL_CDP_PROFILE"] = str(profile.resolve()).lower()
    command = (
        "$profile = $env:_SOCIAL_CDP_PROFILE; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "($_.Name -in @('chrome.exe','msedge.exe')) -and "
        "(([string]$_.CommandLine).ToLowerInvariant().Contains($profile)) "
        "} | ForEach-Object { $_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            env=env,
            timeout=5,
            creationflags=windows_no_window_creationflags(),
            check=False,
        )
    except Exception:
        return set()
    return {int(match) for match in re.findall(r"\d+", result.stdout)}


def chrome_profile_cdp_ports(profile: Path) -> list[int]:
    if sys.platform != "win32":
        return []
    env = os.environ.copy()
    env["_SOCIAL_CDP_PROFILE"] = str(profile.resolve()).lower()
    command = (
        "$profile = $env:_SOCIAL_CDP_PROFILE; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { "
        "($_.Name -in @('chrome.exe','msedge.exe')) -and "
        "(([string]$_.CommandLine).ToLowerInvariant().Contains($profile)) -and "
        "(([string]$_.CommandLine) -match '--remote-debugging-port=(\\d+)') "
        "} | ForEach-Object { "
        "if (([string]$_.CommandLine) -match '--remote-debugging-port=(\\d+)') { $Matches[1] } "
        "}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            env=env,
            timeout=5,
            creationflags=windows_no_window_creationflags(),
            check=False,
        )
    except Exception:
        return []
    ports: list[int] = []
    seen: set[int] = set()
    for match in re.findall(r"\d+", result.stdout):
        port = int(match)
        if port not in seen:
            ports.append(port)
            seen.add(port)
    return ports


def existing_cdp_url_for_profile(profile: Path, wait_s: float = 0.0, timeout_s: float = 1.0) -> str | None:
    deadline = time.monotonic() + max(0.0, wait_s)
    ports = chrome_profile_cdp_ports(profile)
    while ports:
        for port in ports:
            url = cdp_url_from_port(port)
            if is_cdp_available(url, timeout_s=timeout_s):
                return url
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
        ports = chrome_profile_cdp_ports(profile)
    return None


def minimize_windows_for_pids(pids: set[int]) -> bool:
    if sys.platform != "win32":
        return False
    pids = {int(pid) for pid in pids if int(pid) > 0}
    if not pids:
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32
    found = False
    sw_showminnoactive = 7

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: Any, _lparam: Any) -> bool:
        nonlocal found
        if not user32.IsWindowVisible(hwnd):
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if int(process_id.value) in pids:
            user32.ShowWindow(hwnd, sw_showminnoactive)
            found = True
        return True

    user32.EnumWindows(enum_proc, 0)
    return found


def minimize_windows_for_pid(pid: int) -> bool:
    return minimize_windows_for_pids({pid})


def wait_and_minimize_process_windows(
    process: subprocess.Popen[Any],
    profile: Path | None = None,
    *,
    attempts: int = 80,
    interval_s: float = 0.25,
    profile_scan_interval_s: float = 2.0,
) -> bool:
    found_any = False
    profile_pids: set[int] = set()
    next_profile_scan = 0.0
    for _ in range(attempts):
        pids = set()
        if process.poll() is None:
            pids.update(windows_process_tree_pids(process.pid))
        elif profile is None:
            return found_any

        now = time.monotonic()
        if profile is not None and now >= next_profile_scan:
            profile_pids = chrome_profile_pids(profile)
            next_profile_scan = now + profile_scan_interval_s
        pids.update(profile_pids)
        if not pids and process.poll() is not None:
            return found_any

        if minimize_windows_for_pids(pids):
            return True
        time.sleep(interval_s)
    return found_any


def ensure_cdp_profile(
    profile: Path,
    port: int,
    open_url: str,
    *,
    start_minimized: bool,
    auto_close: bool,
    headless: bool = False,
) -> CdpProfileSession:
    existing_profile_url = existing_cdp_url_for_profile(profile, wait_s=30.0, timeout_s=5.0)
    if existing_profile_url:
        print(f"Chrome CDP already running for profile: {existing_profile_url}", file=sys.stderr)
        return CdpProfileSession(url=existing_profile_url)

    preferred_url = cdp_url_from_port(port)
    preferred_info = cdp_endpoint_info(preferred_url, timeout_s=5.0)
    if is_chrome_cdp_info(preferred_info):
        print(f"Chrome CDP already running on preferred port: {preferred_url}", file=sys.stderr)
        return CdpProfileSession(url=preferred_url)

    profile_cdp_ports = chrome_profile_cdp_ports(profile)
    if profile_cdp_ports:
        existing_profile_url = existing_cdp_url_for_profile(profile, wait_s=30.0, timeout_s=5.0)
        if existing_profile_url:
            print(f"Chrome CDP recovered for profile: {existing_profile_url}", file=sys.stderr)
            return CdpProfileSession(url=existing_profile_url)

    if profile_cdp_ports:
        ports_text = ", ".join(str(port) for port in profile_cdp_ports)
        raise SystemExit(
            f"Chrome profile is already running with CDP port(s) {ports_text}, "
            "but no Chrome CDP endpoint responded. Close that Chrome profile or wait for it to recover before retrying."
        )

    port = choose_cdp_port(port)
    cdp_url = cdp_url_from_port(port)

    profile.mkdir(parents=True, exist_ok=True)
    command = [
        find_chrome_executable(),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        command.extend(["--headless=new", "--disable-gpu", "--window-size=1920,1080"])
        command.append(open_url)
    else:
        command.extend(["--new-window", open_url])
    process = subprocess.Popen(
        command,
        startupinfo=chrome_startup_info(start_minimized or headless),
        creationflags=windows_no_window_creationflags(),
    )
    print(f"Started reusable Chrome CDP profile: {profile}", file=sys.stderr)
    print(f"CDP URL: {cdp_url}", file=sys.stderr)
    for _ in range(30):
        if is_cdp_available(cdp_url):
            if start_minimized:
                wait_and_minimize_process_windows(process, profile, attempts=80)
            return CdpProfileSession(url=cdp_url, process=process, started_by_tool=True, auto_close=auto_close)
        time.sleep(0.5)
    raise SystemExit(f"Chrome was started, but {cdp_url} is still unavailable.")


def is_login_page_data(page_data: dict[str, Any], platform: str | None = None) -> bool:
    final_url = page_data.get("final_url") or ""
    body = page_data.get("body_text") or ""
    title = page_data.get("title") or ""
    if platform == "tiktok":
        return "/login" in final_url.lower()
    if platform == "facebook":
        lowered = f"{final_url}\n{title}".lower()
        if "/login" in lowered or "log into facebook" in lowered:
            return True
        body_text = compact_spaces(body).lower()
        return "you must log in to continue" in body_text or "你必须登录才能继续" in body_text
    login_markers = [
        "/accounts/login",
        "Log into Instagram",
        "Log in",
        "Sign in",
        "Sign in to X",
        "Sign up",
        "Create account",
        "登录",
        "登入",
        "注册",
        "创建帐户",
        "创建账号",
    ]
    haystack = f"{final_url}\n{title}\n{body}"
    return any(marker in haystack for marker in login_markers)


def anti_bot_marker_from_page_data(page_data: dict[str, Any]) -> str | None:
    final_url = str(page_data.get("final_url") or "")
    title = str(page_data.get("title") or "")
    body = str(page_data.get("body_text") or "")
    iframe_srcs = page_data.get("iframe_srcs") or []
    if not isinstance(iframe_srcs, list):
        iframe_srcs = []
    url_text = "\n".join([final_url, *(str(item) for item in iframe_srcs)]).lower()
    for marker in ANTI_BOT_URL_MARKERS:
        if marker in url_text:
            return marker
    text = compact_spaces(f"{title}\n{body}\n{url_text}").lower()
    for marker in ANTI_BOT_TEXT_MARKERS:
        if marker in text:
            return marker
    return None


def mark_anti_bot_result(result: SocialResult, marker: str, *, stage: str) -> None:
    marker_text = marker or "unknown"
    result.debug["anti_bot_detected"] = True
    result.debug["anti_bot_marker"] = marker_text
    result.debug["anti_bot_stage"] = stage
    result.error = f"{result.platform} 触发反爬/验证：{marker_text}"


def result_anti_bot_detected(result: SocialResult) -> bool:
    if result.debug.get("anti_bot_detected"):
        return True
    error = str(result.error or "").lower()
    return any(marker in error for marker in ANTI_BOT_TEXT_MARKERS) or any(marker in error for marker in ANTI_BOT_URL_MARKERS)


def result_page_kind(result: SocialResult) -> str:
    kind = str(result.debug.get("page_kind") or "").strip().lower()
    if kind and kind != "unknown":
        return kind
    return social_url_kind(result.platform, result.final_url or result.source_url)


def result_metrics_retry_worthwhile(result: SocialResult) -> bool:
    if result.platform in {"instagram", "facebook"}:
        return result_page_kind(result) == "reel"
    return True


def tiktok_unavailable_reason(result: SocialResult) -> str | None:
    if result.platform != "tiktok":
        return None
    final_url = (result.final_url or "").lower()
    if "/notfound" in final_url:
        return "TikTok 页面跳转到 notfound"
    json_keys = result.debug.get("json_script_keys")
    has_json = isinstance(json_keys, list) and len(json_keys) > 0
    title = compact_spaces(result.title)
    text = compact_spaces(result.text)
    if not has_json and title == "TikTok" and text == "TikTok":
        return "TikTok 页面没有返回视频数据"
    return None


def short_error_message(message: Any, limit: int = 180) -> str:
    text = compact_spaces(str(message or ""))
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def page_open_failure_reason(result: SocialResult) -> str | None:
    messages: list[str] = []
    playwright_error = result.debug.get("playwright_error")
    if playwright_error:
        messages.append(str(playwright_error))
    if result.error and result.error != "login_required":
        messages.append(result.error)
    for message in messages:
        lowered = message.lower()
        if any(marker in lowered for marker in PAGE_OPEN_FAILURE_MARKERS):
            return f"网页打开失败：{short_error_message(message)}"
    return None


class SocialScraper:
    def __init__(
        self,
        *,
        use_playwright: bool,
        headless: bool,
        timeout_ms: int,
        delay_min_s: float,
        delay_max_s: float,
        reel_grid_scrolls: int,
        cdp_url: str | None = None,
        cdp_profile: Path | None = None,
        cdp_port: int = DEFAULT_CDP_PORT,
        use_cdp_for_x: bool = False,
        use_cdp_for_instagram: bool = True,
        use_cdp_for_facebook: bool = False,
        use_cdp_for_tiktok: bool = False,
        use_cdp_for_linkedin: bool = False,
        normalize_instagram_posts: bool = True,
        enable_reel_grid_views: bool = True,
        max_attempts: int = 3,
        require_text_for_retry: bool = False,
        require_time_for_retry: bool = False,
        retry_missing_metrics: bool = False,
        media_kind_aware_retry: bool = False,
        stable_reel_grid_wait: bool = False,
        ignore_anti_bot_stop: bool = False,
        cdp_start_minimized: bool = True,
        cdp_headless: bool = False,
        cdp_auto_close: bool = True,
        cdp_login_timeout_ms: int = 300_000,
        allow_manual_login: bool = True,
    ) -> None:
        self.use_playwright = use_playwright
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.delay_min_s = delay_min_s
        self.delay_max_s = delay_max_s
        self.reel_grid_scrolls = max(0, reel_grid_scrolls)
        self.cdp_url = cdp_url
        self.cdp_profile = cdp_profile or DEFAULT_CDP_PROFILE
        self.cdp_port = cdp_port
        self.use_cdp_for_x = use_cdp_for_x
        self.use_cdp_for_instagram = use_cdp_for_instagram
        self.use_cdp_for_facebook = use_cdp_for_facebook
        self.use_cdp_for_tiktok = use_cdp_for_tiktok
        self.use_cdp_for_linkedin = use_cdp_for_linkedin
        self.normalize_instagram_posts = normalize_instagram_posts
        self.enable_reel_grid_views = enable_reel_grid_views
        self.max_attempts = max(1, max_attempts)
        self.require_text_for_retry = require_text_for_retry
        self.require_time_for_retry = require_time_for_retry
        self.retry_missing_metrics = retry_missing_metrics
        self.media_kind_aware_retry = media_kind_aware_retry
        self.stable_reel_grid_wait = stable_reel_grid_wait
        self.ignore_anti_bot_stop = ignore_anti_bot_stop
        self.cdp_start_minimized = cdp_start_minimized
        self.cdp_headless = cdp_headless
        self.cdp_auto_close = cdp_auto_close
        self.cdp_login_timeout_ms = cdp_login_timeout_ms
        self.allow_manual_login = allow_manual_login
        self._cdp_session: CdpProfileSession | None = None

    def sleep_between(self) -> None:
        if self.delay_max_s <= 0:
            return
        delay = random.uniform(max(0.0, self.delay_min_s), max(self.delay_min_s, self.delay_max_s))
        if delay > 0:
            print(f"[wait] {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)

    def scrape_url_for(self, url: str) -> str:
        if self.normalize_instagram_posts:
            return normalize_scrape_url(url)
        return url

    def should_use_cdp(self, urls: list[str]) -> bool:
        if self.cdp_url:
            return True
        for url in urls:
            if self.should_use_cdp_for_url(url):
                return True
        return False

    def should_use_cdp_for_url(self, url: str) -> bool:
        if self.cdp_url:
            return True
        platform = detect_platform(url)
        if platform == "instagram" and self.use_cdp_for_instagram:
            return True
        if platform == "x" and self.use_cdp_for_x:
            return True
        if platform == "facebook" and self.use_cdp_for_facebook:
            return True
        if platform == "tiktok" and self.use_cdp_for_tiktok:
            return True
        if platform == "linkedin" and self.use_cdp_for_linkedin:
            return True
        return False

    def ensure_cdp(self, open_url: str | None = None, *, visible_for_login: bool = False) -> CdpProfileSession:
        if self.cdp_url:
            return CdpProfileSession(url=self.cdp_url)
        if self._cdp_session and is_cdp_available(self._cdp_session.url):
            return self._cdp_session
        if self._cdp_session:
            self._cdp_session.close()
            self._cdp_session = None
        self._cdp_session = ensure_cdp_profile(
            self.cdp_profile,
            self.cdp_port,
            open_url or "https://www.instagram.com/accounts/login/",
            start_minimized=False if visible_for_login else self.cdp_start_minimized,
            auto_close=self.cdp_auto_close,
            headless=self.cdp_headless and not visible_for_login,
        )
        return self._cdp_session

    def wait_for_login(self, page: Any, login_url: str) -> bool:
        print(f"Login required. Please log in in the opened Chrome window: {login_url}", file=sys.stderr)
        deadline = time.time() + max(1, self.cdp_login_timeout_ms / 1000)
        page.goto(login_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            try:
                page_data = page.evaluate(PLAYWRIGHT_EXTRACT_SCRIPT)
                if not is_login_page_data(page_data):
                    print("Login detected, continuing scrape.", file=sys.stderr)
                    return True
            except Exception:
                pass
        print("Login was not detected before timeout.", file=sys.stderr)
        return False

    def result_needs_retry(self, url: str, result: SocialResult) -> bool:
        platform = detect_platform(url)
        if result_anti_bot_detected(result) and not self.ignore_anti_bot_stop:
            return False
        if page_open_failure_reason(result):
            return True
        if self.require_text_for_retry or self.require_time_for_retry:
            if tiktok_unavailable_reason(result) is not None:
                return True
            if self.require_text_for_retry and not compact_spaces(result.text or result.title):
                return True
            if self.require_time_for_retry and not result.posted_at:
                return True
            if result.error:
                return True
            if (
                self.retry_missing_metrics
                and (not self.media_kind_aware_retry or result_metrics_retry_worthwhile(result))
                and not self.result_has_known_metric(result)
            ):
                return True
            return False
        if platform == "x":
            return not x_has_text_and_time(result)
        if result.error:
            return True
        if platform == "instagram":
            if self.media_kind_aware_retry and result_page_kind(result) == "post":
                return bool(result.error)
            return not (result.metrics.get("views") and result.metrics["views"].value is not None)
        if platform == "tiktok":
            return tiktok_unavailable_reason(result) is not None or not (
                result.metrics.get("views") and result.metrics["views"].value is not None
            )
        if platform == "linkedin":
            return not (compact_spaces(result.text) and result.posted_at)
        return not any(metric.value is not None for metric in result.metrics.values())

    @staticmethod
    def result_has_known_metric(result: SocialResult) -> bool:
        return any(metric.value is not None for metric in result.metrics.values())

    def result_required_failure(self, url: str, result: SocialResult) -> bool:
        if result_anti_bot_detected(result) and not self.ignore_anti_bot_stop:
            return True
        if page_open_failure_reason(result):
            return True
        if self.require_text_for_retry or self.require_time_for_retry:
            if tiktok_unavailable_reason(result) is not None:
                return True
            if self.require_text_for_retry and not compact_spaces(result.text or result.title):
                return True
            if self.require_time_for_retry and not result.posted_at:
                return True
            return bool(result.error)
        return self.result_needs_retry(url, result)

    def result_login_required(self, result: SocialResult) -> bool:
        if result.debug.get("login_required"):
            return True
        grid = result.debug.get("reels_grid_views")
        return isinstance(grid, dict) and grid.get("status") == "login_required"

    def login_url_for_result(self, url: str, result: SocialResult) -> str:
        grid = result.debug.get("reels_grid_views")
        if isinstance(grid, dict) and grid.get("final_url"):
            return str(grid["final_url"])
        platform = detect_platform(url)
        if self.result_login_required(result):
            if platform == "instagram":
                return "https://www.instagram.com/accounts/login/"
            if platform == "x":
                return "https://x.com/i/flow/login"
        if result.final_url:
            return result.final_url
        if platform == "instagram":
            return "https://www.instagram.com/accounts/login/"
        if platform == "x":
            return "https://x.com/i/flow/login"
        return self.scrape_url_for(url)

    def mark_manual_review(self, result: SocialResult, reason: str) -> None:
        result.debug["manual_review"] = True
        result.debug["manual_review_reason"] = reason
        if result.error and result.error != "login_required":
            result.error = f"{reason}；{result.error}"
        else:
            result.error = reason

    def ignore_anti_bot_for_retry(self, result: SocialResult) -> None:
        if not self.ignore_anti_bot_stop or not result_anti_bot_detected(result):
            return
        marker = result.debug.get("anti_bot_marker") or short_error_message(result.error)
        result.debug["ignored_anti_bot_marker"] = marker
        result.debug.pop("anti_bot_detected", None)
        result.debug.pop("anti_bot_marker", None)
        result.debug.pop("anti_bot_stage", None)
        result.error = None

    def manual_review_reason(self, url: str, result: SocialResult) -> str:
        platform = detect_platform(url)
        label = {
            "x": "X",
            "instagram": "Instagram",
            "facebook": "Facebook",
            "tiktok": "TikTok",
            "linkedin": "LinkedIn",
        }.get(platform, "社媒链接")
        if result_anti_bot_detected(result):
            marker = result.debug.get("anti_bot_marker") or short_error_message(result.error)
            if self.ignore_anti_bot_stop:
                return f"{label} 触发反爬/验证：{marker}，连续刷新后仍无法读取信息，请人工查阅"
            return f"{label} 触发反爬/验证：{marker}，请人工查阅"
        if self.result_login_required(result):
            return f"{label} 页面要求登录或访问受限，请人工查阅"
        reason = page_open_failure_reason(result) or tiktok_unavailable_reason(result)
        if reason:
            if reason.startswith(label):
                return f"{reason}，请人工查阅"
            return f"{label} {reason}，请人工查阅"
        if self.require_text_for_retry or self.require_time_for_retry:
            if self.require_text_for_retry and not compact_spaces(result.text or result.title):
                return f"{label} 连续 {self.max_attempts} 次未读取到正文，请人工查阅"
            if self.require_time_for_retry and not result.posted_at:
                return f"{label} 连续 {self.max_attempts} 次未读取到发布时间，请人工查阅"
            if result.error:
                return f"{label} {short_error_message(result.error)}，请人工查阅"
        if platform == "x":
            return f"X 连续 {self.max_attempts} 次未读取到正文或发布时间，请人工查阅"
        if platform in {"facebook", "instagram", "tiktok"}:
            return f"{label} 连续 {self.max_attempts} 次未读取到播放量，请人工查阅"
        if platform == "linkedin":
            return f"LinkedIn 连续 {self.max_attempts} 次未读取到正文或发布时间，请人工查阅"
        return f"{label} 连续 {self.max_attempts} 次未读取到数据，请人工查阅"

    def scrape(self, urls: list[str], on_result: Any | None = None) -> list[SocialResult]:
        if self.use_playwright:
            return self.scrape_with_playwright(urls, on_result=on_result)
        results: list[SocialResult] = []
        for index, url in enumerate(urls, 1):
            item_start = time.perf_counter()
            print(f"[{index}/{len(urls)}] {url}", file=sys.stderr)
            result = self.scrape_http(url)
            if self.result_required_failure(url, result):
                self.mark_manual_review(result, self.manual_review_reason(url, result))
            results.append(result)
            print(
                "[item-time] "
                f"{index}/{len(urls)} "
                f"elapsed_s={time.perf_counter() - item_start:.1f} "
                f"attempts={result.debug.get('attempts', 1)} "
                f"platform={detect_platform(url)} "
                "use_cdp=false "
                f"url={url}",
                file=sys.stderr,
            )
            if on_result and on_result(index, len(urls), url, result) is False:
                break
            if index < len(urls):
                self.sleep_between()
        return results

    def scrape_http(self, url: str) -> SocialResult:
        platform = detect_platform(url)
        scrape_url = self.scrape_url_for(url)
        try:
            final_url, html_text = http_fetch(scrape_url)
            page_data = {
                "final_url": final_url,
                "title": extract_meta(html_text).get("html:title"),
                "body_text": compact_spaces(re.sub(r"<[^>]+>", " ", html_text))[:25000],
                "iframe_srcs": re.findall(r'<iframe[^>]+src=["\']([^"\']+)', html_text, flags=re.I),
                "meta": extract_meta(html_text),
                "json_scripts": extract_json_scripts(html_text),
                "source": "http",
            }
            result = build_result_from_data(url, platform, page_data)
            if scrape_url != url:
                result.debug["normalized_url"] = scrape_url
            marker = anti_bot_marker_from_page_data(page_data)
            if marker:
                mark_anti_bot_result(result, marker, stage="http")
            return result
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return SocialResult(
                source_url=url,
                platform=platform,
                final_url=url,
                error=f"HTTP 读取失败：{str(exc).splitlines()[0]}",
                metrics={key: MetricValue() for key in METRIC_LABELS},
            )

    def scrape_with_playwright(self, urls: list[str], on_result: Any | None = None) -> list[SocialResult]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SystemExit(
                "缺少依赖 playwright。请先运行：\n"
                "  python -m pip install playwright\n"
                "  python -m playwright install chromium"
            ) from exc

        results: list[SocialResult] = []
        with sync_playwright() as playwright:
            browser = None
            normal_context = None
            normal_page = None
            cdp_browser = None
            cdp_context = None

            def get_normal_page() -> Any:
                nonlocal browser, normal_context, normal_page
                if normal_page is not None and not normal_page.is_closed():
                    return normal_page
                if browser is None:
                    browser = playwright.chromium.launch(headless=self.headless)
                    normal_context = browser.new_context(
                        locale="en-US",
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/125.0 Safari/537.36"
                        ),
                    )
                normal_page = normal_context.new_page()
                normal_page.set_default_timeout(self.timeout_ms)
                return normal_page

            def get_cdp_page(open_url: str, *, visible_for_login: bool = False) -> Any:
                nonlocal cdp_browser, cdp_context, cdp_page
                if cdp_browser is not None and not cdp_browser.is_connected():
                    cdp_browser = None
                    cdp_context = None
                    cdp_page = None
                session = self.ensure_cdp(open_url, visible_for_login=visible_for_login)
                if cdp_browser is None:
                    cdp_browser = playwright.chromium.connect_over_cdp(session.url)
                    cdp_context = cdp_browser.contexts[0] if cdp_browser.contexts else cdp_browser.new_context()
                if cdp_page is None or cdp_page.is_closed():
                    cdp_page = cdp_context.new_page()
                    cdp_page.set_default_timeout(self.timeout_ms)
                return cdp_page

            try:
                cdp_page = None
                for index, url in enumerate(urls, 1):
                    item_start = time.perf_counter()
                    print(f"[{index}/{len(urls)}] {url}", file=sys.stderr)
                    platform = detect_platform(url)
                    use_cdp = self.should_use_cdp_for_url(url)
                    try:
                        if use_cdp:
                            page = get_cdp_page(self.scrape_url_for(url), visible_for_login=False)
                        else:
                            page = get_normal_page()

                        result = self.scrape_one_page_with_retry(page, url)
                        self.ignore_anti_bot_for_retry(result)
                        if (
                            self.result_needs_retry(url, result)
                            and self.result_login_required(result)
                            and (use_cdp or platform in {"x", "instagram"})
                        ):
                            if self.allow_manual_login:
                                cdp_page = get_cdp_page(self.login_url_for_result(url, result), visible_for_login=True)
                                if self.wait_for_login(cdp_page, self.login_url_for_result(url, result)):
                                    result = self.scrape_one_page_with_retry(cdp_page, url)
                                    self.ignore_anti_bot_for_retry(result)
                                use_cdp = True

                        if platform == "x" and not use_cdp and self.result_needs_retry(url, result):
                            print(f"[cdp-fallback] X 连续 {self.max_attempts} 次未读取到数据，启用 CDP：{url}", file=sys.stderr)
                            cdp_page = get_cdp_page(self.scrape_url_for(url), visible_for_login=False)
                            result = self.scrape_one_page_with_retry(cdp_page, url)
                            self.ignore_anti_bot_for_retry(result)
                            use_cdp = True
                            if self.result_needs_retry(url, result) and self.result_login_required(result):
                                if self.allow_manual_login and self.wait_for_login(cdp_page, self.login_url_for_result(url, result)):
                                    result = self.scrape_one_page_with_retry(cdp_page, url)
                                    self.ignore_anti_bot_for_retry(result)

                        if use_cdp and platform == "x" and self.result_required_failure(url, result):
                            self.mark_manual_review(result, f"X CDP 连续 {self.max_attempts} 次未读取到正文或发布时间，请人工查阅")
                        if self.result_required_failure(url, result) and not result.debug.get("manual_review"):
                            self.mark_manual_review(result, self.manual_review_reason(url, result))
                    except Exception as exc:
                        message = str(exc).splitlines()[0]
                        print(f"[error] {url} {message}", file=sys.stderr)
                        result = SocialResult(
                            source_url=url,
                            platform=platform,
                            final_url=self.scrape_url_for(url),
                            error=f"读取异常，请人工查阅：{message}",
                            metrics={key: MetricValue() for key in METRIC_LABELS},
                            debug={"exception": message},
                        )
                        if "Target page" in message or "Target context" in message or "Target browser" in message:
                            cdp_page = None
                            if cdp_browser is not None and not cdp_browser.is_connected():
                                cdp_browser = None
                                cdp_context = None

                    results.append(result)
                    print(
                        "[item-time] "
                        f"{index}/{len(urls)} "
                        f"elapsed_s={time.perf_counter() - item_start:.1f} "
                        f"attempts={result.debug.get('attempts', 1)} "
                        f"platform={platform} "
                        f"use_cdp={str(use_cdp).lower()} "
                        f"url={url}",
                        file=sys.stderr,
                    )
                    stop_for_antibot = result_anti_bot_detected(result) and not self.ignore_anti_bot_stop
                    if on_result and on_result(index, len(urls), url, result) is False:
                        break
                    if stop_for_antibot:
                        print(f"[anti-bot-stop] platform={platform} url={url}", file=sys.stderr)
                        break
                    if index < len(urls):
                        self.sleep_between()
            finally:
                if cdp_page is not None:
                    try:
                        cdp_page.close()
                    except Exception:
                        pass
                if normal_context is not None:
                    normal_context.close()
                if browser is not None:
                    browser.close()
                if self._cdp_session:
                    self._cdp_session.close()
        return results

    def scrape_one_page_with_retry(self, page: Any, url: str) -> SocialResult:
        result = self.scrape_one_page(page, url)
        self.ignore_anti_bot_for_retry(result)
        result.debug["attempts"] = 1
        for attempt in range(2, self.max_attempts + 1):
            if result_anti_bot_detected(result) and not self.ignore_anti_bot_stop:
                break
            if not self.result_needs_retry(url, result):
                break
            print(f"[retry {attempt}/{self.max_attempts}] {url}", file=sys.stderr)
            page.wait_for_timeout(1500)
            result = self.scrape_one_page(page, url)
            self.ignore_anti_bot_for_retry(result)
            result.debug["attempts"] = attempt
        return result

    def scrape_one_page(self, page: Any, url: str) -> SocialResult:
        platform = detect_platform(url)
        if platform == "x":
            return self.scrape_x_page(page, url)
        scrape_url = self.scrape_url_for(url)
        try:
            page.goto(scrape_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_timeout(3000)
            if platform == "linkedin":
                try:
                    page.keyboard.press("Escape")
                    page.evaluate(DISMISS_OVERLAYS_SCRIPT)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
            if platform in {"facebook", "instagram", "tiktok", "linkedin"}:
                page.mouse.wheel(0, 600)
                page.wait_for_timeout(1500)
            if platform == "linkedin":
                try:
                    page.keyboard.press("Escape")
                    page.evaluate(DISMISS_OVERLAYS_SCRIPT)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
            page_data = page.evaluate(PLAYWRIGHT_EXTRACT_SCRIPT)
            result = build_result_from_data(url, platform, page_data)
            if scrape_url != url:
                result.debug["normalized_url"] = scrape_url
            marker = anti_bot_marker_from_page_data(page_data)
            if marker:
                mark_anti_bot_result(result, marker, stage="detail")
                return result
            if is_login_page_data(page_data, platform):
                result.debug["login_required"] = True
                result.error = "login_required"
                return result
            self.fill_reel_grid_views(page, result, page_data)
            return result
        except Exception as exc:
            result = self.scrape_http(url)
            if result.error:
                result.error = f"Playwright 读取失败：{str(exc).splitlines()[0]}；{result.error}"
            else:
                result.debug["playwright_error"] = str(exc).splitlines()[0]
            return result

    def scrape_x_page(self, page: Any, url: str) -> SocialResult:
        result = SocialResult(
            source_url=url,
            platform="x",
            final_url=url,
            media_type="post",
            metrics={key: MetricValue() for key in METRIC_LABELS},
        )
        tweet_id = tweet_id_from_url(url)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            result.final_url = page.url
            status = response.status if response else None

            try:
                page.wait_for_selector('article[data-testid="tweet"]', timeout=self.timeout_ms)
            except Exception:
                page_text = page.locator("body").inner_text(timeout=3000)
                marker = anti_bot_marker_from_page_data({"final_url": page.url, "title": page.title(), "body_text": page_text})
                if marker:
                    mark_anti_bot_result(result, marker, stage="x-detail")
                    return result
                result.error = f"{classify_x_page_block(page_text)}（HTTP {status or '未知'}）"
                result.debug = {"page_title": page.title(), "page_text": page_text[:1000], "source": "x_full_parser"}
                if is_login_page_data({"final_url": page.url, "title": result.debug["page_title"], "body_text": page_text}):
                    result.debug["login_required"] = True
                    result.error = "login_required"
                return result

            page.wait_for_timeout(1200)
            raw = page.evaluate(X_EXTRACT_TWEET_SCRIPT, tweet_id)
            if not raw.get("found"):
                marker = anti_bot_marker_from_page_data(
                    {
                        "final_url": page.url,
                        "title": raw.get("title") or page.title(),
                        "body_text": raw.get("pageText", ""),
                    }
                )
                if marker:
                    result.debug = {"source": "x_full_parser", **raw}
                    mark_anti_bot_result(result, marker, stage="x-detail")
                    return result
                result.error = classify_x_page_block(raw.get("pageText", ""))
                result.debug = {"source": "x_full_parser", **raw}
                if is_login_page_data(
                    {
                        "final_url": page.url,
                        "title": raw.get("title") or page.title(),
                        "body_text": raw.get("pageText", ""),
                    }
                ):
                    result.debug["login_required"] = True
                    result.error = "login_required"
                return result

            apply_x_raw_to_result(result, raw, tweet_id, page.url)
            for retry_index in range(2):
                if result.metrics.get("views") and result.metrics["views"].value is not None:
                    break
                page.wait_for_timeout(2000)
                page.mouse.wheel(0, 300)
                page.wait_for_timeout(800)
                page.mouse.wheel(0, -300)
                page.wait_for_timeout(800)
                retry_raw = page.evaluate(X_EXTRACT_TWEET_SCRIPT, result.debug.get("tweet_id") or tweet_id)
                if retry_raw.get("found"):
                    apply_x_raw_to_result(result, retry_raw, tweet_id, page.url)
                    result.debug["views_retry_count"] = retry_index + 1
            return result
        except Exception as exc:
            result.error = str(exc).splitlines()[0]
            try:
                result.final_url = page.url
            except Exception:
                pass
            return result

    def fill_reel_grid_views(self, page: Any, result: SocialResult, page_data: dict[str, Any]) -> None:
        if not self.enable_reel_grid_views:
            return
        if result.platform not in {"facebook", "instagram"}:
            return
        if not (
            is_reel_grid_candidate_url(result.platform, result.final_url)
            or is_reel_grid_candidate_url(result.platform, result.source_url)
        ):
            result.debug["reels_grid_views"] = {"status": "skipped_non_reel"}
            return

        target_id = reel_id_from_url(result.platform, result.final_url) or reel_id_from_url(result.platform, result.source_url)
        if not target_id:
            return

        profile_reels_url = self.profile_reels_url(result, page_data)
        if not profile_reels_url:
            result.debug["reels_grid_views"] = {"status": "no_profile_reels_url", "target_id": target_id}
            print(f"[reels-grid] {result.platform} 未找到作者主页，无法进入网格 target={target_id}", file=sys.stderr)
            return

        print(f"[reels-grid] {result.platform} 查找缩略图播放量 {profile_reels_url}", file=sys.stderr)
        try:
            page.goto(profile_reels_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            grid_wait_history: list[dict[str, Any]] = []
            for scroll_index in range(self.reel_grid_scrolls + 1):
                if self.stable_reel_grid_wait:
                    grid_data = self.wait_for_reels_grid_ready(page, target_id)
                else:
                    if scroll_index == 0:
                        page.wait_for_timeout(3500)
                    grid_data = page.evaluate(REELS_GRID_EXTRACT_SCRIPT, target_id)
                load_state = grid_data.get("load_state") if isinstance(grid_data, dict) else {}
                if isinstance(load_state, dict):
                    grid_wait_history.append(
                        {
                            "scroll_index": scroll_index,
                            "ready_state": load_state.get("ready_state"),
                            "post_like_link_count": load_state.get("post_like_link_count"),
                            "reel_link_count": load_state.get("reel_link_count"),
                            "visible_media_count": load_state.get("visible_media_count"),
                            "loaded_media_count": load_state.get("loaded_media_count"),
                            "matching_seen": load_state.get("matching_seen"),
                        }
                    )
                marker = anti_bot_marker_from_page_data(grid_data)
                if marker:
                    result.debug["reels_grid_views"] = {
                        "status": "anti_bot_detected",
                        "target_id": target_id,
                        "profile_reels_url": profile_reels_url,
                        "final_url": grid_data.get("final_url"),
                    }
                    mark_anti_bot_result(result, marker, stage="reels-grid")
                    return
                if is_login_page_data(grid_data, result.platform):
                    result.debug["reels_grid_views"] = {
                        "status": "login_required",
                        "target_id": target_id,
                        "profile_reels_url": profile_reels_url,
                        "final_url": grid_data.get("final_url"),
                    }
                    return
                metric = self.metric_from_grid_match(grid_data.get("matching"))
                if metric.value is not None:
                    result.metrics["views"] = metric
                    result.debug["reels_grid_views"] = {
                        "status": "matched",
                        "target_id": target_id,
                        "profile_reels_url": profile_reels_url,
                        "scroll_index": scroll_index,
                        "match": grid_data.get("matching"),
                        "wait_history": grid_wait_history[-3:],
                    }
                    return
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(1200)
            result.debug["reels_grid_views"] = {
                "status": "not_found",
                "target_id": target_id,
                "profile_reels_url": profile_reels_url,
                "wait_history": grid_wait_history[-3:],
            }
        except Exception as exc:
            result.debug["reels_grid_views"] = {
                "status": "failed",
                "target_id": target_id,
                "profile_reels_url": profile_reels_url,
                "error": str(exc).splitlines()[0],
            }

    def wait_for_reels_grid_ready(self, page: Any, target_id: str) -> dict[str, Any]:
        timeout_ms = max(0, env_int("SOCIAL_REELS_GRID_READY_TIMEOUT_MS", env_int("WEEKLY_ACCOUNT_REEL_GRID_READY_TIMEOUT_MS", 12_000)))
        poll_ms = max(250, env_int("SOCIAL_REELS_GRID_READY_POLL_MS", env_int("WEEKLY_ACCOUNT_REEL_GRID_READY_POLL_MS", 800)))
        stable_needed = max(1, env_int("SOCIAL_REELS_GRID_READY_STABLE_POLLS", env_int("WEEKLY_ACCOUNT_REEL_GRID_READY_STABLE_POLLS", 2)))
        min_links = max(1, env_int("SOCIAL_REELS_GRID_READY_MIN_LINKS", env_int("WEEKLY_ACCOUNT_REEL_GRID_READY_MIN_LINKS", 3)))
        deadline = time.monotonic() + timeout_ms / 1000
        previous_signature: tuple[Any, ...] | None = None
        stable_count = 0
        last_data: dict[str, Any] = {}
        while True:
            data = page.evaluate(REELS_GRID_EXTRACT_SCRIPT, target_id)
            last_data = data if isinstance(data, dict) else {}
            load_state = last_data.get("load_state") if isinstance(last_data, dict) else {}
            if not isinstance(load_state, dict):
                return last_data
            if last_data.get("matching"):
                load_state["ready_reason"] = "target_matched"
                return last_data

            visible_media = int(load_state.get("visible_media_count") or 0)
            loaded_media = int(load_state.get("loaded_media_count") or 0)
            post_like_links = int(load_state.get("post_like_link_count") or 0)
            reel_links = int(load_state.get("reel_link_count") or 0)
            visible_links = int(load_state.get("visible_link_count") or 0)
            scroll_height = int(load_state.get("scroll_height") or 0)
            signature = (post_like_links, reel_links, visible_links, visible_media, loaded_media, scroll_height)
            if signature == previous_signature:
                stable_count += 1
            else:
                stable_count = 0
                previous_signature = signature

            enough_links = post_like_links >= min_links or reel_links >= min_links or visible_links >= min_links
            enough_media_loaded = visible_media == 0 or loaded_media >= max(1, min(visible_media, min_links))
            page_complete = str(load_state.get("ready_state") or "").lower() == "complete"
            if page_complete and enough_links and enough_media_loaded and stable_count >= stable_needed:
                load_state["ready_reason"] = "stable_grid"
                return last_data
            if time.monotonic() >= deadline:
                load_state["ready_reason"] = "timeout"
                return last_data
            page.wait_for_timeout(poll_ms)

    def profile_reels_url(self, result: SocialResult, page_data: dict[str, Any]) -> str | None:
        if result.platform == "instagram":
            handle = (result.author_handle or "").lstrip("@")
            if handle:
                return f"https://www.instagram.com/{handle}/reels/"
            for link in page_data.get("reel_owner_links") or []:
                href = link.get("href") or ""
                score = int(link.get("score") or 0)
                if score < 120:
                    continue
                handle = link.get("instagram_handle") or ""
                if not handle:
                    match = re.search(r"instagram\.com/([^/?#]+)/?(?:[?#].*)?$", href)
                    if match:
                        handle = match.group(1)
                if (
                    handle
                    and looks_like_instagram_handle(handle)
                    and handle.lower() not in {"accounts", "explore", "p", "reel", "reels", "stories"}
                ):
                    return f"https://www.instagram.com/{handle.lstrip('@')}/reels/"

        if result.platform == "facebook":
            links = page_data.get("reel_owner_links") or []
            for link in links:
                href = link.get("href") or ""
                label = (link.get("label") or "").lower()
                text = (link.get("text") or "").lower()
                if not href or "facebook.com" not in href:
                    continue
                if "owner profile" in label or "owner profile" in text or "sk=reels_tab" in href or "profile.php" in href:
                    href = re.sub(r"([?&])__[^=&]+=[^&]*", "", href)
                    href = re.sub(r"([?&])__cft__\[[^\]]+\]=[^&]*", "", href)
                    href = re.sub(r"([?&])__tn__=[^&]*", "", href)
                    if "sk=" in href:
                        return re.sub(r"sk=[^&]+", "sk=reels_tab", href)
                    return href + ("&" if "?" in href else "?") + "sk=reels_tab"
        return None

    def metric_from_grid_match(self, match: Any) -> MetricValue:
        if not isinstance(match, dict):
            return MetricValue()
        candidates = [
            match.get("text"),
            match.get("label"),
            match.get("title"),
        ]
        for candidate in candidates:
            text = compact_spaces(candidate)
            if not text:
                continue
            direct = parse_count_token(text)
            if direct is not None:
                return MetricValue(raw=f"{text} (profile reels grid)", value=direct)
            extracted = extract_metric("views", [text])
            if extracted.value is not None:
                extracted.raw = f"{extracted.raw} (profile reels grid)"
                return extracted
        card_text = compact_spaces(match.get("card_text"))
        if card_text and not compact_spaces(match.get("text")):
            direct = parse_count_token(card_text)
            if direct is not None:
                return MetricValue(raw=f"{card_text} (profile reels grid)", value=direct)
        return MetricValue()


def write_data_markdown(results: list[SocialResult], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines: list[str] = [
        "# 社媒公开数据汇总",
        "",
        f"- 生成时间：{generated_at}",
        f"- 链接数量：{len(results)}",
        "",
        "> 说明：本报告只汇总页面公开展示或公开 HTML/JSON 中可读取的数据。平台限制、登录墙、地区或风控会导致字段显示为“未知”。",
        "",
        "## 总览",
        "",
        "| # | 平台 | 作者 | 发布时间 | 正文 | 点赞/反应 | 评论 | 分享/转发 | 收藏 | 浏览/播放 | 链接 | 状态 |",
        "|---:|---|---|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for index, result in enumerate(results, 1):
        author = result.author_handle or result.author_name or ""
        if result.author_name and result.author_handle:
            author = f"{result.author_name} ({result.author_handle})"
        status = "失败" if result.error else "成功"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(index),
                    markdown_escape_cell(result.platform),
                    markdown_escape_cell(author),
                    markdown_escape_cell(result.posted_at),
                    markdown_escape_cell(result.text or "未读取到正文"),
                    format_metric(result.metrics.get("likes")),
                    format_metric(result.metrics.get("comments")),
                    format_metric(result.metrics.get("shares")),
                    format_metric(result.metrics.get("saves")),
                    format_metric(result.metrics.get("views")),
                    f"[打开]({result.final_url or result.source_url})",
                    status,
                ]
            )
            + " |"
        )

    lines.extend(["", "## 明细", ""])
    for index, result in enumerate(results, 1):
        lines.extend(
            [
                f"### {index}. {result.platform}",
                "",
                f"- 原始链接：{result.source_url}",
                f"- 最终链接：{result.final_url or '未知'}",
                f"- 标题：{result.title or '未知'}",
                f"- 作者：{result.author_handle or result.author_name or '未知'}",
                f"- 发布时间：{result.posted_at or '未知'}",
            ]
        )
        for key, label in METRIC_LABELS.items():
            lines.append(f"- {label}：{format_metric(result.metrics.get(key))}")
        if result.error:
            lines.append(f"- 读取状态：失败，{result.error}")
        lines.extend(["", "正文：", "", markdown_quote(result.text), ""])

    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8-sig")


def write_text_markdown(results: list[SocialResult], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, result in enumerate(results, 1):
        lines.extend(["## " + str(index), "", result.text.strip() if result.text else "_未读取到正文_", ""])
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8-sig")


def backup_excel_file(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.backup-{timestamp}{path.suffix}")
    shutil.copy2(path, backup)
    return backup


def import_openpyxl_load_workbook() -> Any:
    try:
        from openpyxl import load_workbook

        return load_workbook
    except ImportError:
        original_path = list(sys.path)
        cwd = str(Path.cwd().resolve())
        sys.path = [
            entry
            for entry in sys.path
            if entry and str(Path(entry).resolve()) != cwd
        ]
        for name in list(sys.modules):
            if name == "openpyxl" or name.startswith("openpyxl.") or name == "defusedxml" or name.startswith("defusedxml."):
                sys.modules.pop(name, None)
        try:
            from openpyxl import load_workbook

            return load_workbook
        finally:
            sys.path = original_path


def cell_has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def looks_encoding_damaged(value: Any) -> bool:
    text = str(value or "")
    return "?" in text or "\ufffd" in text


def damaged_name_regex(name: str) -> re.Pattern[str]:
    parts: list[str] = []
    for char in name:
        if char == "?" or char == "\ufffd":
            parts.append(".*")
        else:
            parts.append(re.escape(char))
    return re.compile("^" + "".join(parts) + "$", re.I)


def newest_xlsx_in_dir(search_dir: Path, pattern: str = "*.xlsx", damaged_name: str = "") -> Path | None:
    if not search_dir.exists():
        return None
    files = sorted(
        (path for path in search_dir.glob(pattern) if path.is_file() and not path.name.startswith("~$")),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if damaged_name and looks_encoding_damaged(damaged_name):
        matcher = damaged_name_regex(damaged_name)
        matched = [path for path in files if matcher.match(path.name)]
        if matched:
            files = matched
    preferred = [path for path in files if ".backup-" not in path.name]
    if preferred:
        return preferred[0]
    return files[0] if files else None


def resolve_excel_path(excel_arg: Any) -> Path:
    path = Path(excel_arg).expanduser()
    if path.exists():
        return path.resolve()

    parent = path.parent if str(path.parent) not in {"", "."} else Path.cwd()
    if looks_encoding_damaged(path.name):
        fallback = newest_xlsx_in_dir(parent, damaged_name=path.name)
        if fallback:
            print(f"[excel-path-fallback] {path} not found; using newest workbook in {parent}: {fallback}", file=sys.stderr)
            return fallback.resolve()

    return path.resolve()


@dataclass(frozen=True)
class ExcelLinkCandidate:
    sheet_name: str
    column: int
    header: str
    rows_with_urls: int
    total_urls: int
    platform_rows: int
    multi_url_rows: int
    score: int


def excel_column_index(value: Any) -> int:
    text = str(value).strip()
    if text.isdigit():
        return max(1, int(text))
    total = 0
    for char in text.upper():
        if not ("A" <= char <= "Z"):
            raise ValueError(f"不是有效的 Excel 列：{value}")
        total = total * 26 + (ord(char) - ord("A") + 1)
    return max(1, total)


def excel_column_letter(index: int) -> str:
    if index < 1:
        raise ValueError(f"invalid Excel column index: {index}")
    letters: list[str] = []
    while index:
        index, remainder = divmod(index - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def copy_cell_style(source: Any, target: Any) -> None:
    if not source or not target:
        return
    if source.has_style:
        target._style = copy.copy(source._style)
    if source.number_format:
        target.number_format = source.number_format
    if source.alignment:
        target.alignment = copy.copy(source.alignment)


def last_value_column(ws: Any) -> int:
    last_col = 0
    for row in ws.iter_rows():
        for cell in row:
            if cell_has_value(cell.value):
                last_col = max(last_col, cell.column)
    return max(1, last_col)


def last_value_row(ws: Any) -> int:
    last_row = 0
    for row in ws.iter_rows():
        for cell in row:
            if cell_has_value(cell.value):
                last_row = max(last_row, cell.row)
    return max(1, last_row)


def excel_header_row(ws: Any, row: int, through_col: int) -> list[str]:
    return [str(ws.cell(row, col).value or "").strip() for col in range(1, through_col + 1)]


def normalized_header_key(value: Any) -> str:
    return re.sub(r"[\s\-_()/（）/]+", "", str(value or "").strip().lower())


def find_excel_header_column(ws: Any, header_row: int, candidates: tuple[str, ...]) -> int | None:
    headers = excel_header_row(ws, header_row, last_value_column(ws))
    candidate_keys = {normalized_header_key(candidate) for candidate in candidates}
    for index, header in enumerate(headers, 1):
        if header in candidates or normalized_header_key(header) in candidate_keys:
            return index
    return None


def find_owner_column(ws: Any, header_row: int) -> int | None:
    owner_col = find_excel_header_column(ws, header_row, OWNER_HEADER_CANDIDATES)
    if owner_col:
        return owner_col
    if last_value_column(ws) >= OWNER_FALLBACK_COLUMN:
        return OWNER_FALLBACK_COLUMN
    return None


def select_excel_worksheet(
    wb: Any,
    requested_sheet: str | None,
    header_row: int,
    start_row: int,
    *,
    need_link_candidate: bool,
) -> tuple[Any, ExcelLinkCandidate | None]:
    requested = str(requested_sheet or "").strip()
    if requested:
        if requested in wb.sheetnames:
            ws = wb[requested]
            candidate = find_best_excel_link_candidate([ws], header_row, start_row) if need_link_candidate else None
            return ws, candidate
        requested_key = normalized_header_key(requested)
        for sheet_name in wb.sheetnames:
            if normalized_header_key(sheet_name) == requested_key:
                ws = wb[sheet_name]
                candidate = find_best_excel_link_candidate([ws], header_row, start_row) if need_link_candidate else None
                return ws, candidate
        print(f"[excel-sheet-fallback] requested sheet not found: {requested}; auto-detecting worksheet", file=sys.stderr)

    if need_link_candidate:
        candidate = find_best_excel_link_candidate(wb.worksheets, header_row, start_row)
        return wb[candidate.sheet_name], candidate
    return wb.worksheets[0], None


def urls_from_excel_cell(value: Any) -> list[str]:
    if not cell_has_value(value):
        return []
    return collect_urls([str(value)], [])


def score_excel_link_column(ws: Any, header_row: int, start_row: int, column: int, last_row: int) -> ExcelLinkCandidate | None:
    rows_with_urls = 0
    total_urls = 0
    platform_rows = 0
    multi_url_rows = 0
    unique_urls: set[str] = set()
    for row in range(start_row, last_row + 1):
        urls = urls_from_excel_cell(ws.cell(row, column).value)
        if not urls:
            continue
        rows_with_urls += 1
        total_urls += len(urls)
        unique_urls.update(urls)
        if len(urls) > 1:
            multi_url_rows += 1
        if any(detect_platform(url) != "unknown" for url in urls):
            platform_rows += 1

    if platform_rows == 0:
        return None

    # Prefer original one-link-per-row columns. Penalize cumulative text columns
    # that repeat many URLs in each row, even if they contain valid links.
    score = platform_rows * 100 + min(len(unique_urls), platform_rows)
    score -= multi_url_rows * 50
    score -= max(0, total_urls - rows_with_urls) * 5
    header = str(ws.cell(header_row, column).value or "").strip()
    lowered_header = header.lower()
    if lowered_header in {"url", "urls", "link", "links"}:
        score += 25
    elif "url" in lowered_header or "link" in lowered_header:
        score += 10

    return ExcelLinkCandidate(
        sheet_name=ws.title,
        column=column,
        header=header,
        rows_with_urls=rows_with_urls,
        total_urls=total_urls,
        platform_rows=platform_rows,
        multi_url_rows=multi_url_rows,
        score=score,
    )


def find_best_excel_link_candidate(worksheets: list[Any], header_row: int, start_row: int) -> ExcelLinkCandidate:
    best: ExcelLinkCandidate | None = None
    for ws in worksheets:
        last_col = last_value_column(ws)
        last_row = last_value_row(ws)
        for column in range(1, last_col + 1):
            candidate = score_excel_link_column(ws, header_row, start_row, column, last_row)
            if candidate and (best is None or candidate.score > best.score):
                best = candidate
    if best is None:
        raise SystemExit("No supported social URLs found in the Excel workbook. Pass --sheet and --link-column if needed.")
    return best


def ensure_excel_metric_columns(ws: Any, header_row: int) -> dict[str, int]:
    used_last_col = last_value_column(ws)
    headers = excel_header_row(ws, header_row, used_last_col)
    column_map: dict[str, int] = {}
    for key, label in EXCEL_METRIC_COLUMNS:
        label_key = normalized_header_key(label)
        for index, header in enumerate(headers, 1):
            if header == label or normalized_header_key(header) == label_key:
                column_map[key] = index
                break

    if len(column_map) < len(EXCEL_METRIC_COLUMNS) and used_last_col >= max(EXCEL_METRIC_FALLBACK_COLUMNS.values()):
        for key, column in EXCEL_METRIC_FALLBACK_COLUMNS.items():
            if key not in column_map:
                column_map[key] = column

    if len(column_map) == len(EXCEL_METRIC_COLUMNS):
        return column_map

    append_col = used_last_col + 1
    style_source_col = used_last_col if used_last_col >= 1 else 1
    for key, label in EXCEL_METRIC_COLUMNS:
        if key in column_map:
            continue
        col = append_col
        append_col += 1
        cell = ws.cell(header_row, col, label)
        copy_cell_style(ws.cell(header_row, style_source_col), cell)
        column_map[key] = col
    return column_map


def excel_metric_value(result: SocialResult, key: str) -> int | None:
    if result.error:
        return None
    metric = result.metrics.get(key)
    return metric.value if metric and metric.value is not None else None


def write_excel_result_row(
    *,
    wb: Any,
    ws: Any,
    excel_path: Path,
    metric_columns: dict[str, int],
    owner_col: int | None,
    header_row: int,
    index: int,
    total: int,
    row: int,
    url: str,
    result: SocialResult,
) -> None:
    if result.error:
        owner = compact_spaces(str(ws.cell(row, owner_col).value)) if owner_col else ""
        owner_part = f" owner={owner}" if owner else ""
        print(f"[manual-review] row={row}{owner_part} url={url} error={result.error}", file=sys.stderr)
    for key, _label in EXCEL_METRIC_COLUMNS:
        target = ws.cell(row, metric_columns[key])
        value = excel_metric_value(result, key)
        target.value = value if value is not None else None
        if row > header_row + 1:
            copy_cell_style(ws.cell(row - 1, metric_columns[key]), target)
    wb.save(excel_path)
    print(f"[excel-write {index}/{total}] row={row} {url}", file=sys.stderr)


def build_scraper_from_args(args: argparse.Namespace) -> SocialScraper:
    return SocialScraper(
        use_playwright=not args.http_only,
        headless=not args.headful,
        timeout_ms=args.timeout_ms,
        delay_min_s=max(0.0, args.delay_min),
        delay_max_s=max(args.delay_min, args.delay_max),
        reel_grid_scrolls=max(0, args.reel_grid_scrolls),
        cdp_url=args.cdp_url,
        cdp_profile=args.cdp_profile,
        cdp_port=args.cdp_port,
        use_cdp_for_x=args.use_cdp_for_x,
        use_cdp_for_instagram=args.use_cdp_for_instagram,
        use_cdp_for_facebook=args.use_cdp_for_facebook,
        use_cdp_for_tiktok=args.use_cdp_for_tiktok,
        use_cdp_for_linkedin=args.use_cdp_for_linkedin,
        require_text_for_retry=args.require_text_for_retry,
        require_time_for_retry=args.require_time_for_retry,
        retry_missing_metrics=args.retry_missing_metrics,
        media_kind_aware_retry=args.media_kind_aware_retry,
        stable_reel_grid_wait=args.stable_reel_grid_wait,
        ignore_anti_bot_stop=args.ignore_anti_bot_stop,
        cdp_start_minimized=args.cdp_start_minimized,
        cdp_headless=args.cdp_headless,
        cdp_auto_close=args.cdp_auto_close,
        cdp_login_timeout_ms=args.cdp_login_timeout_ms,
        allow_manual_login=args.allow_manual_login,
    )


def run_excel_mode(args: argparse.Namespace) -> int:
    try:
        load_workbook = import_openpyxl_load_workbook()
    except ImportError as exc:
        raise SystemExit("缺少依赖 openpyxl。请先运行：python -m pip install openpyxl defusedxml") from exc

    excel_path = resolve_excel_path(args.excel)
    if not excel_path.exists():
        raise SystemExit(f"Excel 文件不存在：{excel_path}")

    header_row = max(1, args.header_row)
    start_row = max(header_row + 1, args.start_row)
    wb = load_workbook(excel_path)

    link_column_arg = str(args.link_column or "auto").strip()
    auto_link_column = link_column_arg.lower() in {"auto", "detect", ""}
    if auto_link_column:
        ws, candidate = select_excel_worksheet(wb, args.sheet, header_row, start_row, need_link_candidate=True)
        if candidate is None:
            raise SystemExit("No supported social URLs found in the Excel workbook. Pass --sheet and --link-column if needed.")
        link_col = candidate.column
    else:
        ws, candidate = select_excel_worksheet(wb, args.sheet, header_row, start_row, need_link_candidate=False)
        link_col = excel_column_index(link_column_arg)

    if candidate:
        print(
            "[excel-auto] "
            f"sheet={candidate.sheet_name} "
            f"link_column={excel_column_letter(candidate.column)} "
            f"header={candidate.header or '<blank>'} "
            f"rows_with_urls={candidate.rows_with_urls} "
            f"platform_rows={candidate.platform_rows} "
            f"total_urls={candidate.total_urls} "
            f"multi_url_rows={candidate.multi_url_rows} "
            f"score={candidate.score}",
            file=sys.stderr,
        )

    metric_columns = ensure_excel_metric_columns(ws, header_row)
    owner_col = find_owner_column(ws, header_row)
    last_row = last_value_row(ws)

    rows: list[tuple[int, str]] = []
    platform_filter = set(args.platform or [])
    for row in range(start_row, last_row + 1):
        urls = urls_from_excel_cell(ws.cell(row, link_col).value)
        if platform_filter:
            urls = [url for url in urls if detect_platform(url) in platform_filter]
        if not urls:
            continue
        if not args.force:
            existing = [ws.cell(row, metric_columns[key]).value for key, _label in EXCEL_METRIC_COLUMNS]
            if any(cell_has_value(value) for value in existing):
                continue
        rows.append((row, urls[0]))

    if not rows:
        print("没有需要抓取的 Excel 行。", file=sys.stderr)
        wb.close()
        return 0

    backup = backup_excel_file(excel_path)
    print(f"已备份 Excel：{backup}", file=sys.stderr)

    scraper = build_scraper_from_args(args)
    batch_urls = [url for _row, url in rows]

    def write_result(index: int, total: int, url: str, result: SocialResult) -> None:
        row, expected_url = rows[index - 1]
        if expected_url != url:
            print(
                f"[excel-write-warning] result URL mismatch row={row} expected={expected_url} actual={url}",
                file=sys.stderr,
            )
        write_excel_result_row(
            wb=wb,
            ws=ws,
            excel_path=excel_path,
            metric_columns=metric_columns,
            owner_col=owner_col,
            header_row=header_row,
            index=index,
            total=total,
            row=row,
            url=url,
            result=result,
        )

    try:
        scraper.scrape(batch_urls, on_result=write_result)
    finally:
        wb.close()
    print(f"Excel 已写回：{excel_path}", file=sys.stderr)
    print("已逐条保存；只写入 5 个指标列：点赞/反应、评论、分享/转发、收藏、浏览/播放量", file=sys.stderr)
    return 0


def run_self_test() -> None:
    cases = {
        "1,234 likes": ("likes", 1234),
        "2.5M views": ("views", 2_500_000),
        "8.2K comments": ("comments", 8200),
        "1.2万 点赞": ("likes", 12_000),
        "4 次查看": ("views", 4),
        "1.2萬 次查看": ("views", 12_000),
        "3 次转帖": ("shares", 3),
        "2 条回复": ("comments", 2),
        "0 个喜欢": ("likes", 0),
        "5 個書籤": ("saves", 5),
    }
    for source, (metric, expected) in cases.items():
        actual = extract_metric(metric, [source]).value
        if actual != expected:
            raise AssertionError(f"{source}: expected {expected}, got {actual}")
    assert detect_platform("https://x.com/a/status/1") == "x"
    assert detect_platform("https://www.facebook.com/reel/1") == "facebook"
    assert detect_platform("https://vt.tiktok.com/abc/") == "tiktok"
    assert detect_platform("https://www.instagram.com/reel/abc/") == "instagram"
    assert is_instagram_post_url("https://www.instagram.com/p/abc/")
    assert normalize_scrape_url("https://www.instagram.com/p/abc/?x=1") == "https://www.instagram.com/reel/abc/?x=1"
    no_instagram_rewrite = SocialScraper(
        use_playwright=True,
        headless=True,
        timeout_ms=30_000,
        delay_min_s=0,
        delay_max_s=0,
        reel_grid_scrolls=0,
        normalize_instagram_posts=False,
    )
    assert no_instagram_rewrite.scrape_url_for("https://www.instagram.com/p/abc/?x=1") == "https://www.instagram.com/p/abc/?x=1"
    no_manual_login = SocialScraper(
        use_playwright=True,
        headless=True,
        timeout_ms=30_000,
        delay_min_s=0,
        delay_max_s=0,
        reel_grid_scrolls=0,
        allow_manual_login=False,
    )
    assert no_manual_login.allow_manual_login is False
    assert reel_id_from_url("instagram", "https://www.instagram.com/reel/abc/?x=1") == "abc"
    assert is_reel_grid_candidate_url("instagram", "https://www.instagram.com/reel/abc/?x=1")
    assert not is_reel_grid_candidate_url("instagram", "https://www.instagram.com/p/abc/?x=1")
    assert is_reel_grid_candidate_url("facebook", "https://www.facebook.com/reel/123")
    assert not is_reel_grid_candidate_url("facebook", "https://www.facebook.com/photo/?fbid=123")
    assert (
        instagram_handle_from_description('0 likes, 0 comments - pengfeij.doc on May 28, 2026: "caption"')
        == "@pengfeij.doc"
    )
    assert (
        instagram_handle_from_description('0 likes, 0 comments - pengfeij.doc，May 28, 2026 : "caption"')
        == "@pengfeij.doc"
    )
    assert (
        instagram_handle_from_description('0 likes, 0 comments - pengfeij.doc?May 28, 2026 : "caption"')
        == "@pengfeij.doc"
    )
    assert instagram_caption_datetime('85 likes, 10 comments - xpengaustralia，December 16, 2024 : "caption"').startswith(
        "2024-12-16T00:00:00"
    )
    assert parse_social_datetime_text("6月20日12:22", now=datetime(2026, 6, 29, tzinfo=timezone.utc)).startswith(
        "2026-06-20T12:22:00"
    )
    assert parse_social_datetime_text("5 天前", now=datetime(2026, 6, 29, 8, 0, tzinfo=LOCAL_TIMEZONE)).startswith(
        "2026-06-24T08:00:00"
    )
    args = build_parser().parse_args([])
    assert args.use_cdp_for_instagram is True
    assert args.use_cdp_for_x is False
    assert args.use_cdp_for_facebook is False
    assert args.use_cdp_for_tiktok is False
    assert build_parser().parse_args(["--platform", "tiktok"]).platform == ["tiktok"]
    assert args.cdp_auto_close is True
    assert not is_login_page_data({"final_url": "https://www.tiktok.com/@a/video/1", "body_text": "Log in Sign up"}, "tiktok")
    assert is_login_page_data({"final_url": "https://www.tiktok.com/login", "body_text": ""}, "tiktok")
    scraper = SocialScraper(
        use_playwright=True,
        headless=True,
        timeout_ms=30_000,
        delay_min_s=0,
        delay_max_s=0,
        reel_grid_scrolls=0,
        use_cdp_for_tiktok=True,
    )
    assert scraper.should_use_cdp_for_url("https://www.instagram.com/reel/abc/")
    assert not scraper.should_use_cdp_for_url("https://www.facebook.com/reel/1")
    assert scraper.should_use_cdp_for_url("https://vt.tiktok.com/abc/")
    anti_bot_result = SocialResult(
        source_url="https://x.com/a/status/3",
        platform="x",
        final_url="https://x.com/a/status/3",
        metrics={key: MetricValue() for key in METRIC_LABELS},
    )
    mark_anti_bot_result(anti_bot_result, "captcha", stage="self-test")
    assert not scraper.result_needs_retry("https://x.com/a/status/3", anti_bot_result)
    ignore_antibot_scraper = SocialScraper(
        use_playwright=True,
        headless=True,
        timeout_ms=30_000,
        delay_min_s=0,
        delay_max_s=0,
        reel_grid_scrolls=0,
        ignore_anti_bot_stop=True,
        max_attempts=3,
    )
    assert ignore_antibot_scraper.result_needs_retry("https://x.com/a/status/3", anti_bot_result)
    assert "已停止继续刷新" not in ignore_antibot_scraper.manual_review_reason("https://x.com/a/status/3", anti_bot_result)
    ignore_antibot_scraper.ignore_anti_bot_for_retry(anti_bot_result)
    assert anti_bot_result.debug.get("ignored_anti_bot_marker") == "captcha"
    assert not result_anti_bot_detected(anti_bot_result)
    assert anti_bot_result.error is None
    stop_scraper = SocialScraper(
        use_playwright=False,
        headless=True,
        timeout_ms=30_000,
        delay_min_s=0,
        delay_max_s=0,
        reel_grid_scrolls=0,
    )
    stop_scraper.scrape_http = lambda url: SocialResult(
        source_url=url,
        platform=detect_platform(url),
        final_url=url,
        metrics={key: MetricValue() for key in METRIC_LABELS},
    )
    stopped_urls = []
    stop_results = stop_scraper.scrape(
        ["https://www.facebook.com/reel/1", "https://www.facebook.com/reel/2"],
        on_result=lambda _index, _total, url, _result: stopped_urls.append(url) or False,
    )
    assert stopped_urls == ["https://www.facebook.com/reel/1"]
    assert len(stop_results) == 1
    tiktok_partial = SocialResult(
        source_url="https://www.tiktok.com/@a/video/1",
        platform="tiktok",
        final_url="https://www.tiktok.com/@a/video/1",
        metrics={key: MetricValue() for key in METRIC_LABELS},
    )
    tiktok_partial.metrics["likes"] = MetricValue("1 like", 1)
    assert scraper.result_needs_retry("https://www.tiktok.com/@a/video/1", tiktok_partial)
    tiktok_partial.metrics["views"] = MetricValue("2 views", 2)
    assert not scraper.result_needs_retry("https://www.tiktok.com/@a/video/1", tiktok_partial)
    assert excel_metric_value(tiktok_partial, "views") == 2
    tiktok_partial.debug["playwright_error"] = "Page.goto: net::ERR_CONNECTION_TIMED_OUT"
    assert page_open_failure_reason(tiktok_partial)
    assert scraper.result_needs_retry("https://www.tiktok.com/@a/video/1", tiktok_partial)
    tiktok_partial.error = "TikTok 网页打开失败，请人工查阅"
    assert excel_metric_value(tiktok_partial, "views") is None
    tiktok_notfound = SocialResult(
        source_url="https://www.tiktok.com/t/abc/",
        platform="tiktok",
        final_url="https://www.tiktok.com/hk/notfound",
        title="TikTok",
        text="TikTok",
        metrics={key: MetricValue() for key in METRIC_LABELS},
        debug={"json_script_keys": []},
    )
    assert tiktok_unavailable_reason(tiktok_notfound) == "TikTok 页面跳转到 notfound"
    assert scraper.result_needs_retry("https://www.tiktok.com/t/abc/", tiktok_notfound)
    text_only_scraper = SocialScraper(
        use_playwright=False,
        headless=True,
        timeout_ms=30_000,
        delay_min_s=0,
        delay_max_s=0,
        reel_grid_scrolls=0,
        max_attempts=3,
        require_text_for_retry=True,
        retry_missing_metrics=True,
    )
    text_only_result = SocialResult(
        source_url="https://www.instagram.com/reel/abc/",
        platform="instagram",
        final_url="https://www.instagram.com/reel/abc/",
        text="caption",
        metrics={key: MetricValue() for key in METRIC_LABELS},
    )
    assert text_only_scraper.result_needs_retry("https://www.instagram.com/reel/abc/", text_only_result)
    assert not text_only_scraper.result_required_failure("https://www.instagram.com/reel/abc/", text_only_result)
    text_only_result.metrics["views"] = MetricValue("2 views", 2)
    assert not text_only_scraper.result_needs_retry("https://www.instagram.com/reel/abc/", text_only_result)
    image_post_result = SocialResult(
        source_url="https://www.instagram.com/p/abc/",
        platform="instagram",
        final_url="https://www.instagram.com/p/abc/",
        text="caption",
        metrics={key: MetricValue() for key in METRIC_LABELS},
        debug={"page_kind": "post"},
    )
    assert text_only_scraper.result_needs_retry("https://www.instagram.com/p/abc/", image_post_result)
    weekly_image_post_scraper = SocialScraper(
        use_playwright=False,
        headless=True,
        timeout_ms=30_000,
        delay_min_s=0,
        delay_max_s=0,
        reel_grid_scrolls=0,
        max_attempts=3,
        require_text_for_retry=True,
        retry_missing_metrics=True,
        media_kind_aware_retry=True,
    )
    assert not weekly_image_post_scraper.result_needs_retry("https://www.instagram.com/p/abc/", image_post_result)
    no_text_result = SocialResult(
        source_url="https://www.instagram.com/reel/def/",
        platform="instagram",
        final_url="https://www.instagram.com/reel/def/",
        metrics={key: MetricValue() for key in METRIC_LABELS},
    )
    assert text_only_scraper.result_required_failure("https://www.instagram.com/reel/def/", no_text_result)
    x_partial = SocialResult(
        source_url="https://x.com/a/status/1",
        platform="x",
        metrics={key: MetricValue() for key in METRIC_LABELS},
    )
    x_partial.metrics["likes"] = MetricValue("1 like", 1)
    assert scraper.result_needs_retry("https://x.com/a/status/1", x_partial)
    x_partial.metrics["views"] = MetricValue("2 views", 2)
    assert scraper.result_needs_retry("https://x.com/a/status/1", x_partial)
    x_partial.text = "hello"
    x_partial.posted_at = "2026-05-29T00:00:00Z"
    assert not scraper.result_needs_retry("https://x.com/a/status/1", x_partial)
    x_zero = SocialResult(
        source_url="https://x.com/a/status/2",
        platform="x",
        text="all metrics hidden",
        posted_at="2026-05-29T00:00:00Z",
        metrics={key: MetricValue() for key in METRIC_LABELS},
    )
    default_missing_x_metrics_to_zero(x_zero)
    assert all(metric.value == 0 for metric in x_zero.metrics.values())
    x_chinese_raw = {
        "labels": ["4 次查看", "2 条回复", "3 次转帖", "0 个喜欢", "1 个书签"],
        "byTestId": {},
        "articleText": "",
        "analyticsText": "",
    }
    assert extract_metric("views", x_metric_sources(x_chinese_raw, "views")).value == 4
    assert extract_metric("comments", x_metric_sources(x_chinese_raw, "comments")).value == 2
    assert extract_metric("shares", x_metric_sources(x_chinese_raw, "shares")).value == 3
    assert extract_metric("likes", x_metric_sources(x_chinese_raw, "likes")).value == 0
    assert extract_metric("saves", x_metric_sources(x_chinese_raw, "saves")).value == 1
    print("self-test passed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓取 X、Facebook Reel、TikTok、Instagram Reel 链接的公开数据并输出 Markdown。",
    )
    parser.add_argument("urls", nargs="*", help="一个或多个社媒链接")
    parser.add_argument("-i", "--input", action="append", type=Path, default=[], help="包含链接的文本文件，可重复传入")
    parser.add_argument("--keep-duplicates", action="store_true", help="保留重复 URL；用于按行回填的工作流。")
    parser.add_argument("--excel", type=Path, help="Excel 批处理模式：读取链接列并在同一行写回 5 个指标")
    parser.add_argument("--sheet", help="Excel 工作表名称；默认第一个工作表")
    parser.add_argument("--link-column", default="auto", help="Excel link column, default auto; use A/B/1 or auto")
    parser.add_argument("--header-row", type=int, default=1, help="Excel 表头行，默认 1")
    parser.add_argument("--start-row", type=int, default=2, help="Excel 起始数据行，默认 2")
    parser.add_argument("--force", action="store_true", help="Excel 模式下强制重抓，覆盖已填写指标")
    parser.add_argument(
        "--platform",
        action="append",
        choices=["x", "facebook", "tiktok", "instagram"],
        help="Excel 模式下只抓指定平台；可重复传入。例如：--platform tiktok",
    )
    parser.add_argument("-o", "--output", type=Path, help="数据 Markdown 输出路径；默认保存到系统文档目录当天文件夹")
    parser.add_argument("--text-output", type=Path, help="正文 Markdown 输出路径；默认同目录同名 _text.md")
    parser.add_argument("--json-output", type=Path, help="可选：保存结构化 JSON")
    parser.add_argument("--http-only", action="store_true", help="只读取公开 HTML，不启用 Playwright 浏览器")
    parser.add_argument("--headful", action="store_true", help="Playwright 模式下显示浏览器窗口")
    parser.add_argument("--timeout-ms", type=int, default=30_000, help="单页加载超时，默认 30000")
    parser.add_argument("--delay-min", type=float, default=3.0, help="链接之间随机等待下限，默认 3")
    parser.add_argument("--delay-max", type=float, default=8.0, help="链接之间随机等待上限，默认 8")
    parser.add_argument(
        "--reel-grid-scrolls",
        type=int,
        default=6,
        help="Facebook/Instagram 详情页无播放量时，进入作者 Reels 网格查找目标缩略图的最多滚动次数，默认 6",
    )
    parser.add_argument("--self-test", action="store_true", help="运行解析自检，不访问网络")
    parser.add_argument("--cdp-url", help="Connect to an existing Chrome CDP endpoint, for example http://127.0.0.1:9222")
    parser.add_argument(
        "--cdp-profile",
        type=Path,
        default=DEFAULT_CDP_PROFILE,
        help=f"Reusable Chrome CDP user-data directory. Default: {DEFAULT_CDP_PROFILE}",
    )
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT, help="Chrome CDP port. Default: 9222")
    parser.add_argument(
        "--use-cdp-for-x",
        action="store_true",
        help="Use the reusable Chrome CDP profile for X/Twitter links. Default: off",
    )
    parser.add_argument(
        "--use-cdp-for-facebook",
        action="store_true",
        help="Use the reusable Chrome CDP profile for Facebook links when explicitly requested. Default: off",
    )
    parser.add_argument(
        "--use-cdp-for-tiktok",
        action="store_true",
        help="Use the reusable Chrome CDP profile for TikTok links when explicitly requested. Default: off",
    )
    parser.add_argument(
        "--use-cdp-for-instagram",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use the reusable Chrome CDP profile for Instagram links. Default: on",
    )
    parser.add_argument(
        "--use-cdp-for-linkedin",
        action="store_true",
        help="Use the reusable Chrome CDP profile for LinkedIn links when explicitly requested. Default: off",
    )
    parser.add_argument("--require-text-for-retry", action="store_true", help="Retry when detail text/title is missing.")
    parser.add_argument("--require-time-for-retry", action="store_true", help="Retry when publish time is missing.")
    parser.add_argument("--retry-missing-metrics", action="store_true", help="Retry when no known metric is found.")
    parser.add_argument("--media-kind-aware-retry", action="store_true", help="Avoid metric-only retry for media kinds that usually lack views.")
    parser.add_argument("--stable-reel-grid-wait", action="store_true", help="Wait for Facebook/Instagram Reels grids to stabilize before reading thumbnail metrics.")
    parser.add_argument("--ignore-anti-bot-stop", action="store_true", help="Do not stop the remaining batch when an anti-bot marker is detected.")
    parser.add_argument(
        "--cdp-start-minimized",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Minimize Chrome when this script starts the CDP profile. Default: on",
    )
    parser.add_argument(
        "--cdp-headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run the reusable Chrome CDP profile without a visible window. Default: off",
    )
    parser.add_argument(
        "--cdp-auto-close",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Close Chrome after scraping only if this run started it. Default: on",
    )
    parser.add_argument(
        "--allow-manual-login",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow opening a visible Chrome window when login is required. Default: on",
    )
    parser.add_argument(
        "--cdp-login-timeout-ms",
        type=int,
        default=300_000,
        help="How long to wait for manual login when a login wall is detected. Default: 300000",
    )
    parser.add_argument(
        "--start-cdp-profile",
        action="store_true",
        help="Open the reusable Chrome CDP profile for manual login, then exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.start_cdp_profile:
        ensure_cdp_profile(
            args.cdp_profile,
            args.cdp_port,
            "https://www.instagram.com/accounts/login/",
            start_minimized=False,
            auto_close=False,
        )
        print("Chrome CDP profile is open. Log in there, then rerun the scrape.", file=sys.stderr)
        return 0
    if args.self_test:
        run_self_test()
        return 0

    if args.excel:
        return run_excel_mode(args)

    urls = collect_urls(args.urls, args.input, keep_duplicates=args.keep_duplicates)
    if not urls:
        parser.error("请提供 X、Facebook Reel、TikTok 或 Instagram Reel 链接")

    if args.output is None:
        args.output = default_output_path(urls)
    if args.text_output is None:
        args.text_output = default_text_output_path(args.output)

    scraper = build_scraper_from_args(args)
    results = scraper.scrape(urls)
    write_data_markdown(results, args.output)
    write_text_markdown(results, args.text_output)
    print(f"数据 Markdown 已写入：{args.output}", file=sys.stderr)
    print(f"正文 Markdown 已写入：{args.text_output}", file=sys.stderr)

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps([result.to_jsonable() for result in results], ensure_ascii=False, indent=2),
            encoding="utf-8-sig",
        )
        print(f"JSON 已写入：{args.json_output}", file=sys.stderr)

    failures = sum(1 for result in results if result.error)
    return 1 if results and failures == len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
