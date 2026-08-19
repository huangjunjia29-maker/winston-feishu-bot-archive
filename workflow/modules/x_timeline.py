#!/usr/bin/env python3
# Project-local X/Twitter profile timeline discovery.

from __future__ import annotations

import importlib.util
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore[assignment]


MODULE_DIR = Path(__file__).resolve().parent
TWITTER_EPOCH_MS = 1288834974657
TWEET_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/status(?:es)?/(\d+)(?:[/?#][^\s]*)?',
    re.IGNORECASE,
)
PROFILE_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:x\.com|twitter\.com)/(?P<handle>[A-Za-z0-9_]{1,15})(?:[/?#].*)?$',
    re.IGNORECASE,
)
RESERVED_HANDLES = {'home', 'explore', 'notifications', 'messages', 'i', 'search'}
ANTI_BOT_MARKERS = (
    'recaptcha',
    'captcha',
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
    'checking your browser',
    'complete the security check',
    'checkpoint',
)


EXTRACT_PROFILE_TWEETS_SCRIPT = r'''
({ handle }) => {
  const wanted = String(handle || '').replace(/^@/, '').toLowerCase();
  const articles = Array.from(document.querySelectorAll('article[data-testid=tweet]'));
  const exactStatus = /(?:x|twitter)\.com\/([^/]+)\/status\/(\d+)(?:[?#]|$)/i;

  return articles.map((article, index) => {
    const text = article.innerText || '';
    const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
    const links = Array.from(article.querySelectorAll('a[href]')).map((node) => node.href || '');
    let main = null;
    for (const href of links) {
      const match = href.match(exactStatus);
      if (match && match[1].toLowerCase() === wanted) {
        main = {handle: match[1], tweetId: match[2], href: 'https://x.com/' + match[1] + '/status/' + match[2]};
        break;
      }
    }
    return {
      index,
      found: Boolean(main),
      url: main ? main.href : null,
      tweetId: main ? main.tweetId : null,
      authorHandle: main ? '@' + main.handle : null,
      pinned: lines.slice(0, 4).some((line) => /^(Pinned|已置顶|置顶)/i.test(line)),
      reply: lines.some((line) => /^(Replying to|回复给|正在回复)/i.test(line)),
      repost: lines.slice(0, 3).some((line) => /(reposted|转帖了|转发了)$/i.test(line)),
      quote: lines.some((line) => /^(Quote|引用)$/i.test(line)),
      postedAt: article.querySelector('time')?.getAttribute('datetime') || null,
      textPreview: text.slice(0, 500),
    };
  });
}
'''


X_PROFILE_STATE_SCRIPT = r'''
() => {
  const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
  const text = body.replace(/\s+/g, ' ').trim();
  const hrefs = Array.from(document.querySelectorAll('a[href]'))
    .map(node => node.href || node.getAttribute('href') || '')
    .filter(Boolean);
  const samePlatformLinks = hrefs.filter(href => /(?:x|twitter)\.com/i.test(href)).length;
  const unavailablePattern = /(account doesn't exist|account suspended|profile not found|this account doesn.?t exist|try searching for another|页面不存在|账号不存在|帐号不存在|账号已被冻结|帐号已被冻结)/i;
  const noPostPattern = /(hasn.?t posted|no posts yet|还没有发布|尚未发布)/i;
  return {
    title: document.title || '',
    url: location.href || '',
    readyState: document.readyState || '',
    bodyTextLength: text.length,
    articleCount: document.querySelectorAll('article[data-testid=tweet]').length,
    samePlatformLinks,
    unavailable: unavailablePattern.test(text),
    noPostHint: noPostPattern.test(text),
  };
}
'''


@dataclass(frozen=True)
class DateRange:
    start: datetime | None = None
    end: datetime | None = None

    def contains(self, value: datetime | None) -> bool:
        if value is None:
            return True
        current = value.astimezone(timezone.utc)
        if self.start and current < self.start:
            return False
        if self.end and current >= self.end:
            return False
        return True

    def is_before_start(self, value: datetime | None) -> bool:
        if not self.start or value is None:
            return False
        return value.astimezone(timezone.utc) < self.start


@dataclass(frozen=True)
class DiscoveredTweet:
    url: str
    tweet_id: str
    author_handle: str
    category: str
    pinned: bool = False
    text_preview: str | None = None
    published_at: str | None = None
    discovered_index: int | None = None
    timeline: str = 'posts'

    def to_jsonable(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class TweetDiscoveryResult:
    tweets: list[DiscoveredTweet]
    stats: dict[str, Any]


class TimelineDiscoveryError(RuntimeError):
    pass


class AntiBotDetected(TimelineDiscoveryError):
    pass


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


def compact_spaces(text: str | None) -> str:
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def check_antibot(page: Any, stage: str) -> None:
    try:
        data = page.evaluate(
            """() => ({
                title: document.title || '',
                url: location.href || '',
                body: document.body ? (document.body.innerText || '').slice(0, 10000) : '',
                iframes: Array.from(document.querySelectorAll('iframe[src]')).map(node => node.src || '').join(' '),
            })"""
        )
    except Exception:
        return
    if not isinstance(data, dict):
        return
    text = '\n'.join(str(data.get(key) or '') for key in ('title', 'url', 'body', 'iframes')).lower()
    marker = next((item for item in ANTI_BOT_MARKERS if item in text), '')
    if not marker:
        return
    title = compact_spaces(str(data.get('title') or ''))[:120]
    url = str(data.get('url') or '')[:180]
    raise AntiBotDetected(f"X anti-bot detected at {stage}: marker={marker} title={title} url={url}")


def profile_state(page: Any) -> dict[str, Any]:
    try:
        data = page.evaluate(X_PROFILE_STATE_SCRIPT)
    except Exception as exc:
        return {'evaluation_error': str(exc).splitlines()[0]}
    return data if isinstance(data, dict) else {}


def is_valid_profile_state(state: dict[str, Any]) -> bool:
    if state.get('unavailable') or state.get('evaluation_error'):
        return False
    if int(state.get('articleCount') or 0) > 0:
        return True
    if state.get('noPostHint'):
        return True
    return int(state.get('bodyTextLength') or 0) >= 300 and int(state.get('samePlatformLinks') or 0) >= 5


def import_social_metrics() -> Any:
    module_name = 'weekly_account_social_metrics'
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = MODULE_DIR / 'social_metrics.py'
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f'Cannot import social_metrics module: {module_path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def normalize_profile_url(raw: str) -> tuple[str, str]:
    value = raw.strip()
    parsed = urlparse(value)
    if parsed.netloc.lower().endswith(('x.com', 'twitter.com')):
        parts = [part for part in parsed.path.split('/') if part]
        if parts:
            value = f'https://x.com/{parts[0]}'
    match = PROFILE_URL_RE.search(value)
    if not match:
        raise ValueError(f'Not a valid X/Twitter profile URL: {raw}')
    handle = match.group('handle')
    if handle.lower() in RESERVED_HANDLES:
        raise ValueError(f'Not a valid X/Twitter profile URL: {raw}')
    return f'https://x.com/{handle}', handle


def tweet_id_from_url(url: str) -> str | None:
    match = TWEET_URL_RE.search(url)
    return match.group(1) if match else None


def tweet_datetime_from_id(tweet_id: str | None) -> datetime | None:
    if not tweet_id or not tweet_id.isdigit():
        return None
    try:
        timestamp_ms = (int(tweet_id) >> 22) + TWITTER_EPOCH_MS
    except ValueError:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)


def parse_posted_at(value: str | None) -> datetime | None:
    if not value:
        return None
    clean = value.strip()
    if clean.endswith('Z'):
        clean = clean[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def item_published_datetime(tweet_id: str | None, posted_at: str | None) -> datetime | None:
    return parse_posted_at(posted_at) or tweet_datetime_from_id(tweet_id)


def format_datetime_label(value: datetime | None) -> str:
    if value is None:
        return ''
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def date_range_from_local_dates(start: date, end_exclusive: date, tz_name: str = 'Asia/Shanghai') -> DateRange:
    if ZoneInfo is not None:
        local_tz = ZoneInfo(tz_name)
    else:
        local_tz = timezone(timedelta(hours=8))
    start_dt = datetime.combine(start, datetime_time.min, tzinfo=local_tz).astimezone(timezone.utc)
    end_dt = datetime.combine(end_exclusive, datetime_time.min, tzinfo=local_tz).astimezone(timezone.utc)
    return DateRange(start=start_dt, end=end_dt)


def category_from_raw(raw: dict[str, Any]) -> str:
    if raw.get('repost'):
        category = 'repost'
    elif raw.get('reply'):
        category = 'reply'
    elif raw.get('quote'):
        category = 'quote'
    else:
        category = 'original'
    if raw.get('pinned'):
        return f'pinned/{category}'
    return category


def discover_on_page(
    page: Any,
    target_url: str,
    *,
    handle: str,
    target_name: str,
    date_range: DateRange,
    count: int,
    include_pinned: bool,
    include_replies: bool,
    include_quotes: bool,
    include_reposts: bool,
    max_scrolls: int,
    idle_scrolls: int,
    page_delay_min_s: float,
    page_delay_max_s: float,
    timeout_ms: int,
) -> list[DiscoveredTweet]:
    page.goto(target_url, wait_until='domcontentloaded', timeout=timeout_ms)
    page.wait_for_timeout(2500)
    check_antibot(page, 'after-goto')

    pinned: DiscoveredTweet | None = None
    originals: dict[str, DiscoveredTweet] = {}
    seen_any_ids: set[str] = set()
    older_than_start_seen = False
    seen_order = 0
    idle_count = 0
    last_seen_total = -1
    last_pinned = False
    idle_scrolls = max(1, idle_scrolls)

    for scroll_index in range(max(0, max_scrolls) + 1):
        check_antibot(page, f'scroll-{scroll_index}')
        try:
            raw_items = page.evaluate(EXTRACT_PROFILE_TWEETS_SCRIPT, {'handle': handle})
        except Exception:
            raw_items = []

        for raw in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(raw, dict) or not raw.get('found'):
                continue
            if raw.get('reply') and not include_replies:
                continue
            if raw.get('quote') and not include_quotes:
                continue
            if raw.get('repost') and not include_reposts:
                continue
            tweet_id = str(raw.get('tweetId') or '')
            url = str(raw.get('url') or '')
            if not tweet_id or not url:
                continue

            if tweet_id not in seen_any_ids:
                seen_any_ids.add(tweet_id)
                seen_order += 1
            is_pinned = bool(raw.get('pinned'))
            published_dt = item_published_datetime(tweet_id, raw.get('postedAt'))
            if not date_range.contains(published_dt):
                if not is_pinned and date_range.is_before_start(published_dt):
                    older_than_start_seen = True
                continue

            item = DiscoveredTweet(
                url=url,
                tweet_id=tweet_id,
                author_handle=str(raw.get('authorHandle') or f'@{handle}'),
                category=category_from_raw(raw),
                pinned=is_pinned,
                text_preview=raw.get('textPreview'),
                published_at=format_datetime_label(published_dt) if published_dt else raw.get('postedAt'),
                discovered_index=seen_order,
                timeline=target_name,
            )
            if is_pinned and include_pinned and pinned is None:
                pinned = item
            if not is_pinned and tweet_id not in originals:
                originals[tweet_id] = item

        enough = len(originals) >= count
        pinned_resolved = (not include_pinned) or pinned is not None or scroll_index >= 2
        current_seen_total = len(seen_any_ids)
        current_pinned = pinned is not None
        if current_seen_total > last_seen_total or current_pinned != last_pinned:
            idle_count = 0
        else:
            idle_count += 1
        last_seen_total = current_seen_total
        last_pinned = current_pinned

        if date_range.start and older_than_start_seen and pinned_resolved:
            break
        if enough and pinned_resolved:
            break
        if pinned_resolved and idle_count >= idle_scrolls:
            break

        page.mouse.wheel(0, 800)
        delay = random.uniform(max(0.0, page_delay_min_s), max(page_delay_min_s, page_delay_max_s))
        if delay > 0:
            page.wait_for_timeout(int(delay * 1000))
        check_antibot(page, f'after-scroll-{scroll_index}')

    selected: list[DiscoveredTweet] = []
    if pinned and include_pinned:
        selected.append(pinned)
    selected.extend(list(originals.values())[:count])
    return selected


def discover_x_profile_tweets(
    profile_url: str,
    *,
    start: date,
    end_exclusive: date,
    cdp_profile: Path,
    cdp_port: int,
    count: int = 80,
    timeline: str = 'all',
    include_pinned: bool = False,
    include_replies: bool = True,
    include_quotes: bool = True,
    include_reposts: bool = False,
    max_scrolls: int = 50,
    idle_scrolls: int = 3,
    page_delay_min_s: float = 1.0,
    page_delay_max_s: float = 2.5,
    timeout_ms: int = 30_000,
    cdp_start_minimized: bool | None = None,
    timezone_name: str = 'Asia/Shanghai',
) -> list[DiscoveredTweet]:
    return discover_x_profile_tweets_with_state(
        profile_url,
        start=start,
        end_exclusive=end_exclusive,
        cdp_profile=cdp_profile,
        cdp_port=cdp_port,
        count=count,
        timeline=timeline,
        include_pinned=include_pinned,
        include_replies=include_replies,
        include_quotes=include_quotes,
        include_reposts=include_reposts,
        max_scrolls=max_scrolls,
        idle_scrolls=idle_scrolls,
        page_delay_min_s=page_delay_min_s,
        page_delay_max_s=page_delay_max_s,
        timeout_ms=timeout_ms,
        cdp_start_minimized=cdp_start_minimized,
        timezone_name=timezone_name,
    ).tweets


def discover_x_profile_tweets_with_state(
    profile_url: str,
    *,
    start: date,
    end_exclusive: date,
    cdp_profile: Path,
    cdp_port: int,
    count: int = 80,
    timeline: str = 'all',
    include_pinned: bool = False,
    include_replies: bool = True,
    include_quotes: bool = True,
    include_reposts: bool = False,
    max_scrolls: int = 50,
    idle_scrolls: int = 3,
    page_delay_min_s: float = 1.0,
    page_delay_max_s: float = 2.5,
    timeout_ms: int = 30_000,
    cdp_start_minimized: bool | None = None,
    timezone_name: str = 'Asia/Shanghai',
) -> TweetDiscoveryResult:
    normalized_profile_url, handle = normalize_profile_url(profile_url)
    active_range = date_range_from_local_dates(start, end_exclusive, timezone_name)
    targets: list[tuple[str, str]] = []
    if timeline in {'posts', 'all'}:
        targets.append(('posts', normalized_profile_url))
    if timeline in {'replies', 'all'}:
        targets.append(('replies', f'{normalized_profile_url}/with_replies'))
    if not targets:
        raise ValueError(f'Unsupported X timeline: {timeline}')

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise TimelineDiscoveryError('Missing playwright dependency') from exc

    social_metrics = import_social_metrics()
    if cdp_start_minimized is None:
        cdp_start_minimized = env_bool('WEEKLY_ACCOUNT_CDP_START_MINIMIZED', True)
    session = social_metrics.ensure_cdp_profile(
        cdp_profile,
        cdp_port,
        normalized_profile_url,
        start_minimized=cdp_start_minimized,
        auto_close=False,
    )
    discovered: list[DiscoveredTweet] = []
    seen_ids: set[str] = set()
    stats: dict[str, Any] = {'valid_account_page': False, 'page_state': {}, 'observed_tweets': 0}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(session.url)
            context = browser.contexts[0] if browser.contexts else browser.new_context(locale='en-US')
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            try:
                for target_name, target_url in targets:
                    items = discover_on_page(
                        page,
                        target_url,
                        handle=handle,
                        target_name=target_name,
                        date_range=active_range,
                        count=count,
                        include_pinned=include_pinned,
                        include_replies=include_replies or target_name == 'replies',
                        include_quotes=include_quotes,
                        include_reposts=include_reposts,
                        max_scrolls=max_scrolls,
                        idle_scrolls=idle_scrolls,
                        page_delay_min_s=page_delay_min_s,
                        page_delay_max_s=page_delay_max_s,
                        timeout_ms=timeout_ms,
                    )
                    state = profile_state(page)
                    stats['page_state'] = state
                    stats['observed_tweets'] = max(int(stats.get('observed_tweets') or 0), int(state.get('articleCount') or 0))
                    if state.get('unavailable'):
                        raise TimelineDiscoveryError(f'X account page unavailable: {state}')
                    for item in items:
                        if item.tweet_id in seen_ids:
                            continue
                        seen_ids.add(item.tweet_id)
                        discovered.append(item)
                        if len(discovered) >= count:
                            break
                    if len(discovered) >= count:
                        break
            finally:
                page.close()
    except Exception as exc:
        raise TimelineDiscoveryError(str(exc).splitlines()[0]) from exc
    finally:
        if getattr(session, 'auto_close', False):
            session.close()
    stats['valid_account_page'] = bool(discovered or int(stats.get('observed_tweets') or 0) > 0 or is_valid_profile_state(stats.get('page_state') or {}))
    return TweetDiscoveryResult(discovered, stats)


def self_test() -> None:
    profile, handle = normalize_profile_url('https://twitter.com/xpenguk?ref=foo')
    assert profile == 'https://x.com/xpenguk'
    assert handle == 'xpenguk'
    tweet_id = '2063921050392691150'
    assert tweet_id_from_url(f'https://x.com/test/status/{tweet_id}') == tweet_id
    assert tweet_datetime_from_id(tweet_id) is not None
    range_ = date_range_from_local_dates(date(2026, 6, 15), date(2026, 6, 22))
    assert range_.start and range_.end and range_.start < range_.end


if __name__ == '__main__':
    self_test()
