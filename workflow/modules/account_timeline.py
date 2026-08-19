#!/usr/bin/env python3
"""Best-effort account timeline discovery for non-X social platforms."""

from __future__ import annotations

import importlib.util
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None  # type: ignore[assignment]


MODULE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class TimelineCandidate:
    url: str
    platform: str
    source_account_url: str
    text_preview: str = ""
    image_alt: str = ""
    discovered_index: int = 0
    scroll_index: int = 0

    def to_jsonable(self) -> dict[str, Any]:
        return self.__dict__.copy()


class TimelineDiscoveryError(RuntimeError):
    pass


class AntiBotDetected(TimelineDiscoveryError):
    pass


@dataclass(frozen=True)
class TimelineDiscoveryResult:
    candidates: list[TimelineCandidate]
    stats: dict[str, Any]


def import_social_metrics() -> Any:
    module_name = "weekly_account_social_metrics"
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


def random_delay_ms(min_ms: int, max_ms: int) -> int:
    low = max(0, min_ms)
    high = max(low, max_ms)
    return int(random.uniform(low, high))


def compact_spaces(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def instagram_alt_author(image_alt: str | None) -> str:
    value = compact_spaces(image_alt)
    if not value:
        return ""
    match = re.match(r"Photo(?: shared)? by (.+?)(?: on | in | with | tagging |\. |$)", value, flags=re.I)
    return compact_spaces(match.group(1)).lower() if match else ""


def filter_instagram_candidates(candidates: list[TimelineCandidate]) -> list[TimelineCandidate]:
    authors = [instagram_alt_author(candidate.image_alt) for candidate in candidates]
    counts = Counter(author for author in authors if author)
    if len(counts) < 2:
        return candidates
    profile_author = next((author for author in authors if author), "")
    if not profile_author:
        return candidates
    filtered = [
        candidate for candidate in candidates
        if not instagram_alt_author(candidate.image_alt) or instagram_alt_author(candidate.image_alt) == profile_author
    ]
    return filtered or candidates


def profile_handle_from_url(url: str, platform: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if platform == "instagram" and parts:
        return parts[0].lstrip("@").lower()
    if platform == "tiktok" and parts and parts[0].startswith("@"):
        return parts[0].lower()
    return ""


def local_timezone(tz_name: str) -> Any:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    if tz_name == "Asia/Shanghai":
        return timezone(timedelta(hours=8))
    return timezone.utc


def tiktok_datetime_from_url(url: str | None) -> datetime | None:
    match = re.search(r"/video/(\d{12,})", str(url or ""))
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


def candidate_local_date(candidate: TimelineCandidate, *, timezone_name: str) -> date | None:
    if candidate.platform == "tiktok":
        parsed = tiktok_datetime_from_url(candidate.url)
        if parsed:
            return parsed.astimezone(local_timezone(timezone_name)).date()
    return None


def candidate_date_state(
    candidate: TimelineCandidate,
    *,
    date_start: date | None,
    date_end_exclusive: date | None,
    timezone_name: str,
) -> str:
    candidate_date = candidate_local_date(candidate, timezone_name=timezone_name)
    if date_start and candidate_date and candidate_date < date_start:
        return "before-start"
    if date_end_exclusive and candidate_date and candidate_date >= date_end_exclusive:
        return "after-end"
    return "in-range-or-unknown"


def strip_tracking_query(url: str, keep: set[str] | None = None) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    keep = keep or set()
    kept: list[tuple[str, str]] = []
    for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
        if key in keep:
            kept.extend((key, value) for value in values)
    query = urlencode(kept)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", query, ""))


def canonicalize_candidate_url(raw_url: str, platform: str, account_url: str) -> str | None:
    if not raw_url:
        return None
    value = urljoin(account_url, raw_url).strip()
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"

    if platform == "instagram":
        if not host.endswith("instagram.com"):
            return None
        match = re.search(r"/(?:p|reel|reels)/([A-Za-z0-9_-]+)", path)
        if not match:
            return None
        kind = "p" if "/p/" in path else "reel"
        return f"https://www.instagram.com/{kind}/{match.group(1)}/"

    if platform == "tiktok":
        if not host.endswith("tiktok.com"):
            return None
        match = re.search(r"/@[^/]+/video/(\d+)", path)
        if match:
            handle = path.split("/video/", 1)[0].strip("/")
            expected = profile_handle_from_url(account_url, platform)
            if expected and handle.lower() != expected:
                return None
            return f"https://www.tiktok.com/{handle}/video/{match.group(1)}"
        return None

    if platform == "facebook":
        if not (host.endswith("facebook.com") or host == "fb.watch"):
            return None
        patterns = [
            r"/reel/(\d+)",
            r"/share/r/([A-Za-z0-9_-]+)",
            r"/share/v/([A-Za-z0-9_-]+)",
            r"/watch/?$",
            r"/posts/([A-Za-z0-9_.:-]+)",
            r"/videos/(\d+)",
            r"/permalink\.php$",
            r"/story\.php$",
        ]
        if not any(re.search(pattern, path) for pattern in patterns):
            return None
        keep = {"v", "story_fbid", "id", "fbid"}
        return strip_tracking_query(urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, "")), keep=keep)

    if platform == "linkedin":
        if not host.endswith("linkedin.com"):
            return None
        if re.fullmatch(r"/company/[^/]+/posts/?", path):
            return None
        if "/feed/update/" in path or re.search(r"/posts/.+", path) or "/pulse/" in path:
            return strip_tracking_query(urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, "")))
        return None

    return None


