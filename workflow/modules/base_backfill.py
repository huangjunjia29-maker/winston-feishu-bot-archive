#!/usr/bin/env python3
"""Backfill Feishu Base platform metrics from a jielong XLSX.

The script uses lark-cli for Feishu auth/API access and defaults to dry-run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

import requests

try:
    from feishu_auth import request as shared_feishu_request
    from feishu_auth import tenant_access_token as shared_tenant_access_token
except ImportError:
    shared_feishu_request = None
    shared_tenant_access_token = None

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


def utf8_child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


configure_utf8_runtime()


def import_openpyxl_load_workbook() -> Any:
    try:
        from openpyxl import load_workbook

        return load_workbook
    except ImportError:
        cwd = str(Path.cwd().resolve())
        original_path = list(sys.path)
        sys.path = [path for path in sys.path if path and str(Path(path).resolve()) != cwd]
        for name in list(sys.modules):
            if name == "openpyxl" or name.startswith("openpyxl.") or name == "defusedxml" or name.startswith("defusedxml."):
                sys.modules.pop(name, None)
        try:
            from openpyxl import load_workbook

            return load_workbook
        finally:
            sys.path = original_path

URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
TRAILING_URL_CHARS = ".,;:!?)]}" + "\uff0c\u3002\uff1b\uff1a\uff01\uff1f\uff09\u3011"
X_STATUS_RE = re.compile(r"https?://(?:www\.)?x\.com/((?:i/status|[^/\s\"'<>\\]+/status)/(\d+))", re.I)
TIKTOK_VIDEO_RE = re.compile(r"https?://(?:www\.)?tiktok\.com/@[^/\s\"'<>\\]+/video/\d+", re.I)
TIKTOK_SHORT_RE = re.compile(r"https?://(?:vt\.tiktok\.com|www\.tiktok\.com/t)/[A-Za-z0-9_-]+/?", re.I)
INSTAGRAM_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|reels|p)/[A-Za-z0-9_-]+/?", re.I)
FACEBOOK_RE = re.compile(r"https?://(?:www\.)?facebook\.com/(?:share/r/[A-Za-z0-9_-]+|reel/\d+)/?", re.I)
FB_WATCH_RE = re.compile(r"https?://fb\.watch/[A-Za-z0-9_-]+/?", re.I)
WRITABLE_TYPES = {"text", "number"}

BASE_WRITE_MAX_REQUESTS_PER_MINUTE = 90
# Leave scheduling headroom above the exact 60/90 = 0.666... second boundary.
BASE_WRITE_MIN_INTERVAL_SECONDS = 0.70
AUTOMATION_WRITE_MAX_REQUESTS_PER_SECOND = 5
AUTOMATION_WRITE_MIN_INTERVAL_SECONDS = 0.20
BASE_BATCH_UPDATE_SIZE = 500
LARK_RATE_LIMIT_RETRY_DELAYS_SECONDS = (15.0, 30.0, 60.0)
AUTOMATION_CONFLICT_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 5.0)
AUTOMATION_RATE_RETRY_DELAYS_SECONDS = (5.0, 15.0, 30.0, 60.0)
LARK_TOKEN_REFRESH_SKEW_SECONDS = 300.0
_LARK_TAT_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_LARK_TAT_LOCK = threading.Lock()
_LARK_HTTP_SESSION = requests.Session()
_LARK_FIELD_NAME_CACHE: dict[tuple[str, str], dict[str, str]] = {}


class BaseWriteRateLimiter:
    """Keep sequential Base write starts below the shared per-minute cap."""

    def __init__(
        self,
        interval_seconds: float = BASE_WRITE_MIN_INTERVAL_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if interval_seconds < BASE_WRITE_MIN_INTERVAL_SECONDS:
            raise ValueError(
                "Base write interval must be at least "
                f"{BASE_WRITE_MIN_INTERVAL_SECONDS:.2f}s to preserve headroom below "
                f"{BASE_WRITE_MAX_REQUESTS_PER_MINUTE} requests/minute."
            )
        self.interval_seconds = interval_seconds
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._last_request_started_at: float | None = None

    def wait(self) -> float:
        now = self._clock()
        if self._last_request_started_at is not None:
            remaining = self.interval_seconds - (now - self._last_request_started_at)
            if remaining > 0:
                self._sleeper(remaining)
                now = self._clock()
        self._last_request_started_at = now
        return now

class AutomationWriteRateLimiter:
    """Limit automation-triggering record updates to five sequential writes/sec."""

    def __init__(
        self,
        interval_seconds: float = AUTOMATION_WRITE_MIN_INTERVAL_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if interval_seconds < AUTOMATION_WRITE_MIN_INTERVAL_SECONDS:
            raise ValueError(
                "Automation write interval must be at least "
                f"{AUTOMATION_WRITE_MIN_INTERVAL_SECONDS:.2f}s."
            )
        self.interval_seconds = interval_seconds
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._last_request_started_at: float | None = None

    def wait(self) -> float:
        now = self._clock()
        if self._last_request_started_at is not None:
            remaining = self.interval_seconds - (now - self._last_request_started_at)
            if remaining > 0:
                self._sleeper(remaining)
                now = self._clock()
        self._last_request_started_at = now
        return now


METRIC_HEADERS = {
    "like": "点赞/反应",
    "comment": "评论",
    "share": "分享/转发",
    "favorite": "收藏",
    "views": "浏览/播放量",
}
XLSX_LINK_HEADER_ALIASES = ("接龙信息", "链接", "URL", "url", "link", "links")
XLSX_OWNER_HEADER_ALIASES = ("接龙人", "Owner", "owner")
XLSX_FALLBACK_COLUMNS = {
    "link": 0,
    "owner": 2,
    "like": 13,
    "comment": 14,
    "share": 15,
    "favorite": 16,
    "views": 17,
}

PLATFORM_FIELDS = {
    "X": {
        "link": "X链接",
        "like": "X喜欢",
        "comment": "X评论",
        "share": "X转发",
        "favorite": "X收藏",
        "views": "X浏览",
    },
    "INS": {
        "link": "INS链接",
        "like": "INS点赞",
        "comment": "INS评论",
        "share": "INS转发",
        "favorite": "INS收藏",
        "views": "INS播放",
    },
    "TK": {
        "link": "TK链接",
        "like": "TK点赞",
        "comment": "TK评论",
        "share": "TK转发",
        "favorite": "TK收藏",
        "views": "TK播放",
    },
    "FB": {
        "link": "FB链接",
        "like": "FB点赞",
        "comment": "FB评论",
        "views": "FB曝光",
    },
}


@dataclass
class FieldInfo:
    id: str
    name: str
    type: str


@dataclass
class BaseRecord:
    record_id: str
    values: dict[str, Any]


@dataclass
class XlsxEntry:
    row_num: int
    owner: str
    platform: str
    link: str
    metrics: dict[str, Any]


@dataclass
class PlannedUpdate:
    record_id: str
    auto_id: str
    owner: str
    platform: str
    xlsx_row: int
    readable_patch: dict[str, Any] = field(default_factory=dict)
    patch_by_id: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def str_to_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    if "\\u" in value:
        try:
            return value.encode("utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            return value
    return value


def load_env_file(path: str) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ[key] = unquote_env_value(value)


def preparse_env_file(argv: list[str]) -> str:
    if "--env-file" in argv:
        index = argv.index("--env-file")
        if index + 1 < len(argv):
            return argv[index + 1]
    for arg in argv:
        if arg.startswith("--env-file="):
            return arg.split("=", 1)[1]
    if os.getenv("FEISHU_ENV_FILE"):
        return os.environ["FEISHU_ENV_FILE"]
    return str(Path(__file__).resolve().parent.parent / ".env")


def parse_base_url(url: str) -> dict[str, str]:
    if not url.strip():
        return {}
    parsed = urlparse(url.strip())
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    base_token = ""
    for index, part in enumerate(parts):
        if part in {"base", "bitable"} and index + 1 < len(parts):
            base_token = parts[index + 1]
            break
    if not base_token:
        raise RuntimeError("FEISHU_BASE_URL is not a recognized Base URL with /base/{token}.")
    query = parse_qs(parsed.query)
    return {
        "base_token": base_token,
        "table_id": query.get("table", [""])[0],
        "view_id": query.get("view", [""])[0],
    }


def lark_cli_bin() -> str:
    configured = os.getenv("LARK_CLI_BIN", "lark-cli").strip() or "lark-cli"
    for candidate in (configured, "lark-cli.cmd", "lark-cli.exe", "lark-cli.ps1"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return configured


def run_lark(args: list[str], cwd: Path) -> dict[str, Any]:
    cmd = [lark_cli_bin()]
    profile = os.getenv("FEISHU_LARK_PROFILE") or os.getenv("LARK_CLI_PROFILE")
    if profile:
        cmd.extend(["--profile", profile])
    cmd.extend(args)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=utf8_child_env(),
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    data: dict[str, Any] = {}
    if stdout:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = {"raw": stdout}
    if proc.returncode != 0 or data.get("ok") is False:
        message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else ""
        raise RuntimeError(
            f"lark-cli failed: {message or 'command returned an error'}\n"
            f"STDERR:\n{stderr}\nSTDOUT:\n{stdout}"
        )
    return data


def lark_api_domain() -> str:
    return (
        os.getenv("FEISHU_OPEN_API_DOMAIN")
        or os.getenv("LARK_OPEN_API_DOMAIN")
        or "https://open.feishu.cn"
    ).rstrip("/")


def tenant_access_token(*, force_refresh: bool = False) -> str:
    """Return a process-cached TAT and refresh it only near expiry."""

    if shared_tenant_access_token is not None:
        return shared_tenant_access_token(force_refresh=force_refresh)

    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        load_env_file(str(Path(__file__).resolve().parent.parent / "env" / "chats.env"))
        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required for direct Lark API calls.")
    domain = lark_api_domain()
    key = (domain, app_id)
    now = time.time()
    with _LARK_TAT_LOCK:
        cached = _LARK_TAT_CACHE.get(key)
        if not force_refresh and cached and cached[1] > now:
            return cached[0]
        response = _LARK_HTTP_SESSION.post(
            f"{domain}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=30,
        )
        if response.status_code == 429:
            raise RuntimeError("tenant_access_token request failed with HTTP 429")
        if response.status_code >= 400:
            raise RuntimeError(f"tenant_access_token request failed with HTTP {response.status_code}")
        payload = response.json()
        code = int(payload.get("code") or 0)
        token = str(payload.get("tenant_access_token") or "")
        if code != 0 or not token:
            raise RuntimeError(f"tenant_access_token request failed: code={code} msg={payload.get('msg') or ''}")
        expires_in = max(600.0, float(payload.get("expire") or 7200))
        _LARK_TAT_CACHE[key] = (token, now + expires_in - LARK_TOKEN_REFRESH_SKEW_SECONDS)
        return token


def request_lark_api(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Call Feishu OpenAPI with the process-cached TAT."""

    if shared_feishu_request is not None:
        return shared_feishu_request(method, path, params=params, json_body=json_body, timeout=timeout_s)

    for auth_attempt in range(2):
        token = tenant_access_token(force_refresh=auth_attempt > 0)
        response = _LARK_HTTP_SESSION.request(
            method.upper(),
            f"{lark_api_domain()}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            params=params,
            timeout=timeout_s,
        )
        if response.status_code == 429:
            raise RuntimeError(f"Lark API request failed with HTTP 429 path={path}")
        if response.status_code in {401, 403} and auth_attempt == 0:
            continue
        if response.status_code >= 400:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {}
            code = error_payload.get("code", "") if isinstance(error_payload, dict) else ""
            message = ""
            if isinstance(error_payload, dict):
                message = str(error_payload.get("msg") or error_payload.get("message") or "")
            details = f" code={code} msg={message}" if code or message else ""
            raise RuntimeError(
                f"Lark API request failed with HTTP {response.status_code}{details} path={path}"
            )
        payload = response.json()
        code = int(payload.get("code") or 0)
        if code in {99991663, 99991664} and auth_attempt == 0:
            continue
        if code != 0:
            raise RuntimeError(f"Lark API request failed: code={code} msg={payload.get('msg') or ''} path={path}")
        return payload
    raise RuntimeError(f"Lark API authorization refresh failed path={path}")


