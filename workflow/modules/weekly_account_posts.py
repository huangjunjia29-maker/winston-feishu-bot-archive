#!/usr/bin/env python3
"""Date-range overseas-market account post collection workflow."""

from __future__ import annotations

import json
import importlib.util
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse, urlunparse


DEFAULT_BASE_TOKEN = ""
DEFAULT_SOURCE_TABLE_ID = "海外账号表"
DEFAULT_SOURCE_VIEW_ID = "全部账号"
DEFAULT_IDENTITY = "bot"
MODULE_DIR = Path(__file__).resolve().parent
SOURCE_FIELDS = ("账号名称", "国家", "账号链接", "备注", "属性", "大区")
RESULT_FIELDS = (
    "账号",
    "发帖链接",
    "正文内容",
    "发布日期",
    "曝光",
    "点赞",
    "收藏",
    "评论",
    "转发",
)
LEGACY_RESULT_FIELDS = frozenset({"社交媒体数据"})
RESULT_WRITE_FIELDS = tuple(field for field in RESULT_FIELDS if field not in LEGACY_RESULT_FIELDS)
RESULT_TABLE_FIELDS = [
    {"type": "text", "name": "账号"},
    {"type": "text", "name": "发帖链接", "style": {"type": "url"}},
    {"type": "text", "name": "正文内容"},
    {"type": "datetime", "name": "发布日期", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"type": "number", "name": "曝光", "style": {"type": "plain", "precision": 0, "thousands_separator": True}},
    {"type": "number", "name": "点赞", "style": {"type": "plain", "precision": 0, "thousands_separator": True}},
    {"type": "number", "name": "收藏", "style": {"type": "plain", "precision": 0, "thousands_separator": True}},
    {"type": "number", "name": "评论", "style": {"type": "plain", "precision": 0, "thousands_separator": True}},
    {"type": "number", "name": "转发", "style": {"type": "plain", "precision": 0, "thousands_separator": True}},
]
PLATFORM_HOSTS = {
    "x": ("x.com", "twitter.com"),
    "linkedin": ("linkedin.com",),
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com", "fb.watch"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
}
LOCAL_TIMEZONE = timezone(timedelta(hours=8))


JsonRunner = Callable[[list[str], int], dict[str, Any]]
ProgressCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class PlatformScrapePolicy:
    page_delay_min_s: float
    page_delay_max_s: float
    detail_delay_min_s: float
    detail_delay_max_s: float
    account_delay_min_s: float
    account_delay_max_s: float
    retry_delay_min_s: float
    retry_delay_max_s: float
    candidate_limit: int
    detail_candidate_limit: int
    max_scrolls: int
    idle_scrolls: int
    detail_max_attempts: int = 3
    retry_missing_metrics: bool = False


DEFAULT_PLATFORM_POLICY = PlatformScrapePolicy(
    page_delay_min_s=1.5,
    page_delay_max_s=4.0,
    detail_delay_min_s=2.5,
    detail_delay_max_s=6.0,
    account_delay_min_s=3.0,
    account_delay_max_s=7.0,
    retry_delay_min_s=4.0,
    retry_delay_max_s=9.0,
    candidate_limit=40,
    detail_candidate_limit=24,
    max_scrolls=12,
    idle_scrolls=2,
)


PLATFORM_POLICIES: dict[str, PlatformScrapePolicy] = {
    "x": PlatformScrapePolicy(
        page_delay_min_s=2.0,
        page_delay_max_s=5.0,
        detail_delay_min_s=3.0,
        detail_delay_max_s=7.0,
        account_delay_min_s=3.0,
        account_delay_max_s=7.0,
        retry_delay_min_s=4.0,
        retry_delay_max_s=9.0,
        candidate_limit=80,
        detail_candidate_limit=80,
        max_scrolls=50,
        idle_scrolls=3,
        retry_missing_metrics=True,
    ),
    "instagram": PlatformScrapePolicy(
        page_delay_min_s=1.5,
        page_delay_max_s=4.0,
        detail_delay_min_s=2.5,
        detail_delay_max_s=6.0,
        account_delay_min_s=3.0,
        account_delay_max_s=7.0,
        retry_delay_min_s=4.0,
        retry_delay_max_s=9.0,
        candidate_limit=24,
        detail_candidate_limit=18,
        max_scrolls=8,
        idle_scrolls=2,
        detail_max_attempts=2,
    ),
    "facebook": PlatformScrapePolicy(
        page_delay_min_s=1.5,
        page_delay_max_s=4.0,
        detail_delay_min_s=2.5,
        detail_delay_max_s=6.0,
        account_delay_min_s=3.0,
        account_delay_max_s=7.0,
        retry_delay_min_s=4.0,
        retry_delay_max_s=9.0,
        candidate_limit=24,
        detail_candidate_limit=12,
        max_scrolls=6,
        idle_scrolls=2,
        detail_max_attempts=2,
    ),
    "tiktok": PlatformScrapePolicy(
        page_delay_min_s=1.5,
        page_delay_max_s=4.0,
        detail_delay_min_s=2.5,
        detail_delay_max_s=6.0,
        account_delay_min_s=3.0,
        account_delay_max_s=7.0,
        retry_delay_min_s=4.0,
        retry_delay_max_s=9.0,
        candidate_limit=24,
        detail_candidate_limit=18,
        max_scrolls=8,
        idle_scrolls=2,
        detail_max_attempts=2,
    ),
    "linkedin": PlatformScrapePolicy(
        page_delay_min_s=2.0,
        page_delay_max_s=5.0,
        detail_delay_min_s=0.0,
        detail_delay_max_s=0.0,
        account_delay_min_s=3.0,
        account_delay_max_s=6.0,
        retry_delay_min_s=3.0,
        retry_delay_max_s=6.0,
        candidate_limit=32,
        detail_candidate_limit=32,
        max_scrolls=14,
        idle_scrolls=2,
    ),
    "youtube": PlatformScrapePolicy(
        page_delay_min_s=0.0,
        page_delay_max_s=0.0,
        detail_delay_min_s=0.0,
        detail_delay_max_s=0.0,
        account_delay_min_s=1.0,
        account_delay_max_s=3.0,
        retry_delay_min_s=2.0,
        retry_delay_max_s=5.0,
        candidate_limit=20,
        detail_candidate_limit=20,
        max_scrolls=0,
        idle_scrolls=0,
    ),
}


def platform_policy(platform: str) -> PlatformScrapePolicy:
    return PLATFORM_POLICIES.get(platform, DEFAULT_PLATFORM_POLICY)