COLLECT_LINKS_SCRIPT = r"""
({ platform }) => {
  const main = document.querySelector('main') || document.body;
  const header = main.querySelector('header');
  const headerBottom = header ? Math.round(header.getBoundingClientRect().bottom + window.scrollY) : 0;
  const markerWords = [
    'suggested for you', 'suggested', 'recommended', 'more posts from',
    '推荐', '为你推荐', '更多帖子', '你可能', '探索更多'
  ];
  let suggestedTop = 0;
  if (platform === 'instagram') {
    const all = Array.from(main.querySelectorAll('span, div, h2, h3'));
    for (const node of all) {
      const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (!text || text.length > 120) continue;
      if (!markerWords.some(word => text.includes(word))) continue;
      const rect = node.getBoundingClientRect();
      const top = Math.round(rect.top + window.scrollY);
      if (top > headerBottom + 100 && (!suggestedTop || top < suggestedTop)) suggestedTop = top;
    }
  }
  const nodes = Array.from(document.querySelectorAll('a[href]'));
  return nodes.map((node, index) => {
    const rect = node.getBoundingClientRect();
    const imageAlt = Array.from(node.querySelectorAll('img'))
      .map(img => img.getAttribute('alt') || '')
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 500);
    const text = (node.innerText || node.getAttribute('aria-label') || node.getAttribute('title') || '')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 240);
    return {
      index,
      href: node.href || node.getAttribute('href') || '',
      text,
      image_alt: imageAlt,
      top: Math.round(rect.top + window.scrollY),
      header_bottom: headerBottom,
      suggested_top: suggestedTop,
      in_main: Boolean(node.closest('main')),
      visible: rect.width > 0 && rect.height > 0,
    };
  });
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


ANTIBOT_CHECK_SCRIPT = r"""
() => {
  const text = [
    document.title || '',
    location.href || '',
    document.body ? (document.body.innerText || '') : '',
    Array.from(document.querySelectorAll('iframe[src]')).map(node => node.src || '').join(' '),
  ].join('\n').toLowerCase();
  const markers = [
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
    'li.protechts.net',
    'checkpoint',
  ];
  const marker = markers.find(item => text.includes(item));
  return marker ? {detected: true, marker, title: document.title || '', url: location.href || ''} : {detected: false};
}
"""


VALID_PAGE_STATE_SCRIPT = r"""
({ platform }) => {
  const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';
  const text = body.replace(/\s+/g, ' ').trim();
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  const hrefs = anchors.map(node => node.href || node.getAttribute('href') || '').filter(Boolean);
  const platformHost = {
    instagram: 'instagram.com',
    facebook: 'facebook.com',
    tiktok: 'tiktok.com',
    linkedin: 'linkedin.com',
  }[platform] || '';
  const samePlatformLinks = platformHost ? hrefs.filter(href => href.toLowerCase().includes(platformHost)).length : hrefs.length;
  const postLinkPatterns = {
    instagram: /\/(?:p|reel|reels)\//i,
    facebook: /\/(?:reel|share\/r|share\/v|posts|videos)\//i,
    tiktok: /\/@[^/]+\/video\/\d+/i,
    linkedin: /\/(?:feed\/update|posts|pulse)\//i,
  };
  const postPattern = postLinkPatterns[platform];
  const postLikeLinks = postPattern ? hrefs.filter(href => postPattern.test(href)).length : 0;
  const unavailablePattern = /(page isn't available|page not found|content isn't available|this page isn't available|sorry, this content isn't available|couldn't find this account|user not found|profile not found|页面不存在|无法访问|内容不可用|找不到)/i;
  const loginPattern = /(log in|sign in|登录|登入|注册|join linkedin|加入 linkedin|create account)/i;
  return {
    title: document.title || '',
    url: location.href || '',
    readyState: document.readyState || '',
    bodyTextLength: text.length,
    samePlatformLinks,
    postLikeLinks,
    hasMain: Boolean(document.querySelector('main')),
    unavailable: unavailablePattern.test(text),
    loginLike: loginPattern.test(text),
  };
}
"""


def check_antibot(page: Any, platform: str, stage: str) -> None:
    try:
        data = page.evaluate(ANTIBOT_CHECK_SCRIPT)
    except Exception:
        return
    if not isinstance(data, dict) or not data.get("detected"):
        return
    marker = str(data.get("marker") or "unknown")
    title = compact_spaces(str(data.get("title") or ""))[:120]
    url = str(data.get("url") or "")[:180]
    raise AntiBotDetected(f"{platform} anti-bot detected at {stage}: marker={marker} title={title} url={url}")


def page_validity_state(page: Any, platform: str) -> dict[str, Any]:
    try:
        data = page.evaluate(VALID_PAGE_STATE_SCRIPT, {"platform": platform})
    except Exception as exc:
        return {"evaluation_error": str(exc).splitlines()[0]}
    return data if isinstance(data, dict) else {}


def is_valid_account_page_state(state: dict[str, Any], platform: str) -> bool:
    if state.get("unavailable"):
        return False
    if state.get("evaluation_error"):
        return False
    body_len = int(state.get("bodyTextLength") or 0)
    same_platform_links = int(state.get("samePlatformLinks") or 0)
    post_like_links = int(state.get("postLikeLinks") or 0)
    has_main = bool(state.get("hasMain"))
    if platform in {"instagram", "facebook", "linkedin"} and post_like_links > 0:
        return True
    if platform == "tiktok" and (post_like_links > 0 or same_platform_links >= 8):
        return True
    return has_main and body_len >= 200 and same_platform_links >= 3


def collect_page_candidates(raw_links: list[dict[str, Any]], platform: str, account_url: str, scroll_index: int) -> list[TimelineCandidate]:
    candidates: list[TimelineCandidate] = []
    for raw in raw_links:
        if platform == "instagram":
            if not raw.get("visible") or not raw.get("in_main"):
                continue
            top = int(raw.get("top") or 0)
            header_bottom = int(raw.get("header_bottom") or 0)
            suggested_top = int(raw.get("suggested_top") or 0)
            if header_bottom and top < header_bottom - 20:
                continue
            if suggested_top and top >= suggested_top - 20:
                continue
        url = canonicalize_candidate_url(str(raw.get("href") or ""), platform, account_url)
        if not url:
            continue
        candidates.append(
            TimelineCandidate(
                url=url,
                platform=platform,
                source_account_url=account_url,
                text_preview=compact_spaces(str(raw.get("text") or ""))[:240],
                image_alt=compact_spaces(str(raw.get("image_alt") or ""))[:500],
                discovered_index=int(raw.get("index") or 0),
                scroll_index=scroll_index,
            )
        )
    return candidates


def discover_profile_post_links(
    account_url: str,
    platform: str,
    *,
    cdp_profile: Path,
    cdp_port: int,
    max_scrolls: int | None = None,
    idle_scrolls: int | None = None,
    candidate_limit: int | None = None,
    timeout_ms: int | None = None,
    total_timeout_ms: int | None = None,
    page_delay_ms: int | None = None,
    page_delay_min_ms: int | None = None,
    page_delay_max_ms: int | None = None,
    cdp_start_minimized: bool | None = None,
    date_start: date | None = None,
    date_end_exclusive: date | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> list[TimelineCandidate]:
    result = discover_profile_post_links_with_state(
        account_url,
        platform,
        cdp_profile=cdp_profile,
        cdp_port=cdp_port,
        max_scrolls=max_scrolls,
        idle_scrolls=idle_scrolls,
        candidate_limit=candidate_limit,
        timeout_ms=timeout_ms,
        total_timeout_ms=total_timeout_ms,
        page_delay_ms=page_delay_ms,
        page_delay_min_ms=page_delay_min_ms,
        page_delay_max_ms=page_delay_max_ms,
        cdp_start_minimized=cdp_start_minimized,
        date_start=date_start,
        date_end_exclusive=date_end_exclusive,
        timezone_name=timezone_name,
    )
    return result.candidates


def discover_profile_post_links_with_state(
    account_url: str,
    platform: str,
    *,
    cdp_profile: Path,
    cdp_port: int,
    max_scrolls: int | None = None,
    idle_scrolls: int | None = None,
    candidate_limit: int | None = None,
    timeout_ms: int | None = None,
    total_timeout_ms: int | None = None,
    page_delay_ms: int | None = None,
    page_delay_min_ms: int | None = None,
    page_delay_max_ms: int | None = None,
    cdp_start_minimized: bool | None = None,
    date_start: date | None = None,
    date_end_exclusive: date | None = None,
    timezone_name: str = "Asia/Shanghai",
) -> TimelineDiscoveryResult:
    if platform not in {"instagram", "facebook", "tiktok", "linkedin"}:
        return TimelineDiscoveryResult([], {"valid_account_page": False, "unsupported_platform": True})
    max_scrolls = max_scrolls if max_scrolls is not None else env_int("WEEKLY_ACCOUNT_TIMELINE_MAX_SCROLLS", 30)
    idle_scrolls = idle_scrolls if idle_scrolls is not None else env_int("WEEKLY_ACCOUNT_TIMELINE_IDLE_SCROLLS", 4)
    candidate_limit = candidate_limit if candidate_limit is not None else env_int("WEEKLY_ACCOUNT_TIMELINE_CANDIDATE_LIMIT", 120)
    timeout_ms = timeout_ms if timeout_ms is not None else env_int("WEEKLY_ACCOUNT_TIMELINE_TIMEOUT_MS", 30000)
    if page_delay_min_ms is None:
        page_delay_min_ms = env_int("WEEKLY_ACCOUNT_TIMELINE_PAGE_DELAY_MIN_MS", int(env_float("WEEKLY_ACCOUNT_TIMELINE_PAGE_DELAY_MIN", 3.0) * 1000))
    if page_delay_max_ms is None:
        default_delay_ms = page_delay_ms if page_delay_ms is not None else env_int("WEEKLY_ACCOUNT_TIMELINE_PAGE_DELAY_MS", 8000)
        page_delay_max_ms = env_int("WEEKLY_ACCOUNT_TIMELINE_PAGE_DELAY_MAX_MS", int(env_float("WEEKLY_ACCOUNT_TIMELINE_PAGE_DELAY_MAX", default_delay_ms / 1000) * 1000))
    if total_timeout_ms is None:
        total_timeout_ms = env_int(
            "WEEKLY_ACCOUNT_TIMELINE_TOTAL_TIMEOUT_MS",
            max(timeout_ms * 3, timeout_ms + page_delay_max_ms * (max_scrolls + 3)),
        )
    if cdp_start_minimized is None:
        cdp_start_minimized = bool(env_int("WEEKLY_ACCOUNT_CDP_START_MINIMIZED", 1))
    started_at = time.monotonic()
    deadline = started_at + max(total_timeout_ms, timeout_ms) / 1000

    def remaining_ms() -> int:
        return max(1, int((deadline - time.monotonic()) * 1000))

    def check_deadline(stage: str) -> None:
        if time.monotonic() >= deadline:
            elapsed_s = time.monotonic() - started_at
            raise TimelineDiscoveryError(
                f"{platform} account discovery exceeded {total_timeout_ms}ms at {stage} after {elapsed_s:.1f}s"
            )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise TimelineDiscoveryError("playwright is not installed") from exc

    social_metrics = import_social_metrics()
    session = social_metrics.ensure_cdp_profile(
        cdp_profile,
        cdp_port,
        account_url,
        start_minimized=cdp_start_minimized,
        auto_close=False,
    )

    seen: dict[str, TimelineCandidate] = {}
    observed_urls: set[str] = set()
    validity_state: dict[str, Any] = {}
    stats: dict[str, Any] = {
        "valid_account_page": False,
        "observed_urls": 0,
        "page_state": {},
    }
    print(
        f"[timeline-discovery] start platform={platform} account={account_url} max_scrolls={max_scrolls} "
        f"idle_scrolls={idle_scrolls} candidate_limit={candidate_limit} timeout_ms={timeout_ms} total_timeout_ms={total_timeout_ms} "
        f"page_delay_ms={page_delay_min_ms}-{page_delay_max_ms} "
        f"date_start={date_start or ''} date_end_exclusive={date_end_exclusive or ''}",
        file=sys.stderr,
        flush=True,
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(session.url, timeout=min(timeout_ms, remaining_ms()))
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)
        try:
            check_deadline("before-goto")
            page.goto(account_url, wait_until="domcontentloaded", timeout=min(timeout_ms, remaining_ms()))
            page.wait_for_timeout(min(random_delay_ms(page_delay_min_ms, page_delay_max_ms), remaining_ms()))
            check_antibot(page, platform, "after-goto")
            validity_state = page_validity_state(page, platform)
            stats["page_state"] = validity_state
            if validity_state.get("unavailable"):
                raise TimelineDiscoveryError(f"{platform} account page unavailable: {validity_state}")
            try:
                check_deadline("dismiss-overlays")
                page.keyboard.press("Escape")
                page.evaluate(DISMISS_OVERLAYS_SCRIPT)
                page.wait_for_timeout(min(500, remaining_ms()))
            except Exception:
                pass
            idle = 0
            last_observed_count = 0
            older_than_start_seen = False
            for scroll_index in range(max_scrolls + 1):
                check_deadline(f"scroll-{scroll_index}")
                page.set_default_timeout(min(timeout_ms, remaining_ms()))
                if platform == "linkedin":
                    try:
                        page.keyboard.press("Escape")
                        page.evaluate(DISMISS_OVERLAYS_SCRIPT)
                    except Exception:
                        pass
                check_antibot(page, platform, f"scroll-{scroll_index}")
                raw_links = page.evaluate(COLLECT_LINKS_SCRIPT, {"platform": platform})
                for candidate in collect_page_candidates(raw_links, platform, account_url, scroll_index):
                    first_observed = candidate.url not in observed_urls
                    observed_urls.add(candidate.url)
                    date_state = candidate_date_state(
                        candidate,
                        date_start=date_start,
                        date_end_exclusive=date_end_exclusive,
                        timezone_name=timezone_name,
                    )
                    if date_state == "before-start":
                        # The first viewport can contain pinned older posts. Do not use
                        # those as proof that the normal feed is already before range.
                        if first_observed and scroll_index > 0:
                            older_than_start_seen = True
                        continue
                    if date_state == "after-end":
                        continue
                    seen.setdefault(candidate.url, candidate)
                if len(seen) >= candidate_limit:
                    break
                current_observed_count = len(observed_urls)
                if current_observed_count <= last_observed_count:
                    idle += 1
                else:
                    idle = 0
                last_observed_count = current_observed_count
                if older_than_start_seen:
                    print(
                        f"[timeline-discovery] stop-before-start platform={platform} account={account_url} "
                        f"scroll={scroll_index} candidates={len(seen)} observed={len(observed_urls)}",
                        file=sys.stderr,
                        flush=True,
                    )
                    break
                if idle >= idle_scrolls:
                    break
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(min(random_delay_ms(page_delay_min_ms, page_delay_max_ms), remaining_ms()))
            candidates = list(seen.values())
            if platform == "instagram":
                candidates = filter_instagram_candidates(candidates)
            stats["observed_urls"] = len(observed_urls)
            stats["raw_candidates"] = len(seen)
            stats["candidates"] = len(candidates[:candidate_limit])
            stats["valid_account_page"] = bool(
                candidates
                or observed_urls
                or is_valid_account_page_state(validity_state, platform)
            )
            elapsed_s = time.monotonic() - started_at
            print(
                f"[timeline-discovery] done platform={platform} account={account_url} elapsed_s={elapsed_s:.1f} "
                f"candidates={len(candidates[:candidate_limit])} valid_account_page={str(stats['valid_account_page']).lower()}",
                file=sys.stderr,
                flush=True,
            )
            return TimelineDiscoveryResult(candidates[:candidate_limit], stats)
        except Exception as exc:
            elapsed_s = time.monotonic() - started_at
            print(
                f"[timeline-discovery] failed platform={platform} account={account_url} elapsed_s={elapsed_s:.1f} error={exc}",
                file=sys.stderr,
                flush=True,
            )
            raise
        finally:
            try:
                page.close()
            except Exception:
                pass


def _self_test() -> None:
    assert canonicalize_candidate_url("/reel/abc/", "instagram", "https://www.instagram.com/x/") == "https://www.instagram.com/reel/abc/"
    assert canonicalize_candidate_url("https://www.tiktok.com/@xpeng/video/123?x=1", "tiktok", "https://www.tiktok.com/@xpeng") == "https://www.tiktok.com/@xpeng/video/123"
    assert canonicalize_candidate_url("https://www.linkedin.com/feed/update/urn:li:activity:123/?trk=x", "linkedin", "https://www.linkedin.com/company/x/") == "https://www.linkedin.com/feed/update/urn:li:activity:123"
    assert canonicalize_candidate_url("https://www.linkedin.com/company/xpeng/posts/", "linkedin", "https://www.linkedin.com/company/xpeng/") is None
    filtered = filter_instagram_candidates([
        TimelineCandidate("https://www.instagram.com/p/1/", "instagram", "", image_alt="Photo by XPENG Port Melbourne on June 26, 2026."),
        TimelineCandidate("https://www.instagram.com/p/2/", "instagram", "", image_alt="Photo shared by XPENG Port Melbourne in Melbourne."),
        TimelineCandidate("https://www.instagram.com/p/3/", "instagram", "", image_alt="Photo shared by XPENG Australia on June 26, 2026 tagging @xpengmelbourne."),
    ])
    assert [candidate.url for candidate in filtered] == ["https://www.instagram.com/p/1/", "https://www.instagram.com/p/2/"]
    recent_tiktok = TimelineCandidate("https://www.tiktok.com/@xpeng/video/7655226745653710102", "tiktok", "")
    parsed_tiktok = tiktok_datetime_from_url(recent_tiktok.url)
    assert parsed_tiktok is not None and parsed_tiktok.isoformat() == "2026-06-25T07:09:30+00:00"
    assert candidate_local_date(recent_tiktok, timezone_name="Asia/Shanghai") == date(2026, 6, 25)
    assert candidate_date_state(
        recent_tiktok,
        date_start=date(2026, 6, 24),
        date_end_exclusive=date(2026, 6, 26),
        timezone_name="Asia/Shanghai",
    ) == "in-range-or-unknown"
    assert candidate_date_state(
        recent_tiktok,
        date_start=date(2026, 6, 26),
        date_end_exclusive=date(2026, 6, 30),
        timezone_name="Asia/Shanghai",
    ) == "before-start"
    assert candidate_date_state(
        recent_tiktok,
        date_start=date(2026, 6, 20),
        date_end_exclusive=date(2026, 6, 25),
        timezone_name="Asia/Shanghai",
    ) == "after-end"


if __name__ == "__main__":
    _self_test()
