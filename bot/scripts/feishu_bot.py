#!/usr/bin/env python3
"""Feishu command bot for registered Feishu workflows.

The bot listens to im.message.receive_v1 events through lark-cli. When a group
message asks it to process a topic, it starts the integrated
feishu-workflow child process and forwards progress events.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import requests
import shutil
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import winston_chat


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

BOT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BOT_ROOT.parent
DEFAULT_STATE_PATH = BOT_ROOT / "tmp" / "feishu_bot_state.json"
DEFAULT_LOG_DIR = BOT_ROOT / "tmp" / "feishu_bot_logs"
_BOT_TAT_CACHE: tuple[str, float] | None = None
_BOT_TAT_SESSION = requests.Session()


def _load_bot_api_credentials() -> tuple[str, str]:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if app_id and app_secret:
        return app_id, app_secret
    env_path = PROJECT_ROOT / "workflow" / "env" / "chats.env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip("\"'")
            if key.strip() == "FEISHU_APP_ID":
                app_id = value
            elif key.strip() == "FEISHU_APP_SECRET":
                app_secret = value
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID/FEISHU_APP_SECRET are required for direct IM API calls")
    return app_id, app_secret


def _bot_tenant_access_token() -> str:
    global _BOT_TAT_CACHE
    now = time.time()
    if _BOT_TAT_CACHE and _BOT_TAT_CACHE[1] > now:
        return _BOT_TAT_CACHE[0]
    app_id, app_secret = _load_bot_api_credentials()
    response = _BOT_TAT_SESSION.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=30,
    )
    if response.status_code == 429:
        raise RuntimeError("tenant_access_token returned HTTP 429")
    response.raise_for_status()
    payload = response.json()
    token = str(payload.get("tenant_access_token") or "")
    if not token:
        raise RuntimeError(f"tenant_access_token failed: code={payload.get('code')} msg={payload.get('msg')}")
    _BOT_TAT_CACHE = (token, now + max(600, int(payload.get("expire") or 7200)) - 300)
    return token


def _bot_im_api(method: str, path: str, *, params: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    for attempt in range(2):
        token = _bot_tenant_access_token()
        response = _BOT_TAT_SESSION.request(
            method,
            "https://open.feishu.cn" + path,
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        if response.status_code == 429:
            if attempt == 0:
                global _BOT_TAT_CACHE
                _BOT_TAT_CACHE = None
                time.sleep(15)
                continue
            raise RuntimeError("Feishu IM API returned HTTP 429 after one retry")
        if response.status_code in {401, 403} and attempt == 0:
            _BOT_TAT_CACHE = None
            continue
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("code") or 0) != 0:
            raise RuntimeError(f"Feishu IM API failed: code={payload.get('code')} msg={payload.get('msg')}")
        return payload
    raise RuntimeError("Feishu IM API authorization refresh failed")

WORKFLOW_SCRIPT = Path(
    os.environ.get(
        "FEISHU_WORKFLOW_SCRIPT",
        str(PROJECT_ROOT / "workflow" / "scripts" / "feishu_workflow.py"),
    )
)
CANCELLED_RETURN_CODE = -999


@dataclass(frozen=True)
class CommandSpec:
    trigger: str
    action: str
    workflow_id: str | None
    mode: str | None
    description: str


COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "抓取": CommandSpec(
        trigger="抓取",
        action="run",
        workflow_id="full_social_scrape",
        mode="full",
        description="导出接龙，抓取社媒数据，回填素材推送表。",
    ),
    "72h": CommandSpec(
        trigger="72h",
        action="refresh_72h",
        workflow_id="refresh_72h",
        mode="72h",
        description="重新抓取全平台链接的 72h 播放/曝光和互动数据并回填。",
    ),
    "确认推送": CommandSpec(
        trigger="确认推送",
        action="confirm_push",
        workflow_id="confirm_push",
        mode="confirm-push",
        description="筛选指定 Project 中 Owner 有值且推送通知为空的记录，写入确认推送并复核通知状态。",
    ),
    "二次推送": CommandSpec(
        trigger="二次推送",
        action="secondary_push",
        workflow_id="secondary_push",
        mode="secondary-push",
        description="导出接龙 XLSX，按现有规则回填社媒链接，并标记二次推送。",
    ),
    "今日发布数": CommandSpec(
        trigger="今日发布数",
        action="today_publish_count",
        workflow_id="today_publish_count",
        mode="today-publish-count",
        description="读取账号活跃度三张视图，统计并写入今日素材推送账号编号。",
    ),
    "海外市场账号": CommandSpec(
        trigger="海外市场账号",
        action="weekly_market_accounts",
        workflow_id="weekly_market_accounts",
        mode="weekly-market-accounts",
        description="读取海外账号表，按指令日期范围抓取账号发帖内容和数据，并在同一 Base 生成日期范围表。",
    ),
}
CONTROL_COMMANDS = {
    "停止工作": "stop",
    "状态": "status",
    "帮助": "help",
}
ACTION_TO_WORKFLOW_ID = {
    spec.action: spec.workflow_id
    for spec in COMMAND_REGISTRY.values()
    if spec.workflow_id
}
WORKFLOW_MODE_BY_ID = {
    spec.workflow_id: spec.mode
    for spec in COMMAND_REGISTRY.values()
    if spec.workflow_id and spec.mode
}


@dataclass
class ParsedCommand:
    action: str
    workflow_id: str | None = None
    project: str | None = None
    local_xlsx_only: bool = False
    dry_run: bool = False
    no_backfill: bool = False
    overwrite_backfill: bool = False
    source_chat: str | None = None
    source_base_alias: str | None = None
    source_base_url: str | None = None
    source_base_token: str | None = None
    target_base_alias: str | None = None
    target_base_url: str | None = None
    target_base_token: str | None = None
    target_table_id: str | None = None
    base_url: str | None = None
    base_token: str | None = None
    start_row: int | None = None
    limit_per_platform: int | None = None
    latest_per_account: int | None = None
    platforms: str | None = None
    source_indexes: str | None = None
    raw: str = ""


@dataclass
class BotConfig:
    allowed_chat_ids: set[str] = field(default_factory=set)
    chat_only_chat_ids: set[str] = field(default_factory=set)
    allowed_p2p_chat_ids: set[str] = field(default_factory=set)
    allow_any_chat: bool = False
    source_chat_id: str | None = None
    report_chat_id: str | None = None
    command_chat_aliases: dict[str, str] = field(default_factory=dict)
    source_chat_aliases: dict[str, str] = field(default_factory=dict)
    report_chat_aliases: dict[str, str] = field(default_factory=dict)
    source_base_url_aliases: dict[str, str] = field(default_factory=dict)
    source_base_token_aliases: dict[str, str] = field(default_factory=dict)
    target_base_alias: str | None = None
    target_base_url_aliases: dict[str, str] = field(default_factory=dict)
    target_base_token_aliases: dict[str, str] = field(default_factory=dict)
    use_command_chat_as_source: bool = True
    reply: bool = True
    verify_replies: bool = True
    require_at: bool = False
    mention_matches: set[str] = field(default_factory=set)
    execute_backfill_by_default: bool = True
    heartbeat_interval_s: int = 0
    state_path: Path = DEFAULT_STATE_PATH
    log_dir: Path = DEFAULT_LOG_DIR
    workdir: Path = PROJECT_ROOT


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float


@dataclass
class ActiveJob:
    job_id: str
    command: ParsedCommand
    event_chat_id: str | None
    message_id: str | None
    started_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    current_step: str = ""
    last_progress_message: str = ""
    current_process: subprocess.Popen[Any] | None = None
    thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def project(self) -> str:
        project = self.command.project or "接龙"
        if self.command.workflow_id == "refresh_72h" or self.command.action == "refresh_72h":
            return f"72h：{project}"
        if self.command.workflow_id == "confirm_push" or self.command.action == "confirm_push":
            return f"确认推送：{project}"
        if self.command.workflow_id == "secondary_push" or self.command.action == "secondary_push":
            return f"二次推送：{project}"
        if self.command.workflow_id == "weekly_market_accounts" or self.command.action == "weekly_market_accounts":
            suffix = f"：{self.command.project}" if self.command.project else ""
            return f"海外市场账号{suffix}"
        return project

    def set_process(
        self,
        process: subprocess.Popen[Any] | None,
        step: str = "",
        progress_message: str | None = None,
    ) -> None:
        with self.lock:
            self.current_process = process
            if step:
                self.current_step = step
            if progress_message is not None:
                self.last_progress_message = progress_message


@dataclass
class BotRuntime:
    config: BotConfig
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_job: ActiveJob | None = None
    chat_threads: dict[str, threading.Thread] = field(default_factory=dict)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_set(name: str) -> set[str]:
    value = os.environ.get(name, "")
    return {item.strip() for item in value.split(",") if item.strip()}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "")
    try:
        return int(value.strip()) if value.strip() else default
    except ValueError:
        return default


def parse_mapping_items(items: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        text = item.strip()
        if not text or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            mapping[key] = value
    return mapping


def env_mapping(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items() if str(key) and str(value)}
    return parse_mapping_items(re.split(r"[;\n]+", raw))


def default_ref_with_inline_alias(raw: str | None) -> tuple[str | None, dict[str, str]]:
    value = (raw or "").strip()
    if not value:
        return None, {}
    if "=" not in value or looks_like_chat_id(value):
        return value, {}
    mapping = parse_mapping_items(re.split(r"[;\n]+", value))
    if not mapping:
        return value, {}
    return next(iter(mapping)), mapping


def looks_like_chat_id(value: str) -> bool:
    return value.startswith("oc_")


def looks_like_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def looks_like_base_token(value: str) -> bool:
    return bool(re.match(r"^(?:bascn|app|base|wiki|tbl)[A-Za-z0-9_-]+", value))


def resolve_ref(ref: str, aliases: dict[str, str], role: str, *, allow_direct: bool = True) -> str:
    value = ref.strip()
    if value in aliases:
        return aliases[value]
    if allow_direct:
        if role.endswith("chat") and looks_like_chat_id(value):
            return value
        if "url" in role and looks_like_url(value):
            return value
        if "token" in role and looks_like_base_token(value):
            return value
    raise ValueError(f"Unknown {role} alias: {value}")


def build_config(args: argparse.Namespace) -> BotConfig:
    allowed = set(env_set("JIELONG_BOT_ALLOWED_CHAT_IDS"))
    chat_only = set(env_set("WINSTON_CHAT_ALLOWED_CHAT_IDS"))
    allowed_p2p = set(env_set("WINSTON_CHAT_ALLOWED_P2P_CHAT_IDS"))
    for chat_id in getattr(args, "allowed_chat", []) or []:
        allowed.add(chat_id)

    command_chat_aliases = env_mapping("JIELONG_BOT_COMMAND_CHAT_ALIASES")
    command_chat_aliases.update(parse_mapping_items(getattr(args, "command_chat_alias", []) or []))
    allowed.update(command_chat_aliases.values())

    source_chat_aliases = env_mapping("JIELONG_BOT_SOURCE_CHAT_ALIASES")
    source_chat_aliases.update(env_mapping("JIELONG_BOT_CHAT_ALIASES"))
    source_chat_aliases.update(parse_mapping_items(getattr(args, "source_chat_alias", []) or []))
    source_chat_aliases.update(parse_mapping_items(getattr(args, "chat_alias", []) or []))

    report_chat_aliases = env_mapping("JIELONG_BOT_REPORT_CHAT_ALIASES")
    report_ref, inline_report_aliases = default_ref_with_inline_alias(
        getattr(args, "report_chat_id", None)
        or os.environ.get("JIELONG_BOT_DEFAULT_REPORT_CHAT")
        or os.environ.get("JIELONG_BOT_REPORT_CHAT_ID")
        or None
    )
    report_chat_aliases.update(inline_report_aliases)
    report_chat_id = resolve_ref(report_ref, report_chat_aliases, "report chat") if report_ref else None

    source_base_url_aliases = env_mapping("JIELONG_BOT_SOURCE_BASE_URL_ALIASES")
    source_base_url_aliases.update(parse_mapping_items(getattr(args, "source_base_url_alias", []) or []))
    source_base_token_aliases = env_mapping("JIELONG_BOT_SOURCE_BASE_TOKEN_ALIASES")
    source_base_token_aliases.update(parse_mapping_items(getattr(args, "source_base_token_alias", []) or []))

    target_base_url_aliases = env_mapping("JIELONG_BOT_TARGET_BASE_URL_ALIASES")
    target_base_url_aliases.update(env_mapping("JIELONG_BOT_BASE_URL_ALIASES"))
    target_base_url_aliases.update(parse_mapping_items(getattr(args, "target_base_url_alias", []) or []))
    target_base_url_aliases.update(parse_mapping_items(getattr(args, "base_url_alias", []) or []))
    target_base_token_aliases = env_mapping("JIELONG_BOT_TARGET_BASE_TOKEN_ALIASES")
    target_base_token_aliases.update(env_mapping("JIELONG_BOT_BASE_TOKEN_ALIASES"))
    target_base_token_aliases.update(parse_mapping_items(getattr(args, "target_base_token_alias", []) or []))
    target_base_token_aliases.update(parse_mapping_items(getattr(args, "base_token_alias", []) or []))

    source_ref = (
        getattr(args, "source_chat_id", None)
        or os.environ.get("JIELONG_BOT_DEFAULT_SOURCE_CHAT")
        or os.environ.get("JIELONG_BOT_SOURCE_CHAT_ID")
        or None
    )
    source_chat_id = resolve_ref(source_ref, source_chat_aliases, "source chat") if source_ref else None
    target_base_alias = (
        getattr(args, "target_base", None)
        or getattr(args, "base_alias", None)
        or os.environ.get("JIELONG_BOT_DEFAULT_TARGET_BASE")
        or None
    )
    return BotConfig(
        allowed_chat_ids=allowed,
        chat_only_chat_ids=chat_only,
        allowed_p2p_chat_ids=allowed_p2p,
        allow_any_chat=getattr(args, "allow_any_chat", False) or env_bool("JIELONG_BOT_ALLOW_ANY_CHAT", False),
        source_chat_id=source_chat_id,
        report_chat_id=report_chat_id,
        command_chat_aliases=command_chat_aliases,
        source_chat_aliases=source_chat_aliases,
        report_chat_aliases=report_chat_aliases,
        source_base_url_aliases=source_base_url_aliases,
        source_base_token_aliases=source_base_token_aliases,
        target_base_alias=target_base_alias,
        target_base_url_aliases=target_base_url_aliases,
        target_base_token_aliases=target_base_token_aliases,
        use_command_chat_as_source=env_bool("JIELONG_BOT_USE_COMMAND_CHAT_AS_SOURCE", True),
        reply=not getattr(args, "no_reply", False) and env_bool("JIELONG_BOT_REPLY", True),
        verify_replies=(
            not getattr(args, "no_verify_reply", False)
            and env_bool("JIELONG_BOT_VERIFY_REPLIES", True)
        ),
        require_at=getattr(args, "require_at", False) or env_bool("JIELONG_BOT_REQUIRE_AT", False),
        mention_matches=set(env_set("JIELONG_BOT_MENTION_MATCHES")),
        execute_backfill_by_default=env_bool("JIELONG_BOT_EXECUTE_BACKFILL", True),
        heartbeat_interval_s=max(
            0,
            getattr(args, "heartbeat_seconds", None)
            if getattr(args, "heartbeat_seconds", None) is not None
            else env_int("JIELONG_BOT_HEARTBEAT_SECONDS", 0),
        ),
        state_path=Path(os.environ.get("JIELONG_BOT_STATE_PATH", str(DEFAULT_STATE_PATH))),
        log_dir=Path(os.environ.get("JIELONG_BOT_LOG_DIR", str(DEFAULT_LOG_DIR))),
        workdir=Path(os.environ.get("JIELONG_BOT_WORKDIR", str(PROJECT_ROOT))),
    )


def strip_mentions(text: str) -> str:
    text = re.sub(r"<at\s+[^>]*>.*?</at>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(^|\s)@\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_mention_value(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def mention_match_is_id(value: str) -> bool:
    return bool(re.match(r"^(?:ou_|on_|cli_|ou-|on-|cli-)", value, flags=re.IGNORECASE))


MENTION_ID_KEYS = {
    "id",
    "open_id",
    "openid",
    "user_id",
    "userid",
    "union_id",
    "unionid",
}
MENTION_NAME_KEYS = {
    "name",
    "display_name",
    "displayname",
    "username",
    "user_name",
}


def configured_mention_sets(config: BotConfig) -> tuple[set[str], set[str]]:
    configured = {normalize_mention_value(item) for item in config.mention_matches if normalize_mention_value(item)}
    return {item for item in configured if mention_match_is_id(item)}, {
        item for item in configured if not mention_match_is_id(item)
    }


def mention_candidate_sets(mention: Any) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    names: set[str] = set()
    if isinstance(mention, dict):
        for key, value in mention.items():
            if isinstance(value, (dict, list, tuple, set)):
                continue
            normalized = normalize_mention_value(value)
            if not normalized:
                continue
            key_norm = normalize_mention_value(key)
            if key_norm in MENTION_ID_KEYS or mention_match_is_id(normalized):
                ids.add(normalized)
            if key_norm in MENTION_NAME_KEYS:
                names.add(normalized)
    elif isinstance(mention, (str, int, float)):
        normalized = normalize_mention_value(mention)
        if mention_match_is_id(normalized):
            ids.add(normalized)
        elif normalized:
            names.add(normalized)
    return ids, names


def mention_candidates_match(
    ids: set[str],
    names: set[str],
    configured_ids: set[str],
    configured_names: set[str],
) -> bool:
    if not configured_ids and not configured_names:
        return False
    if ids & configured_ids:
        return True
    return bool(configured_names and names & configured_names)


def iter_event_mentions(event: dict[str, Any]) -> list[Any]:
    mentions: list[Any] = []
    containers: list[dict[str, Any]] = [event]
    for key in ("event", "message", "data", "body", "payload"):
        value = event.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        value = container.get("mentions")
        if isinstance(value, list):
            mentions.extend(value)
        elif isinstance(value, dict):
            mentions.append(value)
    return mentions


def event_mention_collection_matches(
    event: dict[str, Any],
    configured_ids: set[str],
    configured_names: set[str],
) -> bool:
    for mention in iter_event_mentions(event):
        ids, names = mention_candidate_sets(mention)
        if mention_candidates_match(ids, names, configured_ids, configured_names):
            return True
    return False


def at_tags_match_content(
    content: str,
    configured_ids: set[str],
    configured_names: set[str],
) -> bool:
    for attrs, body in re.findall(r"<at\b([^>]*)>(.*?)</at>", content, flags=re.IGNORECASE | re.DOTALL):
        ids: set[str] = set()
        names = {normalize_mention_value(body)} if normalize_mention_value(body) else set()
        attr_pairs = list(re.finditer(r"([\w:-]+)\s*=\s*(['\"])(.*?)\2", attrs))
        attr_pairs.extend(re.finditer(r"([\w:-]+)\s*=\s*([^\s>]+)", attrs))
        for match in attr_pairs:
            key = normalize_mention_value(match.group(1))
            value = normalize_mention_value(match.group(3) if len(match.groups()) >= 3 else match.group(2))
            if not value:
                continue
            if key in MENTION_ID_KEYS or mention_match_is_id(value):
                ids.add(value)
            if key in MENTION_NAME_KEYS:
                names.add(value)
        if mention_candidates_match(ids, names, configured_ids, configured_names):
            return True
    return False


def fetch_recent_chat_message(event: dict[str, Any], config: BotConfig) -> dict[str, Any] | None:
    chat_id = str(event.get("chat_id") or "")
    message_id = str(event.get("message_id") or event.get("id") or "")
    content = str(event.get("content") or "")
    if not chat_id or not message_id:
        return None
    if "@" not in content and "<at" not in content.lower():
        return None
    result = run_command(
        lark_cli_argv()
        + [
            "im",
            "+chat-messages-list",
            "--chat-id",
            chat_id,
            "--page-size",
            "50",
            "--sort",
            "desc",
            "--no-reactions",
            "--as",
            "bot",
            "--format",
            "json",
        ],
        cwd=config.workdir,
        timeout_s=30,
    )
    if result.returncode != 0:
        print("[mention-fetch-error] " + safe_tail(result.stderr or result.stdout), file=sys.stderr, flush=True)
        return None
    try:
        payload = json.loads(result.stdout)
        messages = payload.get("data", {}).get("messages", [])
    except Exception as exc:
        print(f"[mention-fetch-error] failed to parse chat messages: {exc}", file=sys.stderr, flush=True)
        return None
    for message in messages:
        if str(message.get("message_id") or message.get("id") or "") == message_id:
            return message
    return None


def event_mentions_bot(event: dict[str, Any], config: BotConfig) -> bool:
    content = str(event.get("content") or "")
    configured_ids, configured_names = configured_mention_sets(config)
    if event_mention_collection_matches(event, configured_ids, configured_names):
        return True
    if at_tags_match_content(content, configured_ids, configured_names):
        return True
    recent_message = fetch_recent_chat_message(event, config)
    if recent_message and event_mention_collection_matches(recent_message, configured_ids, configured_names):
        return True
    return False


def tokenize_command(text: str) -> list[str]:
    normalized = strip_mentions(text.replace("\u3000", " "))
    try:
        return shlex.split(normalized)
    except ValueError:
        return normalized.split()


def refresh_72h_trigger_length(tokens: list[str], index: int) -> int:
    token = tokens[index].lower()
    compact = re.sub(r"[\s_\-]+", "", token)
    if compact in {"72h", "72小时", "72hour", "72hours", "refresh72h"}:
        return 1
    if token in {"72", "72h"} and index + 1 < len(tokens):
        next_compact = re.sub(r"[\s_\-]+", "", tokens[index + 1].lower())
        if next_compact in {"h", "小时", "hours", "hour"}:
            return 2
    return 0


LOCAL_XLSX_COMMAND_PREFIXES = (
    ('二次推送本地', '二次推送'),
    ('72h本地', '72h'),
    ('抓取本地', '抓取'),
)


def match_local_xlsx_command_token(token: str) -> tuple[str, str] | None:
    lowered = token.lower()
    for prefix, command_name in LOCAL_XLSX_COMMAND_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return command_name, token[len(prefix) :]
    return None


def parse_command(text: str) -> ParsedCommand | None:
    raw = text.strip()
    normalized = strip_mentions(raw)
    if not normalized:
        return None

    if re.search(r"\b(stop|cancel|abort)\b|停止|取消|中止|终止", normalized, flags=re.IGNORECASE):
        return ParsedCommand(action=CONTROL_COMMANDS["停止工作"], raw=raw)

    if re.search(r"\b(status|progress)\b|状态|进度|当前工作", normalized, flags=re.IGNORECASE):
        return ParsedCommand(action=CONTROL_COMMANDS["状态"], raw=raw)

    if re.search(r"\b(help|usage)\b|帮助|用法|怎么用", normalized, flags=re.IGNORECASE):
        return ParsedCommand(action=CONTROL_COMMANDS["帮助"], raw=raw)

    tokens = tokenize_command(raw)
    if not tokens:
        return None

    trigger_index: int | None = None
    trigger_spec = COMMAND_REGISTRY["抓取"]
    trigger_length = 1
    local_xlsx_only = False
    inline_project = ''
    triggers = {"抓取", "导出", "接龙", "社媒", "回填", "run", "fetch", "jielong"}
    for index, token in enumerate(tokens):
        local_match = match_local_xlsx_command_token(token)
        if local_match:
            command_name, inline_project = local_match
            trigger_index = index
            trigger_spec = COMMAND_REGISTRY[command_name]
            local_xlsx_only = True
            break
        lowered = token.lower()
        compact = re.sub(r"[\s_\-]+", "", lowered)
        refresh_72h_length = refresh_72h_trigger_length(tokens, index)
        if refresh_72h_length:
            trigger_index = index
            trigger_spec = COMMAND_REGISTRY["72h"]
            trigger_length = refresh_72h_length
            break
        if compact in {"确认推送", "confirmpush", "pushconfirm"} or any(
            word in token for word in ("确认推送", "推送确认")
        ):
            trigger_index = index
            trigger_spec = COMMAND_REGISTRY["确认推送"]
            break
        if compact in {"二次推送", "secondarypush", "secondpush"} or any(
            word in token for word in ("二次推送", "二推")
        ):
            trigger_index = index
            trigger_spec = COMMAND_REGISTRY["二次推送"]
            break
        if compact in {"今日发布数", "todaypublishcount", "todaypublish"} or any(
            word in token for word in ("今日发布数", "今天发布数")
        ):
            trigger_index = index
            trigger_spec = COMMAND_REGISTRY["今日发布数"]
            break
        if compact in {"海外市场账号", "海外账号", "weeklymarketaccounts", "marketaccounts"} or any(
            word in token for word in ("海外市场账号", "海外账号")
        ):
            trigger_index = index
            trigger_spec = COMMAND_REGISTRY["海外市场账号"]
            break
        if lowered in triggers or any(word in token for word in ("抓取", "导出接龙", "社媒数据", "回填")):
            trigger_index = index
            break
    if trigger_index is None:
        return None

    command = ParsedCommand(
        action=trigger_spec.action,
        workflow_id=trigger_spec.workflow_id,
        local_xlsx_only=local_xlsx_only,
        raw=raw,
    )
    project_parts: list[str] = [inline_project] if inline_project else []
    index = trigger_index + trigger_length
    while index < len(tokens):
        token = tokens[index]
        lowered = token.lower()
        if lowered in {"--dry-run", "dry-run", "试跑", "预览"}:
            command.dry_run = True
        elif lowered in {"--no-backfill", "no-backfill", "不回填", "只抓取"}:
            command.no_backfill = True
        elif lowered in {"--overwrite", "覆盖"}:
            command.overwrite_backfill = True
        elif lowered in {"--chat-id", "chat-id", "chat", "--source", "source", "--source-chat", "source-chat", "来源", "接龙群"} and index + 1 < len(tokens):
            index += 1
            command.source_chat = tokens[index]
        elif lowered.startswith("--chat-id=") or lowered.startswith("--source=") or lowered.startswith("--source-chat="):
            command.source_chat = token.split("=", 1)[1]
        elif lowered.startswith("chat=") or lowered.startswith("source=") or lowered.startswith("来源=") or lowered.startswith("接龙群="):
            command.source_chat = token.split("=", 1)[1]
        elif lowered in {"--source-base", "source-base", "接龙base", "来源base"} and index + 1 < len(tokens):
            index += 1
            command.source_base_alias = tokens[index]
        elif lowered.startswith("--source-base=") or lowered.startswith("source-base=") or lowered.startswith("接龙base=") or lowered.startswith("来源base="):
            command.source_base_alias = token.split("=", 1)[1]
        elif lowered in {"--source-base-url", "source-base-url"} and index + 1 < len(tokens):
            index += 1
            command.source_base_url = tokens[index]
        elif lowered.startswith("--source-base-url="):
            command.source_base_url = token.split("=", 1)[1]
        elif lowered in {"--source-base-token", "source-base-token"} and index + 1 < len(tokens):
            index += 1
            command.source_base_token = tokens[index]
        elif lowered.startswith("--source-base-token="):
            command.source_base_token = token.split("=", 1)[1]
        elif lowered in {"--base", "base", "--target-base", "target-base", "目标表", "素材表"} and index + 1 < len(tokens):
            index += 1
            command.target_base_alias = tokens[index]
        elif lowered.startswith("--base=") or lowered.startswith("base=") or lowered.startswith("--target-base=") or lowered.startswith("target-base=") or lowered.startswith("目标表=") or lowered.startswith("素材表="):
            command.target_base_alias = token.split("=", 1)[1]
        elif lowered in {"--target-base-url", "target-base-url"} and index + 1 < len(tokens):
            index += 1
            command.target_base_url = tokens[index]
        elif lowered.startswith("--target-base-url="):
            command.target_base_url = token.split("=", 1)[1]
        elif lowered in {"--target-base-token", "target-base-token"} and index + 1 < len(tokens):
            index += 1
            command.target_base_token = tokens[index]
        elif lowered.startswith("--target-base-token="):
            command.target_base_token = token.split("=", 1)[1]
        elif lowered in {"--target-table-id", "target-table-id"} and index + 1 < len(tokens):
            index += 1
            command.target_table_id = tokens[index]
        elif lowered.startswith("--target-table-id="):
            command.target_table_id = token.split("=", 1)[1]
        elif lowered in {"--base-url", "base-url"} and index + 1 < len(tokens):
            index += 1
            command.source_base_url = tokens[index]
        elif lowered.startswith("--base-url="):
            command.source_base_url = token.split("=", 1)[1]
        elif lowered in {"--base-token", "base-token"} and index + 1 < len(tokens):
            index += 1
            command.source_base_token = tokens[index]
        elif lowered.startswith("--base-token="):
            command.source_base_token = token.split("=", 1)[1]
        elif lowered in {"--start-row", "start-row"} and index + 1 < len(tokens):
            index += 1
            command.start_row = int(tokens[index])
        elif lowered.startswith("--start-row="):
            command.start_row = int(token.split("=", 1)[1])
        elif lowered in {"--limit-per-platform", "limit"} and index + 1 < len(tokens):
            index += 1
            command.limit_per_platform = int(tokens[index])
        elif lowered.startswith("--limit-per-platform="):
            command.limit_per_platform = int(token.split("=", 1)[1])
        elif lowered in {"--latest-per-account", "latest"} and index + 1 < len(tokens):
            index += 1
            command.latest_per_account = int(tokens[index])
        elif lowered.startswith("--latest-per-account="):
            command.latest_per_account = int(token.split("=", 1)[1])
        elif lowered in {"--platforms", "platforms", "platform", "平台"} and index + 1 < len(tokens):
            index += 1
            command.platforms = tokens[index]
        elif lowered.startswith("--platforms=") or lowered.startswith("platforms=") or lowered.startswith("platform=") or lowered.startswith("平台="):
            command.platforms = token.split("=", 1)[1]
        elif lowered in {"--source-indexes", "source-indexes", "source-index", "源序号"} and index + 1 < len(tokens):
            index += 1
            command.source_indexes = tokens[index]
        elif lowered.startswith("--source-indexes=") or lowered.startswith("source-indexes=") or lowered.startswith("source-index=") or lowered.startswith("源序号="):
            command.source_indexes = token.split("=", 1)[1]
        elif token.startswith("-") and token != "-":
            pass
        else:
            project_parts.append(token)
        index += 1

    command.project = " ".join(project_parts).strip("：:，, ")
    if command.project.startswith("主题=") or command.project.startswith("项目="):
        command.project = command.project.split("=", 1)[1].strip()
    if command.workflow_id == "today_publish_count":
        return command
    if command.workflow_id == "weekly_market_accounts":
        return command
    if (
        not command.project
        and not command.source_base_alias
        and not command.source_base_url
        and not command.source_base_token
        and not command.target_base_alias
        and not command.target_base_url
        and not command.target_base_token
    ):
        return ParsedCommand(action=CONTROL_COMMANDS["帮助"], raw=raw)
    return command


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed_event_ids": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"processed_event_ids": []}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    processed = state.get("processed_event_ids", [])
    if len(processed) > 1000:
        state["processed_event_ids"] = processed[-1000:]
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def active_job_snapshot(job: ActiveJob, status: str) -> dict[str, Any]:
    with job.lock:
        process = job.current_process
        current_step = job.current_step
        progress = job.last_progress_message
    return {
        "status": status,
        "job_id": job.job_id,
        "workflow_id": job.command.workflow_id,
        "action": job.command.action,
        "project": job.command.project,
        "display_project": job.project,
        "event_chat_id": job.event_chat_id,
        "message_id": job.message_id,
        "started_at_unix": job.started_at,
        "elapsed_s": int(time.time() - job.started_at),
        "current_step": current_step,
        "last_progress_message": progress,
        "pid": process.pid if process and process.poll() is None else None,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_active_job_state(config: BotConfig, job: ActiveJob | None, status: str = "") -> None:
    state = load_state(config.state_path)
    if job is None:
        state["active_job"] = None
        state["active_job_updated_at"] = datetime.now().isoformat(timespec="seconds")
    else:
        state["active_job"] = active_job_snapshot(job, status or "running")
    save_state(config.state_path, state)


def finish_active_job_state(config: BotConfig, job: ActiveJob, status: str) -> None:
    state = load_state(config.state_path)
    state["last_active_job"] = active_job_snapshot(job, status)
    state["active_job"] = None
    state["active_job_updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(config.state_path, state)


def terminate_process_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=utf8_child_env(),
        )
        return
    try:
        os.kill(pid, 15)
    except OSError:
        pass


def run_command(
    argv: list[str],
    cwd: Path,
    timeout_s: int | None = None,
    job: ActiveJob | None = None,
    step: str = "",
) -> CommandResult:
    start = time.time()
    if job and job.cancel_event.is_set():
        return CommandResult(
            CANCELLED_RETURN_CODE,
            "",
            f"\n[cancelled] step={step or 'command'}\n",
            time.time() - start,
        )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    child_env = utf8_child_env()
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=child_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    if job:
        job.set_process(process, step)

    def read_stream(stream: Any, chunks: list[str]) -> None:
        try:
            for line in stream:
                chunks.append(line)
        except Exception as exc:
            chunks.append(f"[stream-read-error] {exc}\n")

    stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, stdout_chunks), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=(process.stderr, stderr_chunks), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    deadline = start + timeout_s if timeout_s else None
    cancelled = False
    timed_out = False
    while process.poll() is None:
        if job and job.cancel_event.is_set():
            cancelled = True
            terminate_process_tree(process.pid)
            break
        if deadline and time.time() > deadline:
            timed_out = True
            terminate_process_tree(process.pid)
            break
        time.sleep(0.25)

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process.pid)
        process.wait(timeout=10)

    stdout_thread.join(timeout=3)
    stderr_thread.join(timeout=3)
    if job:
        job.set_process(None)
        if job.cancel_event.is_set():
            cancelled = True

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    if cancelled:
        stderr += f"\n[cancelled] step={step or 'command'}\n"
        return CommandResult(CANCELLED_RETURN_CODE, stdout, stderr, time.time() - start)
    if timed_out:
        stderr += f"\n[timeout] step={step or 'command'} timeout_s={timeout_s}\n"
        return CommandResult(124, stdout, stderr, time.time() - start)
    return CommandResult(
        returncode=process.returncode if process.returncode is not None else 1,
        stdout=stdout,
        stderr=stderr,
        elapsed_s=time.time() - start,
    )


def lark_cli_path() -> str:
    configured = os.environ.get("LARK_CLI_PATH")
    candidates = [
        configured,
        shutil.which("lark-cli"),
        shutil.which("lark-cli.cmd"),
        shutil.which("lark-cli.CMD"),
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "lark-cli.CMD"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate))
    raise FileNotFoundError("lark-cli was not found. Set LARK_CLI_PATH to the lark-cli.CMD path.")


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
    """Return argv prefix for lark-cli without going through npm .cmd shims.

    The Windows npm-generated lark-cli.CMD forwards arguments with %*, so text
    replies containing URLs with '&' can be reinterpreted by cmd.exe and break
    JSON content. Running the CLI's JS entrypoint through node keeps --content
    as a real argv value.
    """
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

    return [lark_cli_path()]


def safe_tail(text: str, max_lines: int = 20, max_chars: int = 2200) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail


def write_job_log(config: BotConfig, job_id: str, name: str, result: CommandResult) -> Path:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    path = config.log_dir / f"{job_id}_{name}.log"
    path.write_text(
        "\n".join(
            [
                f"returncode={result.returncode}",
                f"elapsed_s={result.elapsed_s:.1f}",
                "",
                "STDOUT:",
                result.stdout,
                "",
                "STDERR:",
                result.stderr,
            ]
        ),
        encoding="utf-8",
    )
    return path


def parse_sent_message_id(output: str) -> str:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return ""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return ""
    return str(data.get("message_id") or "")


def verify_text_message(message_id: str, expected_text: str, config: BotConfig) -> bool:
    if not message_id:
        print("[reply-verify-error] missing sent message_id", file=sys.stderr, flush=True)
        return False
    try:
        payload = _bot_im_api("GET", f"/open-apis/im/v1/messages/{message_id}")
        message = (payload.get("data") or {}).get("items") or []
        actual_text = message[0].get("body", {}).get("content", "") if message else ""
        if isinstance(actual_text, str):
            try:
                actual_text = json.loads(actual_text).get("text", actual_text)
            except json.JSONDecodeError:
                pass
    except Exception as exc:
        print(f"[reply-verify-error] {exc}", file=sys.stderr, flush=True)
        return False
    if actual_text != expected_text:
        print("[reply-verify-error] sent text mismatch", file=sys.stderr, flush=True)
        return False
    return True


def relative_cli_file_path(path: Path, cwd: Path) -> str:
    resolved_path = path.resolve()
    resolved_cwd = cwd.resolve()
    relative = resolved_path.relative_to(resolved_cwd)
    return "./" + relative.as_posix()


def reply_to_message(message_id: str | None, text: str, config: BotConfig) -> None:
    print(text, flush=True)
    if not config.reply or not message_id:
        return
    try:
        payload = _bot_im_api(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            body={"content": json.dumps({"text": text}, ensure_ascii=False), "msg_type": "text"},
        )
        sent_id = str((payload.get("data") or {}).get("message_id") or message_id)
    except Exception as exc:
        print(f"[reply-error] {exc}", file=sys.stderr, flush=True)
        return
    if config.verify_replies and not verify_text_message(sent_id, text, config):
        print("[reply-error] Feishu reply was sent but failed content verification.", file=sys.stderr, flush=True)


def reply_file_to_message(message_id: str | None, file_path: str, config: BotConfig) -> bool:
    raw_path = Path(file_path)
    path = raw_path if raw_path.is_absolute() else config.workdir / raw_path
    print(f"[reply-file] {path}", flush=True)
    if not config.reply or not message_id:
        return False
    if not path.exists() or not path.is_file():
        reply_to_message(message_id, f"XLSX发送失败：文件不存在：{path}", config)
        return False
    try:
        cli_path = relative_cli_file_path(path, config.workdir)
    except ValueError:
        reply_to_message(message_id, f"XLSX发送失败：文件不在工作目录内：{path}", config)
        return False
    result = run_command(
        lark_cli_argv()
        + [
            "im",
            "+messages-reply",
            "--message-id",
            message_id,
            "--file",
            cli_path,
            "--as",
            "bot",
        ],
        cwd=config.workdir,
        timeout_s=120,
    )
    if result.returncode != 0:
        print("[reply-file-error] " + safe_tail(result.stderr or result.stdout), file=sys.stderr, flush=True)
        reply_to_message(message_id, "XLSX发送失败：\n" + safe_tail(result.stderr or result.stdout), config)
        return False
    return True


def send_file_to_chat(chat_id: str | None, file_path: str, config: BotConfig) -> bool:
    raw_path = Path(file_path)
    path = raw_path if raw_path.is_absolute() else config.workdir / raw_path
    print(f"[send-file] {path}", flush=True)
    if not config.reply or not chat_id:
        return False
    if not path.exists() or not path.is_file():
        print(f"[send-file-error] file does not exist: {path}", file=sys.stderr, flush=True)
        return False
    try:
        cli_path = relative_cli_file_path(path, config.workdir)
    except ValueError:
        print(f"[send-file-error] file is outside workdir: {path}", file=sys.stderr, flush=True)
        return False
    result = run_command(
        lark_cli_argv()
        + [
            "im",
            "+messages-send",
            "--chat-id",
            chat_id,
            "--file",
            cli_path,
            "--as",
            "bot",
        ],
        cwd=config.workdir,
        timeout_s=120,
    )
    if result.returncode != 0:
        print("[send-file-error] " + safe_tail(result.stderr or result.stdout), file=sys.stderr, flush=True)
        return False
    return True


def help_text() -> str:
    return (
        '本地快捷：@机器人 抓取本地 <接龙主题>\n'
        '本地快捷：@机器人 72h本地 <接龙主题>\n'
        '本地快捷：@机器人 二次推送本地 <接龙主题>\n'
        "用法：@机器人 抓取 <接龙主题>\n"
        "例：@机器人 抓取 登陆哥伦比亚\n"
        "用法：@机器人 72h <接龙主题>\n"
        "例：@机器人 72h 登陆哥伦比亚\n"
        "用法：@机器人 确认推送 <Project>\n"
        "例：@机器人 确认推送 登陆哥伦比亚\n"
        "用法：@机器人 二次推送 <接龙主题>\n"
        "例：@机器人 二次推送 登陆哥伦比亚\n"
        "用法：@机器人 今日发布数\n"
        "例：@机器人 今日发布数\n"
        "用法：@机器人 海外市场账号 <YYYYMMDD-YYYYMMDD>\n"
        "例：@机器人 海外市场账号 20260605-20260609 --dry-run\n"
        "可选：--dry-run 只预览不回填；--latest-per-account N 抓每账号最新 N 条并关闭日期过滤；"
        "--platforms x,tk 只跑指定平台；"
        "--no-backfill 只导出和抓数；"
        "source=<来源群别名> 指定接龙群；base=<目标Base别名> 指定回填表；"
        "source-base=<来源Base别名> 或 --base-url <url> 可直接导出接龙来源 Base。"
    )


def dry_run_uses_command_chat_source(event_chat_id: str | None, command: ParsedCommand) -> bool:
    return (
        command.dry_run
        and bool(event_chat_id)
        and not command.source_chat
        and not command.source_base_alias
        and not command.source_base_url
        and not command.source_base_token
        and not command.base_url
        and not command.base_token
    )


def command_source_chat(event_chat_id: str | None, command: ParsedCommand, config: BotConfig) -> str | None:
    if dry_run_uses_command_chat_source(event_chat_id, command):
        return event_chat_id
    if command.source_chat:
        return resolve_ref(command.source_chat, config.source_chat_aliases, "source chat")
    if config.source_chat_id:
        return config.source_chat_id
    if config.use_command_chat_as_source:
        return event_chat_id
    return None


def resolve_source_base(command: ParsedCommand, config: BotConfig) -> tuple[str | None, str | None]:
    base_url = command.source_base_url or command.base_url
    base_token = command.source_base_token or command.base_token
    alias = command.source_base_alias

    if alias:
        if alias in config.source_base_url_aliases:
            base_url = config.source_base_url_aliases[alias]
        elif alias in config.source_base_token_aliases:
            base_token = config.source_base_token_aliases[alias]
        elif looks_like_url(alias):
            base_url = alias
        elif looks_like_base_token(alias):
            base_token = alias
        else:
            raise ValueError(f"Unknown source Base alias: {alias}")

    if base_url:
        base_url = config.source_base_url_aliases.get(base_url, base_url)
    if base_token:
        base_token = config.source_base_token_aliases.get(base_token, base_token)
    return base_url, base_token


def resolve_target_base(command: ParsedCommand, config: BotConfig) -> tuple[str | None, str | None, str | None]:
    base_url = command.target_base_url
    base_token = command.target_base_token
    table_id = command.target_table_id
    alias = command.target_base_alias or config.target_base_alias

    if alias:
        if alias in config.target_base_url_aliases:
            base_url = config.target_base_url_aliases[alias]
        elif alias in config.target_base_token_aliases:
            base_token = config.target_base_token_aliases[alias]
        elif looks_like_url(alias):
            base_url = alias
        elif looks_like_base_token(alias):
            base_token = alias
        else:
            raise ValueError(f"Unknown target Base alias: {alias}")

    if base_url:
        base_url = config.target_base_url_aliases.get(base_url, base_url)
    if base_token:
        base_token = config.target_base_token_aliases.get(base_token, base_token)
    return base_url, base_token, table_id


def cancelled_response(project: str, step: str) -> str:
    return (
        f"已停止「{project}」。\n"
        f"停止阶段：{step or '未知'}\n"
        "已终止当前后台子进程；不会继续抓取或回填。"
    )


def social_workflow_stage_label(stage: str) -> str:
    return {
        "accepted": "查找接龙",
        "export": "导出接龙",
        "base_fields": "读取素材推送表字段",
        "xlsx": "读取/写回 XLSX",
        "metrics": "抓取社媒数据并回填 XLSX",
        "metrics_72h": "刷新 72h 全平台数据",
        "backfill_72h_dry_run": "72h 回填预检",
        "backfill_72h_execute": "写入 72h 结果",
        "confirm_plan": "确认推送预检",
        "confirm_write": "写入确认推送",
        "confirm_wait": "等待通知状态",
        "confirm_verify": "复核通知状态",
        "secondary_export": "导出二次推送接龙",
        "secondary_plan": "二次推送预检",
        "secondary_write": "写入二次推送",
        "today_count_read": "读取今日发布账号",
        "today_count_plan": "今日发布数预检",
        "today_count_write": "写入今日发布账号",
        "backfill_dry_run": "回填预检",
        "backfill_execute": "写入素材推送表",
        "backfill_verify": "回填复核",
        "dry_run": "dry-run 完成",
        "complete": "完成",
    }.get(stage, stage or "社媒工作流")


def build_social_workflow_argv(
    command: ParsedCommand,
    config: BotConfig,
    *,
    event_chat_id: str | None,
    job_id: str,
) -> list[str]:
    if command.workflow_id == "weekly_market_accounts" or command.action == "weekly_market_accounts":
        project = command.project or ""
    else:
        project = command.project or "接龙"
    source_chat_id = command_source_chat(event_chat_id, command, config)
    source_base_url, source_base_token = resolve_source_base(command, config)
    target_base_url, target_base_token, target_table_id = resolve_target_base(command, config)
    workflow_id = command.workflow_id or ACTION_TO_WORKFLOW_ID.get(command.action, "full_social_scrape")
    mode = WORKFLOW_MODE_BY_ID.get(workflow_id, "full")
    argv = [
        sys.executable,
        "-X",
        "utf8",
        str(WORKFLOW_SCRIPT),
        project,
        "--workflow-id",
        workflow_id,
        "--mode",
        mode,
        "--job-id",
        job_id,
        "--workdir",
        str(config.workdir),
    ]
    if source_chat_id:
        argv.extend(["--chat-id", source_chat_id])
    if source_base_url:
        argv.extend(["--base-url", source_base_url])
    if source_base_token:
        argv.extend(["--base-token", source_base_token])
    if target_base_url:
        argv.extend(["--target-base-url", target_base_url])
    if target_base_token:
        argv.extend(["--target-base-token", target_base_token])
    if target_table_id:
        argv.extend(["--target-table-id", target_table_id])
    if command.dry_run:
        argv.append("--dry-run")
    if command.no_backfill:
        argv.append("--no-backfill")
    if command.overwrite_backfill:
        argv.append("--overwrite")
    if command.local_xlsx_only:
        argv.append('--local-xlsx-only')
    if command.start_row:
        argv.extend(["--start-row", str(command.start_row)])
    if command.limit_per_platform:
        argv.extend(["--limit-per-platform", str(command.limit_per_platform)])
    if command.latest_per_account:
        argv.extend(["--latest-per-account", str(command.latest_per_account)])
    if command.platforms:
        argv.extend(["--platforms", command.platforms])
    if command.source_indexes:
        argv.extend(["--source-indexes", command.source_indexes])
    if not config.execute_backfill_by_default:
        argv.append("--no-execute-backfill")
    return argv


def run_progress_process(
    argv: list[str],
    config: BotConfig,
    *,
    job: ActiveJob | None,
    message_id: str | None,
    job_id: str,
) -> CommandResult:
    start = time.time()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stream_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    child_env = utf8_child_env()
    process = subprocess.Popen(
        argv,
        cwd=str(config.workdir),
        env=child_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    if job:
        job.set_process(process, "社媒工作流")

    def read_stream(name: str, stream: Any) -> None:
        try:
            for line in stream:
                stream_queue.put((name, line))
        except Exception as exc:
            stream_queue.put((name, f"[stream-read-error] {exc}\n"))

    stdout_thread = threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True)
    stderr_thread = threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    saw_error_event = False
    cancelled = False
    xlsx_delivery_handled = False
    last_heartbeat_at = start
    while process.poll() is None or not stream_queue.empty():
        if job and job.cancel_event.is_set() and process.poll() is None:
            cancelled = True
            terminate_process_tree(process.pid)
        try:
            stream_name, line = stream_queue.get(timeout=0.25)
        except queue.Empty:
            if config.heartbeat_interval_s > 0:
                now = time.time()
                if now - last_heartbeat_at >= config.heartbeat_interval_s:
                    if job:
                        with job.lock:
                            step = job.current_step or "社媒工作流"
                            progress = job.last_progress_message
                        title = job.project
                    else:
                        step = "社媒工作流"
                        progress = ""
                        title = "当前工作"
                    lines = [
                        f"仍在进行：{title}",
                        f"阶段：{step}",
                    ]
                    if progress:
                        lines.append(f"进度：{progress}")
                    lines.append(f"已运行：{int(now - start)} 秒")
                    reply_to_message(
                        message_id,
                        "\n".join(lines),
                        config,
                    )
                    last_heartbeat_at = now
            continue

        if stream_name == "stderr":
            stderr_chunks.append(line)
            continue

        stdout_chunks.append(line)
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = str(event.get("event") or "")
        stage = str(event.get("stage") or "")
        message = str(event.get("message") or "").strip()
        progress_message = ""
        event_data = event.get("data")
        if isinstance(event_data, dict):
            progress_message = str(event_data.get("progress_text") or "").strip()
        if not progress_message and message and stage in {"metrics", "metrics_72h"}:
            progress_message = message.splitlines()[0].strip()
        if job and stage:
            job.set_process(
                process,
                social_workflow_stage_label(stage),
                progress_message if progress_message else None,
            )
            write_active_job_state(config, job, "running")
        if event_type == "error":
            saw_error_event = True
        if event_type in {"progress", "final", "error"} and message:
            reply_to_message(message_id, message, config)
            if (
                event_type == "final"
                and stage == "complete"
                and not xlsx_delivery_handled
                and job
                and (job.command.workflow_id == "weekly_market_accounts" or job.command.action == "weekly_market_accounts")
                and isinstance(event_data, dict)
            ):
                summary = event_data.get("summary")
                xlsx_path = str(event_data.get("xlsx_path") or "").strip()
                if not xlsx_path and isinstance(summary, dict):
                    xlsx_path = str(summary.get("local_xlsx_path") or "").strip()
                if xlsx_path:
                    if config.report_chat_id:
                        if not send_file_to_chat(config.report_chat_id, xlsx_path, config):
                            reply_to_message(message_id, "XLSX发送失败：发送群文件发送未成功。", config)
                    else:
                        reply_to_message(message_id, "XLSX发送失败：未配置发送群。", config)
                    xlsx_delivery_handled = True
            last_heartbeat_at = time.time()

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process.pid)
        process.wait(timeout=10)

    stdout_thread.join(timeout=3)
    stderr_thread.join(timeout=3)
    if job:
        job.set_process(None)
        if job.cancel_event.is_set():
            cancelled = True

    result = CommandResult(
        CANCELLED_RETURN_CODE if cancelled else (process.returncode if process.returncode is not None else 1),
        "".join(stdout_chunks),
        "".join(stderr_chunks),
        time.time() - start,
    )
    write_job_log(config, job_id, "00_social_workflow", result)

    if cancelled:
        return result
    if result.returncode != 0 and not saw_error_event:
        reply_to_message(message_id, f"社媒工作流失败：\n{safe_tail(result.stderr or result.stdout)}", config)
    return result


def run_social_workflow_task(
    command: ParsedCommand,
    config: BotConfig,
    *,
    event_chat_id: str | None = None,
    message_id: str | None = None,
    job: ActiveJob | None = None,
) -> int:
    if command.action == "help":
        reply_to_message(message_id, help_text(), config)
        return 0

    job_id = job.job_id if job else datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        argv = build_social_workflow_argv(command, config, event_chat_id=event_chat_id, job_id=job_id)
    except ValueError as exc:
        reply_to_message(message_id, f"指令路由失败：{exc}", config)
        return 2
    result = run_progress_process(argv, config, job=job, message_id=message_id, job_id=job_id)
    if result.returncode == CANCELLED_RETURN_CODE:
        reply_to_message(message_id, cancelled_response(command.project or "接龙", "社媒工作流"), config)
    return result.returncode


def active_job_summary(job: ActiveJob) -> str:
    elapsed = int(time.time() - job.started_at)
    with job.lock:
        step = job.current_step or "排队/准备"
    return f"当前工作：{job.project}\n阶段：{step}\n已运行：{elapsed} 秒"


def stop_active_job(runtime: BotRuntime, message_id: str | None) -> None:
    with runtime.lock:
        job = runtime.active_job
    if not job or not job.thread or not job.thread.is_alive():
        reply_to_message(message_id, "当前无工作。", runtime.config)
        return

    with job.lock:
        step = job.current_step or "准备中"
        process = job.current_process
    job.cancel_event.set()
    if process and process.poll() is None:
        terminate_process_tree(process.pid)
    write_active_job_state(runtime.config, job, "stopping")
    reply_to_message(
        message_id,
        f"收到停止指令，正在停止上一次工作。\n项目：{job.project}\n当前阶段：{step}",
        runtime.config,
    )


def clear_active_job(runtime: BotRuntime, job: ActiveJob) -> None:
    with runtime.lock:
        if runtime.active_job is job:
            runtime.active_job = None
            finish_active_job_state(runtime.config, job, "cancelled" if job.cancel_event.is_set() else "finished")


def start_job(runtime: BotRuntime, command: ParsedCommand, *, event_chat_id: str | None, message_id: str | None) -> None:
    if command.action == "help":
        reply_to_message(message_id, help_text(), runtime.config)
        return
    if command.action == "status":
        with runtime.lock:
            job = runtime.active_job
        if job and job.thread and job.thread.is_alive():
            reply_to_message(message_id, active_job_summary(job), runtime.config)
        else:
            reply_to_message(message_id, "当前无工作。", runtime.config)
        return
    if command.action == "stop":
        stop_active_job(runtime, message_id)
        return

    job = ActiveJob(
        job_id=datetime.now().strftime("%Y%m%d-%H%M%S"),
        command=command,
        event_chat_id=event_chat_id,
        message_id=message_id,
    )

    def worker() -> None:
        try:
            run_social_workflow_task(command, runtime.config, event_chat_id=event_chat_id, message_id=message_id, job=job)
        finally:
            clear_active_job(runtime, job)

    job.thread = threading.Thread(target=worker, daemon=True, name=f"jielong-job-{job.job_id}")
    with runtime.lock:
        current = runtime.active_job
        if current and current.thread and current.thread.is_alive():
            reply_to_message(
                message_id,
                f"当前已有工作在运行。\n{active_job_summary(current)}\n如需终止，请发送：@机器人 停止工作",
                runtime.config,
            )
            return
        runtime.active_job = job
        write_active_job_state(runtime.config, job, "queued")
    job.thread.start()


def start_chat(runtime: BotRuntime, event: dict[str, Any]) -> None:
    message_id = str(event.get("message_id") or event.get("id") or "")
    chat_id = str(event.get("chat_id") or "")
    sender_id = str(event.get("sender_id") or "unknown")
    prompt = strip_mentions(str(event.get("content") or "")).strip()
    if not prompt:
        return
    conversation_key = f"{chat_id}:{sender_id}"

    if prompt in {"清空聊天", "清空对话", "忘记对话", "重新聊天"}:
        winston_chat.clear_history(conversation_key)
        reply_to_message(message_id, "已清空你在本群与温斯顿的聊天记录。", runtime.config)
        return

    def worker() -> None:
        try:
            answer = winston_chat.chat(conversation_key, prompt)
            reply_to_message(message_id, answer, runtime.config)
        except winston_chat.ChatError as exc:
            reply_to_message(message_id, f"聊天失败：{exc}", runtime.config)
        except Exception as exc:
            print(f"[chat-error] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            reply_to_message(message_id, "聊天失败：发生内部错误，请稍后重试。", runtime.config)
        finally:
            with runtime.lock:
                runtime.chat_threads.pop(conversation_key, None)

    with runtime.lock:
        current = runtime.chat_threads.get(conversation_key)
        if current and current.is_alive():
            reply_to_message(message_id, "我正在回复你上一条消息，请稍等。", runtime.config)
            return
        thread = threading.Thread(target=worker, daemon=True, name=f"winston-chat-{sender_id}")
        runtime.chat_threads[conversation_key] = thread
    thread.start()


def event_allowed(event: dict[str, Any], config: BotConfig) -> bool:
    chat_id = str(event.get("chat_id") or "")
    chat_type = str(event.get("chat_type") or "").lower()
    is_group = chat_type == "group"
    is_p2p = chat_type in {"p2p", "private", "direct"}
    if not is_group and not is_p2p:
        return False
    if is_group and (
        not config.allow_any_chat
        and chat_id not in config.allowed_chat_ids
        and chat_id not in config.chat_only_chat_ids
    ):
        return False
    if is_p2p and not config.allow_any_chat and chat_id not in config.allowed_p2p_chat_ids:
        return False
    if is_group and config.require_at and not event_mentions_bot(event, config):
        return False
    return True


def command_allowed_in_chat(chat_id: str, config: BotConfig) -> bool:
    return chat_id not in config.chat_only_chat_ids


def consume_events(config: BotConfig, timeout: str | None = None, max_events: int | None = None) -> int:
    if (
        not config.allowed_chat_ids
        and not config.chat_only_chat_ids
        and not config.allowed_p2p_chat_ids
        and not config.allow_any_chat
    ):
        raise SystemExit(
            "No allowed command chat configured. Set JIELONG_BOT_ALLOWED_CHAT_IDS, pass --allowed-chat oc_xxx, "
            "or pass --allow-any-chat explicitly."
        )

    argv = lark_cli_argv() + ["event", "consume", "im.message.receive_v1", "--as", "bot"]
    if timeout:
        argv.extend(["--timeout", timeout])
    if max_events:
        argv.extend(["--max-events", str(max_events)])

    proc = subprocess.Popen(
        argv,
        cwd=str(config.workdir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    stderr_queue: queue.Queue[str] = queue.Queue()
    ready = threading.Event()

    def stderr_reader() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_queue.put(line)
            print(line.rstrip(), file=sys.stderr, flush=True)
            if "[event] ready event_key=im.message.receive_v1" in line:
                ready.set()

    thread = threading.Thread(target=stderr_reader, daemon=True)
    thread.start()
    if not ready.wait(timeout=45):
        proc.terminate()
        raise SystemExit("event consumer did not become ready within 45 seconds")

    state = load_state(config.state_path)
    processed: list[str] = state.setdefault("processed_event_ids", [])
    state["active_job"] = None
    state["active_job_updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(config.state_path, state)
    runtime = BotRuntime(config=config)

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print("[event-parse-error] " + line[:500], file=sys.stderr, flush=True)
                continue

            event_id = str(event.get("event_id") or event.get("message_id") or "")
            if event_id and event_id in processed:
                continue
            if not event_allowed(event, config):
                continue

            command = parse_command(str(event.get("content") or ""))
            if not command:
                if winston_chat.is_enabled():
                    if event_id:
                        processed.append(event_id)
                        save_state(config.state_path, state)
                    start_chat(runtime, event)
                continue
            if not command_allowed_in_chat(str(event.get("chat_id") or ""), config):
                if event_id:
                    processed.append(event_id)
                    save_state(config.state_path, state)
                continue
            if event_id:
                processed.append(event_id)
                save_state(config.state_path, state)

            start_job(
                runtime,
                command,
                event_chat_id=str(event.get("chat_id") or ""),
                message_id=str(event.get("message_id") or ""),
            )
    except KeyboardInterrupt:
        print("Stopping listener...", file=sys.stderr, flush=True)
    finally:
        with runtime.lock:
            job = runtime.active_job
        if job and job.thread and job.thread.is_alive():
            job.cancel_event.set()
            with job.lock:
                process = job.current_process
            if process and process.poll() is None:
                terminate_process_tree(process.pid)
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
    return proc.returncode or 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Listen for Feishu @bot jielong social metric commands.")
    parser.add_argument("--env-file", type=Path, default=BOT_ROOT / ".env")
    subparsers = parser.add_subparsers(dest="command")

    listen = subparsers.add_parser("listen", help="Listen to Feishu message events and run commands.")
    listen.add_argument("--allowed-chat", action="append", default=[], help="Allowed command chat ID, repeatable.")
    listen.add_argument("--allow-any-chat", action="store_true", help="Process commands from any group chat.")
    listen.add_argument("--source-chat-id", help="Always export jielong from this chat ID instead of command chat.")
    listen.add_argument("--report-chat-id", help="Chat ID or alias for generated report file delivery.")
    listen.add_argument("--command-chat-alias", action="append", default=[], help="Command chat alias, format name=oc_xxx.")
    listen.add_argument("--source-chat-alias", action="append", default=[], help="Source chat alias, format name=oc_xxx.")
    listen.add_argument("--chat-alias", action="append", default=[], help="Legacy source chat alias, format name=oc_xxx.")
    listen.add_argument("--source-base-url-alias", action="append", default=[], help="Source Base URL alias, format name=https://...")
    listen.add_argument("--source-base-token-alias", action="append", default=[], help="Source Base token alias, format name=bascn...")
    listen.add_argument("--target-base", help="Default target Base alias.")
    listen.add_argument("--target-base-url-alias", action="append", default=[], help="Target Base URL alias, format name=https://...")
    listen.add_argument("--target-base-token-alias", action="append", default=[], help="Target Base token alias, format name=bascn...")
    listen.add_argument("--base-url-alias", action="append", default=[], help="Legacy target Base URL alias, format name=https://...")
    listen.add_argument("--base-token-alias", action="append", default=[], help="Legacy target Base token alias, format name=bascn...")
    listen.add_argument("--heartbeat-seconds", type=int, help="Send progress heartbeat after N silent seconds; 0 disables.")
    listen.add_argument("--require-at", action="store_true", help="Require an @ mention marker in the message content.")
    listen.add_argument("--timeout", help="Pass through to lark-cli event consume, for example 10m.")
    listen.add_argument("--max-events", type=int, help="Stop after N received events.")
    listen.add_argument("--no-reply", action="store_true", help="Do not reply in Feishu; print locally only.")
    listen.add_argument("--no-verify-reply", action="store_true", help="Skip reading sent replies back for content verification.")

    run = subparsers.add_parser("run", help="Run one workflow directly without listening to Feishu events.")
    run.add_argument("project")
    run.add_argument("--workflow-id", choices=sorted(WORKFLOW_MODE_BY_ID), help="Registered workflow id.")
    run.add_argument(
        "--mode",
        choices=["full", "72h", "confirm-push", "secondary-push", "today-publish-count", "weekly-market-accounts"],
        default="full",
    )
    run.add_argument("--chat-id", dest="source_chat")
    run.add_argument("--source", dest="source_chat")
    run.add_argument("--report-chat-id")
    run.add_argument("--command-chat-alias", action="append", default=[], help="Command chat alias, format name=oc_xxx.")
    run.add_argument("--source-chat-alias", action="append", default=[], help="Source chat alias, format name=oc_xxx.")
    run.add_argument("--chat-alias", action="append", default=[], help="Legacy source chat alias, format name=oc_xxx.")
    run.add_argument("--source-base")
    run.add_argument("--source-base-url")
    run.add_argument("--source-base-token")
    run.add_argument("--source-base-url-alias", action="append", default=[], help="Source Base URL alias, format name=https://...")
    run.add_argument("--source-base-token-alias", action="append", default=[], help="Source Base token alias, format name=bascn...")
    run.add_argument("--base", dest="target_base")
    run.add_argument("--target-base")
    run.add_argument("--target-base-url")
    run.add_argument("--target-base-token")
    run.add_argument("--target-table-id")
    run.add_argument("--target-base-url-alias", action="append", default=[], help="Target Base URL alias, format name=https://...")
    run.add_argument("--target-base-token-alias", action="append", default=[], help="Target Base token alias, format name=bascn...")
    run.add_argument("--base-url-alias", action="append", default=[], help="Legacy target Base URL alias, format name=https://...")
    run.add_argument("--base-token-alias", action="append", default=[], help="Legacy target Base token alias, format name=bascn...")
    run.add_argument("--base-url")
    run.add_argument("--base-token")
    run.add_argument("--heartbeat-seconds", type=int, help="Send progress heartbeat after N silent seconds; 0 disables.")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-backfill", action="store_true")
    run.add_argument("--overwrite", action="store_true")
    run.add_argument('--local-xlsx-only', action='store_true')
    run.add_argument("--start-row", type=int)
    run.add_argument("--limit-per-platform", type=int)
    run.add_argument("--latest-per-account", type=int)
    run.add_argument("--platforms")
    run.add_argument("--source-indexes")
    run.add_argument("--no-reply", action="store_true")
    run.add_argument("--no-verify-reply", action="store_true", help="Skip reading sent replies back for content verification.")

    parse = subparsers.add_parser("parse", help="Parse a sample command and print JSON.")
    parse.add_argument("text")

    route_smoke = subparsers.add_parser("route-smoke", help=argparse.SUPPRESS)
    report_smoke = subparsers.add_parser("report-smoke", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env_file(args.env_file)
    load_env_file(BOT_ROOT / "chat.env")

    if args.command == "parse":
        parsed = parse_command(args.text)
        print(json.dumps(parsed.__dict__ if parsed else None, ensure_ascii=False, indent=2))
        return 0

    if args.command == "route-smoke":
        config = BotConfig(
            source_chat_id="oc_default_source",
            workdir=PROJECT_ROOT,
            reply=False,
            verify_replies=False,
        )
        dry_command = ParsedCommand(
            action="secondary_push",
            workflow_id="secondary_push",
            project="test",
            dry_run=True,
        )
        live_command = ParsedCommand(
            action="secondary_push",
            workflow_id="secondary_push",
            project="test",
            dry_run=False,
        )
        local_command = ParsedCommand(
            action='secondary_push',
            workflow_id='secondary_push',
            project='test',
            local_xlsx_only=True,
        )
        print(
            json.dumps(
                {
                    "dry_run_argv": build_social_workflow_argv(
                        dry_command,
                        config,
                        event_chat_id="oc_command_chat",
                        job_id="route-smoke-dry",
                    ),
                    "live_argv": build_social_workflow_argv(
                        live_command,
                        config,
                        event_chat_id="oc_command_chat",
                        job_id="route-smoke-live",
                    ),
                    'local_argv': build_social_workflow_argv(
                        local_command,
                        config,
                        event_chat_id='oc_command_chat',
                        job_id='route-smoke-local',
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "report-smoke":
        previous_default_report_chat = os.environ.get("JIELONG_BOT_DEFAULT_REPORT_CHAT")
        previous_report_aliases = os.environ.get("JIELONG_BOT_REPORT_CHAT_ALIASES")
        os.environ["JIELONG_BOT_DEFAULT_REPORT_CHAT"] = "report=oc_report"
        os.environ.pop("JIELONG_BOT_REPORT_CHAT_ALIASES", None)
        try:
            args.allowed_chat = []
            args.allow_any_chat = False
            args.source_chat_id = None
            args.report_chat_id = None
            args.command_chat_alias = []
            args.source_chat_alias = []
            args.chat_alias = []
            args.source_base_url_alias = []
            args.source_base_token_alias = []
            args.target_base = None
            args.base_alias = None
            args.target_base_url_alias = []
            args.base_url_alias = []
            args.target_base_token_alias = []
            args.base_token_alias = []
            args.no_reply = False
            args.no_verify_reply = True
            args.require_at = False
            args.heartbeat_seconds = 0
            config = build_config(args)
        finally:
            if previous_default_report_chat is None:
                os.environ.pop("JIELONG_BOT_DEFAULT_REPORT_CHAT", None)
            else:
                os.environ["JIELONG_BOT_DEFAULT_REPORT_CHAT"] = previous_default_report_chat
            if previous_report_aliases is None:
                os.environ.pop("JIELONG_BOT_REPORT_CHAT_ALIASES", None)
            else:
                os.environ["JIELONG_BOT_REPORT_CHAT_ALIASES"] = previous_report_aliases

        sent: list[dict[str, str]] = []
        replies: list[str] = []
        original_workflow_script = globals()["WORKFLOW_SCRIPT"]
        original_send_file = globals()["send_file_to_chat"]
        original_reply_file = globals()["reply_file_to_message"]
        original_reply = globals()["reply_to_message"]
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            workflow = temp_root / "fake_weekly_workflow.py"
            xlsx = temp_root / "exports" / "weekly.xlsx"
            xlsx.parent.mkdir(parents=True, exist_ok=True)
            xlsx.write_bytes(b"fake xlsx")
            payload = {
                "event": "final",
                "stage": "complete",
                "message": "done",
                "data": {"xlsx_path": str(xlsx)},
            }
            workflow.write_text(
                "import json\n"
                f"print({json.dumps(json.dumps(payload, ensure_ascii=False))}, flush=True)\n",
                encoding="utf-8",
            )

            def fake_send_file(chat_id: str | None, file_path: str, config: BotConfig) -> bool:
                sent.append({"chat_id": chat_id or "", "file_path": file_path})
                return True

            def fail_reply_file(message_id: str | None, file_path: str, config: BotConfig) -> bool:
                raise AssertionError("weekly report xlsx must not use message reply delivery")

            def fake_reply(message_id: str | None, text: str, config: BotConfig) -> None:
                replies.append(text)

            globals()["WORKFLOW_SCRIPT"] = workflow
            globals()["send_file_to_chat"] = fake_send_file
            globals()["reply_file_to_message"] = fail_reply_file
            globals()["reply_to_message"] = fake_reply
            try:
                config.workdir = temp_root
                config.state_path = temp_root / "feishu_bot_state.json"
                config.log_dir = temp_root / "feishu_bot_logs"
                command = ParsedCommand(
                    action="weekly_market_accounts",
                    workflow_id="weekly_market_accounts",
                    project="20260605-20260609",
                )
                job = ActiveJob(
                    job_id="report-smoke",
                    command=command,
                    event_chat_id="oc_command",
                    message_id="om_command",
                )
                returncode = run_social_workflow_task(command, config, event_chat_id="oc_command", message_id="om_command", job=job)
            finally:
                globals()["WORKFLOW_SCRIPT"] = original_workflow_script
                globals()["send_file_to_chat"] = original_send_file
                globals()["reply_file_to_message"] = original_reply_file
                globals()["reply_to_message"] = original_reply
        if returncode != 0:
            raise SystemExit("report smoke workflow did not complete")
        if sent != [{"chat_id": "oc_report", "file_path": str(xlsx)}]:
            raise SystemExit(f"unexpected report delivery calls: {sent!r}")
        if replies != ["done"]:
            raise SystemExit(f"unexpected command-group replies: {replies!r}")
        print(json.dumps({"ok": True, "sent_to_report_chat": True}, ensure_ascii=False))
        return 0

    if args.command == "run":
        config = build_config(args)
        workflow_id = args.workflow_id or {
            "72h": "refresh_72h",
            "confirm-push": "confirm_push",
            "secondary-push": "secondary_push",
            "today-publish-count": "today_publish_count",
            "weekly-market-accounts": "weekly_market_accounts",
        }.get(args.mode, "full_social_scrape")
        if args.workflow_id and args.mode == "full":
            args.mode = WORKFLOW_MODE_BY_ID.get(workflow_id, args.mode)
        action = "run"
        if workflow_id == "refresh_72h":
            action = "refresh_72h"
        elif workflow_id == "confirm_push":
            action = "confirm_push"
        elif workflow_id == "secondary_push":
            action = "secondary_push"
        elif workflow_id == "today_publish_count":
            action = "today_publish_count"
        elif workflow_id == "weekly_market_accounts":
            action = "weekly_market_accounts"
        command = ParsedCommand(
            action=action,
            workflow_id=workflow_id,
            project=args.project,
            local_xlsx_only=args.local_xlsx_only,
            dry_run=args.dry_run,
            no_backfill=args.no_backfill,
            overwrite_backfill=args.overwrite,
            source_chat=args.source_chat,
            source_base_alias=args.source_base,
            source_base_url=args.source_base_url,
            source_base_token=args.source_base_token,
            target_base_alias=args.target_base,
            target_base_url=args.target_base_url,
            target_base_token=args.target_base_token,
            target_table_id=args.target_table_id,
            base_url=args.base_url,
            base_token=args.base_token,
            start_row=args.start_row,
            limit_per_platform=args.limit_per_platform,
            latest_per_account=args.latest_per_account,
            platforms=args.platforms,
            source_indexes=args.source_indexes,
            raw="manual run",
        )
        return run_social_workflow_task(command, config, event_chat_id=args.source_chat, message_id=None)

    if args.command is None:
        args.command = "listen"
        args.allowed_chat = []
        args.allow_any_chat = False
        args.source_chat_id = None
        args.require_at = False
        args.timeout = None
        args.max_events = None
        args.no_reply = False

    config = build_config(args)
    return consume_events(config, timeout=args.timeout, max_events=args.max_events)


if __name__ == "__main__":
    raise SystemExit(main())