def quartile_milestones(total: int) -> list[tuple[int, int]]:
    if total <= 1:
        return []
    return [(max(1, (total * percent + 99) // 100), percent) for percent in (25, 50, 75)]


def no_post_placeholder(account: AccountRow) -> WeeklyPost:
    return WeeklyPost(
        account=account.account or account.url,
        url="",
        text="",
        published_at="",
        metrics={},
        platform=account.platform,
        source_account_url=account.url,
        placeholder=True,
    )


def import_local_module(module_name: str, filename: str) -> Any:
    full_name = f'weekly_account_{module_name}'
    if full_name in sys.modules:
        return sys.modules[full_name]
    module_path = MODULE_DIR / filename
    spec = importlib.util.spec_from_file_location(full_name, module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f'Cannot import local module: {module_path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def cdp_profile_path(workdir: Path) -> Path:
    raw = os.getenv('WEEKLY_ACCOUNT_CDP_PROFILE') or os.getenv('WEEKLY_ACCOUNT_X_CDP_PROFILE') or os.getenv('SOCIAL_CDP_PROFILE')
    if raw:
        return Path(raw).expanduser()
    return workdir / 'workflow' / 'assets' / 'chrome-cdp-profile'


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


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {'1', 'true', 'yes', 'on'}:
        return True
    if value in {'0', 'false', 'no', 'off', ''}:
        return False
    return default


def env_int_or_none(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw == '':
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def normalized_delay_range(min_s: float, max_s: float) -> tuple[float, float]:
    low = max(0.0, min_s)
    high = max(low, max_s)
    return low, high


def platform_env_float(platform: str, suffix: str, global_name: str, default: float) -> float:
    platform_name = f'WEEKLY_ACCOUNT_{platform.upper()}_{suffix}'
    if os.getenv(platform_name) is not None:
        return env_float(platform_name, default)
    return env_float(global_name, default)


def timeline_page_delay_s(platform: str, latest_per_account: int | None = None) -> tuple[float, float]:
    del latest_per_account
    policy = platform_policy(platform)
    min_s = platform_env_float(platform, 'PAGE_DELAY_MIN', 'WEEKLY_ACCOUNT_TIMELINE_PAGE_DELAY_MIN', policy.page_delay_min_s)
    max_s = platform_env_float(platform, 'PAGE_DELAY_MAX', 'WEEKLY_ACCOUNT_TIMELINE_PAGE_DELAY_MAX', policy.page_delay_max_s)
    return normalized_delay_range(min_s, max_s)


def detail_delay_s(platform: str) -> tuple[float, float]:
    policy = platform_policy(platform)
    min_s = platform_env_float(platform, 'DETAIL_DELAY_MIN', 'WEEKLY_ACCOUNT_DETAIL_DELAY_MIN', policy.detail_delay_min_s)
    max_s = platform_env_float(platform, 'DETAIL_DELAY_MAX', 'WEEKLY_ACCOUNT_DETAIL_DELAY_MAX', policy.detail_delay_max_s)
    return normalized_delay_range(min_s, max_s)


def account_delay_s(platform: str) -> tuple[float, float]:
    platform_min = f'WEEKLY_ACCOUNT_{platform.upper()}_ACCOUNT_DELAY_MIN'
    platform_max = f'WEEKLY_ACCOUNT_{platform.upper()}_ACCOUNT_DELAY_MAX'
    policy = platform_policy(platform)
    default_min = policy.account_delay_min_s
    default_max = policy.account_delay_max_s
    if os.getenv(platform_min) is not None:
        min_s = env_float(platform_min, default_min)
    else:
        min_s = env_float('WEEKLY_ACCOUNT_ACCOUNT_DELAY_MIN', env_float('WEEKLY_ACCOUNT_DELAY_MIN', default_min))
    if os.getenv(platform_max) is not None:
        max_s = env_float(platform_max, default_max)
    else:
        max_s = env_float('WEEKLY_ACCOUNT_ACCOUNT_DELAY_MAX', env_float('WEEKLY_ACCOUNT_DELAY_MAX', default_max))
    return normalized_delay_range(min_s, max_s)


def sleep_between_accounts(account: AccountRow) -> None:
    if env_bool('WEEKLY_ACCOUNT_DISABLE_ACCOUNT_DELAY', False):
        return
    min_s, max_s = account_delay_s(account.platform)
    if max_s <= 0:
        return
    delay = random.uniform(min_s, max_s)
    if delay <= 0:
        return
    print(
        f"[account-wait] platform={account.platform} account={account.account} delay_s={delay:.1f}",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(delay)


def sleep_between_retries(platform: str, attempt: int) -> None:
    policy = platform_policy(platform)
    default_min = policy.retry_delay_min_s
    default_max = policy.retry_delay_max_s
    min_s = platform_env_float(platform, 'DISCOVERY_RETRY_DELAY_MIN', 'WEEKLY_ACCOUNT_DISCOVERY_RETRY_DELAY_MIN', default_min)
    max_s = platform_env_float(platform, 'DISCOVERY_RETRY_DELAY_MAX', 'WEEKLY_ACCOUNT_DISCOVERY_RETRY_DELAY_MAX', default_max)
    low, high = normalized_delay_range(min_s, max_s)
    delay = random.uniform(low, high) if high > 0 else 0.0
    if delay <= 0:
        return
    print(
        f"[account-retry-wait] platform={platform} attempt={attempt} delay_s={delay:.1f}",
        file=sys.stderr,
        flush=True,
    )
    time.sleep(delay)


def account_discovery_max_attempts(platform: str) -> int:
    return max(1, env_int(f'WEEKLY_ACCOUNT_{platform.upper()}_DISCOVERY_MAX_ATTEMPTS', env_int('WEEKLY_ACCOUNT_DISCOVERY_MAX_ATTEMPTS', 3)))


def account_abnormal_error(platform: str, reason: str | None = None, attempts: int = 3) -> str:
    label = {
        'x': 'X',
        'instagram': 'Instagram',
        'facebook': 'Facebook',
        'tiktok': 'TikTok',
        'linkedin': 'LinkedIn',
        'youtube': 'YouTube',
    }.get(platform, platform or '社媒账号')
    detail = compact_error_text(reason or '')
    suffix = f'：{detail}' if detail else ''
    return f'{label} 账号链接连续 {max(1, attempts)} 次无法获取有效信息，已标记账号异常{suffix}'


ANTIBOT_ERROR_MARKERS = (
    'anti-bot',
    'captcha',
    'recaptcha',
    'hcaptcha',
    'security check',
    'unusual activity',
    'automated requests',
    'automated access',
    'automated behavior',
    'verify you are human',
    'are you a robot',
    'browser challenge',
    'just a moment',
    'li.protechts.net',
    'checkpoint',
)


def compact_error_text(value: Any, limit: int = 220) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + '…'


def is_antibot_error(value: Any) -> bool:
    text = str(value or '').lower()
    return any(marker in text for marker in ANTIBOT_ERROR_MARKERS)


def result_has_antibot(result: Any) -> bool:
    debug = getattr(result, 'debug', {})
    if isinstance(debug, dict) and debug.get('anti_bot_detected'):
        return True
    return is_antibot_error(getattr(result, 'error', ''))


def timeline_candidate_limit(platform: str) -> int:
    platform_limit = env_int_or_none(f'WEEKLY_ACCOUNT_{platform.upper()}_CANDIDATE_LIMIT')
    if platform_limit is not None:
        return platform_limit
    global_limit = env_int_or_none('WEEKLY_ACCOUNT_TIMELINE_CANDIDATE_LIMIT')
    if global_limit is not None:
        return global_limit
    return platform_policy(platform).candidate_limit


def timeline_detail_candidate_limit(platform: str, latest_per_account: int | None = None) -> int | None:
    if latest_per_account:
        return max(latest_per_account, latest_per_account * 2)
    platform_limit = env_int_or_none(f'WEEKLY_ACCOUNT_{platform.upper()}_DETAIL_CANDIDATE_LIMIT')
    if platform_limit is not None:
        return None if platform_limit <= 0 else platform_limit
    global_limit = env_int_or_none('WEEKLY_ACCOUNT_DETAIL_CANDIDATE_LIMIT')
    if global_limit is not None:
        return None if global_limit <= 0 else global_limit
    return platform_policy(platform).detail_candidate_limit


def detail_max_attempts(platform: str) -> int:
    policy = platform_policy(platform)
    return max(
        1,
        env_int(
            f'WEEKLY_ACCOUNT_{platform.upper()}_DETAIL_MAX_ATTEMPTS',
            env_int('WEEKLY_ACCOUNT_DETAIL_MAX_ATTEMPTS', policy.detail_max_attempts),
        ),
    )


def detail_retry_missing_metrics(platform: str) -> bool:
    name = f'WEEKLY_ACCOUNT_{platform.upper()}_DETAIL_RETRY_MISSING_METRICS'
    if os.getenv(name) is not None:
        return env_bool(name, platform_policy(platform).retry_missing_metrics)
    return env_bool('WEEKLY_ACCOUNT_DETAIL_RETRY_MISSING_METRICS', platform_policy(platform).retry_missing_metrics)


def timeline_max_scrolls(platform: str, latest_per_account: int | None) -> int:
    if latest_per_account:
        default_value = env_int('WEEKLY_ACCOUNT_LATEST_TIMELINE_MAX_SCROLLS', 6)
    else:
        default_value = env_int('WEEKLY_ACCOUNT_TIMELINE_MAX_SCROLLS', platform_policy(platform).max_scrolls)
    return env_int(f'WEEKLY_ACCOUNT_{platform.upper()}_MAX_SCROLLS', default_value)


def timeline_idle_scrolls(platform: str, latest_per_account: int | None) -> int:
    if latest_per_account:
        default_value = env_int('WEEKLY_ACCOUNT_LATEST_TIMELINE_IDLE_SCROLLS', 2)
    else:
        default_value = env_int('WEEKLY_ACCOUNT_TIMELINE_IDLE_SCROLLS', platform_policy(platform).idle_scrolls)
    return env_int(f'WEEKLY_ACCOUNT_{platform.upper()}_IDLE_SCROLLS', default_value)


def timeline_total_timeout_ms(platform: str, latest_per_account: int | None) -> int:
    platform_value = env_int_or_none(f'WEEKLY_ACCOUNT_{platform.upper()}_TOTAL_TIMEOUT_MS')
    if platform_value is not None:
        return platform_value
    global_value = env_int_or_none('WEEKLY_ACCOUNT_TIMELINE_TOTAL_TIMEOUT_MS')
    if global_value is not None:
        return global_value
    timeout_ms = env_int(f'WEEKLY_ACCOUNT_{platform.upper()}_TIMEOUT_MS', env_int('WEEKLY_ACCOUNT_TIMELINE_TIMEOUT_MS', 30000))
    max_scrolls = timeline_max_scrolls(platform, latest_per_account)
    _delay_min_s, delay_max_s = timeline_page_delay_s(platform, latest_per_account)
    pacing_timeout_ms = timeout_ms + int(delay_max_s * 1000) * (max_scrolls + 3)
    floor_ms = env_int('WEEKLY_ACCOUNT_LATEST_TIMELINE_TOTAL_TIMEOUT_MS', 45_000) if latest_per_account else 60_000
    return max(floor_ms, timeout_ms * 3, pacing_timeout_ms)


def weekly_cdp_start_minimized(platform: str | None = None) -> bool:
    if platform:
        name = f'WEEKLY_ACCOUNT_{platform.upper()}_CDP_START_MINIMIZED'
        if os.getenv(name) is not None:
            return env_bool(name, True)
    return env_bool('WEEKLY_ACCOUNT_CDP_START_MINIMIZED', True)


def reel_grid_scrolls(platform: str) -> int:
    default_value = 6 if platform in {'instagram', 'facebook'} else 0
    return env_int(f'WEEKLY_ACCOUNT_{platform.upper()}_REEL_GRID_SCROLLS', env_int('WEEKLY_ACCOUNT_REEL_GRID_SCROLLS', default_value))


def enable_reel_grid_views(platform: str) -> bool:
    default_value = 1 if platform in {'instagram', 'facebook'} else 0
    return bool(env_int(f'WEEKLY_ACCOUNT_{platform.upper()}_ENABLE_REEL_GRID_VIEWS', env_int('WEEKLY_ACCOUNT_ENABLE_REEL_GRID_VIEWS', default_value)))


def linkedin_feed_env_float(name: str, default: float) -> float:
    return env_float(f'WEEKLY_ACCOUNT_LINKEDIN_FEED_{name}', default)


def linkedin_feed_env_int(name: str, default: int) -> int:
    return env_int(f'WEEKLY_ACCOUNT_LINKEDIN_FEED_{name}', default)


def linkedin_feed_env_bool(name: str, default: bool) -> bool:
    return env_bool(f'WEEKLY_ACCOUNT_LINKEDIN_FEED_{name}', default)


def parse_platform_filter(value: str | None) -> set[str]:
    text = str(value or '').strip()
    if not text:
        return set()
    aliases = {
        'twitter': 'x',
        'tweet': 'x',
        'tweets': 'x',
        'tk': 'tiktok',
        'tt': 'tiktok',
        'ig': 'instagram',
        'ins': 'instagram',
        'insta': 'instagram',
        'fb': 'facebook',
        'yt': 'youtube',
        'youtube': 'youtube',
        'li': 'linkedin',
        'linkedin': 'linkedin',
        'x': 'x',
        'tiktok': 'tiktok',
        'facebook': 'facebook',
        'instagram': 'instagram',
    }
    selected: set[str] = set()
    for item in re.split(r'[,，\s]+', text.lower()):
        if not item:
            continue
        platform = aliases.get(item)
        if not platform:
            raise ValueError(f'未知平台筛选：{item}')
        selected.add(platform)
    return selected


def parse_source_index_filter(value: str | None) -> set[int]:
    text = str(value or '').strip()
    if not text:
        return set()
    selected: set[int] = set()
    for item in re.split(r'[,，\s]+', text):
        if not item:
            continue
        if '-' in item:
            start_text, end_text = item.split('-', 1)
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError as exc:
                raise ValueError(f'未知源序号筛选：{item}') from exc
            if start <= 0 or end <= 0 or end < start:
                raise ValueError(f'源序号筛选范围错误：{item}')
            selected.update(range(start, end + 1))
            continue
        try:
            number = int(item)
        except ValueError as exc:
            raise ValueError(f'未知源序号筛选：{item}') from exc
        if number <= 0:
            raise ValueError(f'源序号筛选必须大于 0：{item}')
        selected.add(number)
    return selected


@dataclass(frozen=True)
class WeekRange:
    start: date
    end: date

    @property
    def end_exclusive(self) -> date:
        return self.end + timedelta(days=1)

    @property
    def table_name(self) -> str:
        return f"{self.start:%Y.%m.%d}-{self.end:%Y.%m.%d}"

    @property
    def label(self) -> str:
        return f"{self.start:%Y-%m-%d} - {self.end:%Y-%m-%d}"


def result_table_name(week: WeekRange, latest_per_account: int | None = None) -> str:
    if latest_per_account and latest_per_account > 0:
        return f"{week.table_name}-latest{latest_per_account}"
    return week.table_name


def metric_number(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"([\d,.]+(?:\.\d+)?)\s*([KkMmBb]|万|萬|亿|億)?", text)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = match.group(2) or ""
    multiplier = {
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
        "万": 10_000,
        "萬": 10_000,
        "亿": 100_000_000,
        "億": 100_000_000,
    }.get(suffix.lower() if suffix.isascii() else suffix, 1)
    return int(number * multiplier)


@dataclass
class AccountRow:
    account: str
    country: str
    url: str
    note: str
    attribute: str
    region: str
    platform: str
    record_id: str = ""
    source_order: int = 0


@dataclass
class WeeklyPost:
    account: str
    url: str
    text: str
    published_at: str
    metrics: dict[str, Any] = field(default_factory=dict)
    platform: str = ""
    source_account_url: str = ""
    error: str = ""
    placeholder: bool = False

    def metric_value(self, *keys: str) -> int | None:
        for key in keys:
            parsed = metric_number(self.metrics.get(key))
            if parsed is not None:
                return parsed
        return None

    def to_row(self) -> list[Any]:
        if self.placeholder:
            return [self.account, None, None, None, None, None, None, None, None]
        return [
            self.account,
            self.url,
            self.text,
            self.published_at or None,
            self.metric_value("views", "plays", "impressions", "exposure", "exposures"),
            self.metric_value("likes", "reactions"),
            self.metric_value("saves", "bookmarks"),
            self.metric_value("comments", "replies"),
            self.metric_value("reposts", "retweets", "repost", "retweet", "shares", "share"),
        ]


@dataclass
class YoutubeBrowserVideo:
    url: str
    title: str
    published_at: str = ""
    raw_date: str = ""
    views: int | None = None


@dataclass
class WeeklyAccountSummary:
    source_accounts: int = 0
    selected_accounts: int = 0
    supported_accounts: int = 0
    unsupported_accounts: int = 0
    platform_counts: dict[str, int] = field(default_factory=dict)
    selected_platform_counts: dict[str, int] = field(default_factory=dict)
    limit_per_platform: int | None = None
    platform_filter: tuple[str, ...] = ()
    source_index_filter: tuple[int, ...] = ()
    date_range_label: str = ""
    scraped_posts: int = 0
    candidate_posts: int = 0
    filtered_out_posts: int = 0
    failed_accounts: int = 0
    created_table: bool = False
    table_id: str = ""
    table_name: str = ""
    latest_per_account: int | None = None
    written_records: int = 0
    written_placeholder_records: int = 0
    no_post_accounts: int = 0
    skipped_existing_posts: int = 0
    skipped_existing_placeholders: int = 0
    resumed_accounts: int = 0
    local_completed_accounts: int = 0
    local_ledger_path: str = ""
    local_xlsx_path: str = ""
    local_xlsx_error: str = ""
    base_backfill_skipped: bool = False
    failure_examples: list[str] = field(default_factory=list)


def parse_date_range(value: str | None) -> WeekRange:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{8})\s*-\s*(\d{8})", text)
    if not match:
        raise ValueError("海外市场账号需要日期范围，格式：海外市场账号 20260605-20260609")
    start = datetime.strptime(match.group(1), "%Y%m%d").date()
    end = datetime.strptime(match.group(2), "%Y%m%d").date()
    if end < start:
        raise ValueError("海外市场账号日期范围错误：结束日期不能早于开始日期")
    return WeekRange(start=start, end=end)


def parse_detail_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number //= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    clean = text.replace('Z', '+00:00') if text.endswith('Z') else text
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def in_week(value: str | None, week: WeekRange) -> bool:
    parsed = parse_detail_datetime(value)
    return bool(parsed and week.start <= parsed.astimezone(LOCAL_TIMEZONE).date() <= week.end)


def detail_date_state(value: str | None, week: WeekRange) -> str:
    parsed = parse_detail_datetime(value)
    if not parsed:
        return 'unknown'
    local_date = parsed.astimezone(LOCAL_TIMEZONE).date()
    if local_date < week.start:
        return 'before-start'
    if local_date > week.end:
        return 'after-end'
    return 'in-range'


def instagram_candidate_date(image_alt: str | None, *, default_year: int) -> date | None:
    text = re.sub(r'\s+', ' ', str(image_alt or '')).strip()
    if not text:
        return None
    match = re.search(r'\bon\s+([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?', text)
    if not match:
        return None
    year = int(match.group(3) or default_year)
    for fmt in ('%B %d %Y', '%b %d %Y'):
        try:
            return datetime.strptime(f'{match.group(1)} {int(match.group(2))} {year}', fmt).date()
        except ValueError:
            continue
    return None


def instagram_candidate_datetime_value(candidate_date: date | None) -> str:
    if not candidate_date:
        return ''
    parsed = datetime(candidate_date.year, candidate_date.month, candidate_date.day, tzinfo=LOCAL_TIMEZONE)
    return parsed.isoformat()


def filter_instagram_candidates_by_week(candidates: list[Any], week: WeekRange) -> tuple[list[Any], int]:
    filtered: list[Any] = []
    skipped = 0
    for candidate in candidates:
        candidate_date = instagram_candidate_date(getattr(candidate, 'image_alt', ''), default_year=week.end.year)
        if candidate_date and not (week.start <= candidate_date <= week.end):
            skipped += 1
            continue
        filtered.append(candidate)
    return filtered, skipped


def tiktok_candidate_datetime(url: str | None) -> datetime | None:
    match = re.search(r'/video/(\d{12,})', str(url or ''))
    if not match:
        return None
    try:
        timestamp = int(match.group(1)) >> 32
        parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    if not (datetime(2020, 1, 1, tzinfo=timezone.utc) <= parsed <= datetime(2100, 1, 1, tzinfo=timezone.utc)):
        return None
    return parsed


def filter_tiktok_candidates_by_week(candidates: list[Any], week: WeekRange) -> tuple[list[Any], int]:
    filtered: list[Any] = []
    skipped = 0
    for candidate in candidates:
        parsed = tiktok_candidate_datetime(getattr(candidate, 'url', ''))
        if parsed and not (week.start <= parsed.astimezone(LOCAL_TIMEZONE).date() <= week.end):
            skipped += 1
            continue
        filtered.append(candidate)
    return filtered, skipped


def extract_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"\((https?://[^)]+)\)", text)
    if match:
        return match.group(1).strip()
    match = re.search(r"https?://[^\s\])]+", text)
    return match.group(0).strip() if match else text


def platform_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "unknown"
    host = host[4:] if host.startswith("www.") else host
    for platform, hosts in PLATFORM_HOSTS.items():
        if any(host == item or host.endswith("." + item) for item in hosts):
            return platform
    return "unknown"


def platform_distribution(accounts: list[AccountRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for account in accounts:
        counts[account.platform] = counts.get(account.platform, 0) + 1
    return counts


def limit_accounts_per_platform(accounts: list[AccountRow], limit: int | None) -> list[AccountRow]:
    if not limit or limit <= 0:
        return list(accounts)
    counts: dict[str, int] = {}
    selected: list[AccountRow] = []
    for account in accounts:
        current = counts.get(account.platform, 0)
        if current >= limit:
            continue
        selected.append(account)
        counts[account.platform] = current + 1
    return selected


def source_ordered_accounts(accounts: list[AccountRow]) -> list[AccountRow]:
    return [
        account
        for _index, account in sorted(
            enumerate(accounts),
            key=lambda item: (
                item[1].source_order if item[1].source_order > 0 else item[0] + 1,
                item[0],
            ),
        )
    ]


def trigger_time_suffix(job_id: str | None, now: datetime | None = None) -> str:
    text = str(job_id or "")
    match = re.search(r"(\d{8})[-_]?(\d{6})", text)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    moment = now or datetime.now(LOCAL_TIMEZONE)
    return moment.strftime("%Y%m%d-%H%M%S")


def safe_export_stem(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._") or "weekly_market_accounts"


def safe_optional_export_suffix(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return safe_export_stem(text)


def local_export_paths(workdir: Path, table_name: str, *, trigger_suffix: str | None = None) -> tuple[Path, Path]:
    export_dir = workdir / "exports"
    safe_name = safe_export_stem(table_name)
    safe_suffix = safe_optional_export_suffix(trigger_suffix)
    export_name = f"{safe_name}-{safe_suffix}" if safe_suffix else safe_name
    return export_dir / f"{export_name}.jsonl", export_dir / f"{export_name}.xlsx"


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def account_ledger_key(account: AccountRow) -> str:
    normalized = normalize_profile_url(account.url, account.platform).strip().rstrip("/")
    return f"{account.platform}|{normalized}".lower()


def weekly_post_to_json(post: WeeklyPost) -> dict[str, Any]:
    return {
        "account": post.account,
        "url": post.url,
        "text": post.text,
        "published_at": post.published_at,
        "metrics": json_safe(post.metrics),
        "platform": post.platform,
        "source_account_url": post.source_account_url,
        "error": post.error,
        "placeholder": post.placeholder,
    }


def weekly_post_from_json(data: dict[str, Any]) -> WeeklyPost:
    return WeeklyPost(
        account=str(data.get("account") or ""),
        url=str(data.get("url") or ""),
        text=str(data.get("text") or ""),
        published_at=str(data.get("published_at") or ""),
        metrics=data.get("metrics") if isinstance(data.get("metrics"), dict) else {},
        platform=str(data.get("platform") or ""),
        source_account_url=str(data.get("source_account_url") or ""),
        error=str(data.get("error") or ""),
        placeholder=bool(data.get("placeholder")),
    )


def account_entry_status(posts: list[WeeklyPost], error: str) -> str:
    if error and not posts:
        return "failed"
    if error:
        return "partial"
    if posts and all(post.placeholder for post in posts):
        return "no_post"
    return "done"


def build_ledger_entry(
    *,
    account: AccountRow,
    posts: list[WeeklyPost],
    error: str,
    stats: dict[str, int],
    week: WeekRange,
    table_name: str,
    latest_per_account: int | None,
) -> dict[str, Any]:
    return {
        "type": "account_result",
        "version": 1,
        "table_name": table_name,
        "date_range": week.label,
        "latest_per_account": latest_per_account,
        "account_key": account_ledger_key(account),
        "source_order": account.source_order,
        "account": account.account,
        "platform": account.platform,
        "source_account_url": account.url,
        "status": account_entry_status(posts, error),
        "posts": [weekly_post_to_json(post) for post in posts],
        "error": str(error or ""),
        "stats": json_safe(stats),
        "saved_at": datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
    }


def append_ledger_entry(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_ledger_entries(path: Path, table_name: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") != "account_result" or item.get("table_name") != table_name:
                continue
            entries.append(item)
    return entries


def completed_account_keys(entries: list[dict[str, Any]]) -> set[str]:
    return {
        str(entry.get("account_key") or "")
        for entry in entries
        if entry.get("status") in {"done", "no_post"} and entry.get("account_key") and not ledger_entry_requires_retry(entry)
    }


def ledger_entry_requires_retry(entry: dict[str, Any]) -> bool:
    if entry.get("platform") != "linkedin" or entry.get("status") != "no_post":
        return False
    stats = entry.get("stats")
    if not isinstance(stats, dict):
        return False
    skipped_no_date = int(stats.get("skipped_no_date") or 0)
    early_stop = int(stats.get("early_stop_before_start") or 0)
    filtered = int(stats.get("filtered") or 0)
    return skipped_no_date > 0 and early_stop <= 0 and filtered <= 0


def ledger_posts_for_accounts(entries: list[dict[str, Any]], accounts: list[AccountRow]) -> list[WeeklyPost]:
    entries_by_key: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key = str(entry.get("account_key") or "")
        if not key:
            continue
        entries_by_key.setdefault(key, []).append(entry)

    output: list[WeeklyPost] = []
    seen_urls: set[str] = set()
    seen_placeholders: set[str] = set()
    for account in accounts:
        key = account_ledger_key(account)
        account_posts: list[WeeklyPost] = []
        placeholder: WeeklyPost | None = None
        for entry in entries_by_key.get(key, []):
            for post_data in entry.get("posts", []) if isinstance(entry.get("posts"), list) else []:
                if not isinstance(post_data, dict):
                    continue
                post = weekly_post_from_json(post_data)
                if post.placeholder:
                    placeholder = placeholder or post
                    continue
                if post.url and post.url in seen_urls:
                    continue
                if post.url:
                    seen_urls.add(post.url)
                account_posts.append(post)
        if account_posts:
            output.extend(account_posts)
        elif placeholder and key not in seen_placeholders:
            output.append(placeholder)
            seen_placeholders.add(key)
    return output


def write_local_xlsx(path: Path, entries: list[dict[str, Any]], accounts: list[AccountRow]) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as exc:
        raise RuntimeError("缺少依赖 openpyxl，无法生成本地 XLSX。") from exc

    wb = Workbook()
    ws_data = wb.active
    ws_data.title = "数据"
    ws_status = wb.create_sheet("进度")
    header_fill = PatternFill("solid", fgColor="D9EAF7")
    header_font = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    data_headers = ["源序号", "账号", "平台", "账号链接", *RESULT_WRITE_FIELDS, "状态", "错误"]
    status_headers = ["源序号", "账号", "平台", "账号链接", "状态", "抓到帖子", "最后保存时间", "错误"]
    ws_data.append(data_headers)
    ws_status.append(status_headers)
    for sheet in (ws_data, ws_status):
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

    entries_by_key: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key = str(entry.get("account_key") or "")
        if key:
            entries_by_key.setdefault(key, []).append(entry)

    for account in accounts:
        key = account_ledger_key(account)
        account_entries = entries_by_key.get(key, [])
        latest_entry = account_entries[-1] if account_entries else {}
        status = str(latest_entry.get("status") or "pending")
        error = str(latest_entry.get("error") or "")
        saved_at = str(latest_entry.get("saved_at") or "")
        posts = ledger_posts_for_accounts(account_entries, [account])
        ws_status.append([
            account.source_order,
            account.account,
            account.platform,
            account.url,
            status,
            sum(1 for post in posts if not post.placeholder),
            saved_at,
            error,
        ])
        if not posts and status in {"failed", "partial"}:
            ws_data.append([account.source_order, account.account, account.platform, account.url, None, None, None, None, None, None, None, None, None, status, error])
            continue
        for post in posts:
            ws_data.append([account.source_order, account.account, account.platform, account.url, *post.to_row(), status, error])

    for sheet in (ws_data, ws_status):
        for row in sheet.iter_rows():
            for cell in row:
                cell.alignment = wrap
        for column_cells in sheet.columns:
            letter = column_cells[0].column_letter
            max_len = max(len(str(cell.value or "")) for cell in column_cells)
            sheet.column_dimensions[letter].width = min(max(max_len + 2, 10), 60)

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.stem}.tmp{path.suffix}")
    wb.save(temp_path)
    temp_path.replace(path)


def normalize_profile_url(url: str, platform: str) -> str:
    if platform == "x":
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if parts:
            return f"https://x.com/{parts[0]}"
    return url


def profile_handle_from_url(url: str, platform: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split('/') if part]
    if platform == 'instagram' and parts:
        return parts[0].lstrip('@').lower()
    if platform == 'tiktok' and parts and parts[0].startswith('@'):
        return parts[0].lstrip('@').lower()
    return ''


def metric_payload(metrics: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, metric in (metrics or {}).items():
        if isinstance(metric, dict):
            out[key] = metric.get("value") if metric.get("value") is not None else metric.get("raw")
        else:
            out[key] = metric
    return out


def read_source_accounts(
    run_lark_json: JsonRunner,
    *,
    base_token: str = DEFAULT_BASE_TOKEN,
    table_id: str = DEFAULT_SOURCE_TABLE_ID,
    view_id: str = DEFAULT_SOURCE_VIEW_ID,
    identity: str = DEFAULT_IDENTITY,
) -> list[AccountRow]:
    accounts: list[AccountRow] = []
    offset = 0
    field_order: list[str] | None = None
    while True:
        payload = run_lark_json(
            [
                "base",
                "+record-list",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--view-id",
                view_id,
                "--limit",
                "200",
                "--offset",
                str(offset),
                "--as",
                identity,
                "--format",
                "json",
            ],
            60,
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        rows = data.get("data", []) if isinstance(data, dict) else []
        record_ids = data.get("record_id_list", []) if isinstance(data, dict) else []
        if field_order is None:
            field_order = [str(item) for item in data.get("fields", [])]
        indexes = {name: field_order.index(name) for name in SOURCE_FIELDS if field_order and name in field_order}
        for idx, row in enumerate(rows if isinstance(rows, list) else []):
            cells = list(row) if isinstance(row, list) else []
            url = extract_url(cells[indexes["账号链接"]]) if "账号链接" in indexes and indexes["账号链接"] < len(cells) else ""
            if not url:
                continue
            account = str(cells[indexes["账号名称"]] if "账号名称" in indexes and indexes["账号名称"] < len(cells) else "").strip()
            accounts.append(
                AccountRow(
                    account=account or url,
                    country=str(cells[indexes["国家"]] if "国家" in indexes and indexes["国家"] < len(cells) else "").strip(),
                    url=url,
                    note=str(cells[indexes["备注"]] if "备注" in indexes and indexes["备注"] < len(cells) else "").strip(),
                    attribute=str(cells[indexes["属性"]] if "属性" in indexes and indexes["属性"] < len(cells) else "").strip(),
                    region=str(cells[indexes["大区"]] if "大区" in indexes and indexes["大区"] < len(cells) else "").strip(),
                    platform=platform_from_url(url),
                    record_id=str(record_ids[idx] if idx < len(record_ids) else ""),
                    source_order=len(accounts) + 1,
                )
            )
        offset += len(rows) if isinstance(rows, list) else 0
        if not data.get("has_more") or not rows:
            break
    return accounts


def list_tables(run_lark_json: JsonRunner, base_token: str, identity: str = DEFAULT_IDENTITY) -> list[dict[str, Any]]:
    payload = run_lark_json(
        ["base", "+table-list", "--base-token", base_token, "--as", identity, "--format", "json"],
        60,
    )
    tables = payload.get("data", {}).get("tables", []) if isinstance(payload, dict) else []
    return tables if isinstance(tables, list) else []


def find_table_id(tables: list[dict[str, Any]], table_name: str) -> str:
    for table in tables:
        if str(table.get("name") or "") == table_name:
            return str(table.get("id") or "")
    return ""


def ensure_week_table(
    run_lark_json: JsonRunner,
    *,
    base_token: str,
    table_name: str,
    identity: str = DEFAULT_IDENTITY,
    dry_run: bool = False,
) -> tuple[str, bool]:
    table_id = find_table_id(list_tables(run_lark_json, base_token, identity), table_name)
    if table_id:
        return table_id, False
    if dry_run:
        return "", True
    payload = run_lark_json(
        [
            "base",
            "+table-create",
            "--base-token",
            base_token,
            "--name",
            table_name,
            "--fields",
            json.dumps(RESULT_TABLE_FIELDS, ensure_ascii=False, separators=(",", ":")),
            "--as",
            identity,
            "--format",
            "json",
        ],
        120,
    )
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    table = data.get("table") if isinstance(data, dict) else None
    created_id = str(
        data.get("table_id")
        or data.get("id")
        or (table.get("id") if isinstance(table, dict) else "")
        or ""
    )
    if not created_id:
        created_id = find_table_id(list_tables(run_lark_json, base_token, identity), table_name)
    return created_id, True


def list_fields(run_lark_json: JsonRunner, *, base_token: str, table_id: str, identity: str) -> list[dict[str, Any]]:
    payload = run_lark_json(
        [
            "base",
            "+field-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--as",
            identity,
            "--format",
            "json",
        ],
        60,
    )
    fields = payload.get("data", {}).get("fields", []) if isinstance(payload, dict) else []
    return fields if isinstance(fields, list) else []


def ensure_result_fields(
    run_lark_json: JsonRunner,
    *,
    base_token: str,
    table_id: str,
    identity: str,
) -> None:
    if not table_id:
        return
    existing_names = {str(field.get("name") or "") for field in list_fields(run_lark_json, base_token=base_token, table_id=table_id, identity=identity) if isinstance(field, dict)}
    for field in RESULT_TABLE_FIELDS:
        name = str(field.get("name") or "")
        if not name or name in existing_names:
            continue
        run_lark_json(
            [
                "base",
                "+field-create",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--json",
                json.dumps(field, ensure_ascii=False, separators=(",", ":")),
                "--as",
                identity,
                "--format",
                "json",
            ],
            60,
        )
        existing_names.add(name)


def read_existing_post_keys(
    run_lark_json: JsonRunner,
    *,
    base_token: str,
    table_id: str,
    identity: str,
) -> tuple[set[str], set[str]]:
    if not table_id:
        return set(), set()
    existing_links: set[str] = set()
    existing_placeholders: set[str] = set()
    offset = 0
    account_index = 0
    link_index = 1
    while True:
        payload = run_lark_json(
            [
                "base",
                "+record-list",
                "--base-token",
                base_token,
                "--table-id",
                table_id,
                "--limit",
                "200",
                "--offset",
                str(offset),
                "--as",
                identity,
                "--format",
                "json",
            ],
            60,
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        fields = data.get("fields", []) if isinstance(data, dict) else []
        if isinstance(fields, list):
            if "账号" in fields:
                account_index = fields.index("账号")
            if "发帖链接" in fields:
                link_index = fields.index("发帖链接")
        rows = data.get("data", []) if isinstance(data, dict) else []
        for row in rows if isinstance(rows, list) else []:
            cells = list(row) if isinstance(row, list) else []
            account = str(cells[account_index] if len(cells) > account_index else "").strip()
            link = extract_url(cells[link_index]) if len(cells) > link_index and cells[link_index] else ""
            if link:
                existing_links.add(link)
            elif account:
                existing_placeholders.add(account)
        offset += len(rows) if isinstance(rows, list) else 0
        if not data.get("has_more") or not rows:
            break
    return existing_links, existing_placeholders


def base_datetime_millis(value: Any) -> int | None:
    """Convert the local review datetime text to Feishu Base milliseconds."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TIMEZONE)
    except ValueError:
        parsed = parse_detail_datetime(text)
        if parsed is None:
            raise RuntimeError(f"Invalid published datetime for Base: {text!r}")
    return int(parsed.timestamp() * 1000)


def batch_create_posts(
    run_lark_json: JsonRunner,
    *,
    base_token: str,
    table_id: str,
    posts: list[WeeklyPost],
    identity: str,
) -> int:
    fields = list(RESULT_WRITE_FIELDS)
    if any(field in LEGACY_RESULT_FIELDS for field in fields):
        raise RuntimeError("weekly_market_accounts must not write legacy social-media metric blob fields")
    records = []
    for post in posts:
        row = post.to_row()
        if len(row) != len(fields):
            raise RuntimeError("weekly_market_accounts row width does not match writable result fields")
        record_fields = dict(zip(fields, row, strict=True))
        record_fields["\u53d1\u5e03\u65e5\u671f"] = base_datetime_millis(record_fields.get("\u53d1\u5e03\u65e5\u671f"))
        records.append({"fields": record_fields})
    helper = import_local_module("base_write_batch", "base_backfill.py")
    helper.batch_create_records(
        MODULE_DIR.parents[1],
        identity,
        base_token,
        table_id,
        records,
        write_limiter=helper.BaseWriteRateLimiter(),
    )
    return len(records)


def base_datetime(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    parsed = parse_detail_datetime(text)
    if parsed:
        return parsed.astimezone(LOCAL_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
    clean = text.replace('T', ' ').replace('Z', '').replace('+00:00', '')
    return clean[:19]


def metric_payload_from_social(metrics: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if not isinstance(metrics, dict):
        return payload
    for key, metric in metrics.items():
        value = getattr(metric, 'value', None)
        raw = getattr(metric, 'raw', None)
        if value is not None:
            payload[str(key)] = value
        elif raw:
            payload[str(key)] = raw
    return payload


def scrape_x_account(
    account: AccountRow,
    week: WeekRange,
    *,
    workdir: Path,
    output_dir: Path,
    latest_per_account: int | None = None,
) -> tuple[list[WeeklyPost], str, dict[str, int]]:
    stats = {'candidates': 0, 'filtered': 0}
    x_timeline = import_local_module('x_timeline', 'x_timeline.py')
    social_metrics = import_local_module('social_metrics', 'social_metrics.py')
    try:
        profile_url, _handle = x_timeline.normalize_profile_url(account.url)
    except Exception as exc:
        return [], f'X 主页链接无效：{exc}', stats

    cdp_profile = cdp_profile_path(workdir)
    cdp_port = env_int('WEEKLY_ACCOUNT_X_CDP_PORT', env_int('SOCIAL_CDP_PORT', 9222))
    page_delay_min_s, page_delay_max_s = timeline_page_delay_s('x', latest_per_account)
    discovered = []
    last_error = ''
    max_attempts = account_discovery_max_attempts('x')
    for attempt in range(1, max_attempts + 1):
        try:
            discovery_result = x_timeline.discover_x_profile_tweets_with_state(
                profile_url,
                start=date(1970, 1, 1) if latest_per_account else week.start,
                end_exclusive=date(2100, 1, 1) if latest_per_account else week.end_exclusive,
                cdp_profile=cdp_profile,
                cdp_port=cdp_port,
                count=latest_per_account or env_int('WEEKLY_ACCOUNT_X_COUNT', 80),
                timeline=os.getenv('WEEKLY_ACCOUNT_X_TIMELINE', 'all'),
                include_pinned=os.getenv('WEEKLY_ACCOUNT_X_INCLUDE_PINNED', '0') == '1',
                include_replies=os.getenv('WEEKLY_ACCOUNT_X_INCLUDE_REPLIES', '1') != '0',
                include_quotes=os.getenv('WEEKLY_ACCOUNT_X_INCLUDE_QUOTES', '1') != '0',
                include_reposts=os.getenv('WEEKLY_ACCOUNT_X_INCLUDE_REPOSTS', '0') == '1',
                max_scrolls=env_int('WEEKLY_ACCOUNT_X_MAX_SCROLLS', 50),
                idle_scrolls=env_int('WEEKLY_ACCOUNT_X_IDLE_SCROLLS', 3),
                page_delay_min_s=page_delay_min_s,
                page_delay_max_s=page_delay_max_s,
                timeout_ms=env_int('WEEKLY_ACCOUNT_X_TIMEOUT_MS', 30000),
                cdp_start_minimized=weekly_cdp_start_minimized('x'),
            )
            discovered = list(discovery_result.tweets)
            discovery_stats = discovery_result.stats if isinstance(discovery_result.stats, dict) else {}
            stats['valid_account_page'] = 1 if discovery_stats.get('valid_account_page') else 0
            stats['discovery_attempts'] = attempt
            if discovered or discovery_stats.get('valid_account_page'):
                break
            last_error = f'X 账号页未发现有效时间线信号：{discovery_stats}'
        except Exception as exc:
            last_error = str(exc)
            if is_antibot_error(exc):
                stats['anti_bot_accounts'] = stats.get('anti_bot_accounts', 0) + 1
                return [], f'X 时间线发现失败：{exc}', stats
        if attempt < max_attempts:
            sleep_between_retries('x', attempt + 1)
    else:
        stats['account_abnormal'] = 1
        stats['discovery_attempts'] = max_attempts
        return [], account_abnormal_error('x', last_error, max_attempts), stats
    stats['candidates'] = len(discovered)

    safe_name = re.sub(r'[^A-Za-z0-9_.-]+', '_', account.account).strip('._-') or 'x_account'
    discovery_path = output_dir / f'x_{safe_name}_{week.start:%Y%m%d}_{week.end:%Y%m%d}_discovery.json'
    discovery_path.write_text(
        json.dumps([item.to_jsonable() for item in discovered], ensure_ascii=False, indent=2),
        encoding='utf-8-sig',
    )
    if not discovered:
        return [], '', stats

    detail_delay_min_s, detail_delay_max_s = detail_delay_s('x')
    scraper = social_metrics.SocialScraper(
        use_playwright=True,
        headless=True,
        timeout_ms=env_int('WEEKLY_ACCOUNT_X_DETAIL_TIMEOUT_MS', 30000),
        delay_min_s=detail_delay_min_s,
        delay_max_s=detail_delay_max_s,
        reel_grid_scrolls=0,
        cdp_profile=cdp_profile,
        cdp_port=cdp_port,
        use_cdp_for_x=True,
        cdp_start_minimized=weekly_cdp_start_minimized('x'),
        cdp_auto_close=False,
        cdp_login_timeout_ms=env_int('WEEKLY_ACCOUNT_X_LOGIN_TIMEOUT_MS', 300000),
        allow_manual_login=bool(env_int('WEEKLY_ACCOUNT_ALLOW_MANUAL_LOGIN', 0)),
        max_attempts=detail_max_attempts('x'),
        require_text_for_retry=True,
        require_time_for_retry=True,
        retry_missing_metrics=detail_retry_missing_metrics('x'),
    )
    urls = [item.url for item in discovered]
    results = scraper.scrape(urls)
    posts: list[WeeklyPost] = []
    anti_bot_error = ''
    discovery_by_url = {item.url: item for item in discovered}
    for result in results:
        if result_has_antibot(result):
            anti_bot_error = compact_error_text(getattr(result, 'error', '') or getattr(result, 'debug', {}))
            stats['anti_bot_details'] = stats.get('anti_bot_details', 0) + 1
            continue
        text = str(result.text or '').strip()
        url = str(result.final_url or result.source_url or '').strip()
        discovery = discovery_by_url.get(result.source_url) or discovery_by_url.get(url)
        published_value = str(result.posted_at or (discovery.published_at if discovery else '') or '').strip()
        if not url:
            stats['skipped_details'] = stats.get('skipped_details', 0) + 1
            continue
        if not text:
            stats['skipped_no_text'] = stats.get('skipped_no_text', 0) + 1
            continue
        if not latest_per_account and not in_week(published_value, week):
            stats['filtered'] += 1
            continue
        metrics = metric_payload_from_social(result.metrics)
        if discovery:
            metrics['category'] = discovery.category
            metrics['timeline'] = discovery.timeline
            metrics['discovered_published_at'] = discovery.published_at
        posts.append(
            WeeklyPost(
                account=account.account,
                url=url,
                text=text,
                published_at=base_datetime(published_value),
                metrics=metrics,
                platform='x',
                source_account_url=profile_url,
                error=str(result.error or ''),
            )
        )
    if anti_bot_error:
        return posts, f'X 触发反爬/验证，已停止继续刷新：{anti_bot_error}', stats
    return posts, '', stats


def youtube_channel_id(account_url: str) -> str:
    raise RuntimeError('F009 YouTube uses browser /videos discovery; RSS/HTTP channel-id discovery is disabled')
    parsed = urlparse(account_url)
    parts = [part for part in parsed.path.split('/') if part]
    if len(parts) >= 2 and parts[0] == 'channel':
        return parts[1]
    html = http_get_text(account_url, timeout_s=env_int('WEEKLY_ACCOUNT_HTTP_TIMEOUT_S', 20))
    patterns = [
        r'channelId[^U]{0,80}(UC[A-Za-z0-9_-]+)',
        r'externalId[^U]{0,80}(UC[A-Za-z0-9_-]+)',
        r'<meta[^>]+channelId[^>]+(UC[A-Za-z0-9_-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    raise ValueError('未在 YouTube 账号页识别到 channel_id')


def youtube_video_metrics(video_url: str) -> dict[str, Any]:
    raise RuntimeError('F009 YouTube metrics come from browser /videos discovery; HTTP video metrics are disabled')
    try:
        html = http_get_text(video_url, timeout_s=env_int('WEEKLY_ACCOUNT_YOUTUBE_VIDEO_TIMEOUT_S', 15))
    except Exception:
        return {}
    metrics: dict[str, Any] = {}
    patterns = [
        r'"videoViewCountRenderer"\s*:\s*\{.*?"viewCount"\s*:\s*\{\s*"simpleText"\s*:\s*"(?P<views>[^"]+)"',
        r'"viewCountFactoidRenderer"\s*:\s*\{.*?"accessibilityText"\s*:\s*"(?P<views>[^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.DOTALL)
        if not match:
            continue
        parsed = metric_number(match.group('views'))
        if parsed is not None:
            metrics['views'] = parsed
            break
    return metrics


YOUTUBE_VIDEO_CARD_SCRIPT = r"""
() => {
  const compact = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const result = [];
  const seen = new Set();
  const nearestText = (node) => {
    let current = node;
    const parts = [];
    for (let depth = 0; current && depth < 7; depth += 1) {
      const text = compact(current.innerText || current.textContent || '');
      if (text) parts.push(text);
      if (text.length > 80 && /(?:views?|ago|\d+[smhdw]|mo)\b/i.test(text)) break;
      current = current.parentElement;
    }
    return parts.join(' | ');
  };
  const cards = Array.from(document.querySelectorAll([
    'ytd-rich-grid-media',
    'ytd-grid-video-renderer',
    'ytd-video-renderer',
    'ytd-compact-video-renderer',
    'yt-lockup-view-model',
    '.ytLockupViewModelHost'
  ].join(',')));
  for (const card of cards) {
    const linkCandidates = Array.from(card.querySelectorAll('a[href*="/watch?v="], a[href*="/shorts/"]'));
    const titleLike = linkCandidates.find(node => {
      const text = compact(node.getAttribute('title') || node.textContent || node.getAttribute('aria-label') || '');
      return text && !/^\d{0,2}:?\d{1,2}:\d{2}$/.test(text) && !/^\d{1,2}:\d{2}$/.test(text);
    });
    const link = card.querySelector('a#video-title-link, a#video-title, h3 a[href], a.ytLockupMetadataViewModelTitle[href]')
      || titleLike
      || linkCandidates[0];
    const href = link ? (link.href || link.getAttribute('href') || '') : '';
    if (!href || seen.has(href)) continue;
    seen.add(href);
    const title = compact(
      (link && (link.getAttribute('title') || link.textContent || link.getAttribute('aria-label'))) || ''
    );
    const aria = compact((link && link.getAttribute('aria-label')) || '');
    const metaTexts = Array.from(card.querySelectorAll([
      '#metadata-line span',
      '.inline-metadata-item',
      'yt-formatted-string',
      'span'
    ].join(','))).map(node => compact(node.textContent || node.innerText || '')).filter(Boolean);
    result.push({
      href,
      title,
      aria,
      text: compact(card.innerText || card.textContent || ''),
      metaTexts: Array.from(new Set(metaTexts)).slice(0, 80),
    });
  }
  for (const link of Array.from(document.querySelectorAll('a[href*="/watch?v="], a[href*="/shorts/"]'))) {
    const href = link.href || link.getAttribute('href') || '';
    if (!href || seen.has(href)) continue;
    const text = compact(link.innerText || link.textContent || '');
    if (/^\d{0,2}:?\d{1,2}:\d{2}$/.test(text) || /^\d{1,2}:\d{2}$/.test(text)) continue;
    seen.add(href);
    result.push({
      href,
      title: compact(link.getAttribute('title') || text || link.getAttribute('aria-label') || ''),
      aria: compact(link.getAttribute('aria-label') || ''),
      text: nearestText(link),
      metaTexts: [],
    });
  }
  return {
    url: location.href || '',
    title: document.title || '',
    bodyTextLength: compact(document.body ? (document.body.innerText || document.body.textContent || '') : '').length,
    videos: result,
  };
}
"""


def compact_text(value: Any) -> str:
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def youtube_account_home(account_url: str) -> str:
    value = str(account_url or '').strip()
    if not value:
        raise ValueError('empty YouTube account URL')
    if not re.match(r'https?://', value, flags=re.I):
        value = 'https://' + value.lstrip('/')
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    if host not in {'youtube.com', 'm.youtube.com'}:
        raise ValueError(f'not a YouTube account URL: {account_url}')
    parts = [part for part in parsed.path.split('/') if part]
    if not parts:
        return 'https://www.youtube.com/'
    if parts[0].startswith('@'):
        path = '/' + parts[0]
    elif len(parts) >= 2 and parts[0] in {'channel', 'c', 'user'}:
        path = '/' + parts[0] + '/' + parts[1]
    else:
        path = '/' + parts[0]
    return urlunparse(('https', 'www.youtube.com', path.rstrip('/') or '/', '', '', ''))


def youtube_account_videos_url(account_url: str) -> str:
    return youtube_account_home(account_url).rstrip('/') + '/videos'


def canonical_youtube_video_url(value: str | None) -> str:
    href = str(value or '').strip()
    if not href:
        return ''
    if href.startswith('//'):
        href = 'https:' + href
    elif href.startswith('/'):
        href = 'https://www.youtube.com' + href
    elif not re.match(r'https?://', href, flags=re.I):
        href = 'https://www.youtube.com/' + href.lstrip('/')
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    video_id = ''
    if host == 'youtu.be':
        video_id = parsed.path.strip('/').split('/')[0]
    elif host in {'youtube.com', 'm.youtube.com'}:
        if parsed.path == '/watch':
            video_id = (parse_qs(parsed.query).get('v') or [''])[0]
        else:
            parts = [part for part in parsed.path.split('/') if part]
            if len(parts) >= 2 and parts[0] in {'shorts', 'live'}:
                video_id = parts[1]
    if not re.fullmatch(r'[A-Za-z0-9_-]{6,}', video_id or ''):
        return ''
    return f'https://www.youtube.com/watch?v={video_id}'


def infer_year_for_month_day(month: int, day: int, now: datetime) -> int:
    year = now.year
    try:
        candidate = datetime(year, month, day, tzinfo=LOCAL_TIMEZONE)
    except ValueError:
        return year
    if candidate > now + timedelta(days=7):
        return year - 1
    return year


def parse_youtube_datetime_text(value: str | None, *, now: datetime | None = None) -> str:
    text = compact_text(value)
    if not text:
        return ''
    now = (now or datetime.now(LOCAL_TIMEZONE)).astimezone(LOCAL_TIMEZONE)
    lowered = text.lower()
    if 'yesterday' in lowered:
        return (now - timedelta(days=1)).isoformat()
    if re.search(r'\btoday\b|\bjust now\b|\bnow\b', lowered):
        return now.isoformat()
    match = re.search(
        r'(?P<count>\d+)\s*(?:\u4e2a|\u500b)?\s*'
        r'(?P<unit>\u79d2|\u5206\u949f|\u5206\u9418|\u5206|\u5c0f\u65f6|\u5c0f\u6642|'
        r'\u5929|\u65e5|\u5468|\u9031|\u661f\u671f|\u6708|\u5e74)\s*\u524d',
        text,
    )
    if match:
        count = int(match.group('count'))
        unit = match.group('unit')
        if unit == '\u5e74':
            delta = timedelta(days=count * 365)
        elif unit == '\u6708':
            delta = timedelta(days=count * 30)
        elif unit in {'\u5468', '\u9031', '\u661f\u671f'}:
            delta = timedelta(weeks=count)
        elif unit in {'\u5929', '\u65e5'}:
            delta = timedelta(days=count)
        elif unit in {'\u5c0f\u65f6', '\u5c0f\u6642'}:
            delta = timedelta(hours=count)
        elif unit in {'\u5206\u949f', '\u5206\u9418', '\u5206'}:
            delta = timedelta(minutes=count)
        else:
            delta = timedelta(seconds=count)
        return (now - delta).isoformat()
    match = re.search(
        r'\b(?:streamed|premiered)?\s*(?P<count>\d+)\s+'
        r'(?P<unit>seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b',
        lowered,
        flags=re.I,
    )
    if match:
        count = int(match.group('count'))
        unit = match.group('unit').lower()
        if unit.startswith('year'):
            delta = timedelta(days=count * 365)
        elif unit.startswith('month'):
            delta = timedelta(days=count * 30)
        elif unit.startswith('week'):
            delta = timedelta(weeks=count)
        elif unit.startswith('day'):
            delta = timedelta(days=count)
        elif unit.startswith('hour'):
            delta = timedelta(hours=count)
        elif unit.startswith('minute'):
            delta = timedelta(minutes=count)
        else:
            delta = timedelta(seconds=count)
        return (now - delta).isoformat()
    match = re.search(r'\b(?P<count>\d+)\s*(?P<unit>s|m|h|d|w|mo|yr|y)\s+ago\b', lowered, flags=re.I)
    if match:
        count = int(match.group('count'))
        unit = match.group('unit').lower()
        if unit in {'y', 'yr'}:
            delta = timedelta(days=count * 365)
        elif unit == 'mo':
            delta = timedelta(days=count * 30)
        elif unit == 'w':
            delta = timedelta(weeks=count)
        elif unit == 'd':
            delta = timedelta(days=count)
        elif unit == 'h':
            delta = timedelta(hours=count)
        elif unit == 'm':
            delta = timedelta(minutes=count)
        else:
            delta = timedelta(seconds=count)
        return (now - delta).isoformat()
    months = {
        'jan': 1, 'january': 1,
        'feb': 2, 'february': 2,
        'mar': 3, 'march': 3,
        'apr': 4, 'april': 4,
        'may': 5,
        'jun': 6, 'june': 6,
        'jul': 7, 'july': 7,
        'aug': 8, 'august': 8,
        'sep': 9, 'sept': 9, 'september': 9,
        'oct': 10, 'october': 10,
        'nov': 11, 'november': 11,
        'dec': 12, 'december': 12,
    }
    match = re.search(
        r'\b(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
        r'\s+(?P<day>\d{1,2})(?:,\s*(?P<year>\d{4}))?\b',
        text,
        flags=re.I,
    )
    if match:
        month = months[match.group('month').lower()]
        day = int(match.group('day'))
        year = int(match.group('year') or infer_year_for_month_day(month, day, now))
        try:
            return datetime(year, month, day, tzinfo=LOCAL_TIMEZONE).isoformat()
        except ValueError:
            return ''
    return ''


def youtube_raw_date_from_texts(texts: list[str]) -> tuple[str, str]:
    candidates: list[str] = []
    for text in texts:
        value = compact_text(text)
        if not value:
            continue
        candidates.extend(part.strip() for part in re.split(r'[|\u2022\n\r]+', value) if part.strip())
        candidates.append(value)
    for candidate in candidates:
        match = re.search(
            r'\d+\s*(?:\u4e2a|\u500b)?\s*'
            r'(?:\u79d2|\u5206\u949f|\u5206\u9418|\u5206|\u5c0f\u65f6|\u5c0f\u6642|'
            r'\u5929|\u65e5|\u5468|\u9031|\u661f\u671f|\u6708|\u5e74)\s*\u524d',
            candidate,
        )
        if match:
            raw = compact_text(match.group(0))
            return raw, parse_youtube_datetime_text(raw)
        match = re.search(
            r'\b(?:streamed|premiered)?\s*\d+\s+'
            r'(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b',
            candidate,
            flags=re.I,
        )
        if match:
            raw = compact_text(match.group(0))
            return raw, parse_youtube_datetime_text(raw)
        match = re.search(r'\b\d+\s*(?:s|m|h|d|w|mo|yr|y)\s+ago\b', candidate, flags=re.I)
        if match:
            raw = compact_text(match.group(0))
            return raw, parse_youtube_datetime_text(raw)
        match = re.search(
            r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|'
            r'aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
            r'\s+\d{1,2}(?:,\s*\d{4})?\b',
            candidate,
            flags=re.I,
        )
        if match:
            raw = compact_text(match.group(0))
            return raw, parse_youtube_datetime_text(raw)
        for raw in ('yesterday', 'today', 'just now'):
            if re.search(rf'\b{raw}\b', candidate, flags=re.I):
                return raw, parse_youtube_datetime_text(raw)
    return '', ''


def youtube_views_from_texts(texts: list[str]) -> int | None:
    for text in texts:
        value = compact_text(text)
        match = re.search(r'(?P<count>[\d,.]+(?:\.\d+)?\s*[KkMmBb]?)\s+views?\b', value, flags=re.I)
        if match:
            parsed = metric_number(match.group('count'))
            if parsed is not None:
                return parsed
        match = re.search(
            r'\b(?P<count>[\d,.]+(?:\.\d+)?\s*[KkMmBb])\s+'
            r'(?:\d+\s*(?:s|m|h|d|w|mo|yr|y)\s+ago|'
            r'(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago)\b',
            value,
            flags=re.I,
        )
        if match:
            parsed = metric_number(match.group('count'))
            if parsed is not None:
                return parsed
        if re.search(r'\bno views?\b', value, flags=re.I):
            return 0
    return None


def youtube_browser_video_from_raw(raw: dict[str, Any]) -> YoutubeBrowserVideo | None:
    url = canonical_youtube_video_url(str(raw.get('href') or ''))
    title = compact_text(raw.get('title') or '')
    if not title:
        title = compact_text(raw.get('aria') or '')
    if not url or not title:
        return None
    meta_texts = [str(item) for item in raw.get('metaTexts') or []]
    texts = [*meta_texts, str(raw.get('aria') or ''), str(raw.get('text') or '')]
    raw_date, published_at = youtube_raw_date_from_texts(texts)
    views = youtube_views_from_texts(texts)
    return YoutubeBrowserVideo(url=url, title=title, published_at=published_at, raw_date=raw_date, views=views)


def discover_youtube_browser_videos(account_url: str, *, limit: int | None = None) -> tuple[list[YoutubeBrowserVideo], dict[str, int]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError('playwright is not installed') from exc

    timeout_ms = env_int('WEEKLY_ACCOUNT_YOUTUBE_TIMEOUT_MS', env_int('WEEKLY_ACCOUNT_TIMELINE_TIMEOUT_MS', 30000))
    max_scrolls = env_int('WEEKLY_ACCOUNT_YOUTUBE_MAX_SCROLLS', max(3, timeline_max_scrolls('youtube', None)))
    idle_scrolls = env_int('WEEKLY_ACCOUNT_YOUTUBE_IDLE_SCROLLS', max(1, timeline_idle_scrolls('youtube', None)))
    settle_ms = env_int('WEEKLY_ACCOUNT_YOUTUBE_SETTLE_MS', 2500)
    scroll_settle_ms = env_int('WEEKLY_ACCOUNT_YOUTUBE_SCROLL_SETTLE_MS', 1200)
    headless = env_bool('WEEKLY_ACCOUNT_YOUTUBE_HEADLESS', True)
    videos_url = youtube_account_videos_url(account_url)
    wanted = max(1, int(limit or timeline_candidate_limit('youtube')))
    stats: dict[str, int] = {
        'browser_headless': 1 if headless else 0,
        'max_scrolls': max_scrolls,
        'raw_cards': 0,
        'valid_account_page': 0,
    }
    videos_by_url: dict[str, YoutubeBrowserVideo] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = None
        try:
            context = browser.new_context(
                locale='en-US',
                timezone_id='Asia/Shanghai',
                extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'},
            )
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            page.goto(videos_url, wait_until='domcontentloaded', timeout=timeout_ms)
            try:
                page.wait_for_load_state('networkidle', timeout=min(10000, timeout_ms))
            except Exception:
                pass
            page.wait_for_timeout(settle_ms)
            idle = 0
            last_count = 0
            for scroll_index in range(max_scrolls + 1):
                data = page.evaluate(YOUTUBE_VIDEO_CARD_SCRIPT)
                raw_videos = data.get('videos', []) if isinstance(data, dict) else []
                if not isinstance(raw_videos, list):
                    raw_videos = []
                stats['raw_cards'] = max(stats['raw_cards'], len(raw_videos))
                body_len = int(data.get('bodyTextLength') or 0) if isinstance(data, dict) else 0
                if body_len > 300 or raw_videos:
                    stats['valid_account_page'] = 1
                for raw in raw_videos:
                    if not isinstance(raw, dict):
                        continue
                    video = youtube_browser_video_from_raw(raw)
                    if video and video.url not in videos_by_url:
                        videos_by_url[video.url] = video
                if len(videos_by_url) >= wanted:
                    break
                if len(videos_by_url) <= last_count:
                    idle += 1
                else:
                    idle = 0
                last_count = len(videos_by_url)
                if idle >= idle_scrolls:
                    break
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(scroll_settle_ms)
                stats['scrolls'] = scroll_index + 1
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            browser.close()
    if not stats.get('valid_account_page'):
        raise RuntimeError(f'YouTube account page did not expose readable browser content: {videos_url}')
    return list(videos_by_url.values())[:wanted], stats


def scrape_youtube_account(
    account: AccountRow,
    week: WeekRange,
    *,
    latest_per_account: int | None = None,
) -> tuple[list[WeeklyPost], str, dict[str, int]]:
    stats = {'candidates': 0, 'filtered': 0}
    last_error = ''
    max_attempts = account_discovery_max_attempts('youtube')
    for attempt in range(1, max_attempts + 1):
        try:
            videos, discovery_stats = discover_youtube_browser_videos(
                account.url,
                limit=latest_per_account or timeline_candidate_limit('youtube'),
            )
            stats.update({key: value for key, value in discovery_stats.items() if isinstance(value, int)})
            stats['discovery_attempts'] = attempt
            break
        except Exception as exc:
            last_error = str(exc)
            stats['discovery_attempts'] = attempt
            if attempt < max_attempts:
                sleep_between_retries('youtube', attempt + 1)
    else:
        stats['account_abnormal'] = 1
        return [], account_abnormal_error('youtube', last_error, max_attempts), stats

    posts: list[WeeklyPost] = []
    for video in videos:
        stats['candidates'] += 1
        if not latest_per_account:
            state = detail_date_state(video.published_at, week)
            if state == 'unknown':
                stats['filtered'] += 1
                continue
            if state == 'before-start':
                stats['filtered'] += 1
                break
            if state == 'after-end':
                stats['filtered'] += 1
                continue
        if not video.url or not video.title:
            continue
        metrics: dict[str, Any] = {'source': 'youtube_browser'}
        if video.raw_date:
            metrics['raw_date'] = video.raw_date
        if video.views is not None:
            metrics['views'] = video.views
        posts.append(
            WeeklyPost(
                account=account.account,
                url=video.url,
                text=video.title,
                published_at=base_datetime(video.published_at),
                metrics=metrics,
                platform='youtube',
                source_account_url=account.url,
            )
        )
        if latest_per_account and len(posts) >= latest_per_account:
            break
    return posts, '', stats


def scrape_linkedin_account(
    account: AccountRow,
    week: WeekRange,
    *,
    workdir: Path,
    output_dir: Path,
    latest_per_account: int | None = None,
    progress: ProgressCallback | None = None,
) -> tuple[list[WeeklyPost], str, dict[str, int]]:
    stats = {'candidates': 0, 'filtered': 0}
    linkedin_feed = import_local_module('linkedin_feed', 'linkedin_feed.py')
    cdp_profile = cdp_profile_path(workdir)
    cdp_port = env_int('WEEKLY_ACCOUNT_LINKEDIN_CDP_PORT', env_int('WEEKLY_ACCOUNT_CDP_PORT', env_int('SOCIAL_CDP_PORT', 9222)))
    safe_name = re.sub(r'[^A-Za-z0-9_.-]+', '_', account.account).strip('._-') or 'linkedin'
    post_limit = latest_per_account or linkedin_feed_env_int('POST_LIMIT', timeline_candidate_limit('linkedin'))
    options = linkedin_feed.LinkedInFeedOptions(
        timeout_ms=env_int('WEEKLY_ACCOUNT_LINKEDIN_TIMEOUT_MS', env_int('WEEKLY_ACCOUNT_TIMELINE_TIMEOUT_MS', 60000)),
        max_scrolls=linkedin_feed_env_int('MAX_SCROLLS', timeline_max_scrolls('linkedin', latest_per_account)),
        idle_scrolls=linkedin_feed_env_int('IDLE_SCROLLS', timeline_idle_scrolls('linkedin', latest_per_account)),
        post_limit=max(1, post_limit),
        home_read_min_s=linkedin_feed_env_float('HOME_READ_MIN', 2.0),
        home_read_max_s=linkedin_feed_env_float('HOME_READ_MAX', 5.0),
        posts_read_min_s=linkedin_feed_env_float('POSTS_READ_MIN', 2.5),
        posts_read_max_s=linkedin_feed_env_float('POSTS_READ_MAX', 5.5),
        card_read_min_s=linkedin_feed_env_float('CARD_READ_MIN', 1.5),
        card_read_max_s=linkedin_feed_env_float('CARD_READ_MAX', 4.0),
        expand_read_min_s=linkedin_feed_env_float('EXPAND_READ_MIN', 1.0),
        expand_read_max_s=linkedin_feed_env_float('EXPAND_READ_MAX', 3.0),
        scroll_delay_min_s=linkedin_feed_env_float('SCROLL_DELAY_MIN', 2.0),
        scroll_delay_max_s=linkedin_feed_env_float('SCROLL_DELAY_MAX', 5.0),
        scroll_min_px=linkedin_feed_env_int('SCROLL_MIN_PX', 650),
        scroll_max_px=linkedin_feed_env_int('SCROLL_MAX_PX', 1300),
        cdp_start_minimized=weekly_cdp_start_minimized('linkedin'),
        manual_challenge_wait=linkedin_feed_env_bool(
            'MANUAL_CHALLENGE_WAIT',
            env_bool('WEEKLY_ACCOUNT_LINKEDIN_MANUAL_CHALLENGE_WAIT', True),
        ),
        manual_challenge_timeout_s=linkedin_feed_env_float(
            'MANUAL_CHALLENGE_TIMEOUT_S',
            env_float('WEEKLY_ACCOUNT_LINKEDIN_MANUAL_CHALLENGE_TIMEOUT_S', 3600.0),
        ),
        manual_challenge_poll_s=linkedin_feed_env_float(
            'MANUAL_CHALLENGE_POLL_S',
            env_float('WEEKLY_ACCOUNT_LINKEDIN_MANUAL_CHALLENGE_POLL_S', 10.0),
        ),
        date_unknown_text_limit=linkedin_feed_env_int('DATE_UNKNOWN_TEXT_LIMIT', 3),
    )

    records = []
    last_error = ''
    max_attempts = account_discovery_max_attempts('linkedin')

    def on_challenge(event: str, stage: str, state: dict[str, Any], elapsed_s: float) -> None:
        stats['linkedin_manual_challenge_events'] = stats.get('linkedin_manual_challenge_events', 0) + 1
        markers = ','.join(str(item) for item in (state.get('hardMarkers') or [])) or 'unknown'
        if event == 'start':
            stats['linkedin_manual_challenge_waits'] = stats.get('linkedin_manual_challenge_waits', 0) + 1
            if progress:
                progress(
                    'linkedin_challenge',
                    f"LinkedIn 遇到反爬挑战，已停留当前页面等待人工验证。\n账号：{account.account}\n阶段：{stage}\n标记：{markers}",
                )
        elif event == 'done':
            stats['linkedin_manual_challenge_cleared'] = stats.get('linkedin_manual_challenge_cleared', 0) + 1
            if progress:
                progress(
                    'linkedin_challenge_cleared',
                    f"LinkedIn 反爬挑战已通过，继续抓取。\n账号：{account.account}\n阶段：{stage}\n等待：{elapsed_s:.0f}s",
                )
        elif event == 'timeout':
            stats['linkedin_manual_challenge_timeout'] = stats.get('linkedin_manual_challenge_timeout', 0) + 1
            if progress:
                progress(
                    'linkedin_challenge_timeout',
                    f"LinkedIn 反爬挑战等待超时，标记账号异常。\n账号：{account.account}\n阶段：{stage}\n等待：{elapsed_s:.0f}s\n标记：{markers}",
                )
        elif event == 'cleared_unavailable':
            stats['linkedin_manual_challenge_cleared_unavailable'] = stats.get('linkedin_manual_challenge_cleared_unavailable', 0) + 1
            if progress:
                progress(
                    'linkedin_challenge_unavailable',
                    f"LinkedIn 反爬挑战已消失，但页面不是有效账号页，后续按账号异常处理。\n账号：{account.account}\n阶段：{stage}\n等待：{elapsed_s:.0f}s",
                )

    for attempt in range(1, max_attempts + 1):
        discovery_path = output_dir / f'linkedin_{safe_name}_{week.start:%Y%m%d}_{week.end:%Y%m%d}_feed_attempt{attempt}.json'
        event_log_path = output_dir / f'linkedin_{safe_name}_{week.start:%Y%m%d}_{week.end:%Y%m%d}_feed_attempt{attempt}.events.jsonl'
        try:
            records, feed_stats = linkedin_feed.collect_linkedin_feed_posts(
                normalize_profile_url(account.url, account.platform),
                account.account,
                cdp_profile=cdp_profile,
                cdp_port=cdp_port,
                options=options,
                latest_mode=bool(latest_per_account),
                date_start=None if latest_per_account else week.start,
                date_end_exclusive=None if latest_per_account else week.end_exclusive,
                log_path=event_log_path,
                discovery_path=discovery_path,
                on_challenge=on_challenge,
            )
            stats.update({key: value for key, value in feed_stats.items() if isinstance(value, int)})
            stats['discovery_attempts'] = attempt
            posts = linkedin_feed_records_to_weekly_posts(account, records)
            if int(feed_stats.get('date_unreadable_stop') or 0) > 0:
                stats['account_abnormal'] = 1
                return [], 'LinkedIn 日期无法判定，已停止该账号避免越过日期范围', stats
            if posts or int(feed_stats.get('valid_account_page') or 0) > 0:
                return posts, '', stats
            last_error = f'LinkedIn 账号页未发现有效动态流信号：{feed_stats}'
        except Exception as exc:
            feed_records = getattr(exc, 'records', [])
            feed_stats = getattr(exc, 'stats', {}) if isinstance(getattr(exc, 'stats', {}), dict) else {}
            records = list(feed_records) if isinstance(feed_records, list) else []
            stats.update({key: value for key, value in feed_stats.items() if isinstance(value, int)})
            stats['discovery_attempts'] = attempt
            for record in records:
                if getattr(record, 'text', ''):
                    stats['partial_records'] = stats.get('partial_records', 0) + 1
            posts = linkedin_feed_records_to_weekly_posts(account, records)
            if int(feed_stats.get('date_unreadable_stop') or 0) > 0:
                stats['account_abnormal'] = 1
                return posts, 'LinkedIn 日期无法判定，已停止该账号避免越过日期范围', stats
            if posts:
                return posts, f'LinkedIn 慢速动态流抓取失败：{exc}', stats
            last_error = str(exc)
            if is_antibot_error(exc):
                stats['anti_bot_accounts'] = stats.get('anti_bot_accounts', 0) + 1
                return [], f'LinkedIn 慢速动态流抓取失败：{exc}', stats
        if attempt < max_attempts:
            sleep_between_retries('linkedin', attempt + 1)
    stats['account_abnormal'] = 1
    stats['discovery_attempts'] = max_attempts
    return [], account_abnormal_error('linkedin', last_error, max_attempts), stats


def linkedin_feed_records_to_weekly_posts(account: AccountRow, records: list[Any]) -> list[WeeklyPost]:
    posts: list[WeeklyPost] = []
    for record in records:
        text = str(getattr(record, 'text', '') or '').strip()
        if not text:
            continue
        metrics = dict(getattr(record, 'metrics', {}) or {})
        metrics['source'] = 'linkedin_slow_feed'
        metrics['raw_date'] = str(getattr(record, 'raw_date', '') or '')
        soft_markers = getattr(record, 'soft_markers', []) or []
        if soft_markers:
            metrics['soft_markers'] = list(soft_markers)
        posts.append(
            WeeklyPost(
                account=account.account,
                url=str(getattr(record, 'post_url', '') or ''),
                text=text,
                published_at=base_datetime(str(getattr(record, 'published_at', '') or '')),
                metrics=metrics,
                platform='linkedin',
                source_account_url=account.url,
            )
        )
    return posts


def scrape_timeline_account(
    account: AccountRow,
    week: WeekRange,
    *,
    workdir: Path,
    output_dir: Path,
    latest_per_account: int | None = None,
) -> tuple[list[WeeklyPost], str, dict[str, int]]:
    stats = {'candidates': 0, 'filtered': 0}
    account_timeline = import_local_module('account_timeline', 'account_timeline.py')
    social_metrics = import_local_module('social_metrics', 'social_metrics.py')
    cdp_profile = cdp_profile_path(workdir)
    cdp_port = env_int('WEEKLY_ACCOUNT_CDP_PORT', env_int('SOCIAL_CDP_PORT', 9222))
    page_delay_min_s, page_delay_max_s = timeline_page_delay_s(account.platform, latest_per_account)
    discovered = []
    last_error = ''
    max_attempts = account_discovery_max_attempts(account.platform)
    for attempt in range(1, max_attempts + 1):
        try:
            discovery_result = account_timeline.discover_profile_post_links_with_state(
                normalize_profile_url(account.url, account.platform),
                account.platform,
                cdp_profile=cdp_profile,
                cdp_port=cdp_port,
                max_scrolls=timeline_max_scrolls(account.platform, latest_per_account),
                idle_scrolls=timeline_idle_scrolls(account.platform, latest_per_account),
                candidate_limit=max(latest_per_account * 4, latest_per_account) if latest_per_account else timeline_candidate_limit(account.platform),
                timeout_ms=env_int(f'WEEKLY_ACCOUNT_{account.platform.upper()}_TIMEOUT_MS', env_int('WEEKLY_ACCOUNT_TIMELINE_TIMEOUT_MS', 30000)),
                total_timeout_ms=timeline_total_timeout_ms(account.platform, latest_per_account),
                page_delay_min_ms=int(page_delay_min_s * 1000),
                page_delay_max_ms=int(page_delay_max_s * 1000),
                cdp_start_minimized=weekly_cdp_start_minimized(account.platform),
                date_start=None if latest_per_account else week.start,
                date_end_exclusive=None if latest_per_account else week.end_exclusive,
            )
            discovered = list(discovery_result.candidates)
            discovery_stats = discovery_result.stats if isinstance(discovery_result.stats, dict) else {}
            for key, value in discovery_stats.items():
                if isinstance(value, int):
                    stats[f'discovery_{key}'] = stats.get(f'discovery_{key}', 0) + value
            stats['valid_account_page'] = 1 if discovery_stats.get('valid_account_page') else 0
            stats['discovery_attempts'] = attempt
            if discovered or discovery_stats.get('valid_account_page'):
                break
            last_error = f'{account.platform} 账号页未发现有效时间线信号：{discovery_stats}'
        except Exception as exc:
            last_error = str(exc)
            if is_antibot_error(exc):
                stats['anti_bot_accounts'] = stats.get('anti_bot_accounts', 0) + 1
                return [], f'{account.platform} 账号页发现失败：{exc}', stats
        if attempt < max_attempts:
            sleep_between_retries(account.platform, attempt + 1)
    else:
        stats['account_abnormal'] = 1
        stats['discovery_attempts'] = max_attempts
        return [], account_abnormal_error(account.platform, last_error, max_attempts), stats

    stats['candidates'] = len(discovered)
    if account.platform == 'instagram' and discovered and not latest_per_account:
        discovered, skipped_by_discovery_date = filter_instagram_candidates_by_week(discovered, week)
        stats['filtered'] += skipped_by_discovery_date
    elif account.platform == 'tiktok' and discovered and not latest_per_account:
        discovered, skipped_by_discovery_date = filter_tiktok_candidates_by_week(discovered, week)
        stats['filtered'] += skipped_by_discovery_date
    if latest_per_account:
        discovered = discovered[:latest_per_account]
    detail_limit = timeline_detail_candidate_limit(account.platform, latest_per_account)
    if detail_limit and len(discovered) > detail_limit:
        stats['detail_candidates_capped'] = len(discovered) - detail_limit
        discovered = discovered[:detail_limit]
    safe_name = re.sub(r'[^A-Za-z0-9_.-]+', '_', account.account).strip('._-') or account.platform
    discovery_path = output_dir / f'{account.platform}_{safe_name}_{week.start:%Y%m%d}_{week.end:%Y%m%d}_discovery.json'
    discovery_path.write_text(
        json.dumps([item.to_jsonable() for item in discovered], ensure_ascii=False, indent=2),
        encoding='utf-8-sig',
    )
    if not discovered:
        return [], '', stats

    detail_delay_min_s, detail_delay_max_s = detail_delay_s(account.platform)
    scraper = social_metrics.SocialScraper(
        use_playwright=True,
        headless=True,
        timeout_ms=env_int(f'WEEKLY_ACCOUNT_{account.platform.upper()}_DETAIL_TIMEOUT_MS', env_int('WEEKLY_ACCOUNT_DETAIL_TIMEOUT_MS', 30000)),
        delay_min_s=detail_delay_min_s,
        delay_max_s=detail_delay_max_s,
        reel_grid_scrolls=reel_grid_scrolls(account.platform),
        cdp_profile=cdp_profile,
        cdp_port=cdp_port,
        use_cdp_for_instagram=account.platform == 'instagram',
        use_cdp_for_facebook=account.platform == 'facebook',
        use_cdp_for_tiktok=account.platform == 'tiktok',
        normalize_instagram_posts=False,
        enable_reel_grid_views=enable_reel_grid_views(account.platform),
        max_attempts=detail_max_attempts(account.platform),
        require_text_for_retry=True,
        require_time_for_retry=True,
        retry_missing_metrics=detail_retry_missing_metrics(account.platform),
        media_kind_aware_retry=True,
        stable_reel_grid_wait=True,
        cdp_start_minimized=weekly_cdp_start_minimized(account.platform),
        cdp_auto_close=False,
        cdp_login_timeout_ms=env_int('WEEKLY_ACCOUNT_LOGIN_TIMEOUT_MS', 300000),
        allow_manual_login=bool(env_int('WEEKLY_ACCOUNT_ALLOW_MANUAL_LOGIN', 0)),
    )
    urls = [item.url for item in discovered]
    candidate_by_url = {item.url: item for item in discovered}
    expected_handle = profile_handle_from_url(account.url, account.platform)
    posts: list[WeeklyPost] = []
    stop_detail_scan = False
    anti_bot_error = ''

    def handle_detail_result(result: Any, *, detail_index: int) -> bool:
        nonlocal anti_bot_error, stop_detail_scan
        if result_has_antibot(result):
            anti_bot_error = compact_error_text(getattr(result, 'error', '') or getattr(result, 'debug', {}))
            stats['anti_bot_details'] = stats.get('anti_bot_details', 0) + 1
            return False
        text = str(result.text or result.title or '').strip()
        url = str(result.final_url or result.source_url or '').strip()
        candidate = candidate_by_url.get(result.source_url) or candidate_by_url.get(url)
        published_value = str(result.posted_at or '').strip()
        if not url:
            stats['skipped_details'] = stats.get('skipped_details', 0) + 1
            return True
        if account.platform == 'instagram' and expected_handle:
            author_handle = str(result.author_handle or '').lstrip('@').lower()
            if not author_handle:
                stats['skipped_author_missing'] = stats.get('skipped_author_missing', 0) + 1
                return True
            if author_handle != expected_handle:
                stats['skipped_author_mismatch'] = stats.get('skipped_author_mismatch', 0) + 1
                return True
        if not published_value and account.platform == 'instagram' and candidate:
            fallback_date = instagram_candidate_date(candidate.image_alt, default_year=week.end.year)
            published_value = instagram_candidate_datetime_value(fallback_date)
        if not published_value and account.platform == 'tiktok' and candidate:
            fallback_datetime = tiktok_candidate_datetime(candidate.url)
            published_value = fallback_datetime.isoformat() if fallback_datetime else ''
        date_state = 'in-range' if latest_per_account else detail_date_state(published_value, week)
        if not latest_per_account and date_state != 'in-range':
            stats['filtered'] += 1
            if (
                published_value
                and account.platform in {'instagram', 'facebook'}
                and date_state == 'before-start'
            ):
                stop_detail_scan = True
                return False
            return True
        if account.platform == 'linkedin' and candidate and candidate.text_preview:
            text = candidate.text_preview
        if not text:
            text = candidate.text_preview if candidate else ''
        if not text and latest_per_account and candidate:
            text = candidate.image_alt
        if not text:
            stats['skipped_no_text'] = stats.get('skipped_no_text', 0) + 1
            return True
        metrics = metric_payload_from_social(result.metrics)
        if candidate:
            metrics['discovery_text_preview'] = candidate.text_preview
            metrics['discovered_scroll_index'] = candidate.scroll_index
            fallback_date = instagram_candidate_date(candidate.image_alt, default_year=week.end.year)
            if fallback_date:
                metrics['discovery_date'] = fallback_date.isoformat()
            fallback_datetime = tiktok_candidate_datetime(candidate.url) if account.platform == 'tiktok' else None
            if fallback_datetime:
                metrics['discovery_date'] = fallback_datetime.astimezone(LOCAL_TIMEZONE).date().isoformat()
        posts.append(
            WeeklyPost(
                account=account.account,
                url=url,
                text=text,
                published_at=base_datetime(published_value),
                metrics=metrics,
                platform=account.platform,
                source_account_url=account.url,
                error=str(result.error or ''),
            )
        )
        return True

    def on_detail_result(index: int, _total: int, _url: str, result: Any) -> bool:
        return handle_detail_result(result, detail_index=index)

    scraper.scrape(urls, on_result=on_detail_result)
    if stop_detail_scan:
        stats['detail_early_stop'] = stats.get('detail_early_stop', 0) + 1
    if anti_bot_error:
        return posts, f'{account.platform} 触发反爬/验证，已停止继续刷新：{anti_bot_error}', stats
    return posts, '', stats


def scrape_accounts(
    accounts: list[AccountRow],
    week: WeekRange,
    *,
    workdir: Path,
    log_dir: Path,
    job_id: str,
    latest_per_account: int | None = None,
    progress: ProgressCallback | None = None,
    on_account_posts: Callable[[list[WeeklyPost]], None] | None = None,
    on_account_result: Callable[[AccountRow, list[WeeklyPost], str, dict[str, int]], None] | None = None,
    progress_base_total: int | None = None,
    progress_initial_done: int = 0,
) -> tuple[list[WeeklyPost], list[str], dict[str, int]]:
    output_dir = log_dir / f"{job_id}_weekly_accounts"
    output_dir.mkdir(parents=True, exist_ok=True)
    posts: list[WeeklyPost] = []
    failures: list[str] = []
    stats = {'candidates': 0, 'filtered': 0}
    supported_platforms = {'x', 'youtube', 'instagram', 'facebook', 'tiktok', 'linkedin'}
    supported = [account for account in accounts if account.platform in supported_platforms]
    progress_total = progress_base_total if progress_base_total and progress_base_total > 0 else len(supported)
    progress_done = min(max(0, progress_initial_done), progress_total)
    progress_milestones = quartile_milestones(progress_total)
    halted_platforms: dict[str, str] = {}

    def report_quartile_progress(account: AccountRow | None = None) -> None:
        if not progress or not progress_total:
            return
        while progress_milestones and progress_done >= progress_milestones[0][0]:
            _point, percent = progress_milestones.pop(0)
            progress('scrape_progress', f'海外市场账号进度：{progress_done}/{progress_total}（{percent}%）')

    report_quartile_progress()
    for index, account in enumerate(supported, 1):
        account_started_at = time.perf_counter()
        skipped_for_halt = account.platform in halted_platforms
        if skipped_for_halt:
            account_posts = []
            error = f'{account.platform} 平台已因反爬/验证暂停，本账号未继续访问：{halted_platforms[account.platform]}'
            account_stats = {'candidates': 0, 'filtered': 0, 'platform_halted_skip': 1}
        else:
            try:
                if account.platform == 'x':
                    account_posts, error, account_stats = scrape_x_account(
                        account,
                        week,
                        workdir=workdir,
                        output_dir=output_dir,
                        latest_per_account=latest_per_account,
                    )
                elif account.platform == 'youtube':
                    account_posts, error, account_stats = scrape_youtube_account(account, week, latest_per_account=latest_per_account)
                elif account.platform == 'linkedin':
                    account_posts, error, account_stats = scrape_linkedin_account(
                        account,
                        week,
                        workdir=workdir,
                        output_dir=output_dir,
                        latest_per_account=latest_per_account,
                        progress=progress,
                    )
                else:
                    account_posts, error, account_stats = scrape_timeline_account(
                        account,
                        week,
                        workdir=workdir,
                        output_dir=output_dir,
                        latest_per_account=latest_per_account,
                    )
            except Exception as exc:
                account_posts = []
                error = f'{account.platform} 抓取异常：{exc}'
                account_stats = {'candidates': 0, 'filtered': 0}
        for stat_key, stat_value in account_stats.items():
            if isinstance(stat_value, int):
                stats[stat_key] = stats.get(stat_key, 0) + stat_value
        if not account_posts and not latest_per_account and not error:
            account_posts = [no_post_placeholder(account)]
            stats['no_post_accounts'] = stats.get('no_post_accounts', 0) + 1
        posts.extend(account_posts)
        if on_account_posts and account_posts:
            on_account_posts(account_posts)
        if on_account_result:
            on_account_result(account, account_posts, error, account_stats)
        if error:
            failures.append(f'{account.account}: {error}')
            stats['account_failures'] = stats.get('account_failures', 0) + 1
            if is_antibot_error(error) and account.platform != 'linkedin' and account.platform not in halted_platforms:
                halted_platforms[account.platform] = compact_error_text(error)
                stats['platform_halted_antibot'] = stats.get('platform_halted_antibot', 0) + 1
        elapsed_s = time.perf_counter() - account_started_at
        print(
            "[account-time] "
            f"{index}/{len(supported)} "
            f"elapsed_s={elapsed_s:.1f} "
            f"platform={account.platform} "
            f"status={account_entry_status(account_posts, error)} "
            f"posts={sum(1 for post in account_posts if not post.placeholder)} "
            f"placeholders={sum(1 for post in account_posts if post.placeholder)} "
            f"candidates={account_stats.get('candidates', 0)} "
            f"filtered={account_stats.get('filtered', 0)} "
            f"account={account.account}",
            file=sys.stderr,
            flush=True,
        )
        progress_done = min(progress_total, progress_done + 1)
        report_quartile_progress(account)
        if index < len(supported) and not skipped_for_halt:
            sleep_between_accounts(account)
    unsupported_by_platform: dict[str, int] = {}
    for account in accounts:
        if account.platform not in supported_platforms:
            unsupported_by_platform[account.platform] = unsupported_by_platform.get(account.platform, 0) + 1
    if unsupported_by_platform:
        unsupported_count = sum(unsupported_by_platform.values())
        stats['unsupported_accounts'] = unsupported_count
        progress_done = min(progress_total, progress_done + unsupported_count)
        report_quartile_progress()
        details = ', '.join(f'{platform}:{count}' for platform, count in sorted(unsupported_by_platform.items()))
        failures.append(f'暂未接入账号时间线抓取的平台账号：{unsupported_count} 个（{details}）')
    return posts, failures, stats


def run_weekly_account_posts(
    *,
    run_lark_json: JsonRunner,
    workdir: Path,
    log_dir: Path,
    job_id: str,
    dry_run: bool = False,
    date_range: str | None = None,
    limit_per_platform: int | None = None,
    latest_per_account: int | None = None,
    platforms: str | None = None,
    source_indexes: str | None = None,
    no_backfill: bool = False,
    base_token: str = DEFAULT_BASE_TOKEN,
    identity: str = DEFAULT_IDENTITY,
    progress: ProgressCallback | None = None,
) -> WeeklyAccountSummary:
    base_token = str(base_token or "").strip() or os.getenv("WEEKLY_ACCOUNT_BASE_TOKEN", "").strip()
    source_table_id = os.getenv("WEEKLY_ACCOUNT_SOURCE_TABLE", "").strip()
    source_view_id = os.getenv("WEEKLY_ACCOUNT_SOURCE_VIEW", "").strip()
    if not base_token:
        raise RuntimeError("Set dedicated WEEKLY_ACCOUNT_BASE_TOKEN in workflow/env/bases.env.")
    if not source_table_id or not source_view_id:
        raise RuntimeError(
            "Set WEEKLY_ACCOUNT_SOURCE_TABLE and WEEKLY_ACCOUNT_SOURCE_VIEW in workflow/env/bases.env."
        )

    week = parse_date_range(date_range)
    latest_per_account = latest_per_account if latest_per_account and latest_per_account > 0 else None
    platform_filter = parse_platform_filter(platforms)
    source_index_filter = parse_source_index_filter(source_indexes)
    table_name = result_table_name(week, latest_per_account)
    trigger_suffix = trigger_time_suffix(job_id)
    ledger_path, xlsx_path = local_export_paths(workdir, table_name, trigger_suffix=trigger_suffix)
    summary = WeeklyAccountSummary(
        table_name=table_name,
        date_range_label=week.label,
        limit_per_platform=limit_per_platform,
        platform_filter=tuple(sorted(platform_filter)),
        source_index_filter=tuple(sorted(source_index_filter)),
        latest_per_account=latest_per_account,
        local_ledger_path=str(ledger_path),
        local_xlsx_path=str(xlsx_path),
        base_backfill_skipped=no_backfill,
    )
    accounts = read_source_accounts(
        run_lark_json,
        base_token=base_token,
        table_id=source_table_id,
        view_id=source_view_id,
        identity=identity,
    )
    workbook_accounts = source_ordered_accounts(accounts)
    summary.source_accounts = len(accounts)
    summary.platform_counts = platform_distribution(accounts)
    if platform_filter:
        accounts = [account for account in accounts if account.platform in platform_filter]
    if source_index_filter:
        accounts = [account for account in accounts if account.source_order in source_index_filter]
    selected_accounts = source_ordered_accounts(limit_accounts_per_platform(accounts, limit_per_platform))
    xlsx_accounts = workbook_accounts if source_index_filter else selected_accounts
    summary.selected_accounts = len(selected_accounts)
    summary.selected_platform_counts = platform_distribution(selected_accounts)
    supported_platforms = {'x', 'youtube', 'instagram', 'facebook', 'tiktok', 'linkedin'}
    summary.supported_accounts = sum(summary.selected_platform_counts.get(platform, 0) for platform in supported_platforms)
    summary.unsupported_accounts = summary.selected_accounts - summary.supported_accounts
    if progress:
        filter_text = f"\n平台筛选：{','.join(summary.platform_filter)}" if summary.platform_filter else ""
        source_filter_text = f"\n源序号筛选：{','.join(str(item) for item in summary.source_index_filter)}" if summary.source_index_filter else ""
        latest_text = f"\n最新模式：每账号最新 {latest_per_account} 条，不按日期过滤" if latest_per_account else ""
        progress("start", f"海外市场账号开始抓取。\n日期范围：{week.label}{latest_text}{filter_text}{source_filter_text}\n全量账号：{summary.selected_accounts}\n结果表：{table_name}\n团队传阅XLSX：{xlsx_path}")

    if dry_run:
        return summary

    ledger_entries = load_ledger_entries(ledger_path, table_name)
    completed_keys = completed_account_keys(ledger_entries)
    summary.resumed_accounts = sum(1 for account in selected_accounts if account_ledger_key(account) in completed_keys)
    accounts_to_scrape = [account for account in selected_accounts if account_ledger_key(account) not in completed_keys]
    def persist_account_result(account: AccountRow, account_posts: list[WeeklyPost], error: str, account_stats: dict[str, int]) -> None:
        entry = build_ledger_entry(
            account=account,
            posts=account_posts,
            error=error,
            stats=account_stats,
            week=week,
            table_name=table_name,
            latest_per_account=latest_per_account,
        )
        append_ledger_entry(ledger_path, entry)
        ledger_entries.append(entry)
        try:
            write_local_xlsx(xlsx_path, ledger_entries, xlsx_accounts)
        except Exception as exc:
            summary.local_xlsx_error = str(exc)

    posts, failures, scrape_stats = scrape_accounts(
        accounts_to_scrape,
        week,
        workdir=workdir,
        log_dir=log_dir,
        job_id=job_id,
        latest_per_account=latest_per_account,
        progress=progress,
        on_account_result=persist_account_result,
        progress_base_total=summary.selected_accounts,
        progress_initial_done=summary.resumed_accounts,
    )
    ledger_posts = ledger_posts_for_accounts(ledger_entries, selected_accounts)
    summary.scraped_posts = sum(1 for post in ledger_posts if not post.placeholder)
    summary.no_post_accounts = sum(1 for post in ledger_posts if post.placeholder)
    summary.candidate_posts = scrape_stats.get('candidates', 0)
    summary.filtered_out_posts = scrape_stats.get('filtered', 0)
    summary.failed_accounts = scrape_stats.get('account_failures', 0) + scrape_stats.get('unsupported_accounts', 0)
    summary.failure_examples = failures[:8]
    final_completed_keys = completed_account_keys(ledger_entries)
    summary.local_completed_accounts = sum(1 for account in selected_accounts if account_ledger_key(account) in final_completed_keys)
    try:
        write_local_xlsx(xlsx_path, ledger_entries, xlsx_accounts)
    except Exception as exc:
        summary.local_xlsx_error = str(exc)
    if no_backfill:
        return summary

    table_id, created = ensure_week_table(
        run_lark_json,
        base_token=base_token,
        table_name=table_name,
        identity=identity,
        dry_run=False,
    )
    summary.table_id = table_id
    summary.created_table = created
    if not table_id:
        raise RuntimeError(f'未能创建或定位结果表：{week.table_name}')
    ensure_result_fields(run_lark_json, base_token=base_token, table_id=table_id, identity=identity)

    existing_links, existing_placeholders = read_existing_post_keys(
        run_lark_json,
        base_token=base_token,
        table_id=table_id,
        identity=identity,
    )
    posts_to_write: list[WeeklyPost] = []
    for post in ledger_posts:
        if post.placeholder:
            placeholder_key = post.account.strip()
            if placeholder_key in existing_placeholders:
                summary.skipped_existing_placeholders += 1
                continue
            posts_to_write.append(post)
            existing_placeholders.add(placeholder_key)
            continue
        if post.url and post.url in existing_links:
            summary.skipped_existing_posts += 1
            continue
        posts_to_write.append(post)
        if post.url:
            existing_links.add(post.url)
    if posts_to_write:
        summary.written_records = batch_create_posts(
            run_lark_json,
            base_token=base_token,
            table_id=table_id,
            posts=posts_to_write,
            identity=identity,
        )
        summary.written_placeholder_records = sum(1 for post in posts_to_write if post.placeholder)
    return summary


def summary_lines(summary: WeeklyAccountSummary, *, dry_run: bool = False) -> list[str]:
    lines = [
        f"日期范围：{summary.date_range_label or summary.table_name}",
        f"结果表名称：{summary.table_name}",
        f"源账号：{summary.source_accounts}",
        "平台分布：" + (", ".join(f"{k}:{v}" for k, v in sorted(summary.platform_counts.items())) or "无"),
        f"本次执行账号：{summary.selected_accounts}",
        "本次平台分布：" + (", ".join(f"{k}:{v}" for k, v in sorted(summary.selected_platform_counts.items())) or "无"),
        "写入顺序：源视图账号顺序",
        f"当前已接入时间线抓取账号：{summary.supported_accounts}",
        f"暂未接入平台账号：{summary.unsupported_accounts}",
    ]
    if summary.limit_per_platform and summary.limit_per_platform > 0:
        lines.append(f"每个平台最多：{summary.limit_per_platform}")
    if summary.platform_filter:
        lines.append(f"平台筛选：{','.join(summary.platform_filter)}")
    if summary.source_index_filter:
        lines.append(f"源序号筛选：{','.join(str(item) for item in summary.source_index_filter)}")
    if summary.latest_per_account and summary.latest_per_account > 0:
        lines.append(f"最新模式：每账号最新 {summary.latest_per_account} 条，不按日期过滤")
    if summary.local_ledger_path:
        lines.append(f"本地账本：{summary.local_ledger_path}")
    if summary.local_xlsx_path:
        lines.append(f"团队传阅XLSX：{summary.local_xlsx_path}")
    if dry_run:
        lines.append("dry-run：不创建结果表，不抓取，不写入记录。")
        return lines
    lines.extend(
        [
            f"结果表：{summary.table_id or '未知'}",
            f"断点续跑已完成账号：{summary.resumed_accounts}",
            f"本地已完成账号：{summary.local_completed_accounts}",
            f"发现候选帖子：{summary.candidate_posts}",
            f"过滤非本日期范围/无日期帖子：{summary.filtered_out_posts}",
            f"本次抓到帖子：{summary.scraped_posts}",
            f"期限内无发布账号：{summary.no_post_accounts}",
            f"跳过已存在帖子：{summary.skipped_existing_posts}",
            f"跳过已存在无发布占位：{summary.skipped_existing_placeholders}",
            f"写入新记录：{summary.written_records}",
            f"其中无发布占位行：{summary.written_placeholder_records}",
            f"异常/暂未支持账号：{summary.failed_accounts}",
        ]
    )
    if summary.base_backfill_skipped:
        lines.append("Base回填：已按 --no-backfill 跳过")
    if summary.local_xlsx_error:
        lines.append(f"XLSX生成警告：{summary.local_xlsx_error}")
    if summary.failure_examples:
        lines.append("需后续处理：" + "；".join(summary.failure_examples[:3]))
    return lines