def run_lark_api(args: list[str], cwd: Path) -> dict[str, Any]:
    """Execute the raw lark-cli API argument contract without spawning lark-cli."""

    if len(args) < 3 or args[0] != "api":
        raise ValueError(f"direct Lark API runner only accepts raw api commands: {args[:3]!r}")
    method, path = args[1], args[2]
    json_body: dict[str, Any] | None = None
    if "--data" in args:
        raw = args[args.index("--data") + 1]
        if raw.startswith("@"):
            json_body = json.loads((cwd / raw[1:]).read_text(encoding="utf-8"))
        else:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("Lark API --data must be a JSON object")
            json_body = parsed
    payload = request_lark_api(method, path, json_body=json_body)
    return {"ok": True, "data": payload.get("data", {}), "raw": payload}


_REAL_RUN_LARK_API = run_lark_api

def is_lark_rate_limit_error(error: BaseException) -> bool:
    """Return true only for explicit Lark/HTTP rate-limit failures."""

    message = str(error).lower()
    markers = (
        "http 429",
        "too many requests",
        "rate limit",
        "rate-limit",
        "request frequency",
        "1254290",
        "too many request",
    )
    return any(marker in message for marker in markers)


def is_lark_write_conflict(error: BaseException) -> bool:
    message = str(error).lower()
    return "1254291" in message or "write conflict" in message


def automation_retry_delays(error: BaseException) -> tuple[float, ...]:
    if is_lark_write_conflict(error):
        return AUTOMATION_CONFLICT_RETRY_DELAYS_SECONDS
    if is_lark_rate_limit_error(error):
        return AUTOMATION_RATE_RETRY_DELAYS_SECONDS
    return ()


def run_lark_write_with_rate_limit_retry(
    args: list[str],
    cwd: Path,
    *,
    write_limiter: BaseWriteRateLimiter,
    runner: Callable[[list[str], Path], dict[str, Any]] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Run one idempotent Base write with bounded retries for explicit 429s."""

    call = runner or run_lark_api
    sleep = sleeper or time.sleep
    for attempt in range(len(LARK_RATE_LIMIT_RETRY_DELAYS_SECONDS) + 1):
        write_limiter.wait()
        try:
            return call(args, cwd)
        except RuntimeError as error:
            if not is_lark_rate_limit_error(error) or attempt >= len(LARK_RATE_LIMIT_RETRY_DELAYS_SECONDS):
                raise
            delay = LARK_RATE_LIMIT_RETRY_DELAYS_SECONDS[attempt]
            print(
                f"[rate-limit-retry] attempt={attempt + 2} "
                f"delay_s={delay:.0f}",
                file=sys.stderr,
                flush=True,
            )
            sleep(delay)
    raise AssertionError("unreachable rate-limit retry loop")


def write_json_arg(skill_root: Path, name: str, body: dict[str, Any]) -> str:
    path = skill_root / name
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return "@" + path.name


def cleanup_json_arg(skill_root: Path, name: str) -> None:
    try:
        (skill_root / name).unlink()
    except FileNotFoundError:
        pass


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("name", "text", "value", "id"):
            if key in value:
                return normalize_text(value[key])
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        parts = [normalize_text(item) for item in value]
        return ", ".join(part for part in parts if part)
    return str(value).strip()


def normalized_header_key(value: Any) -> str:
    return re.sub(r"[\s\-_()/（）/]+", "", normalize_text(value).lower())


def looks_encoding_damaged(value: str) -> bool:
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


def find_header_index(headers: list[str], candidates: list[str] | tuple[str, ...]) -> int | None:
    candidate_keys = {normalized_header_key(candidate) for candidate in candidates if normalize_text(candidate)}
    for index, header in enumerate(headers):
        if header in candidates or normalized_header_key(header) in candidate_keys:
            return index
    return None


def resolve_xlsx_header_indices(headers: list[str], link_header: str, owner_header: str) -> dict[str, int]:
    resolved: dict[str, int] = {}
    link_candidates = tuple(dict.fromkeys((link_header, *XLSX_LINK_HEADER_ALIASES)))
    owner_candidates = tuple(dict.fromkeys((owner_header, *XLSX_OWNER_HEADER_ALIASES)))
    link_index = find_header_index(headers, link_candidates)
    owner_index = find_header_index(headers, owner_candidates)
    if link_index is not None:
        resolved["link"] = link_index
    if owner_index is not None:
        resolved["owner"] = owner_index
    for key, header in METRIC_HEADERS.items():
        index = find_header_index(headers, (header,))
        if index is not None:
            resolved[key] = index

    for key, fallback in XLSX_FALLBACK_COLUMNS.items():
        if key not in resolved and fallback < len(headers):
            resolved[key] = fallback
    return resolved


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list):
        return len(value) == 0 or all(is_blank(item) for item in value)
    return False


def extract_first_url(value: Any) -> str:
    text = normalize_text(value)
    candidates: list[tuple[int, int, str]] = []
    platform_patterns = [
        (X_STATUS_RE, lambda match: "https://x.com/" + match.group(1)),
        (TIKTOK_VIDEO_RE, lambda match: match.group(0)),
        (TIKTOK_SHORT_RE, lambda match: match.group(0)),
        (INSTAGRAM_RE, lambda match: match.group(0)),
        (FACEBOOK_RE, lambda match: match.group(0)),
        (FB_WATCH_RE, lambda match: match.group(0)),
    ]
    specific_starts: set[int] = set()
    for pattern, build_url in platform_patterns:
        for match in pattern.finditer(text):
            specific_starts.add(match.start())
            candidates.append((match.start(), 0, build_url(match)))
    match = URL_RE.search(text)
    if match:
        if match.start() not in specific_starts:
            url = match.group(0).rstrip(TRAILING_URL_CHARS)
            candidates.append((match.start(), 1, url))
    if candidates:
        return sorted(candidates, key=lambda item: (item[0], item[1]))[0][2]
    return text


def preview_text(value: Any, limit: int = 140) -> str:
    text = normalize_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def link_match_key(value: Any) -> str:
    link = extract_first_url(value).strip().rstrip("/")
    return link.lower()


def platform_from_url(url: str) -> str:
    lowered = url.lower()
    if "x.com" in lowered or "twitter.com" in lowered:
        return "X"
    if "instagram.com" in lowered:
        return "INS"
    if "tiktok.com" in lowered or "vt.tiktok.com" in lowered or "douyin.com" in lowered:
        return "TK"
    if "facebook.com" in lowered or "fb.watch" in lowered:
        return "FB"
    return ""


def parse_number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    multiplier = 1.0
    suffix = text[-1:].lower()
    if suffix in {"k", "m", "b", "w"}:
        text = text[:-1].strip()
        multiplier = {"k": 1000, "m": 1_000_000, "b": 1_000_000_000, "w": 10_000}[suffix]
    number = float(text) * multiplier
    return int(number) if number.is_integer() else number


def value_for_field(raw_value: Any, field_info: FieldInfo) -> Any | None:
    if is_blank(raw_value):
        return None
    if field_info.type == "number":
        return parse_number(raw_value)
    if field_info.type == "text":
        return normalize_text(raw_value)
    return raw_value


def parse_fields(data: dict[str, Any]) -> dict[str, FieldInfo]:
    fields = data.get("data", {}).get("fields", [])
    parsed: dict[str, FieldInfo] = {}
    for item in fields:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        field_id = str(item.get("id", ""))
        field_type = str(item.get("type", ""))
        if name and field_id:
            parsed[name] = FieldInfo(id=field_id, name=name, type=field_type)
    return parsed


def get_fields(skill_root: Path, identity: str, base_token: str, table_id: str) -> dict[str, FieldInfo]:
    data = run_lark(
        [
            "base",
            "+field-list",
            "--as",
            identity,
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--offset",
            "0",
            "--limit",
            "200",
        ],
        cwd=skill_root,
    )
    return parse_fields(data)


def search_project_records(
    skill_root: Path,
    identity: str,
    base_token: str,
    table_id: str,
    project_value: str,
    project_field: FieldInfo,
    select_fields: list[FieldInfo],
) -> list[BaseRecord]:
    records: list[BaseRecord] = []
    offset = 0
    limit = 200
    temp_name = f"._backfill_search_{os.getpid()}.json"
    select_ids = [field_info.id for field_info in select_fields]
    try:
        while True:
            body = {
                "keyword": project_value,
                "search_fields": [project_field.id],
                "select_fields": select_ids,
                "offset": offset,
                "limit": limit,
            }
            arg = write_json_arg(skill_root, temp_name, body)
            data = run_lark(
                [
                    "base",
                    "+record-search",
                    "--as",
                    identity,
                    "--base-token",
                    base_token,
                    "--table-id",
                    table_id,
                    "--json",
                    arg,
                    "--format",
                    "json",
                ],
                cwd=skill_root,
            )
            payload = data.get("data", {})
            rows = payload.get("data", [])
            record_ids = payload.get("record_id_list", [])
            field_ids = payload.get("field_id_list", [])
            for record_id, row in zip(record_ids, rows):
                values = dict(zip(field_ids, row))
                if normalize_text(values.get(project_field.id)) == project_value:
                    records.append(BaseRecord(record_id=str(record_id), values=values))
            if not payload.get("has_more"):
                break
            offset += limit
    finally:
        cleanup_json_arg(skill_root, temp_name)
    return records


def newest_xlsx_in_dir(search_dir: Path, pattern: str, damaged_name: str = "") -> Path | None:
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


def xlsx_path_from_args(value: str, xlsx_dir: str, xlsx_glob: str) -> Path:
    pattern = xlsx_glob or "*.xlsx"
    if value:
        path = Path(value).expanduser()
        if path.exists():
            return path
        parent = path.parent if str(path.parent) not in {"", "."} else Path(xlsx_dir or Path.cwd())
        if parent.exists() and looks_encoding_damaged(path.name):
            fallback = newest_xlsx_in_dir(parent, pattern, path.name)
            if fallback:
                print(f"[xlsx-path-fallback] {path} not found; using newest workbook in {parent}: {fallback}")
                return fallback
        return path
    search_dir = Path(xlsx_dir) if xlsx_dir else Path.cwd()
    fallback = newest_xlsx_in_dir(search_dir, pattern)
    if fallback is None:
        raise RuntimeError(
            f"No XLSX path was provided and no workbook matching {pattern!r} exists in {search_dir}."
        )
    return fallback


def select_xlsx_worksheet(wb: Any, sheet_name: str, link_header: str, owner_header: str) -> Any:
    requested = normalize_text(sheet_name)
    if requested:
        if requested in wb.sheetnames:
            return wb[requested]
        requested_key = normalized_header_key(requested)
        for candidate in wb.sheetnames:
            if normalized_header_key(candidate) == requested_key:
                return wb[candidate]
        print(f"[xlsx-sheet-fallback] requested sheet not found: {requested}; auto-detecting worksheet")

    for ws in wb.worksheets:
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
        headers = [normalize_text(value) for value in header_row]
        indices = resolve_xlsx_header_indices(headers, link_header, owner_header)
        if "link" in indices and "owner" in indices:
            return ws
    return wb.worksheets[0]


def read_xlsx_entries(
    xlsx_path: Path,
    sheet_name: str,
    link_header: str,
    owner_header: str,
) -> tuple[list[XlsxEntry], dict[str, Any]]:
    load_workbook = import_openpyxl_load_workbook()
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = select_xlsx_worksheet(wb, sheet_name, link_header, owner_header)

    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    headers = [normalize_text(value) for value in header_row]
    header_index = resolve_xlsx_header_indices(headers, link_header, owner_header)
    required = ["link", "owner", *METRIC_HEADERS]
    missing = [key for key in required if key not in header_index]
    if missing:
        raise RuntimeError("XLSX missing required column(s): " + ", ".join(missing))

    entries: list[XlsxEntry] = []
    ignored_non_platform = 0
    ignored_blank_owner = 0
    ignored_non_platform_rows: list[str] = []
    ignored_blank_owner_rows: list[str] = []
    for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        owner = normalize_text(row[header_index["owner"]])
        link_cell = row[header_index["link"]]
        link = extract_first_url(link_cell)
        platform = platform_from_url(link)
        if not platform:
            ignored_non_platform += 1
            ignored_non_platform_rows.append(
                f"row {row_num} {owner or '<blank owner>'}: No supported social URL in {link_header}: {preview_text(link_cell)}"
            )
            continue
        if not owner:
            ignored_blank_owner += 1
            ignored_blank_owner_rows.append(f"row {row_num}: XLSX blank Owner for {link_header}: {preview_text(link_cell)}")
            continue
        metrics = {key: row[header_index[key]] for key in METRIC_HEADERS}
        entries.append(XlsxEntry(row_num=row_num, owner=owner, platform=platform, link=link, metrics=metrics))

    meta = {
        "sheet": ws.title,
        "rows": ws.max_row,
        "ignored_non_platform": ignored_non_platform,
        "ignored_blank_owner": ignored_blank_owner,
        "ignored_non_platform_rows": ignored_non_platform_rows,
        "ignored_blank_owner_rows": ignored_blank_owner_rows,
        "columns": {key: index + 1 for key, index in header_index.items()},
    }
    return entries, meta


def limit_entries_per_platform(entries: list[XlsxEntry], limit: int) -> list[XlsxEntry]:
    if limit <= 0:
        return entries
    counts = {platform: 0 for platform in PLATFORM_FIELDS}
    selected: list[XlsxEntry] = []
    for entry in entries:
        if entry.platform not in counts:
            continue
        if counts[entry.platform] >= limit:
            continue
        selected.append(entry)
        counts[entry.platform] += 1
    missing = [platform for platform, count in counts.items() if count < limit]
    if missing:
        detail = ", ".join(f"{platform}={counts[platform]}" for platform in missing)
        raise RuntimeError(f"Not enough XLSX rows to select {limit} per platform: {detail}")
    return selected


def build_field_requirements(fields_by_name: dict[str, FieldInfo]) -> dict[str, dict[str, FieldInfo]]:
    platform_field_info: dict[str, dict[str, FieldInfo]] = {}
    for platform, mapping in PLATFORM_FIELDS.items():
        platform_field_info[platform] = {}
        for slot, field_name in mapping.items():
            field_info = fields_by_name.get(field_name)
            if field_info and field_info.type in WRITABLE_TYPES:
                platform_field_info[platform][slot] = field_info
    return platform_field_info


def build_owner_index(records: list[BaseRecord], owner_field: FieldInfo) -> dict[str, BaseRecord]:
    owner_index: dict[str, BaseRecord] = {}
    duplicates: dict[str, list[str]] = {}
    for record in records:
        owner = normalize_text(record.values.get(owner_field.id))
        if not owner:
            continue
        if owner in owner_index:
            duplicates.setdefault(owner, [owner_index[owner].record_id]).append(record.record_id)
        else:
            owner_index[owner] = record
    if duplicates:
        details = "; ".join(f"{owner}: {ids}" for owner, ids in duplicates.items())
        raise RuntimeError("Base has duplicate Owner values in the same Project: " + details)
    return owner_index


def build_owner_index_lenient(records: list[BaseRecord], owner_field: FieldInfo) -> tuple[dict[str, BaseRecord], dict[str, list[str]]]:
    owner_index: dict[str, BaseRecord] = {}
    duplicates: dict[str, list[str]] = {}
    for record in records:
        owner = normalize_text(record.values.get(owner_field.id))
        if not owner:
            continue
        if owner in duplicates:
            duplicates[owner].append(record.record_id)
            continue
        if owner in owner_index:
            duplicates[owner] = [owner_index[owner].record_id, record.record_id]
            owner_index.pop(owner, None)
            continue
        owner_index[owner] = record
    return owner_index, duplicates


def build_owner_record_buckets(records: list[BaseRecord], owner_field: FieldInfo) -> dict[str, list[BaseRecord]]:
    buckets: dict[str, list[BaseRecord]] = {}
    for record in records:
        owner = normalize_text(record.values.get(owner_field.id))
        if not owner:
            continue
        buckets.setdefault(owner, []).append(record)
    return buckets


def auto_id_for(record: BaseRecord, auto_field: FieldInfo | None) -> str:
    if not auto_field:
        return record.record_id
    return normalize_text(record.values.get(auto_field.id)) or record.record_id


def choose_record_for_entry(
    entry: XlsxEntry,
    owner_records: list[BaseRecord],
    target_fields: dict[str, FieldInfo],
    reserved_slots: set[tuple[str, str]],
) -> tuple[BaseRecord | None, str]:
    link_field = target_fields.get("link")
    slot_key_platform = entry.platform
    entry_link_key = link_match_key(entry.link)

    if link_field:
        for record in owner_records:
            slot_key = (record.record_id, slot_key_platform)
            if slot_key in reserved_slots:
                continue
            current_link_key = link_match_key(record.values.get(link_field.id))
            if current_link_key and current_link_key == entry_link_key:
                return record, ""

        for record in owner_records:
            slot_key = (record.record_id, slot_key_platform)
            if slot_key in reserved_slots:
                continue
            if is_blank(record.values.get(link_field.id)):
                return record, ""

        return None, f"No matching or empty Base row for {entry.platform} link {entry.link}"

    for record in owner_records:
        slot_key = (record.record_id, slot_key_platform)
        if slot_key not in reserved_slots:
            return record, ""
    return None, f"No available Base row for {entry.platform}; link field is missing or not writable"


def plan_updates(
    entries: list[XlsxEntry],
    platform_fields: dict[str, dict[str, FieldInfo]],
    owner_buckets: dict[str, list[BaseRecord]],
    auto_field: FieldInfo | None,
    overwrite: bool,
    owner_skip_reasons: dict[str, str] | None = None,
) -> tuple[list[PlannedUpdate], list[str]]:
    owner_skip_reasons = owner_skip_reasons or {}
    plans: list[PlannedUpdate] = []
    skipped: list[str] = []
    reserved_slots: set[tuple[str, str]] = set()
    for entry in entries:
        owner_records = owner_buckets.get(entry.owner, [])
        if not owner_records:
            reason = owner_skip_reasons.get(entry.owner, "Owner not found in Base Project records")
            skipped.append(f"row {entry.row_num} {entry.owner}/{entry.platform}: {reason}")
            continue
        target_fields = platform_fields.get(entry.platform, {})
        record, skip_reason = choose_record_for_entry(entry, owner_records, target_fields, reserved_slots)
        if not record:
            skipped.append(f"row {entry.row_num} {entry.owner}/{entry.platform}: {skip_reason}")
            continue
        reserved_slots.add((record.record_id, entry.platform))
        plan = PlannedUpdate(
            record_id=record.record_id,
            auto_id=auto_id_for(record, auto_field),
            owner=entry.owner,
            platform=entry.platform,
            xlsx_row=entry.row_num,
        )
        for slot, raw_value in [("link", entry.link), *entry.metrics.items()]:
            target_field = target_fields.get(slot)
            source_name = "link" if slot == "link" else METRIC_HEADERS[slot]
            if not target_field:
                configured_name = PLATFORM_FIELDS.get(entry.platform, {}).get(slot, "")
                if configured_name:
                    plan.skipped.append(f"{source_name} -> {configured_name}: Base field missing or not writable")
                continue
            value = value_for_field(raw_value, target_field)
            if value is None:
                plan.skipped.append(f"{source_name} -> {target_field.name}: XLSX blank")
                continue
            current_value = record.values.get(target_field.id)
            if not overwrite and not is_blank(current_value):
                plan.skipped.append(f"{target_field.name}: Base already has value")
                continue
            plan.readable_patch[target_field.name] = value
            plan.patch_by_id[target_field.id] = value
        if plan.readable_patch:
            plans.append(plan)
        elif plan.skipped:
            skipped.append(f"row {entry.row_num} {entry.owner}/{entry.platform}: " + "; ".join(plan.skipped))
    return plans, skipped


def manual_review_skips(skipped: list[str]) -> list[str]:
    markers = (
        "Owner not found in Base Project records",
        "No matching or empty Base row",
        "No available Base row",
        "No supported social URL",
        "XLSX blank Owner",
    )
    return [item for item in skipped if any(marker in item for marker in markers)]


def resolve_write_field_names(base_token: str, table_id: str, field_keys: set[str]) -> dict[str, str]:
    """Resolve stored field IDs to official API field names once per table."""

    cache_key = (base_token, table_id)
    mapping = _LARK_FIELD_NAME_CACHE.get(cache_key)
    if mapping is None:
        payload = request_lark_api(
            "GET",
            f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
            params={"page_size": 500},
        )
        items = (payload.get("data") or {}).get("items") or []
        mapping = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            field_id = str(item.get("field_id") or item.get("id") or "")
            field_name = str(item.get("field_name") or item.get("name") or "")
            if field_id and field_name:
                mapping[field_id] = field_name
                mapping[field_name] = field_name
        _LARK_FIELD_NAME_CACHE[cache_key] = mapping
    missing = sorted(key for key in field_keys if key not in mapping)
    if missing:
        raise RuntimeError("Base write field id/name not found: " + ", ".join(missing[:20]))
    return {key: mapping[key] for key in field_keys}


def batch_update_records(
    skill_root: Path,
    identity: str,
    base_token: str,
    table_id: str,
    records: list[dict[str, Any]],
    *,
    runner: Callable[[list[str], Path], dict[str, Any]] | None = None,
    write_limiter: BaseWriteRateLimiter | None = None,
    batch_size: int = BASE_BATCH_UPDATE_SIZE,
) -> list[dict[str, Any]]:
    """Update heterogeneous Base records with one lark-cli process per batch."""

    if batch_size <= 0 or batch_size > BASE_BATCH_UPDATE_SIZE:
        raise ValueError(f"batch_size must be between 1 and {BASE_BATCH_UPDATE_SIZE}")
    if not records:
        return []
    endpoint = f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records/batch_update"
    merged_by_record: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("record_id") or "")
        if not record_id:
            raise RuntimeError("Batch update record is missing record_id.")
        target = merged_by_record.setdefault(record_id, {})
        for field_key, value in (record.get("fields") or {}).items():
            key = str(field_key)
            if key in target and target[key] != value:
                raise RuntimeError(f"Conflicting batch values for record={record_id} field={key}")
            target[key] = value
    merged_records = [
        {"record_id": record_id, "fields": fields}
        for record_id, fields in merged_by_record.items()
    ]
    field_keys = {
        str(field_key)
        for record in merged_records
        for field_key in (record.get("fields") or {})
    }
    field_names = (
        resolve_write_field_names(base_token, table_id, field_keys)
        if runner is None and run_lark_api is _REAL_RUN_LARK_API
        else {key: key for key in field_keys}
    )
    normalized_records = [
        {
            "record_id": record["record_id"],
            "fields": {
                field_names[str(key)]: value
                for key, value in (record.get("fields") or {}).items()
            },
        }
        for record in merged_records
    ]
    limiter = write_limiter or BaseWriteRateLimiter()
    temp_name = f"._record_batch_update_{os.getpid()}.json"
    responses: list[dict[str, Any]] = []
    try:
        for offset in range(0, len(records), batch_size):
            body = {"records": normalized_records[offset : offset + batch_size]}
            arg = write_json_arg(skill_root, temp_name, body)
            responses.append(
                run_lark_write_with_rate_limit_retry(
                    ["api", "POST", endpoint, "--as", identity, "--data", arg],
                    cwd=skill_root,
                    write_limiter=limiter,
                    runner=runner,
                )
            )
    finally:
        cleanup_json_arg(skill_root, temp_name)
    return responses


def update_records_individually(
    identity: str,
    base_token: str,
    table_id: str,
    records: list[dict[str, Any]],
    *,
    write_limiter: AutomationWriteRateLimiter | None = None,
) -> int:
    """Update records one by one so Base record-change automation can fire."""

    merged_by_record: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("record_id") or "")
        if not record_id:
            raise RuntimeError("Individual update record is missing record_id.")
        target = merged_by_record.setdefault(record_id, {})
        for field_key, value in (record.get("fields") or {}).items():
            key = str(field_key)
            if key in target and target[key] != value:
                raise RuntimeError(f"Conflicting individual values for record={record_id} field={key}")
            target[key] = value
    field_keys = {key for fields in merged_by_record.values() for key in fields}
    field_names = resolve_write_field_names(base_token, table_id, field_keys)
    limiter = write_limiter or AutomationWriteRateLimiter()
    for record_id, fields in merged_by_record.items():
        normalized = {field_names[key]: value for key, value in fields.items()}
        max_retries = max(len(AUTOMATION_CONFLICT_RETRY_DELAYS_SECONDS), len(AUTOMATION_RATE_RETRY_DELAYS_SECONDS))
        for attempt in range(max_retries + 1):
            limiter.wait()
            try:
                request_lark_api(
                    "PUT",
                    f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
                    json_body={"fields": normalized},
                )
                break
            except RuntimeError as error:
                delays = automation_retry_delays(error)
                if not delays or attempt >= len(delays):
                    raise
                delay = delays[attempt]
                retry_kind = "write-conflict" if is_lark_write_conflict(error) else "rate-limit"
                print(
                    f"[{retry_kind}-retry] attempt={attempt + 2} delay_s={delay:g}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
    return len(merged_by_record)


def batch_create_records(
    skill_root: Path,
    identity: str,
    base_token: str,
    table_id: str,
    records: list[dict[str, Any]],
    *,
    runner: Callable[[list[str], Path], dict[str, Any]] | None = None,
    write_limiter: BaseWriteRateLimiter | None = None,
    batch_size: int = BASE_BATCH_UPDATE_SIZE,
) -> list[dict[str, Any]]:
    """Create heterogeneous Base records with one lark-cli process per batch."""

    if batch_size <= 0 or batch_size > BASE_BATCH_UPDATE_SIZE:
        raise ValueError(f"batch_size must be between 1 and {BASE_BATCH_UPDATE_SIZE}")
    if not records:
        return []
    endpoint = f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records/batch_create"
    limiter = write_limiter or BaseWriteRateLimiter()
    temp_name = f"._record_batch_create_{os.getpid()}.json"
    responses: list[dict[str, Any]] = []
    try:
        for offset in range(0, len(records), batch_size):
            body = {"records": records[offset : offset + batch_size]}
            arg = write_json_arg(skill_root, temp_name, body)
            responses.append(
                run_lark_write_with_rate_limit_retry(
                    ["api", "POST", endpoint, "--as", identity, "--data", arg],
                    cwd=skill_root,
                    write_limiter=limiter,
                    runner=runner,
                )
            )
    finally:
        cleanup_json_arg(skill_root, temp_name)
    return responses


def execute_updates(
    skill_root: Path,
    identity: str,
    base_token: str,
    table_id: str,
    plans: list[PlannedUpdate],
) -> None:
    """Write heterogeneous record patches without per-record lark-cli processes."""

    records = [
        {"record_id": plan.record_id, "fields": plan.patch_by_id}
        for plan in plans
    ]
    batch_update_records(skill_root, identity, base_token, table_id, records)
    for index, plan in enumerate(plans, 1):
        print(f"[{index}/{len(plans)}] updated {plan.auto_id} {plan.owner} {plan.platform}")


def print_plan(
    project: str,
    xlsx_path: Path,
    entries: list[XlsxEntry],
    xlsx_meta: dict[str, Any],
    records: list[BaseRecord],
    plans: list[PlannedUpdate],
    skipped: list[str],
    sample: int,
    execute: bool,
    overwrite: bool,
    limit_per_platform: int,
) -> None:
    print("Feishu Base XLSX backfill")
    print(f"mode: {'EXECUTE' if execute else 'DRY-RUN'}")
    print(f"project: {project}")
    print(f"xlsx: {xlsx_path}")
    print(f"sheet: {xlsx_meta['sheet']}")
    print(f"xlsx valid platform rows: {len(entries)}")
    print(f"xlsx ignored non-platform rows: {xlsx_meta['ignored_non_platform']}")
    print(f"base project records: {len(records)}")
    print(f"planned record-platform patches: {len(plans)}")
    print(f"skipped manual-review rows: {len(manual_review_skips(skipped))}")
    print(f"skipped rows/fields: {len(skipped)}")
    print(f"overwrite existing values: {overwrite}")
    if limit_per_platform > 0:
        print(f"limit per platform: {limit_per_platform}")
    print()
    if not plans:
        print("No updates planned.")
    for plan in plans[:sample]:
        print(f"- {plan.auto_id} | Owner={plan.owner} | platform={plan.platform} | xlsx row={plan.xlsx_row}")
        for name, value in plan.readable_patch.items():
            print(f"  {name} = {value}")
        if plan.skipped:
            print("  skipped: " + "; ".join(plan.skipped[:5]))
    if len(plans) > sample:
        print(f"... {len(plans) - sample} more planned patches omitted from preview.")
    manual_skips = manual_review_skips(skipped)
    if manual_skips:
        print()
        print("Manual-review skipped rows:")
        for item in manual_skips:
            print(f"- {item}")
    if skipped:
        print()
        print("Skipped rows/fields:")
        for item in skipped[:sample]:
            print(f"- {item}")
        if len(skipped) > sample:
            print(f"... {len(skipped) - sample} more skipped items omitted.")


def main() -> int:
    env_file = preparse_env_file(sys.argv[1:])
    load_env_file(env_file)
    skill_root = Path(__file__).resolve().parent.parent

    parser = argparse.ArgumentParser(description="Backfill Feishu Base metrics from a jielong XLSX.")
    parser.add_argument("--env-file", default=env_file)
    parser.add_argument("--xlsx", default=os.getenv("BACKFILL_XLSX_PATH", ""))
    parser.add_argument("--xlsx-dir", default=os.getenv("BACKFILL_XLSX_DIR", ""))
    parser.add_argument("--xlsx-glob", default=os.getenv("BACKFILL_XLSX_GLOB", "*.xlsx"))
    parser.add_argument("--sheet", default=os.getenv("BACKFILL_WORKSHEET", ""))
    parser.add_argument("--base-url", default=os.getenv("FEISHU_BASE_URL", ""))
    parser.add_argument("--base-token", default=os.getenv("FEISHU_BASE_TOKEN", ""))
    parser.add_argument("--table-id", default=os.getenv("FEISHU_TABLE_ID", ""))
    parser.add_argument("--project", default=os.getenv("BACKFILL_PROJECT", ""))
    parser.add_argument("--project-field", default=os.getenv("BACKFILL_PROJECT_FIELD", "Project"))
    parser.add_argument("--owner-field", default=os.getenv("BACKFILL_OWNER_FIELD", "Owner"))
    parser.add_argument("--xlsx-link-header", default=os.getenv("BACKFILL_XLSX_LINK_HEADER", "接龙信息"))
    parser.add_argument("--xlsx-owner-header", default=os.getenv("BACKFILL_XLSX_OWNER_HEADER", "接龙人"))
    parser.add_argument("--as-identity", choices=["bot", "user"], default=os.getenv("FEISHU_IDENTITY", "bot"))
    parser.add_argument(
        "--limit-per-platform",
        type=int,
        default=int(os.getenv("BACKFILL_LIMIT_PER_PLATFORM", "0")),
        help="select only the first N XLSX rows per platform before planning/writing",
    )
    parser.add_argument(
        "--platform",
        action="append",
        choices=sorted(PLATFORM_FIELDS),
        help="only backfill selected platform(s), for example: --platform TK",
    )
    parser.add_argument("--execute", action="store_true", help="perform writes; default is dry-run")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=str_to_bool(os.getenv("BACKFILL_OVERWRITE", "")),
        help="overwrite existing non-empty Base cells",
    )
    parser.add_argument("--sample", type=int, default=int(os.getenv("BACKFILL_SAMPLE", "12")))
    args = parser.parse_args()

    parsed_base_url = parse_base_url(args.base_url) if args.base_url else {}
    if not args.base_token:
        args.base_token = parsed_base_url.get("base_token", "")
    if not args.table_id:
        args.table_id = parsed_base_url.get("table_id", "")

    if not args.base_token:
        raise RuntimeError("Set FEISHU_BASE_URL or FEISHU_BASE_TOKEN in .env.")
    if not args.table_id:
        raise RuntimeError("Set FEISHU_BASE_URL with ?table=... or set FEISHU_TABLE_ID in .env.")

    xlsx_path = xlsx_path_from_args(args.xlsx, args.xlsx_dir, args.xlsx_glob)
    if not xlsx_path.exists():
        raise RuntimeError(f"XLSX file not found: {xlsx_path}")
    project = args.project or xlsx_path.stem

    fields_by_name = get_fields(skill_root, args.as_identity, args.base_token, args.table_id)
    missing_base = [
        name for name in (args.project_field, args.owner_field)
        if name not in fields_by_name
    ]
    if missing_base:
        raise RuntimeError("Base missing required field(s): " + ", ".join(missing_base))

    platform_fields = build_field_requirements(fields_by_name)
    update_field_names = sorted({field_info.name for mapping in platform_fields.values() for field_info in mapping.values()})
    auto_field = fields_by_name.get("自动素材编号")
    select_field_names = [
        name for name in ["自动素材编号", args.project_field, args.owner_field, *update_field_names]
        if name in fields_by_name
    ]
    select_fields = [fields_by_name[name] for name in select_field_names]

    records = search_project_records(
        skill_root=skill_root,
        identity=args.as_identity,
        base_token=args.base_token,
        table_id=args.table_id,
        project_value=project,
        project_field=fields_by_name[args.project_field],
        select_fields=select_fields,
    )
    if not records:
        raise RuntimeError(f"No Base records found for {args.project_field}={project}.")

    owner_buckets = build_owner_record_buckets(records, fields_by_name[args.owner_field])
    entries, xlsx_meta = read_xlsx_entries(
        xlsx_path=xlsx_path,
        sheet_name=args.sheet,
        link_header=args.xlsx_link_header,
        owner_header=args.xlsx_owner_header,
    )
    platform_filter = set(args.platform or [])
    if platform_filter:
        entries = [entry for entry in entries if entry.platform in platform_filter]
    entries = limit_entries_per_platform(entries, args.limit_per_platform)

    plans, skipped = plan_updates(
        entries=entries,
        platform_fields=platform_fields,
        owner_buckets=owner_buckets,
        auto_field=auto_field,
        overwrite=args.overwrite,
    )
    if not platform_filter:
        skipped = xlsx_meta.get("ignored_non_platform_rows", []) + xlsx_meta.get("ignored_blank_owner_rows", []) + skipped
    print_plan(
        project=project,
        xlsx_path=xlsx_path,
        entries=entries,
        xlsx_meta=xlsx_meta,
        records=records,
        plans=plans,
        skipped=skipped,
        sample=args.sample,
        execute=args.execute,
        overwrite=args.overwrite,
        limit_per_platform=args.limit_per_platform,
    )

    if args.execute:
        if not plans:
            print("Nothing to execute.")
        else:
            execute_updates(
                skill_root=skill_root,
                identity=args.as_identity,
                base_token=args.base_token,
                table_id=args.table_id,
                plans=plans,
            )
    else:
        print()
        print("Dry-run only. Re-run with --execute after user confirmation to write changes.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
