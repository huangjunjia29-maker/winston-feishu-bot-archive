#!/usr/bin/env python3
"""Integrated jielong social-metrics workflow.

This script is the single business task boundary for the bot listener. It owns
jielong export, public social scraping, XLSX preservation, and Base backfill
through scripts and CDP assets vendored inside this skill.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
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


def utf8_child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def normalize_subprocess_argv(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    executable = Path(argv[0]).name.lower()
    if executable.startswith("python") and "-X" not in argv[1:3]:
        return [argv[0], "-X", "utf8", *argv[1:]]
    return argv


configure_utf8_runtime()

SKILL_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_ENV_FILE = SKILL_ROOT / "env" / "global.env"
CDP_ENV_FILE = SKILL_ROOT / "env" / "cdp.env"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


for _env_file in (GLOBAL_ENV_FILE, CDP_ENV_FILE):
    load_env_file(_env_file)


DEFAULT_LOG_DIR = SKILL_ROOT / "tmp" / "feishu_workflow_logs"
DEFAULT_WORKDIR = Path(os.environ.get("JIELONG_SOCIAL_WORKDIR", str(SKILL_ROOT.parent)))

JIELONG_SCRIPT = SKILL_ROOT / "modules" / "export_jielong.py"
METRICS_SCRIPT = SKILL_ROOT / "modules" / "social_metrics.py"
BACKFILL_SCRIPT = SKILL_ROOT / "modules" / "base_backfill.py"
CONFIRM_PUSH_SCRIPT = SKILL_ROOT / "modules" / "confirm_push.py"
SECONDARY_PUSH_SCRIPT = SKILL_ROOT / "modules" / "secondary_push.py"
WEEKLY_ACCOUNT_POSTS_SCRIPT = SKILL_ROOT / "modules" / "weekly_account_posts.py"
CDP_CACHE_CLEANUP_SCRIPT = SKILL_ROOT / "modules" / "cdp_cache_cleanup.py"
BACKFILL_SKILL_ROOT = BACKFILL_SCRIPT.parents[1]
JIELONG_ENV_FILE = SKILL_ROOT / "env" / "chats.env"
BACKFILL_ENV_FILE = SKILL_ROOT / "env" / "bases.env"

WINDOWS_FILENAME_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.I)
TRAILING_URL_CHARS = ".,;:!?)]}" + "\uff0c\u3002\uff1b\uff1a\uff01\uff1f\uff09\u3011"
X_STATUS_RE = re.compile(r"https?://(?:www\.)?x\.com/((?:i/status|[^/\s\"'<>\\]+/status)/(\d+))", re.I)
REFRESH_72H_PLATFORMS = {
    "INS": {
        "label": "Instagram",
        "metric_platform": "instagram",
        "link": "INS链接",
        "views": "INS播放(72h)",
        "original_views": "INS播放",
        "likes": "INS点赞",
        "comments": "INS评论",
        "saves": "INS收藏",
        "shares": "INS转发",
    },
    "X": {
        "label": "X",
        "metric_platform": "x",
        "link": "X链接",
        "views": "X浏览(72h)",
        "original_views": "X浏览",
        "likes": "X喜欢",
        "comments": "X评论",
        "saves": "X收藏",
        "shares": "X转发",
    },
    "TK": {
        "label": "TikTok",
        "metric_platform": "tiktok",
        "link": "TK链接",
        "views": "TK播放(72h)",
        "original_views": "TK播放",
        "likes": "TK点赞",
        "comments": "TK评论",
        "saves": "TK收藏",
        "shares": "TK转发",
    },
    "FB": {
        "label": "Facebook",
        "metric_platform": "facebook",
        "link": "FB链接",
        "views": "FB曝光(72h)",
        "original_views": "FB曝光",
        "likes": "FB点赞",
        "comments": "FB评论",
    },
}
REFRESH_72H_RESULT_METRICS = ("likes", "comments", "shares", "saves", "views")
REFRESH_72H_GENERIC_XLSX_HEADERS = {
    "likes": "点赞/反应",
    "comments": "评论",
    "shares": "分享/转发",
    "saves": "收藏",
}
TODAY_PUBLISH_DEFAULT_BASE_TOKEN = ""
TODAY_PUBLISH_SOURCE_TABLE_ID = "账号活跃度"
TODAY_PUBLISH_TARGET_TABLE_ID = "素材推送表"
TODAY_PUBLISH_SOURCE_ACCOUNT_FIELD_ID = "账号编号"
TODAY_PUBLISH_TARGET_ACCOUNT_FIELD_ID = "账号编号"
TODAY_PUBLISH_TARGET_AUTO_FIELD_ID = "自动素材编号"
TODAY_PUBLISH_TARGET_DATE_FIELD_ID = "Date"
TODAY_PUBLISH_VIEWS = (
    ("回头草", "回头草"),
    ("地区代表", "地区代表"),
    ("新手村", "新手村"),
    ("30日活跃度", "30日活跃度"),
)
TODAY_PUBLISH_ROW_CREATE_BATCH_SIZE = 100
TODAY_PUBLISH_ROW_CREATE_RETRY_LIMIT = 8
TODAY_PUBLISH_ROW_CREATE_SETTLE_SECONDS = 0.8


@dataclass(frozen=True)
class WorkflowSpec:
    feature_id: str
    workflow_id: str
    name: str
    mode: str
    required_env: tuple[str, ...]
    description: str


FUNCTION_DIRECTORY = {
    "F001": "export_jielong_xlsx",
    "F002": "scrape_social_metrics_to_xlsx",
    "F003": "backfill_xlsx_to_base",
    "F004": "full_social_scrape",
    "F005": "refresh_72h",
    "F006": "confirm_push",
    "F007": "secondary_push",
    "F008": "today_publish_count",
    "F009": "weekly_market_accounts",
    "F010": "cdp_cache_cleanup",
}
SOCIAL_CDP_CLEANUP_WORKFLOWS = {"full_social_scrape", "refresh_72h", "weekly_market_accounts"}
WORKFLOW_REGISTRY: dict[str, WorkflowSpec] = {
    "full_social_scrape": WorkflowSpec(
        feature_id="F004",
        workflow_id="full_social_scrape",
        name="抓取",
        mode="full",
        required_env=("global.env", "chats.env", "bases.env", "cdp.env"),
        description="F001 -> F002 -> F003：导出接龙，抓取社媒数据，回填素材推送表。",
    ),
    "refresh_72h": WorkflowSpec(
        feature_id="F005",
        workflow_id="refresh_72h",
        name="72h",
        mode="72h",
        required_env=("global.env", "chats.env", "bases.env", "cdp.env"),
        description="本地 XLSX 优先；缺失则导出接龙；重新抓取全平台链接并写入 72h 播放/曝光与互动数据。",
    ),
    "confirm_push": WorkflowSpec(
        feature_id="F006",
        workflow_id="confirm_push",
        name="确认推送",
        mode="confirm-push",
        required_env=("global.env", "bases.env"),
        description="按 Project 筛选 Owner 有值且推送通知为空的记录，写入确认推送并复核通知状态。",
    ),
    "secondary_push": WorkflowSpec(
        feature_id="F007",
        workflow_id="secondary_push",
        name="二次推送",
        mode="secondary-push",
        required_env=("global.env", "chats.env", "bases.env"),
        description="导出接龙 XLSX，按 Project+Owner 回填社媒链接，再标记发布状态仍为空的二次推送记录。",
    ),
    "today_publish_count": WorkflowSpec(
        feature_id="F008",
        workflow_id="today_publish_count",
        name="今日发布数",
        mode="today-publish-count",
        required_env=("global.env", "bases.env"),
        description="读取账号活跃度三张视图，统计账号编号并写入素材推送表。",
    ),
    "weekly_market_accounts": WorkflowSpec(
        feature_id="F009",
        workflow_id="weekly_market_accounts",
        name="海外市场账号",
        mode="weekly-market-accounts",
        required_env=("global.env", "cdp.env"),
        description="读取海外账号表，按指令日期范围抓取账号发布内容，并在同一 Base 创建结果表记录。",
    ),
    "cdp_cache_cleanup": WorkflowSpec(
        feature_id="F010",
        workflow_id="cdp_cache_cleanup",
        name="CDP cache cleanup",
        mode="cdp-cache-cleanup",
        required_env=("cdp.env",),
        description="Internal silent cleanup of Chrome CDP cache artifacts. Preserves login/session data.",
    ),
}
MODE_TO_WORKFLOW_ID = {
    spec.mode: workflow_id
    for workflow_id, spec in WORKFLOW_REGISTRY.items()
}


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float


@dataclass
class MetricsSummary:
    attempted_links: int | None = None
    excel_write_rows: int = 0
    manual_review_count: int = 0
    workbook_link_rows: int | None = None
    workbook_platform_rows: int | None = None
    workbook_total_urls: int | None = None

    @property
    def successful_links(self) -> int | None:
        if self.attempted_links is None:
            return None
        return max(0, self.attempted_links - self.manual_review_count)


@dataclass
class Refresh72hRow:
    row_num: int
    auto_id: str
    owner: str
    platform: str
    url: str
    result: dict[str, Any] | None = None


@dataclass
class Refresh72hSummary:
    attempted_links: int
    successful_links: int
    missed_links: int
    executed_patches: int = 0


@dataclass
class BackfillContext:
    module: Any
    skill_root: Path
    identity: str
    base_token: str
    table_id: str
    project_field_name: str
    owner_field_name: str
    xlsx_link_header: str
    xlsx_owner_header: str
    worksheet: str
    fields_by_name: dict[str, Any]
    target_fields: dict[str, Any]
    auto_field: Any | None


@dataclass
class XlsxContext:
    module: Any
    xlsx_link_header: str
    xlsx_owner_header: str
    worksheet: str


def emit(event: str, stage: str, message: str, **data: Any) -> None:
    payload = {"event": event, "stage": stage, "message": message, "data": data}
    print(json.dumps(payload, ensure_ascii=False), flush=True)


SCRAPE_PROGRESS_RE = re.compile(r"^\[(\d+)/(\d+)\]\s+(https?://\S+)", re.I)
EXCEL_WRITE_PROGRESS_RE = re.compile(r"^\[excel-write\s+(\d+)/(\d+)\]", re.I)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def round_half_up(value: float) -> int:
    return int(value + 0.5)


def progress_milestones(total: int) -> set[int]:
    if total <= 1:
        return set()
    return {
        point
        for point in (
            round_half_up(total / 3),
            round_half_up(total * 2 / 3),
        )
        if 0 < point < total
    }


def social_url_host(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def make_social_metrics_progress_callback(stage: str, label: str) -> Any:
    state: dict[str, tuple[int, int] | None] = {"scrape": None, "excel": None}

    def callback(line: str) -> None:
        text = line.strip()
        scrape_match = SCRAPE_PROGRESS_RE.match(text)
        if scrape_match:
            current = int(scrape_match.group(1))
            total = int(scrape_match.group(2))
            if state.get("scrape") == (current, total):
                return
            state["scrape"] = (current, total)
            if current in progress_milestones(total):
                url = scrape_match.group(3)
                host = social_url_host(url)
                progress_text = f"{label}已尝试 {current}/{total} 条"
                host_line = f"\n当前域名：{host}" if host else ""
                emit(
                    "progress",
                    stage,
                    f"{label}已尝试：{current}/{total} 条。{host_line}",
                    current=current,
                    total=total,
                    current_url=url,
                    progress_text=progress_text,
                )
            return

        excel_match = EXCEL_WRITE_PROGRESS_RE.match(text)
        if excel_match:
            current = int(excel_match.group(1))
            total = int(excel_match.group(2))
            if state.get("excel") == (current, total):
                return
            state["excel"] = (current, total)

    return callback


def run_command(argv: list[str], cwd: Path, timeout_s: int | None = None) -> CommandResult:
    return run_command_streamed(argv, cwd, timeout_s=timeout_s)


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


def run_command_streamed(
    argv: list[str],
    cwd: Path,
    timeout_s: int | None = None,
    *,
    idle_timeout_s: int | None = None,
    on_stdout_line: Any | None = None,
    on_stderr_line: Any | None = None,
) -> CommandResult:
    start = time.time()
    last_output_at = start
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stream_queue: queue.Queue[tuple[str, str]] = queue.Queue()
    child_env = utf8_child_env()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        normalize_subprocess_argv(argv),
        cwd=str(cwd),
        env=child_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )

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

    deadline = start + timeout_s if timeout_s else None
    timed_out = False
    timeout_reason = ""
    while process.poll() is None or not stream_queue.empty():
        if deadline and time.time() > deadline and process.poll() is None:
            timed_out = True
            timeout_reason = f"timeout_s={timeout_s}"
            terminate_process_tree(process.pid)
        if (
            idle_timeout_s
            and process.poll() is None
            and time.time() - last_output_at > idle_timeout_s
        ):
            timed_out = True
            timeout_reason = f"idle_timeout_s={idle_timeout_s}"
            terminate_process_tree(process.pid)
        try:
            stream_name, line = stream_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        last_output_at = time.time()

        if stream_name == "stdout":
            stdout_chunks.append(line)
            if on_stdout_line:
                try:
                    on_stdout_line(line)
                except Exception as exc:
                    stderr_chunks.append(f"[stdout-callback-error] {exc}\n")
        else:
            stderr_chunks.append(line)
            if on_stderr_line:
                try:
                    on_stderr_line(line)
                except Exception as exc:
                    stderr_chunks.append(f"[stderr-callback-error] {exc}\n")

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        terminate_process_tree(process.pid)
        process.wait(timeout=10)

    stdout_thread.join(timeout=3)
    stderr_thread.join(timeout=3)
    while not stream_queue.empty():
        stream_name, line = stream_queue.get()
        if stream_name == "stdout":
            stdout_chunks.append(line)
            if on_stdout_line:
                try:
                    on_stdout_line(line)
                except Exception as exc:
                    stderr_chunks.append(f"[stdout-callback-error] {exc}\n")
        else:
            stderr_chunks.append(line)
            if on_stderr_line:
                try:
                    on_stderr_line(line)
                except Exception as exc:
                    stderr_chunks.append(f"[stderr-callback-error] {exc}\n")
    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    if timed_out:
        stderr += f"\n[timeout] {timeout_reason or 'timeout'}\n"
        return CommandResult(124, stdout, stderr, time.time() - start)
    return CommandResult(
        process.returncode if process.returncode is not None else 1,
        stdout,
        stderr,
        time.time() - start,
    )


def write_job_log(log_dir: Path, job_id: str, name: str, result: CommandResult) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{job_id}_{name}.log"
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


def write_text_log(log_dir: Path, job_id: str, name: str, text: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{job_id}_{name}.log"
    path.write_text(text, encoding="utf-8")
    return path


def run_cdp_cache_cleanup_step(
    log_dir: Path,
    job_id: str,
    *,
    silent: bool,
    dry_run: bool = False,
) -> bool:
    log_path: Path | None = None
    try:
        cleanup = import_cdp_cache_cleanup_module()
        report = cleanup.clean_cdp_profiles(dry_run=dry_run)
        text = cleanup.format_cleanup_report(report)
        log_path = write_text_log(log_dir, job_id, "99_cdp_cache_cleanup", text)
    except Exception as exc:
        text = f"F010 CDP cache cleanup failed: {exc}\n"
        try:
            log_path = write_text_log(log_dir, job_id, "99_cdp_cache_cleanup", text)
        except Exception:
            log_path = None
        if not silent:
            emit("error", "cdp_cache_cleanup", f"F010 CDP 缓存清理失败：{exc}")
        return False

    if not silent:
        action = "预检" if dry_run else "清理"
        file_label = "可清理文件" if dry_run else "清理文件"
        dir_label = "可清理目录" if dry_run else "清理目录"
        bytes_label = "可释放空间" if dry_run else "释放空间"
        emit(
            "final",
            "dry_run" if dry_run else "complete",
            f"F010 CDP 缓存{action}完成。\n"
            f"{file_label}：{report.removed_files}\n"
            f"{dir_label}：{report.removed_dirs}\n"
            f"{bytes_label}：{cleanup.human_bytes(report.removed_bytes)}\n"
            f"跳过文件：{report.skipped_files}\n"
            f"跳过目录：{report.skipped_dirs}\n"
            f"错误数：{report.error_count}"
            + (f"\n日志：{log_path}" if log_path else ""),
            removed_files=report.removed_files,
            removed_dirs=report.removed_dirs,
            removed_bytes=report.removed_bytes,
            skipped_files=report.skipped_files,
            skipped_dirs=report.skipped_dirs,
            errors=report.error_count,
            log_path=str(log_path) if log_path else "",
            dry_run=dry_run,
        )
    return True


def run_cdp_cache_cleanup_workflow(job_id: str, log_dir: Path, *, dry_run: bool = False) -> int:
    return 0 if run_cdp_cache_cleanup_step(log_dir, job_id, silent=False, dry_run=dry_run) else 1

def configured_social_cdp_profiles() -> list[Path]:
    try:
        cleanup = import_cdp_cache_cleanup_module()
        return [Path(profile) for profile in cleanup.configured_cdp_profiles()]
    except Exception:
        candidates: list[Path] = []
        for env_name in ("SOCIAL_CDP_PROFILE", "WEEKLY_ACCOUNT_CDP_PROFILE", "WEEKLY_ACCOUNT_X_CDP_PROFILE"):
            raw = os.getenv(env_name, "").strip()
            if raw:
                candidates.append(Path(raw))
        candidates.append(SKILL_ROOT / "assets" / "chrome-cdp-profile")

        unique: list[Path] = []
        seen: set[str] = set()
        for profile in candidates:
            try:
                resolved = profile.expanduser().resolve()
            except Exception:
                resolved = profile
            key = str(resolved).casefold() if os.name == "nt" else str(resolved)
            if key in seen:
                continue
            seen.add(key)
            unique.append(resolved)
        return unique


def snapshot_social_cdp_pids() -> tuple[dict[str, set[int]], str]:
    try:
        social_metrics = import_social_metrics_module()
    except Exception as exc:
        return {}, f"snapshot import failed: {exc}"

    snapshots: dict[str, set[int]] = {}
    errors: list[str] = []
    for profile in configured_social_cdp_profiles():
        try:
            resolved = profile.expanduser().resolve()
            snapshots[str(resolved)] = set(social_metrics.chrome_profile_pids(resolved))
        except Exception as exc:
            errors.append(f"{profile}: {exc}")
    return snapshots, "\n".join(errors)


def close_new_social_cdp_pids(before: dict[str, set[int]], log_dir: Path, job_id: str, snapshot_note: str = "") -> None:
    lines: list[str] = []
    if snapshot_note:
        lines.append(snapshot_note)
    if not before:
        lines.append("No CDP PID snapshot; skipped browser close to avoid touching pre-existing sessions.")
        write_text_log(log_dir, job_id, "98_cdp_browser_close", "\n".join(lines) + "\n")
        return

    try:
        social_metrics = import_social_metrics_module()
    except Exception as exc:
        write_text_log(log_dir, job_id, "98_cdp_browser_close", f"social_metrics import failed: {exc}\n")
        return

    closed: list[int] = []
    for profile_text, before_pids in before.items():
        try:
            profile = Path(profile_text)
            after_pids = set(social_metrics.chrome_profile_pids(profile))
        except Exception as exc:
            lines.append(f"{profile_text}: after snapshot failed: {exc}")
            continue
        new_pids = sorted(after_pids - set(before_pids))
        lines.append(
            f"{profile_text}: before={sorted(before_pids)} after={sorted(after_pids)} new={new_pids}"
        )
        for pid in new_pids:
            try:
                terminate_process_tree(int(pid))
                closed.append(int(pid))
            except Exception as exc:
                lines.append(f"PID {pid}: close failed: {exc}")
    lines.append(f"closed={sorted(set(closed))}")
    write_text_log(log_dir, job_id, "98_cdp_browser_close", "\n".join(lines) + "\n")


def run_with_silent_cdp_cleanup(workflow_func: Any, log_dir: Path, job_id: str) -> int:
    before_cdp_pids, snapshot_note = snapshot_social_cdp_pids()
    try:
        return int(workflow_func())
    finally:
        close_new_social_cdp_pids(before_cdp_pids, log_dir, job_id, snapshot_note)
        run_cdp_cache_cleanup_step(log_dir, job_id, silent=True)


def safe_tail(text: str, max_lines: int = 20, max_chars: int = 2200) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-max_lines:])
    return tail[-max_chars:] if len(tail) > max_chars else tail


def friendly_export_error(text: str) -> str:
    if "app_scope_not_applied" in text or "99991672" in text:
        missing = [
            scope
            for scope in ("drive:export:readonly", "docs:document:export")
            if scope in text
        ]
        scope_text = "、".join(missing) or "文档导出只读权限"
        return (
            "飞书应用尚未开通导出权限，未执行后续抓取或写入。\n"
            f"缺少：{scope_text}\n"
            "请应用管理员在飞书开放平台开通并发布这些权限后重试。"
        )
    return "导出失败：\n" + safe_tail(text)


def read_env_file_value(path: Path, key: str, default: str = "") -> str:
    if not path.exists():
        return default
    prefix = key + "="
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        return value or default
    return default


def env_value(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def lark_cli_bin() -> str:
    configured = env_value("LARK_CLI_BIN", "lark-cli")
    for candidate in (configured, "lark-cli.cmd", "lark-cli.exe", "lark-cli.ps1"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return configured


def lark_cli_argv() -> list[str]:
    argv = [lark_cli_bin()]
    profile = os.getenv("FEISHU_LARK_PROFILE") or os.getenv("LARK_CLI_PROFILE")
    if profile:
        argv.extend(["--profile", profile])
    return argv


_DIRECT_RECORD_LIST_CACHE: dict[tuple[str, str, str, tuple[str, ...]], list[dict[str, Any]]] = {}
_DIRECT_FIELD_NAME_CACHE: dict[tuple[str, str], dict[str, str]] = {}


def _flag_value(args: list[str], flag: str, default: str = "") -> str:
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def _direct_record_list_payload(args: list[str]) -> dict[str, Any] | None:
    if args[:2] != ["base", "+record-list"]:
        return None
    field_ids = tuple(
        args[index + 1]
        for index, value in enumerate(args[:-1])
        if value == "--field-id"
    )
    if not field_ids:
        return None
    base_token = _flag_value(args, "--base-token")
    table_id = _flag_value(args, "--table-id")
    view_id = _flag_value(args, "--view-id")
    identity = _flag_value(args, "--as", "bot")
    if not base_token or not table_id or os.getenv("LARK_DIRECT_API") != "1":
        return None
    helper = import_backfill_module()
    key = (base_token, table_id, view_id, field_ids)
    items = _DIRECT_RECORD_LIST_CACHE.get(key)
    field_key = (base_token, table_id)
    field_names_by_id = _DIRECT_FIELD_NAME_CACHE.get(field_key) or {}
    if items is None:
        path = f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records"
        if field_names_by_id is None:
            fields_payload = helper.request_lark_api(
                "GET",
                f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
                params={"page_size": 500},
            )
            field_items = (fields_payload.get("data") or {}).get("items") or []
            field_names_by_id = {
                str(item.get("field_id") or item.get("id") or ""): str(item.get("field_name") or item.get("name") or "")
                for item in field_items
                if isinstance(item, dict)
            }
            _DIRECT_FIELD_NAME_CACHE[field_key] = field_names_by_id
        requested_names = [field_names_by_id.get(field_id, field_id) for field_id in field_ids]
        params: dict[str, Any] = {"page_size": 500}
        if view_id:
            params["view_id"] = view_id
        items = []
        page_token = ""
        for _ in range(1000):
            if page_token:
                params["page_token"] = page_token
            payload = helper.request_lark_api("GET", path, params=params)
            data = payload.get("data") or {}
            page_items = data.get("items") or []
            if not isinstance(page_items, list):
                raise RuntimeError("Direct Base record-list response has invalid items.")
            items.extend(item for item in page_items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise RuntimeError("Direct Base record-list response omitted page_token while has_more=true.")
        else:
            raise RuntimeError("Direct Base record-list pagination exceeded 1000 pages.")
        _DIRECT_RECORD_LIST_CACHE[key] = items
    offset = int(_flag_value(args, "--offset", "0") or 0)
    limit = int(_flag_value(args, "--limit", "200") or 200)
    page = items[offset : offset + limit]
    rows = []
    record_ids = []
    for item in page:
        fields = item.get("fields") or {}
        rows.append([fields.get(field_names_by_id.get(field_id, field_id), fields.get(field_id)) for field_id in field_ids])
        record_ids.append(str(item.get("record_id") or item.get("id") or ""))
    return {
        "ok": True,
        "data": {
            "data": rows,
            "record_id_list": record_ids,
            "has_more": offset + len(page) < len(items),
            "fields": list(field_ids),
        },
        "identity": identity,
    }


def run_lark_json(args: list[str], cwd: Path, timeout_s: int = 60) -> dict[str, Any]:
    direct = _direct_record_list_payload(args)
    if direct is not None:
        return direct
    result = run_command(lark_cli_argv() + args, cwd=cwd, timeout_s=timeout_s)
    stdout = result.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"lark-cli returned non-JSON output: {exc}\n{safe_tail(stdout or result.stderr)}") from exc
    if result.returncode != 0 or payload.get("ok") is False:
        message = ""
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = str(error.get("message") or "")
        raise RuntimeError(
            f"lark-cli failed: {message or 'command returned an error'}\n"
            f"{safe_tail(result.stderr or stdout)}"
        )
    return payload


def jielong_scan_scope_text() -> str:
    pages_text = read_env_file_value(JIELONG_ENV_FILE, "FEISHU_SCAN_PAGES", "3")
    try:
        pages = max(1, int(pages_text))
    except ValueError:
        pages = 3
    return f"当前固定只扫来源群最近 {pages} 页消息（每页 50 条，最多约 {pages * 50} 条）。"


def parse_export_saved_path(stdout: str) -> Path:
    data = json.loads(stdout)
    saved = data.get("export", {}).get("data", {}).get("saved_path")
    if not saved:
        raise ValueError("export output did not include export.data.saved_path")
    return Path(saved)


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


def resolve_existing_xlsx_path(path: Path, workdir: Path, fallback_dir: Path) -> Path:
    candidate = path if path.is_absolute() else (workdir / path).resolve()
    if candidate.exists():
        return candidate

    parent = candidate.parent if candidate.parent.exists() else fallback_dir
    if looks_encoding_damaged(candidate.name):
        fallback = newest_xlsx_in_dir(parent, damaged_name=candidate.name) or newest_xlsx_in_dir(
            fallback_dir,
            damaged_name=candidate.name,
        )
        if fallback:
            print(f"[xlsx-path-fallback] {candidate} not found; using newest workbook: {fallback}", file=sys.stderr)
            return fallback.resolve()
    return candidate


def parse_planned_patches(output: str) -> int | None:
    match = re.search(r"planned record-platform patches:\s*(\d+)", output)
    return int(match.group(1)) if match else None


def parse_valid_rows(output: str) -> int | None:
    match = re.search(r"xlsx valid platform rows:\s*(\d+)", output)
    return int(match.group(1)) if match else None


def parse_base_project_records(output: str) -> int | None:
    match = re.search(r"base project records:\s*(\d+)", output)
    return int(match.group(1)) if match else None


def parse_skipped_rows(output: str) -> int | None:
    match = re.search(r"skipped manual-review rows:\s*(\d+)", output)
    if match:
        return int(match.group(1))
    match = re.search(r"skipped rows/fields:\s*(\d+)", output)
    return int(match.group(1)) if match else None


MANUAL_REVIEW_SKIP_MARKERS = (
    "Owner not found in Base Project records",
    "No matching or empty Base row",
    "No available Base row",
    "No supported social URL",
    "XLSX blank Owner",
)


def parse_list_section(output: str, heading: str) -> list[str]:
    lines = output.splitlines()
    items: list[str] = []
    in_section = False
    for line in lines:
        text = line.rstrip()
        if text == heading:
            in_section = True
            continue
        if not in_section:
            continue
        if not text:
            if items:
                break
            continue
        if text.startswith("- "):
            items.append(text[2:].strip())
            continue
        if items:
            break
    return items


def parse_manual_review_skipped_items(output: str) -> list[str]:
    section_items = parse_list_section(output, "Manual-review skipped rows:")
    if section_items:
        return section_items
    skipped_items = parse_list_section(output, "Skipped rows/fields:")
    return [
        item
        for item in skipped_items
        if any(marker in item for marker in MANUAL_REVIEW_SKIP_MARKERS)
    ]


def platform_label_from_url(url: str) -> str:
    lowered = url.lower()
    if "tiktok.com" in lowered:
        return "TikTok"
    if "instagram.com" in lowered:
        return "Instagram"
    if "facebook.com" in lowered or "fb.watch" in lowered:
        return "Facebook"
    if "x.com" in lowered or "twitter.com" in lowered:
        return "X"
    return "社媒"


def parse_metrics_manual_review_items(output: str) -> list[str]:
    items: list[str] = []
    for line in manual_review_lines(output):
        match = re.match(
            r"^\[manual-review\]\s+row=(?P<row>\d+)"
            r"(?:\s+owner=(?P<owner>.*?))?"
            r"\s+url=(?P<url>\S+)\s+error=(?P<error>.*)$",
            line,
        )
        if not match:
            match = re.match(
                r"^\[manual-review\]\s+row=(?P<row>\d+)\s+(?P<url>\S+)\s+error=(?P<error>.*)$",
                line,
            )
        if not match:
            continue
        row = match.group("row")
        owner = (match.groupdict().get("owner") or "").strip()
        url = match.group("url")
        error = match.group("error").strip()
        platform = platform_label_from_url(url)
        if owner:
            items.append(f"row {row} {owner}/{platform}: {error}")
        else:
            items.append(f"row {row}: {platform}: {error}")
    return items


def unique_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def skipped_item_location(item: str) -> str:
    match = re.match(r"row\s+(\d+)\s*(.*)", item, flags=re.I)
    if not match:
        return item.split(":", 1)[0].strip()
    row_num = match.group(1)
    rest = match.group(2).strip()
    owner = ""
    if rest.startswith(":"):
        reason_preview = rest[1:].strip()
        if not re.match(r"^(TikTok|Instagram|Facebook|X|社媒):", reason_preview):
            owner = "接龙人为空"
    elif ":" in rest:
        owner = rest.split(":", 1)[0].strip()
        owner = owner.split("/", 1)[0].strip()
    if not owner:
        return f"XLSX第 {row_num} 行"
    return f"XLSX第 {row_num} 行，接龙人 {owner}"


def skipped_item_reason(item: str) -> str:
    match = re.match(r"row\s+\d+\s*(.*)", item, flags=re.I)
    if match:
        rest = match.group(1).strip()
        bracket_colon = rest.find("]:")
        if bracket_colon >= 0:
            return rest[bracket_colon + 2 :].strip()
        colon_match = re.search(r":\s+", rest)
        if colon_match:
            return rest[colon_match.end() :].strip()
    colon_match = re.search(r":\s+", item)
    if colon_match:
        return item[colon_match.end() :].strip()
    return ""


def redact_urls(text: str) -> str:
    def replace_markdown_link(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        if "http://" in label.lower() or "https://" in label.lower():
            return ""
        return label

    text = re.sub(r"\[([^\]]*)\]\(https?://[^)\s]+\)", replace_markdown_link, text)
    text = URL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t-:：,，;；")


def skipped_item_detail(item: str, *, redact_links: bool = False) -> str:
    location = skipped_item_location(item)
    reason = skipped_item_reason(item)
    if redact_links:
        reason = redact_urls(reason)
    if not reason:
        return location
    return f"{location}，原因：{reason}"


def skipped_details_suffix(items: list[str], limit: int = 8, *, redact_links: bool = False) -> str:
    if not items:
        return ""
    details = [skipped_item_detail(item, redact_links=redact_links) for item in items[:limit]]
    suffix = "（分别是" + "；".join(details)
    if len(items) > limit:
        suffix += f"；另有 {len(items) - limit} 项"
    return suffix + "）"


def skipped_summary_text(count: int | None, items: list[str] | None = None, *, redact_links: bool = False) -> str:
    return f"跳过/需人工处理：{fmt_unknown(count)} 项{skipped_details_suffix(items or [], redact_links=redact_links)}"


def effective_backfill_rows(valid_rows: int | None, skipped_rows: int | None) -> int | None:
    if valid_rows is None:
        return None
    if skipped_rows is None:
        return valid_rows
    return max(0, valid_rows - skipped_rows)


def final_full_summary_message(
    project: str,
    metrics_summary: MetricsSummary,
    base_records: int | None,
    valid_rows: int | None,
    skipped_rows: int | None,
    skipped_items: list[str],
    xlsx_path: Path,
) -> str:
    review_count = skipped_rows if skipped_rows is not None else metrics_summary.manual_review_count
    lines = [
        f"完成「{project}」。",
        f"本次抓取链接：{fmt_unknown(metrics_summary.attempted_links)}",
        f"未抓到/需人工复核：{fmt_unknown(review_count)}",
    ]
    details = skipped_details_suffix(skipped_items)
    if details:
        lines.append(details)
    lines.extend(
        [
            f"项目推送记录：{fmt_unknown(base_records)}",
            f"有效回填：{fmt_unknown(effective_backfill_rows(valid_rows, skipped_rows))}",
            f"文件：{xlsx_path}",
        ]
    )
    return "\n".join(lines)


def parse_labeled_int(output: str, label: str) -> int | None:
    match = re.search(rf"\b{re.escape(label)}=(\d+)", output)
    return int(match.group(1)) if match else None


def count_updated_lines(output: str) -> int:
    return len(re.findall(r"^\[\d+/\d+\]\s+updated\s+", output, flags=re.MULTILINE))


def manual_review_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if "[manual-review]" in line]


def summarize_metrics_output(output: str) -> MetricsSummary:
    excel_write_matches = re.findall(r"^\[excel-write\s+(\d+)/(\d+)\]", output, flags=re.MULTILINE)
    scrape_progress_matches = re.findall(r"^\[(\d+)/(\d+)\]\s+https?://", output, flags=re.MULTILINE)

    attempted_links: int | None = None
    if excel_write_matches:
        attempted_links = int(excel_write_matches[-1][1])
    elif scrape_progress_matches:
        attempted_links = int(scrape_progress_matches[-1][1])

    platform_rows = parse_labeled_int(output, "platform_rows")
    total_urls = parse_labeled_int(output, "total_urls")
    if attempted_links is None:
        attempted_links = platform_rows or total_urls

    return MetricsSummary(
        attempted_links=attempted_links,
        excel_write_rows=len(excel_write_matches),
        manual_review_count=len(manual_review_lines(output)),
        workbook_link_rows=parse_labeled_int(output, "rows_with_urls"),
        workbook_platform_rows=platform_rows,
        workbook_total_urls=total_urls,
    )


def safe_xlsx_name(topic: str) -> str:
    name = WINDOWS_FILENAME_UNSAFE_RE.sub("_", topic).strip().rstrip(".")
    if not name:
        name = "jielong"
    if len(name) > 120:
        name = name[:120].rstrip(" .")
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    return name


def expected_export_xlsx_path(project: str, workdir: Path) -> Path:
    return workdir / "exports" / safe_xlsx_name(project)


def require_local_xlsx_path(project: str, workdir: Path) -> Path:
    path = expected_export_xlsx_path(project, workdir)
    if not path.is_file():
        raise RuntimeError(
            f'本地 XLSX 不存在：{path}\n'
            '请先运行对应的标准指令完成飞书导出，或确认话题与文件名完全一致。'
        )
    return path


def import_backfill_module() -> Any:
    spec = importlib.util.spec_from_file_location("feishu_base_xlsx_backfill_skill", BACKFILL_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import backfill helper: {BACKFILL_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def import_confirm_push_module() -> Any:
    spec = importlib.util.spec_from_file_location("feishu_confirm_push_workflow", CONFIRM_PUSH_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import confirm push helper: {CONFIRM_PUSH_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def import_secondary_push_module() -> Any:
    spec = importlib.util.spec_from_file_location("feishu_secondary_push_workflow", SECONDARY_PUSH_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import secondary push helper: {SECONDARY_PUSH_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def import_weekly_account_posts_module() -> Any:
    spec = importlib.util.spec_from_file_location("feishu_weekly_account_posts_workflow", WEEKLY_ACCOUNT_POSTS_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import weekly account posts helper: {WEEKLY_ACCOUNT_POSTS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def import_social_metrics_module() -> Any:
    spec = importlib.util.spec_from_file_location("feishu_social_metrics_for_cdp", METRICS_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import social metrics helper: {METRICS_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def import_cdp_cache_cleanup_module() -> Any:
    spec = importlib.util.spec_from_file_location("feishu_cdp_cache_cleanup", CDP_CACHE_CLEANUP_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import cdp cache cleanup helper: {CDP_CACHE_CLEANUP_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def refresh_72h_field_names() -> list[str]:
    names: list[str] = []
    for config in REFRESH_72H_PLATFORMS.values():
        for key in ("link", "views", "likes", "comments", "shares", "saves"):
            value = config.get(key)
            if value:
                names.append(value)
    return list(dict.fromkeys(names))


def resolve_refresh_72h_target_fields(backfill: Any, fields_by_name: dict[str, Any]) -> dict[str, dict[str, Any]]:
    writable_types = getattr(backfill, "WRITABLE_TYPES", {"text", "number"})
    target_fields: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    not_writable: list[str] = []
    for platform, config in REFRESH_72H_PLATFORMS.items():
        platform_fields: dict[str, Any] = {}
        for key in ("link", "views", "likes", "comments", "shares", "saves"):
            field_name = config.get(key)
            if not field_name:
                continue
            field = fields_by_name.get(field_name)
            if not field:
                missing.append(field_name)
                continue
            if key != "link" and getattr(field, "type", "") not in writable_types:
                not_writable.append(field.name)
                continue
            platform_fields[key] = field
        target_fields[platform] = platform_fields
    if missing:
        raise RuntimeError("Base 缺少 72h 必需字段：" + ", ".join(dict.fromkeys(missing)))
    if not_writable:
        raise RuntimeError("Base 字段不是可写的文本/数字字段：" + ", ".join(dict.fromkeys(not_writable)))
    return target_fields


def load_backfill_context(
    target_base_url: str | None = None,
    target_base_token: str | None = None,
    target_table_id: str | None = None,
) -> BackfillContext:
    backfill = import_backfill_module()
    backfill.load_env_file(str(BACKFILL_ENV_FILE))

    base_url = target_base_url or os.getenv("FEISHU_BASE_URL", "")
    parsed_base_url = backfill.parse_base_url(base_url) if base_url else {}
    base_token = target_base_token or os.getenv("FEISHU_BASE_TOKEN", "") or parsed_base_url.get("base_token", "")
    table_id = target_table_id or os.getenv("FEISHU_TABLE_ID", "") or parsed_base_url.get("table_id", "")
    if not base_token:
        raise RuntimeError(f"Set FEISHU_BASE_URL or FEISHU_BASE_TOKEN in {BACKFILL_ENV_FILE}.")
    if not table_id:
        raise RuntimeError(f"Set FEISHU_BASE_URL with ?table=... or set FEISHU_TABLE_ID in {BACKFILL_ENV_FILE}.")

    identity = os.getenv("FEISHU_IDENTITY", "bot")
    project_field_name = os.getenv("BACKFILL_PROJECT_FIELD", "Project")
    owner_field_name = os.getenv("BACKFILL_OWNER_FIELD", "Owner")
    xlsx_link_header = os.getenv("BACKFILL_XLSX_LINK_HEADER", "接龙信息")
    xlsx_owner_header = os.getenv("BACKFILL_XLSX_OWNER_HEADER", "接龙人")
    worksheet = os.getenv("BACKFILL_WORKSHEET", "")

    fields_by_name = backfill.get_fields(
        BACKFILL_SKILL_ROOT,
        identity,
        base_token,
        table_id,
    )
    missing = [name for name in (project_field_name, owner_field_name) if name not in fields_by_name]
    if missing:
        raise RuntimeError("Base 缺少必需字段：" + ", ".join(missing))

    target_fields = resolve_refresh_72h_target_fields(backfill, fields_by_name)
    auto_field = fields_by_name.get("自动素材编号")
    return BackfillContext(
        module=backfill,
        skill_root=BACKFILL_SKILL_ROOT,
        identity=identity,
        base_token=base_token,
        table_id=table_id,
        project_field_name=project_field_name,
        owner_field_name=owner_field_name,
        xlsx_link_header=xlsx_link_header,
        xlsx_owner_header=xlsx_owner_header,
        worksheet=worksheet,
        fields_by_name=fields_by_name,
        target_fields=target_fields,
        auto_field=auto_field,
    )


def load_xlsx_context() -> XlsxContext:
    backfill = import_backfill_module()
    backfill.load_env_file(str(BACKFILL_ENV_FILE))
    return XlsxContext(
        module=backfill,
        xlsx_link_header=os.getenv("BACKFILL_XLSX_LINK_HEADER", "接龙信息"),
        xlsx_owner_header=os.getenv("BACKFILL_XLSX_OWNER_HEADER", "接龙人"),
        worksheet=os.getenv("BACKFILL_WORKSHEET", ""),
    )


def import_openpyxl_load_workbook() -> Any:
    try:
        from openpyxl import load_workbook

        return load_workbook
    except ImportError as exc:
        raise RuntimeError("缺少依赖 openpyxl。请先安装 openpyxl 和 defusedxml。") from exc


def cell_has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


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


def copy_cell_style(source: Any, target: Any) -> None:
    if not source or not target:
        return
    if source.has_style:
        target._style = copy.copy(source._style)
    if source.number_format:
        target.number_format = source.number_format
    if source.alignment:
        target.alignment = copy.copy(source.alignment)


def header_values(ws: Any, header_row: int) -> list[str]:
    through_col = last_value_column(ws)
    return [str(ws.cell(header_row, col).value or "").strip() for col in range(1, through_col + 1)]


def normalized_header_key(value: Any) -> str:
    return re.sub(r"[\s\-_()/（）/]+", "", str(value or "").strip().lower())


def find_header_column(ws: Any, header_row: int, header: str) -> int | None:
    wanted = header.strip()
    if not wanted:
        return None
    wanted_key = normalized_header_key(wanted)
    for index, value in enumerate(header_values(ws, header_row), 1):
        if value == wanted or normalized_header_key(value) == wanted_key:
            return index
    return None


def worksheet_has_refresh_72h_headers(ws: Any, ctx: XlsxContext, header_row: int) -> bool:
    owner_present = bool(
        find_header_column(ws, header_row, ctx.xlsx_owner_header)
        or find_header_column(ws, header_row, "Owner")
        or find_header_column(ws, header_row, "owner")
    )
    if not owner_present:
        return False
    if find_header_column(ws, header_row, ctx.xlsx_link_header):
        return True
    return any(
        bool(find_header_column(ws, header_row, config["link"]))
        for config in REFRESH_72H_PLATFORMS.values()
    )


def choose_xlsx_worksheet(wb: Any, ctx: XlsxContext, header_row: int) -> Any:
    if ctx.worksheet:
        if ctx.worksheet in wb.sheetnames:
            return wb[ctx.worksheet]
        requested_key = normalized_header_key(ctx.worksheet)
        for sheet_name in wb.sheetnames:
            if normalized_header_key(sheet_name) == requested_key:
                return wb[sheet_name]
        print(f"[xlsx-sheet-fallback] requested sheet not found: {ctx.worksheet}; auto-detecting worksheet", file=sys.stderr)

    for ws in wb.worksheets:
        if worksheet_has_refresh_72h_headers(ws, ctx, header_row):
            return ws
    return wb.worksheets[0]


def ensure_header_column(ws: Any, header_row: int, header: str) -> int:
    existing = find_header_column(ws, header_row, header)
    if existing:
        return existing
    style_source_col = last_value_column(ws)
    column = style_source_col + 1
    cell = ws.cell(header_row, column, header)
    copy_cell_style(ws.cell(header_row, style_source_col), cell)
    return column


def platform_from_metric_name(name: str) -> str:
    lowered = str(name or "").lower()
    if lowered == "x" or "twitter" in lowered:
        return "X"
    if "instagram" in lowered:
        return "INS"
    if "tiktok" in lowered:
        return "TK"
    if "facebook" in lowered:
        return "FB"
    return ""


def refresh_72h_detect_platform(url: str) -> str:
    metrics = import_social_metrics_module()
    platform = platform_from_metric_name(metrics.detect_platform(url))
    if platform:
        return platform
    lowered = str(url or "").lower()
    if "x.com" in lowered or "twitter.com" in lowered:
        return "X"
    if "instagram.com" in lowered:
        return "INS"
    if "tiktok.com" in lowered or "vt.tiktok.com" in lowered or "vm.tiktok.com" in lowered:
        return "TK"
    if "facebook.com" in lowered or "fb.watch" in lowered:
        return "FB"
    return ""


def refresh_72h_cell_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    metrics = import_social_metrics_module()
    urls = metrics.extract_urls_from_text(text)
    if urls:
        return str(urls[0]).rstrip(TRAILING_URL_CHARS)
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(TRAILING_URL_CHARS)
    return ""


def score_refresh_72h_link_column(ws: Any, start_row: int, column: int, last_row: int) -> tuple[int, int, int, int]:
    platform_rows = 0
    total_urls = 0
    multi_url_rows = 0
    unique_urls: set[str] = set()
    metrics = import_social_metrics_module()
    for row in range(start_row, last_row + 1):
        urls = metrics.extract_urls_from_text(str(ws.cell(row, column).value or ""))
        if not urls:
            continue
        total_urls += len(urls)
        unique_urls.update(urls)
        if len(urls) > 1:
            multi_url_rows += 1
        if any(refresh_72h_detect_platform(url) for url in urls):
            platform_rows += 1
    score = platform_rows * 100 + min(len(unique_urls), platform_rows)
    score -= multi_url_rows * 50
    score -= max(0, total_urls - platform_rows) * 5
    return score, platform_rows, total_urls, multi_url_rows


def find_refresh_72h_link_column(ws: Any, start_row: int) -> int:
    last_col = last_value_column(ws)
    last_row = last_value_row(ws)
    best: tuple[int, int, int, int, int] | None = None
    for column in range(1, last_col + 1):
        score, platform_rows, total_urls, multi_url_rows = score_refresh_72h_link_column(ws, start_row, column, last_row)
        if platform_rows <= 0:
            continue
        candidate = (score, platform_rows, total_urls, multi_url_rows, column)
        if best is None or candidate > best:
            best = candidate
    if best is None:
        raise RuntimeError("XLSX 中没有识别到支持的社媒链接。")
    return best[-1]


def find_owner_column(ws: Any, header_row: int, ctx: XlsxContext) -> int:
    owner_col = find_header_column(ws, header_row, ctx.xlsx_owner_header)
    if owner_col:
        return owner_col
    for candidate in ("接龙人", "Owner", "owner"):
        owner_col = find_header_column(ws, header_row, candidate)
        if owner_col:
            return owner_col
    if last_value_column(ws) >= 3:
        return 3
    raise RuntimeError(f"XLSX 缺少 Owner/接龙人列：{ctx.xlsx_owner_header}")


def apply_limit_per_platform(rows: list[Refresh72hRow], limit: int | None) -> list[Refresh72hRow]:
    if not limit or limit <= 0:
        return rows
    counts = {platform: 0 for platform in REFRESH_72H_PLATFORMS}
    selected: list[Refresh72hRow] = []
    for row in rows:
        if counts[row.platform] >= limit:
            continue
        selected.append(row)
        counts[row.platform] += 1
    return selected


def prepare_refresh_72h_workbook(
    xlsx_path: Path,
    ctx: XlsxContext,
    start_row: int | None,
    limit_per_platform: int | None,
) -> tuple[Any, Any, list[Refresh72hRow], dict[str, Any], dict[str, Any]]:
    load_workbook = import_openpyxl_load_workbook()
    wb = load_workbook(xlsx_path)
    header_row = 1
    first_data_row = max(header_row + 1, start_row or header_row + 1)
    ws = choose_xlsx_worksheet(wb, ctx, header_row)
    owner_col = find_owner_column(ws, header_row, ctx)
    last_row = last_value_row(ws)

    platform_link_cols = {
        platform: find_header_column(ws, header_row, config["link"])
        for platform, config in REFRESH_72H_PLATFORMS.items()
    }
    use_wide_platform_columns = any(column for column in platform_link_cols.values())

    columns: dict[str, Any] = {"mode": "wide" if use_wide_platform_columns else "long", "platforms": {}}
    rows: list[Refresh72hRow] = []
    ignored_blank_owner = 0
    ignored_unknown_platform = 0

    if use_wide_platform_columns:
        for platform, config in REFRESH_72H_PLATFORMS.items():
            if not platform_link_cols.get(platform):
                continue
            platform_columns: dict[str, int] = {"views": ensure_header_column(ws, header_row, config["views"])}
            for metric_key in ("likes", "comments", "shares", "saves"):
                field_name = config.get(metric_key)
                if field_name:
                    platform_columns[metric_key] = ensure_header_column(ws, header_row, field_name)
            columns["platforms"][platform] = platform_columns

        for row_num in range(first_data_row, last_row + 1):
            owner = ctx.module.normalize_text(ws.cell(row_num, owner_col).value)
            row_has_link = False
            for platform, link_col in platform_link_cols.items():
                if not link_col:
                    continue
                url = refresh_72h_cell_url(ws.cell(row_num, link_col).value)
                if not url:
                    continue
                row_has_link = True
                if not owner:
                    continue
                detected = refresh_72h_detect_platform(url)
                if detected and detected != platform:
                    ignored_unknown_platform += 1
                    continue
                auto_id = ctx.module.normalize_text(ws.cell(row_num, 1).value)
                rows.append(Refresh72hRow(row_num=row_num, auto_id=auto_id, owner=owner, platform=platform, url=url))
            if row_has_link and not owner:
                ignored_blank_owner += 1
    else:
        link_col = find_header_column(ws, header_row, ctx.xlsx_link_header)
        if not link_col:
            score, platform_rows, _total_urls, _multi_url_rows = score_refresh_72h_link_column(ws, first_data_row, 1, last_row)
            if platform_rows > 0:
                link_col = 1
            else:
                link_col = find_refresh_72h_link_column(ws, first_data_row)
        columns["link_column"] = link_col
        columns["generic"] = {
            metric_key: ensure_header_column(ws, header_row, header)
            for metric_key, header in REFRESH_72H_GENERIC_XLSX_HEADERS.items()
        }
        columns["platforms"] = {
            platform: {"views": ensure_header_column(ws, header_row, config["views"])}
            for platform, config in REFRESH_72H_PLATFORMS.items()
        }

        metrics = import_social_metrics_module()
        for row_num in range(first_data_row, last_row + 1):
            urls = metrics.extract_urls_from_text(str(ws.cell(row_num, link_col).value or ""))
            if not urls:
                continue
            owner = ctx.module.normalize_text(ws.cell(row_num, owner_col).value)
            if not owner:
                ignored_blank_owner += 1
                continue
            url = str(urls[0]).rstrip(TRAILING_URL_CHARS)
            platform = refresh_72h_detect_platform(url)
            if not platform:
                ignored_unknown_platform += 1
                continue
            auto_id = ctx.module.normalize_text(ws.cell(row_num, 1).value)
            rows.append(Refresh72hRow(row_num=row_num, auto_id=auto_id, owner=owner, platform=platform, url=url))

    rows = apply_limit_per_platform(rows, limit_per_platform)
    meta = {
        "sheet": ws.title,
        "mode": columns["mode"],
        "owner_column": owner_col,
        "last_row": last_row,
        "ignored_blank_owner": ignored_blank_owner,
        "ignored_unknown_platform": ignored_unknown_platform,
        "per_platform": {platform: sum(1 for row in rows if row.platform == platform) for platform in REFRESH_72H_PLATFORMS},
    }
    return wb, ws, rows, columns, meta


def metric_value(result: dict[str, Any] | None, key: str) -> int | None:
    if not result or result.get("error"):
        return None
    metric = (result.get("metrics") or {}).get(key) or {}
    value = metric.get("value") if isinstance(metric, dict) else None
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def refresh_72h_result_keys(url: str) -> list[str]:
    text = str(url or "").strip()
    keys: list[str] = []
    status_id = re.search(r"/(?:i/)?status(?:es)?/(\d+)", text, flags=re.I)
    if status_id:
        keys.append(f"x:{status_id.group(1)}")
    if text:
        keys.append("url:" + re.sub(r"[?#].*$", "", text).rstrip("/").lower())
    return keys


def assign_refresh_72h_results(rows: list[Refresh72hRow], data: list[Any]) -> None:
    if len(data) == len(rows):
        for row, item in zip(rows, data, strict=False):
            row.result = item if isinstance(item, dict) else None
        return

    lookup: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        for url_field in ("source_url", "final_url"):
            for key in refresh_72h_result_keys(str(item.get(url_field) or "")):
                lookup.setdefault(key, item)

    missing: list[str] = []
    for row in rows:
        row.result = None
        for key in refresh_72h_result_keys(row.url):
            if key in lookup:
                row.result = lookup[key]
                break
        if row.result is None:
            missing.append(f"row={row.row_num} auto_id={row.auto_id or '<blank>'} owner={row.owner} platform={row.platform} url={row.url}")
    if missing:
        raise RuntimeError(
            "72h 抓取结果无法映射到部分 XLSX 行。\n"
            f"rows={len(rows)} results={len(data)} missing={len(missing)}\n"
            + "\n".join(missing[:10])
        )
    print(f"[refresh-72h-map] mapped results by URL/id: rows={len(rows)} results={len(data)}", file=sys.stderr)


def scrape_refresh_72h_rows(rows: list[Refresh72hRow], log_dir: Path, job_id: str, workdir: Path) -> CommandResult:
    input_path = log_dir / f"{job_id}_72h_urls.txt"
    json_path = log_dir / f"{job_id}_72h_results.json"
    md_path = log_dir / f"{job_id}_72h_metrics.md"
    text_path = log_dir / f"{job_id}_72h_text.md"
    log_dir.mkdir(parents=True, exist_ok=True)
    input_path.write_text("\n".join(row.url for row in rows) + "\n", encoding="utf-8")

    argv = [
        sys.executable,
        str(METRICS_SCRIPT),
        "--input",
        str(input_path),
        "--keep-duplicates",
        "--use-cdp-for-x",
        "--use-cdp-for-facebook",
        "--use-cdp-for-instagram",
        "--use-cdp-for-linkedin",
        "--cdp-headless",
        "--no-allow-manual-login",
        "--retry-missing-metrics",
        "--media-kind-aware-retry",
        "--stable-reel-grid-wait",
        "--ignore-anti-bot-stop",
        "--json-output",
        str(json_path),
        "--output",
        str(md_path),
        "--text-output",
        str(text_path),
    ]
    result = run_command_streamed(
        argv,
        cwd=workdir,
        idle_timeout_s=3600,
        on_stderr_line=make_social_metrics_progress_callback("metrics_72h", "72h 抓取"),
    )
    write_job_log(log_dir, job_id, "02_72h_metrics", result)
    if not json_path.exists():
        raise RuntimeError("72h 抓取没有生成 JSON 结果。\n" + safe_tail(result.stderr or result.stdout))

    data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise RuntimeError("72h 抓取 JSON 结果格式不是列表。")
    assign_refresh_72h_results(rows, data)
    return result


def write_refresh_72h_results(
    wb: Any,
    ws: Any,
    rows: list[Refresh72hRow],
    columns: dict[str, Any],
    xlsx_path: Path,
) -> tuple[Path, int]:
    protected_columns = {
        find_header_column(ws, 1, config["original_views"])
        for config in REFRESH_72H_PLATFORMS.values()
    }
    protected_columns.discard(None)
    backup = xlsx_path.with_name(f"{xlsx_path.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{xlsx_path.suffix}")
    shutil.copy2(xlsx_path, backup)
    written_row_nums: set[int] = set()
    for row in rows:
        row_written = False
        if columns["mode"] == "wide":
            target_columns = columns["platforms"].get(row.platform, {})
        else:
            target_columns = {"views": columns["platforms"][row.platform]["views"], **columns["generic"]}

        for metric_key in ("likes", "comments", "shares", "saves", "views"):
            target_col = target_columns.get(metric_key)
            if not target_col:
                continue
            if target_col in protected_columns:
                raise RuntimeError(f"拒绝写入原始播放/曝光列：{ws.cell(1, target_col).value}")
            value = metric_value(row.result, metric_key)
            if value is None:
                continue
            target = ws.cell(row.row_num, target_col)
            if row.row_num > 2:
                copy_cell_style(ws.cell(row.row_num - 1, target_col), target)
            target.value = value
            row_written = True

        if row_written:
            written_row_nums.add(row.row_num)
    wb.save(xlsx_path)
    wb.close()
    return backup, len(written_row_nums)


def summarize_refresh_72h_rows(rows: list[Refresh72hRow]) -> Refresh72hSummary:
    successful = 0
    for row in rows:
        if row.result and not row.result.get("error") and metric_value(row.result, "views") is not None:
            successful += 1
    missed = max(0, len(rows) - successful)
    return Refresh72hSummary(
        attempted_links=len(rows),
        successful_links=successful,
        missed_links=missed,
    )


def build_refresh_72h_backfill_plan(
    ctx: BackfillContext,
    project: str,
    rows: list[Refresh72hRow],
) -> tuple[list[Any], list[Any], list[str]]:
    skipped: list[str] = []
    project_field = ctx.fields_by_name[ctx.project_field_name]
    owner_field = ctx.fields_by_name[ctx.owner_field_name]
    select_fields: list[Any] = []
    seen_ids: set[str] = set()
    all_target_fields = [field for mapping in ctx.target_fields.values() for field in mapping.values()]
    for field in [ctx.auto_field, project_field, owner_field, *all_target_fields]:
        if not field or field.id in seen_ids:
            continue
        select_fields.append(field)
        seen_ids.add(field.id)

    records = ctx.module.search_project_records(
        skill_root=ctx.skill_root,
        identity=ctx.identity,
        base_token=ctx.base_token,
        table_id=ctx.table_id,
        project_value=project,
        project_field=project_field,
        select_fields=select_fields,
    )
    if not records:
        raise RuntimeError(f"No Base records found for {ctx.project_field_name}={project}.")

    owner_buckets = ctx.module.build_owner_record_buckets(records, owner_field)
    plans: list[Any] = []
    reserved_slots: set[tuple[str, str]] = set()
    for row in rows:
        owner_records = owner_buckets.get(row.owner, [])
        if not owner_records:
            skipped.append(f"row {row.row_num} {row.owner}/{row.platform} [{row.auto_id or 'A列空'}]: Owner not found in Base Project records")
            continue
        target_fields = ctx.target_fields.get(row.platform, {})
        entry = ctx.module.XlsxEntry(row_num=row.row_num, owner=row.owner, platform=row.platform, link=row.url, metrics={})
        record, skip_reason = ctx.module.choose_record_for_entry(entry, owner_records, target_fields, reserved_slots)
        if not record:
            skipped.append(f"row {row.row_num} {row.owner}/{row.platform} [{row.auto_id or 'A列空'}]: {skip_reason}")
            continue
        reserved_slots.add((record.record_id, row.platform))
        plan = ctx.module.PlannedUpdate(
            record_id=record.record_id,
            auto_id=ctx.module.auto_id_for(record, ctx.auto_field),
            owner=row.owner,
            platform=row.platform,
            xlsx_row=row.row_num,
        )
        for metric_key in ("views", "likes", "comments", "shares", "saves"):
            target_field = target_fields.get(metric_key)
            if not target_field:
                continue
            raw_value = metric_value(row.result, metric_key)
            if raw_value is None:
                plan.skipped.append(f"{target_field.name}: 72h 抓取结果为空")
                continue
            value = ctx.module.value_for_field(raw_value, target_field)
            if value is None:
                plan.skipped.append(f"{target_field.name}: 值不可写")
                continue
            plan.readable_patch[target_field.name] = value
            plan.patch_by_id[target_field.id] = value
        if plan.patch_by_id:
            plans.append(plan)
        elif plan.skipped:
            skipped.append(f"row {row.row_num} {row.owner}/{row.platform} [{row.auto_id or 'A列空'}]: " + "; ".join(plan.skipped))
    return records, plans, skipped


def execute_refresh_72h_plans(ctx: BackfillContext, plans: list[Any]) -> list[str]:
    records = [
        {"record_id": plan.record_id, "fields": plan.patch_by_id}
        for plan in plans
    ]
    ctx.module.batch_update_records(
        ctx.skill_root,
        ctx.identity,
        ctx.base_token,
        ctx.table_id,
        records,
    )
    return [
        f"[{index}/{len(plans)}] updated {plan.auto_id} {plan.owner} {plan.platform}"
        for index, plan in enumerate(plans, 1)
    ]


def refresh_72h_overview_lines(summary: Refresh72hSummary, rows: list[Refresh72hRow]) -> list[str]:
    per_platform = {platform: sum(1 for row in rows if row.platform == platform) for platform in REFRESH_72H_PLATFORMS}
    platform_text = "，".join(f"{platform} {count}" for platform, count in per_platform.items() if count)
    return [
        f"本次 72h 链接：{summary.attempted_links} 条" + (f"（{platform_text}）" if platform_text else ""),
        f"成功抓取/写回：{summary.successful_links} 条",
        f"未抓到/需人工复核：{summary.missed_links} 条",
    ]


def fmt_unknown(value: int | None) -> str:
    return str(value) if value is not None else "未知"


def export_xlsx_for_refresh_72h(args: argparse.Namespace, project: str, workdir: Path, log_dir: Path, job_id: str) -> Path:
    export_argv = [
        sys.executable,
        str(JIELONG_SCRIPT),
        "--env-file",
        str(JIELONG_ENV_FILE),
        "--keyword",
        project,
        "--overwrite",
        "--output-dir",
        "exports",
        "--file-name",
        safe_xlsx_name(project),
    ]
    if args.strict_keyword and not args.base_url and not args.base_token:
        export_argv.append("--require-keyword-match")
    if args.chat_id:
        export_argv.extend(["--chat-id", args.chat_id])
    if args.base_url:
        export_argv.extend(["--base-url", args.base_url])
    if args.base_token:
        export_argv.extend(["--base-token", args.base_token])

    export_result = run_command(export_argv, cwd=workdir, timeout_s=300)
    write_job_log(log_dir, job_id, "01_72h_export", export_result)
    if export_result.returncode != 0:
        export_error = export_result.stderr or export_result.stdout
        if "No Base or Wiki URL was found in scanned messages containing keyword" in export_error:
            raise RuntimeError(
                f"没有找到「{project}」对应的接龙，未开始 72h 刷新。\n"
                + jielong_scan_scope_text()
                + "\n请确认接龙标题/关键词是否正确，或改用本地 XLSX / source-base / --base-url 指定来源。"
            )
        raise RuntimeError(friendly_export_error(export_error))

    try:
        exported_path = resolve_existing_xlsx_path(
            parse_export_saved_path(export_result.stdout),
            workdir,
            workdir / "exports",
        )
    except Exception as exc:
        raise RuntimeError(f"导出完成但没有读到 XLSX 路径：{exc}\n{safe_tail(export_result.stdout)}") from exc
    return exported_path


def locate_or_export_refresh_72h_xlsx(
    args: argparse.Namespace,
    project: str,
    workdir: Path,
    log_dir: Path,
    job_id: str,
) -> Path:
    expected_path = expected_export_xlsx_path(project, workdir)
    if args.local_xlsx_only:
        expected_path = require_local_xlsx_path(project, workdir)
        emit(
            "progress",
            "export",
            f"\u6536\u5230\uff1a\u76f4\u63a5\u8bfb\u53d6\u672c\u5730 XLSX\uff1a{expected_path}",
            xlsx_path=str(expected_path),
            local_hit=True,
            export_fallback=False,
        )
        return expected_path

    if expected_path.is_file():
        emit(
            "progress",
            "export",
            f"\u6536\u5230\uff1a\u4f18\u5148\u8bfb\u53d6\u672c\u5730 XLSX\uff1a{expected_path}",
            xlsx_path=str(expected_path),
            local_hit=True,
            export_fallback=False,
        )
        return expected_path

    exported_path = export_xlsx_for_refresh_72h(args, project, workdir, log_dir, job_id)
    if not exported_path.exists() and expected_path.exists():
        exported_path = expected_path
    if not exported_path.exists():
        raise RuntimeError(f"\u5bfc\u51fa\u8fd4\u56de\u7684 XLSX \u4e0d\u5b58\u5728\uff1a{exported_path}")
    emit(
        "progress",
        "export",
        f"\u6536\u5230\uff1a\u672c\u5730 XLSX \u4e0d\u5b58\u5728\uff0c\u5df2\u91cd\u65b0\u5bfc\u51fa\u63a5\u9f99\uff1a{exported_path}",
        xlsx_path=str(exported_path),
        local_hit=False,
        export_fallback=True,
    )
    return exported_path


def run_refresh_72h_workflow(args: argparse.Namespace, project: str, job_id: str, workdir: Path, log_dir: Path) -> int:
    try:
        xlsx_path = locate_or_export_refresh_72h_xlsx(args, project, workdir, log_dir, job_id)
    except Exception as exc:
        emit("error", "export", str(exc))
        return 1

    try:
        xlsx_ctx = load_xlsx_context()
    except Exception as exc:
        emit("error", "xlsx", f"读取 XLSX 配置失败：\n{safe_tail(str(exc))}")
        return 1

    try:
        wb, ws, rows, columns, xlsx_meta = prepare_refresh_72h_workbook(
            xlsx_path=xlsx_path,
            ctx=xlsx_ctx,
            start_row=args.start_row,
            limit_per_platform=args.limit_per_platform,
        )
    except Exception as exc:
        emit("error", "xlsx", f"读取 XLSX 失败：\n{safe_tail(str(exc))}")
        return 1

    if not rows:
        wb.close()
        emit(
            "final",
            "complete",
            f"完成【{project}】72h：未找到需要刷新的全平台链接。\n文件：{xlsx_path}",
            xlsx_path=str(xlsx_path),
            attempted_links=0,
        )
        return 0

    try:
        scrape_result = scrape_refresh_72h_rows(rows, log_dir, job_id, workdir)
    except Exception as exc:
        wb.close()
        error_text = str(exc)
        label = "72h 数据映射失败" if "映射" in error_text else "72h 数据抓取失败"
        emit("error", "metrics_72h", f"{label}：\n{safe_tail(error_text)}")
        return 1

    try:
        ctx = load_backfill_context(args.target_base_url, args.target_base_token, args.target_table_id)
    except Exception as exc:
        wb.close()
        emit(
            "error",
            "base_fields",
            "72h 数据已抓取，但素材推送表字段校验失败，已停止写入 XLSX/Base。\n"
            + f"{safe_tail(str(exc))}",
        )
        return 1

    emit(
        "progress",
        "base_fields",
        "开始写回 XLSX。",
    )

    try:
        backup_path, _written_rows = write_refresh_72h_results(wb, ws, rows, columns, xlsx_path)
    except Exception as exc:
        try:
            wb.close()
        except Exception:
            pass
        emit("error", "xlsx", f"XLSX 写回失败：\n{safe_tail(str(exc))}")
        return 1

    summary = summarize_refresh_72h_rows(rows)
    scrape_note = ""
    if scrape_result.returncode != 0:
        scrape_note = "\n抓取子进程返回非零状态；已按 JSON 结果继续汇总，详情见本地日志。"
    if scrape_note:
        emit(
            "progress",
            "metrics_72h",
            scrape_note.strip(),
            xlsx_path=str(xlsx_path),
            backup_path=str(backup_path),
            summary=summary.__dict__,
        )

    if args.no_backfill:
        emit(
            "final",
            "complete",
            f"完成【{project}】72h，本次按指令不回填素材推送表。\n"
            + "\n".join(refresh_72h_overview_lines(summary, rows))
            + f"\n文件：{xlsx_path}",
            xlsx_path=str(xlsx_path),
            summary=summary.__dict__,
        )
        return 0

    try:
        records, plans, skipped = build_refresh_72h_backfill_plan(ctx, project, rows)
    except Exception as exc:
        emit("error", "backfill_72h_dry_run", f"72h 回填预检失败，未写入：\n{safe_tail(str(exc))}")
        return 1

    skipped_summary = skipped_summary_text(len(skipped), skipped, redact_links=True)

    if args.dry_run or not args.execute_backfill:
        emit(
            "final",
            "dry_run",
            f"完成【{project}】72h dry-run，未写入素材推送表。\n"
            + "\n".join(refresh_72h_overview_lines(summary, rows))
            + "\n"
            f"{skipped_summary}\n"
            f"文件：{xlsx_path}",
            xlsx_path=str(xlsx_path),
            summary=summary.__dict__,
            skipped=len(skipped),
        )
        return 0

    if not plans:
        emit(
            "final",
            "complete",
            f"完成【{project}】72h。\n"
            + "\n".join(refresh_72h_overview_lines(summary, rows))
            + f"\n没有可写入素材推送表的 72h 结果。\n"
            f"{skipped_summary}\n"
            f"文件：{xlsx_path}",
            xlsx_path=str(xlsx_path),
            summary=summary.__dict__,
            skipped=len(skipped),
        )
        return 0

    try:
        execute_lines = execute_refresh_72h_plans(ctx, plans)
        write_text_log(log_dir, job_id, "03_72h_backfill_execute", "\n".join(execute_lines) + "\n")
    except Exception as exc:
        emit("error", "backfill_72h_execute", f"72h 回填失败：\n{safe_tail(str(exc))}")
        return 1

    summary.executed_patches = len(plans)
    emit(
        "final",
        "complete",
        f"完成【{project}】72h。\n"
        + "\n".join(refresh_72h_overview_lines(summary, rows))
        + "\n"
        f"已回填记录：{summary.executed_patches}\n"
        f"{skipped_summary}\n"
        f"文件：{xlsx_path}",
        xlsx_path=str(xlsx_path),
        summary=summary.__dict__,
        skipped=len(skipped),
    )
    return 0


def run_confirm_push_workflow(args: argparse.Namespace, project: str, job_id: str, workdir: Path, log_dir: Path) -> int:
    try:
        confirm_push = import_confirm_push_module()
        ctx = confirm_push.load_context(
            target_base_url=args.target_base_url,
            target_base_token=args.target_base_token,
            target_table_id=args.target_table_id,
        )
        plan = confirm_push.build_plan(ctx, project)
    except Exception as exc:
        emit("error", "confirm_plan", f"确认推送预检失败，未写入：\n{safe_tail(str(exc))}")
        return 1

    write_job_log(
        log_dir,
        job_id,
        "01_confirm_push_plan",
        CommandResult(
            0,
            "\n".join(confirm_push.plan_lines(plan)) + "\n",
            "",
            0.0,
        ),
    )

    emit(
        "progress",
        "confirm_plan",
        f"收到：\n确认推送预检完成。\n准备推送：{plan.ready_count} 人\n"
        f"跳过 Owner 为空：{len(plan.skipped_owner_blank)} 条\n"
        f"跳过推送通知已存在：{len(plan.skipped_push_existing)} 条",
        ready_count=plan.ready_count,
        skipped_owner_blank=len(plan.skipped_owner_blank),
        skipped_push_existing=len(plan.skipped_push_existing),
        base_project_records=plan.total_project_records,
    )

    if args.dry_run or not args.execute_backfill:
        emit(
            "final",
            "dry_run",
            f"确认推送 dry-run 完成「{project}」。\n准备推送：{plan.ready_count} 人\n"
            f"跳过 Owner 为空：{len(plan.skipped_owner_blank)} 条\n"
            f"跳过推送通知已存在：{len(plan.skipped_push_existing)} 条\n"
            "本次未写入推送通知。",
            ready_count=plan.ready_count,
            skipped_owner_blank=len(plan.skipped_owner_blank),
            skipped_push_existing=len(plan.skipped_push_existing),
            base_project_records=plan.total_project_records,
        )
        return 0

    if not plan.candidates:
        emit(
            "final",
            "complete",
            f"确认推送完成「{project}」。\n准备推送：0 人\n没有需要写入“确认推送”的记录。",
            ready_count=0,
            skipped_owner_blank=len(plan.skipped_owner_blank),
            skipped_push_existing=len(plan.skipped_push_existing),
            base_project_records=plan.total_project_records,
        )
        return 0

    try:
        write_result = confirm_push.execute_confirm_push(ctx, plan.candidates)
        write_text_log(log_dir, job_id, "02_confirm_push_execute", "\n".join(write_result.lines) + "\n")
    except Exception as exc:
        emit("error", "confirm_write", f"确认推送写入失败：\n{safe_tail(str(exc))}")
        return 1

    wait_seconds = getattr(confirm_push, "DEFAULT_WAIT_SECONDS", 60)
    emit(
        "progress",
        "confirm_wait",
        f"已写入确认推送：{len(write_result.written)} 人。\n等待 {wait_seconds} 秒后复核通知状态。",
        written=len(write_result.written),
        wait_seconds=wait_seconds,
    )
    time.sleep(wait_seconds)

    try:
        verify = confirm_push.verify_notification_status(ctx, project, write_result.written)
    except Exception as exc:
        emit("error", "confirm_verify", f"通知状态复核失败，请人工校验：\n{safe_tail(str(exc))}")
        return 1

    pending_count = len(verify.pending) + len(verify.missing_record_ids)
    if pending_count == 0:
        emit(
            "final",
            "complete",
            f"确认推送成功「{project}」。\n准备推送：{plan.ready_count} 人\n"
            f"已写入确认推送：{len(write_result.written)} 人\n通知状态已通知：{len(verify.notified)} 人",
            ready_count=plan.ready_count,
            written=len(write_result.written),
            notified=len(verify.notified),
            pending=0,
        )
        return 0

    emit(
        "final",
        "complete",
        f"确认推送已写入「{project}」，但需人工校验通知状态。\n准备推送：{plan.ready_count} 人\n"
        f"已写入确认推送：{len(write_result.written)} 人\n通知状态已通知：{len(verify.notified)} 人\n"
        f"未确认已通知/记录缺失：{pending_count} 人",
        ready_count=plan.ready_count,
        written=len(write_result.written),
        notified=len(verify.notified),
        pending=pending_count,
    )
    return 0


def export_xlsx_for_secondary_push(args: argparse.Namespace, project: str, workdir: Path, log_dir: Path, job_id: str) -> Path:
    export_argv = [
        sys.executable,
        str(JIELONG_SCRIPT),
        "--env-file",
        str(JIELONG_ENV_FILE),
        "--keyword",
        project,
        "--overwrite",
        "--output-dir",
        "exports",
        "--file-name",
        safe_xlsx_name(project),
    ]
    if args.strict_keyword and not args.base_url and not args.base_token:
        export_argv.append("--require-keyword-match")
    if args.chat_id:
        export_argv.extend(["--chat-id", args.chat_id])
    if args.base_url:
        export_argv.extend(["--base-url", args.base_url])
    if args.base_token:
        export_argv.extend(["--base-token", args.base_token])

    export_result = run_command(export_argv, cwd=workdir, timeout_s=300)
    write_job_log(log_dir, job_id, "01_secondary_export", export_result)
    if export_result.returncode != 0:
        export_error = export_result.stderr or export_result.stdout
        if "No Base or Wiki URL was found in scanned messages containing keyword" in export_error:
            raise RuntimeError(
                f"没有找到「{project}」对应的接龙，未开始二次推送预检或写入。\n"
                + jielong_scan_scope_text()
                + "\n请确认接龙标题/关键词是否正确，或改用 source-base / --base-url 指定来源。"
            )
        raise RuntimeError(friendly_export_error(export_error))

    try:
        exported_path = resolve_existing_xlsx_path(
            parse_export_saved_path(export_result.stdout),
            workdir,
            workdir / "exports",
        )
    except Exception as exc:
        raise RuntimeError(f"导出完成但没有读到 XLSX 路径：{exc}\n{safe_tail(export_result.stdout)}") from exc
    if not exported_path.exists():
        raise RuntimeError(f"导出返回的 XLSX 不存在：{exported_path}")
    return exported_path


def secondary_push_final_count_lines(stats: Any) -> list[str]:
    return [
        f"推送数：{stats.owner_nonempty}",
        f"已发布：{stats.owner_nonempty_publish_status_nonempty}",
        f"二次推送：{stats.secondary_push_nonempty} 条",
    ]


def locate_secondary_push_xlsx(
    args: argparse.Namespace,
    project: str,
    workdir: Path,
    log_dir: Path,
    job_id: str,
) -> tuple[Path, str]:
    if args.local_xlsx_only:
        return require_local_xlsx_path(project, workdir), "直接读取本地 XLSX"
    return export_xlsx_for_secondary_push(args, project, workdir, log_dir, job_id), "接龙已导出"


def run_secondary_push_workflow(args: argparse.Namespace, project: str, job_id: str, workdir: Path, log_dir: Path) -> int:
    try:
        xlsx_path, source_text = locate_secondary_push_xlsx(args, project, workdir, log_dir, job_id)
    except Exception as exc:
        emit("error", "secondary_export", str(exc))
        return 1

    emit(
        "progress",
        "secondary_export",
        f"收到：{source_text}：{xlsx_path}",
        project=project,
        job_id=job_id,
        workflow_id="secondary_push",
        mode="secondary-push",
        xlsx_path=str(xlsx_path),
    )

    try:
        secondary_push = import_secondary_push_module()
        ctx = secondary_push.load_context(
            target_base_url=args.target_base_url,
            target_base_token=args.target_base_token,
            target_table_id=args.target_table_id,
        )
        plan = secondary_push.build_plan(ctx, project, xlsx_path)
    except Exception as exc:
        emit("error", "secondary_plan", f"二次推送预检失败，未写入：\n{safe_tail(str(exc))}")
        return 1

    write_job_log(
        log_dir,
        job_id,
        "02_secondary_plan",
        CommandResult(
            0,
            "\n".join(secondary_push.plan_lines(plan)) + "\n",
            "",
            0.0,
        ),
    )

    if args.dry_run or args.no_backfill or not args.execute_backfill:
        emit(
            "final",
            "dry_run",
            f"二次推送 dry-run 完成。「{project}」\n"
            f"本次未写入 Base。\n"
            f"准备回填社媒链接：{plan.link_plan_count} 条\n"
            f"准备写入二次推送：{plan.marker_plan_count} 条\n"
            f"跳过 XLSX/链接项：{len(plan.link_skipped)} 条\n"
            + "\n".join(secondary_push_final_count_lines(plan.stats_before)),
            xlsx_path=str(xlsx_path),
            planned_link_backfills=plan.link_plan_count,
            planned_secondary_marks=plan.marker_plan_count,
            skipped_link_rows=len(plan.link_skipped),
            stats=plan.stats_before.__dict__,
        )
        return 0

    if not plan.link_plans and not plan.marker_records:
        emit(
            "final",
            "complete",
            f"二次推送完成。「{project}」\n没有需要写入 Base 的社媒链接或二次推送标记。\n"
            + "\n".join(secondary_push_final_count_lines(plan.stats_before)),
            xlsx_path=str(xlsx_path),
            planned_link_backfills=0,
            planned_secondary_marks=0,
            skipped_link_rows=len(plan.link_skipped),
            stats=plan.stats_before.__dict__,
        )
        return 0

    try:
        result = secondary_push.execute_plan(ctx, plan)
        write_text_log(log_dir, job_id, "03_secondary_execute", "\n".join(result.lines) + "\n")
    except Exception as exc:
        emit("error", "secondary_write", f"二次推送写入失败：\n{safe_tail(str(exc))}")
        return 1

    emit(
        "final",
        "complete",
        f"二次推送完成。「{project}」\n"
        f"已回填社媒链接：{result.link_written} 条\n"
        f"跳过 XLSX/链接项：{len(plan.link_skipped)} 条\n"
        + "\n".join(secondary_push_final_count_lines(result.stats_after)),
        xlsx_path=str(xlsx_path),
        link_written=result.link_written,
        marker_written=result.marker_written,
        skipped_link_rows=len(plan.link_skipped),
        stats=result.stats_after.__dict__,
    )
    return 0


def today_publish_base_token(args: argparse.Namespace) -> str:
    value = (
        args.target_base_token
        or args.base_token
        or env_value("TODAY_PUBLISH_BASE_TOKEN")
        or env_value("FEISHU_BASE_TOKEN")
        or TODAY_PUBLISH_DEFAULT_BASE_TOKEN
    )
    if not value:
        raise RuntimeError("Set TODAY_PUBLISH_BASE_TOKEN (or FEISHU_BASE_TOKEN) in workflow/env/bases.env.")
    return value


def today_publish_source_table_id() -> str:
    return env_value("TODAY_PUBLISH_SOURCE_TABLE_ID", TODAY_PUBLISH_SOURCE_TABLE_ID)


def today_publish_views() -> tuple[tuple[str, str], ...]:
    env_keys = (
        "TODAY_PUBLISH_VIEW_RETURNING_ID",
        "TODAY_PUBLISH_VIEW_REGION_ID",
        "TODAY_PUBLISH_VIEW_NEWCOMER_ID",
        "TODAY_PUBLISH_VIEW_30D_ID",
    )
    return tuple(
        (label, env_value(env_key, default_view_id))
        for (label, default_view_id), env_key in zip(TODAY_PUBLISH_VIEWS, env_keys)
    )


def today_publish_target_table_id(args: argparse.Namespace) -> str:
    return args.target_table_id or env_value("TODAY_PUBLISH_TARGET_TABLE_ID", TODAY_PUBLISH_TARGET_TABLE_ID)


def today_publish_identity() -> str:
    return env_value("TODAY_PUBLISH_IDENTITY", env_value("FEISHU_IDENTITY", "bot"))


def first_cell(row: Any) -> str:
    if isinstance(row, list) and row:
        return str(row[0] or "").strip()
    return ""


def read_today_publish_accounts(base_token: str, identity: str, workdir: Path) -> tuple[list[str], dict[str, int]]:
    source_table = today_publish_source_table_id()
    accounts: list[str] = []
    counts: dict[str, int] = {}
    for label, view_id in today_publish_views():
        offset = 0
        items: list[str] = []
        while True:
            payload = run_lark_json(
                [
                    "base",
                    "+record-list",
                    "--base-token",
                    base_token,
                    "--table-id",
                    source_table,
                    "--view-id",
                    view_id,
                    "--field-id",
                    TODAY_PUBLISH_SOURCE_ACCOUNT_FIELD_ID,
                    "--limit",
                    "200",
                    "--offset",
                    str(offset),
                    "--as",
                    identity,
                    "--format",
                    "json",
                ],
                cwd=workdir,
            )
            rows = payload.get("data", {}).get("data", [])
            if not isinstance(rows, list):
                rows = []
            for row in rows:
                value = first_cell(row)
                if value:
                    items.append(value)
            offset += len(rows)
            if not payload.get("data", {}).get("has_more") or not rows:
                break
        counts[label] = len(items)
        accounts.extend(items)
    return accounts, counts


def today_publish_count_lines(counts: dict[str, int]) -> str:
    return "\n".join(f"{label}：{counts.get(label, 0)}" for label, _view_id in today_publish_views())


def auto_number_sort_key(value: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", value or "")
    return (int(match.group(1)) if match else 0, value or "")


@dataclass(frozen=True)
class TodayPublishBounds:
    last_date_row: dict[str, Any]
    separator_index: int
    start_index: int


class TodayPublishInsufficientRows(RuntimeError):
    def __init__(self, required: int, start_index: int, available: int) -> None:
        self.required = required
        self.start_index = start_index
        self.start_row_1_based = start_index + 1
        self.available = max(0, available)
        self.shortage = max(0, required - self.available)
        super().__init__(
            f"素材推送表空行不足：需要 {required} 行，从 {self.start_row_1_based} 行开始。"
        )


def read_today_publish_target_rows(base_token: str, table_id: str, identity: str, workdir: Path) -> list[dict[str, Any]]:
    rows_out: list[dict[str, Any]] = []
    offset = 0
    fields = (
        TODAY_PUBLISH_TARGET_AUTO_FIELD_ID,
        TODAY_PUBLISH_TARGET_DATE_FIELD_ID,
        TODAY_PUBLISH_TARGET_ACCOUNT_FIELD_ID,
    )
    while True:
        argv = [
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
        ]
        for field_id in fields:
            argv.extend(["--field-id", field_id])
        payload = run_lark_json(argv, cwd=workdir)
        data = payload.get("data", {})
        rows = data.get("data", [])
        record_ids = data.get("record_id_list", [])
        if not isinstance(rows, list):
            rows = []
        if not isinstance(record_ids, list):
            record_ids = []
        for row, record_id in zip(rows, record_ids):
            cells = list(row) if isinstance(row, list) else []
            while len(cells) < 3:
                cells.append(None)
            auto_id = str(cells[0] or "").strip()
            rows_out.append(
                {
                    "auto_id": auto_id,
                    "sort_key": auto_number_sort_key(auto_id),
                    "date": str(cells[1] or "").strip(),
                    "account": str(cells[2] or "").strip(),
                    "record_id": str(record_id or "").strip(),
                }
            )
        offset += len(rows)
        if not data.get("has_more") or not rows:
            break

    rows_out.sort(key=lambda item: item["sort_key"])
    for index, row in enumerate(rows_out):
        row["index"] = index
    return rows_out


def find_today_publish_bounds(target_rows: list[dict[str, Any]]) -> TodayPublishBounds:
    dated_rows = [row for row in target_rows if row["date"]]
    if not dated_rows:
        raise RuntimeError("素材推送表中没有找到 Date 有值的记录，无法定位写入起点。")
    last_date_row = dated_rows[-1]
    separator_index = int(last_date_row["index"]) + 1
    start_index = int(last_date_row["index"]) + 2
    return TodayPublishBounds(
        last_date_row=last_date_row,
        separator_index=separator_index,
        start_index=start_index,
    )


def build_today_publish_plan(
    accounts: list[str],
    target_rows: list[dict[str, Any]],
    bounds: TodayPublishBounds | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bounds = bounds or find_today_publish_bounds(target_rows)
    last_date_row = bounds.last_date_row
    separator_index = bounds.separator_index
    start_index = bounds.start_index

    planned: list[dict[str, Any]] = []
    skipped_existing: list[dict[str, Any]] = []
    account_index = 0
    scan_index = start_index
    while account_index < len(accounts) and scan_index < len(target_rows):
        target = target_rows[scan_index]
        if target["account"]:
            skipped_existing.append(
                {
                    "auto_id": target["auto_id"],
                    "record_id": target["record_id"],
                    "existing_account": target["account"],
                }
            )
            scan_index += 1
            continue

        account = accounts[account_index]
        planned.append(
            {
                "idx": account_index + 1,
                "account": account,
                "auto_id": target["auto_id"],
                "record_id": target["record_id"],
                "existing_account": target["account"],
            }
        )
        account_index += 1
        scan_index += 1

    if account_index < len(accounts):
        raise TodayPublishInsufficientRows(len(accounts), start_index, len(planned))

    separator = target_rows[separator_index] if separator_index < len(target_rows) else None
    plan_meta = {
        "last_date_auto": last_date_row["auto_id"],
        "separator_auto": separator["auto_id"] if separator else "",
        "start_auto": planned[0]["auto_id"] if planned else "",
        "end_auto": planned[-1]["auto_id"] if planned else "",
        "skipped_existing_count": len(skipped_existing),
        "skipped_existing": skipped_existing[:20],
    }
    return plan_meta, planned


def summarize_today_publish_slots(
    target_rows: list[dict[str, Any]],
    bounds: TodayPublishBounds,
) -> dict[str, Any]:
    rows = target_rows[bounds.start_index :]
    occupied = [row for row in rows if row["account"]]
    empty = [row for row in rows if not row["account"]]
    return {
        "available_empty": len(empty),
        "skipped_existing_count": len(occupied),
        "skipped_existing": [
            {
                "auto_id": row["auto_id"],
                "record_id": row["record_id"],
                "existing_account": row["account"],
            }
            for row in occupied[:20]
        ],
    }


def today_publish_created_record_count(payload: Any, requested: int) -> int:
    if not isinstance(payload, dict):
        return requested
    data = payload.get("data")
    if not isinstance(data, dict):
        return requested
    for key in ("record_id_list", "record_ids", "records"):
        values = data.get(key)
        if isinstance(values, list):
            return min(len(values), requested)
    return requested


def create_today_publish_blank_rows(
    base_token: str,
    table_id: str,
    identity: str,
    workdir: Path,
    count: int,
    write_limiter: Any | None = None,
) -> int:
    if count <= 0:
        return 0
    helper = import_backfill_module()
    records = [
        {"fields": {TODAY_PUBLISH_TARGET_ACCOUNT_FIELD_ID: None}}
        for _ in range(count)
    ]
    helper.batch_create_records(
        workdir,
        identity,
        base_token,
        table_id,
        records,
        write_limiter=write_limiter,
    )
    return count


def ensure_today_publish_plan(
    accounts: list[str],
    target_rows: list[dict[str, Any]],
    bounds: TodayPublishBounds,
    *,
    base_token: str,
    table_id: str,
    identity: str,
    workdir: Path,
    allow_create: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    created_rows = 0
    attempts = 0
    while True:
        try:
            plan_meta, planned = build_today_publish_plan(accounts, target_rows, bounds)
            return plan_meta, planned, created_rows
        except TodayPublishInsufficientRows as exc:
            if not allow_create:
                raise
            attempts += 1
            if attempts > TODAY_PUBLISH_ROW_CREATE_RETRY_LIMIT:
                raise RuntimeError(
                    "素材推送表自动创建空行后仍不足："
                    f"需要 {exc.required} 行，可用 {exc.available} 行，"
                    f"已自动创建 {created_rows} 行。请检查 Base 排序、权限或自动编号。"
                ) from exc
            request_count = max(1, exc.shortage)
            created_rows += create_today_publish_blank_rows(
                base_token,
                table_id,
                identity,
                workdir,
                request_count,
            )
            time.sleep(TODAY_PUBLISH_ROW_CREATE_SETTLE_SECONDS)
            target_rows = read_today_publish_target_rows(base_token, table_id, identity, workdir)


def execute_today_publish_plan(
    base_token: str,
    table_id: str,
    identity: str,
    workdir: Path,
    planned: list[dict[str, Any]],
    write_limiter: Any | None = None,
) -> int:
    helper = import_backfill_module()
    records = [
        {
            "record_id": item["record_id"],
            "fields": {TODAY_PUBLISH_TARGET_ACCOUNT_FIELD_ID: item["account"]},
        }
        for item in planned
    ]
    helper.batch_update_records(
        workdir,
        identity,
        base_token,
        table_id,
        records,
        write_limiter=write_limiter,
    )
    return len(planned)


def run_today_publish_count_workflow(args: argparse.Namespace, job_id: str, workdir: Path, log_dir: Path) -> int:
    load_env_file(BACKFILL_ENV_FILE)
    os.environ["LARK_DIRECT_API"] = "1"
    base_token = today_publish_base_token(args)
    table_id = today_publish_target_table_id(args)
    identity = today_publish_identity()

    try:
        accounts, counts = read_today_publish_accounts(base_token, identity, workdir)
        total = len(accounts)
        emit(
            "progress",
            "today_count_read",
            "今日发布数读取完成。\n"
            f"{today_publish_count_lines(counts)}\n"
            f"本次需要推送的真人推文总量：{total}",
            counts=counts,
            total=total,
        )

        target_rows = read_today_publish_target_rows(base_token, table_id, identity, workdir)
        bounds = find_today_publish_bounds(target_rows)
        try:
            plan_meta, planned, created_rows = ensure_today_publish_plan(
                accounts,
                target_rows,
                bounds,
                base_token=base_token,
                table_id=table_id,
                identity=identity,
                workdir=workdir,
                allow_create=not args.dry_run,
            )
        except TodayPublishInsufficientRows as exc:
            slot_summary = summarize_today_publish_slots(target_rows, bounds)
            skipped_existing_count = int(slot_summary.get("skipped_existing_count") or 0)
            skipped_line = f"\n已跳过已有账号编号：{skipped_existing_count} 行" if skipped_existing_count else ""
            if args.dry_run:
                emit(
                    "final",
                    "dry_run",
                    "今日发布数 dry-run 完成，未写入素材推送表。\n"
                    f"{today_publish_count_lines(counts)}\n"
                    f"本次需要推送的真人推文总量：{total}\n"
                    f"目标表空行不足：已有 {exc.available} 行，需要 {exc.required} 行\n"
                    f"正式运行将自动补齐空行，当前至少需要创建：{exc.shortage} 行"
                    f"{skipped_line}",
                    counts=counts,
                    total=total,
                    shortage=exc.shortage,
                    skipped_existing=skipped_existing_count,
                )
                return 0
            raise
        skipped_existing_count = int(plan_meta.get("skipped_existing_count") or 0)
        plan_meta["created_rows"] = created_rows
        created_line = f"\n已自动创建空行：{created_rows} 行" if created_rows else ""
        skipped_line = f"\n已跳过已有账号编号：{skipped_existing_count} 行" if skipped_existing_count else ""
        emit(
            "progress",
            "today_count_plan",
            "今日发布数预检通过。\n"
            f"最后一个 Date 有值的行：{plan_meta['last_date_auto']}\n"
            f"分隔空行：{plan_meta['separator_auto']}\n"
            f"账号编号回填区间：{plan_meta['start_auto']} - {plan_meta['end_auto']}"
            f"{created_line}"
            f"{skipped_line}",
            plan=plan_meta,
            planned=len(planned),
        )

        if args.dry_run:
            emit(
                "final",
                "dry_run",
                "今日发布数 dry-run 完成，未写入素材推送表。\n"
                f"{today_publish_count_lines(counts)}\n"
                f"本次需要推送的真人推文总量：{total}\n"
                f"账号编号回填区间：{plan_meta['start_auto']} - {plan_meta['end_auto']}"
                f"{skipped_line}",
                counts=counts,
                total=total,
                plan=plan_meta,
            )
            return 0

        written = execute_today_publish_plan(base_token, table_id, identity, workdir, planned)
        write_text_log(
            log_dir,
            job_id,
            "01_today_publish_count",
            json.dumps(
                {
                    "counts": counts,
                    "total": total,
                    "plan": plan_meta,
                    "written": written,
                    "accounts": accounts,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        emit(
            "final",
            "complete",
            "今日发布数填写已完成。",
            counts=counts,
            total=total,
            written=written,
            plan=plan_meta,
        )
        return 0
    except Exception as exc:
        emit("error", "today_count_plan", f"今日发布数失败：{exc}")
        return 2


def run_weekly_market_accounts_workflow(args: argparse.Namespace, job_id: str, workdir: Path, log_dir: Path) -> int:
    load_env_file(BACKFILL_ENV_FILE)
    load_env_file(CDP_ENV_FILE)
    os.environ["LARK_DIRECT_API"] = "1"
    try:
        module = import_weekly_account_posts_module()
    except Exception as exc:
        emit("error", "weekly_accounts", f"海外市场账号模块加载失败：{safe_tail(str(exc))}")
        return 2

    date_range = str(args.project or "").strip()
    try:
        module.parse_date_range(date_range)
    except Exception as exc:
        emit("error", "weekly_accounts", f"海外市场账号指令格式错误：{safe_tail(str(exc))}")
        return 2

    def progress(stage: str, message: str) -> None:
        emit("progress", f"weekly_{stage}", message, progress_text=message.splitlines()[0] if message else "")

    def runner(argv: list[str], timeout_s: int = 60) -> dict[str, Any]:
        return run_lark_json(argv, cwd=workdir, timeout_s=timeout_s)

    try:
        summary = module.run_weekly_account_posts(
            run_lark_json=runner,
            workdir=workdir,
            log_dir=log_dir,
            job_id=job_id,
            dry_run=args.dry_run,
            date_range=date_range,
            limit_per_platform=args.limit_per_platform,
            latest_per_account=args.latest_per_account,
            platforms=args.platforms,
            source_indexes=args.source_indexes,
            no_backfill=args.no_backfill,
            progress=progress,
        )
    except Exception as exc:
        emit("error", "weekly_accounts", f"海外市场账号失败：{safe_tail(str(exc))}")
        return 2

    lines = module.summary_lines(summary, dry_run=args.dry_run)
    write_text_log(log_dir, job_id, "01_weekly_market_accounts", "\n".join(lines) + "\n")
    if args.dry_run:
        emit(
            "final",
            "dry_run",
            "海外市场账号 dry-run 完成，未创建结果表、未抓取、未写入。\n" + "\n".join(lines),
            summary=summary.__dict__,
        )
        return 0

    emit(
        "final",
        "complete",
        "海外市场账号抓取结束。\n"
        f"回填数目：{summary.written_records if not args.no_backfill else '0（--no-backfill）'}\n"
        f"无内容账号数：{summary.no_post_accounts}\n"
        f"失效数：{summary.failed_accounts}",
        summary=summary.__dict__,
        xlsx_path=summary.local_xlsx_path,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the integrated Feishu jielong social workflow.")
    parser.add_argument("project", help="Jielong topic/project keyword.")
    parser.add_argument("--workflow-id", choices=sorted(WORKFLOW_REGISTRY), help="Registered workflow id.")
    parser.add_argument(
        "--mode",
        choices=[
            "full",
            "72h",
            "confirm-push",
            "secondary-push",
            "today-publish-count",
            "weekly-market-accounts",
            "cdp-cache-cleanup",
        ],
        default="full",
        help="Workflow mode.",
    )
    parser.add_argument("--chat-id", help="Source Feishu group chat id for jielong scan.")
    parser.add_argument("--base-url", help="Direct Base/Wiki URL; skips source group scan.")
    parser.add_argument("--base-token", help="Direct Bitable token; skips source group scan.")
    parser.add_argument("--target-base-url", help="Target Base URL for backfill; overrides env/bases.env.")
    parser.add_argument("--target-base-token", help="Target Base token for backfill; overrides env/bases.env.")
    parser.add_argument("--target-table-id", help="Target Base table id for backfill; overrides env/bases.env.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write final Base changes.")
    parser.add_argument("--no-backfill", action="store_true", help="Export and scrape only; do not touch Base.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing Base values during execute.")
    parser.add_argument("--start-row", type=int, help="Start scraping Excel rows at this row.")
    parser.add_argument("--limit-per-platform", type=int, help="For weekly_market_accounts tests: limit selected source accounts per platform.")
    parser.add_argument("--latest-per-account", type=int, help="For weekly_market_accounts diagnostics: scrape latest N posts per account without date filtering.")
    parser.add_argument("--platforms", help="For weekly_market_accounts: comma/space separated platform filter, for example x,tiktok.")
    parser.add_argument("--source-indexes", help="For weekly_market_accounts repairs: comma/space separated source row indexes, ranges allowed, for example 3,5,10-12.")
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--execute-backfill", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--strict-keyword", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        '--local-xlsx-only',
        action='store_true',
        help='Require exports/<project>.xlsx and skip the Feishu export step.',
    )
    return parser


def locate_full_social_xlsx(
    args: argparse.Namespace,
    project: str,
    job_id: str,
    workdir: Path,
    log_dir: Path,
) -> tuple[Path | None, int]:
    if args.local_xlsx_only:
        try:
            xlsx_path = require_local_xlsx_path(project, workdir)
        except Exception as exc:
            emit("error", "export", str(exc))
            return None, 1
        emit(
            "progress",
            "export",
            f"收到：直接读取本地 XLSX：{xlsx_path}   开始抓取社媒公开数据。",
            xlsx_path=str(xlsx_path),
            local_hit=True,
        )
        return xlsx_path, 0

    export_argv = [
        sys.executable,
        str(JIELONG_SCRIPT),
        "--env-file",
        str(JIELONG_ENV_FILE),
        "--keyword",
        project,
        "--overwrite",
    ]
    if args.strict_keyword and not args.base_url and not args.base_token:
        export_argv.append("--require-keyword-match")
    if args.chat_id:
        export_argv.extend(["--chat-id", args.chat_id])
    if args.base_url:
        export_argv.extend(["--base-url", args.base_url])
    if args.base_token:
        export_argv.extend(["--base-token", args.base_token])

    export_result = run_command(export_argv, cwd=workdir, timeout_s=300)
    write_job_log(log_dir, job_id, "01_export", export_result)
    if export_result.returncode != 0:
        export_error = export_result.stderr or export_result.stdout
        if "No Base or Wiki URL was found in scanned messages containing keyword" in export_error:
            emit(
                "error",
                "export",
                f"没有找到「{project}」对应的接龙，未开始抓取或回填。\n"
                + jielong_scan_scope_text()
                + "\n请确认接龙标题/关键词是否正确，或改用 source-base / --base-url 指定来源。",
                returncode=export_result.returncode,
            )
        else:
            emit("error", "export", friendly_export_error(export_error), returncode=export_result.returncode)
        return None, export_result.returncode or 1

    try:
        xlsx_path = resolve_existing_xlsx_path(
            parse_export_saved_path(export_result.stdout),
            workdir,
            workdir / "exports",
        )
    except Exception as exc:
        emit("error", "export", f"导出完成但没有读到 XLSX 路径：{exc}\n{safe_tail(export_result.stdout)}")
        return None, 1

    emit(
        "progress",
        "export",
        f"收到：接龙已导出：{xlsx_path}   开始抓取社媒公开数据。",
        xlsx_path=str(xlsx_path),
        elapsed_s=export_result.elapsed_s,
        local_hit=False,
    )
    return xlsx_path, 0


def run_workflow_body(
    args: argparse.Namespace,
    project: str,
    workflow_id: str,
    job_id: str,
    workdir: Path,
    log_dir: Path,
) -> int:
    if args.local_xlsx_only and workflow_id not in {'full_social_scrape', 'refresh_72h', 'secondary_push'}:
        emit('error', 'accepted', f'工作流 {workflow_id} 不支持本地 XLSX 快捷模式。')
        return 2

    if workflow_id == "refresh_72h":
        return run_refresh_72h_workflow(args, project, job_id, workdir, log_dir)

    if workflow_id == "confirm_push":
        return run_confirm_push_workflow(args, project, job_id, workdir, log_dir)

    if workflow_id == "secondary_push":
        return run_secondary_push_workflow(args, project, job_id, workdir, log_dir)

    if workflow_id == "today_publish_count":
        return run_today_publish_count_workflow(args, job_id, workdir, log_dir)

    if workflow_id == "weekly_market_accounts":
        return run_weekly_market_accounts_workflow(args, job_id, workdir, log_dir)

    if workflow_id != "full_social_scrape":
        emit("error", "accepted", f"未注册的工作流：{workflow_id}", workflow_id=workflow_id)
        return 2

    xlsx_path, source_returncode = locate_full_social_xlsx(args, project, job_id, workdir, log_dir)
    if xlsx_path is None:
        return source_returncode or 1

    metrics_argv = [
        sys.executable,
        str(METRICS_SCRIPT),
        "--excel",
        str(xlsx_path),
        "--use-cdp-for-x",
        "--use-cdp-for-facebook",
        "--use-cdp-for-instagram",
        "--use-cdp-for-linkedin",
        "--retry-missing-metrics",
        "--media-kind-aware-retry",
        "--stable-reel-grid-wait",
        "--ignore-anti-bot-stop",
    ]
    if args.start_row:
        metrics_argv.extend(["--start-row", str(args.start_row)])
    if args.overwrite:
        metrics_argv.append("--force")
    metrics_result = run_command_streamed(
        metrics_argv,
        cwd=workdir,
        idle_timeout_s=3600,
        on_stderr_line=make_social_metrics_progress_callback("metrics", "社媒抓取"),
    )
    write_job_log(log_dir, job_id, "02_metrics", metrics_result)
    metrics_output = metrics_result.stderr + "\n" + metrics_result.stdout
    if metrics_result.returncode != 0:
        emit("error", "metrics", f"社媒抓取失败：\n{safe_tail(metrics_output)}", returncode=metrics_result.returncode)
        return metrics_result.returncode or 1

    metrics_summary = summarize_metrics_output(metrics_output)
    if args.no_backfill:
        emit(
            "final",
            "metrics",
            "社媒数据抓取完成，XLSX 已回填。"
            + f"\n按指令不回填素材推送表。\n文件：{xlsx_path}",
            xlsx_path=str(xlsx_path),
            metrics_summary=metrics_summary.__dict__,
        )
        return 0

    emit(
        "progress",
        "metrics",
        "社媒数据抓取完成，XLSX 已回填。",
        xlsx_path=str(xlsx_path),
        metrics_summary=metrics_summary.__dict__,
    )

    backfill_base_argv = [
        sys.executable,
        str(BACKFILL_SCRIPT),
        "--env-file",
        str(BACKFILL_ENV_FILE),
        "--xlsx",
        str(xlsx_path),
        "--project",
        project,
    ]
    if args.limit_per_platform:
        backfill_base_argv.extend(["--limit-per-platform", str(args.limit_per_platform)])
    if args.target_base_url:
        backfill_base_argv.extend(["--base-url", args.target_base_url])
    if args.target_base_token:
        backfill_base_argv.extend(["--base-token", args.target_base_token])
    if args.target_table_id:
        backfill_base_argv.extend(["--table-id", args.target_table_id])

    dry_result = run_command(backfill_base_argv, cwd=workdir, timeout_s=300)
    write_job_log(log_dir, job_id, "03_backfill_dry_run", dry_result)
    dry_output = dry_result.stdout + "\n" + dry_result.stderr
    if dry_result.returncode != 0:
        emit("error", "backfill_dry_run", f"回填 dry-run 失败，未写入：\n{safe_tail(dry_output)}", returncode=dry_result.returncode)
        return dry_result.returncode or 1

    planned = parse_planned_patches(dry_output)
    valid_rows = parse_valid_rows(dry_output)
    base_records = parse_base_project_records(dry_output)
    base_skipped_rows = parse_skipped_rows(dry_output)
    metrics_skipped_items = parse_metrics_manual_review_items(metrics_output)
    base_skipped_items = parse_manual_review_skipped_items(dry_output)
    skipped_items = unique_items(metrics_skipped_items + base_skipped_items)
    if skipped_items:
        skipped_rows = len(skipped_items)
    else:
        skipped_rows = base_skipped_rows if base_skipped_rows is not None else metrics_summary.manual_review_count
    skipped_summary = skipped_summary_text(skipped_rows, skipped_items)

    if args.dry_run or not args.execute_backfill:
        emit(
            "final",
            "dry_run",
            final_full_summary_message(
                project,
                metrics_summary,
                base_records,
                valid_rows,
                skipped_rows,
                skipped_items,
                xlsx_path,
            )
            + "\n本次为 dry-run，未写入素材推送表。",
            xlsx_path=str(xlsx_path),
            planned=planned,
            valid_rows=valid_rows,
            base_records=base_records,
            skipped_rows=skipped_rows,
        )
        return 0

    if planned == 0:
        emit(
            "final",
            "complete",
            final_full_summary_message(
                project,
                metrics_summary,
                base_records,
                valid_rows,
                skipped_rows,
                skipped_items,
                xlsx_path,
            ),
            xlsx_path=str(xlsx_path),
            planned=planned,
            valid_rows=valid_rows,
            base_records=base_records,
            skipped_rows=skipped_rows,
        )
        return 0

    execute_argv = backfill_base_argv + ["--execute"]
    if args.overwrite:
        execute_argv.append("--overwrite")
    execute_result = run_command(execute_argv, cwd=workdir, timeout_s=900)
    write_job_log(log_dir, job_id, "04_backfill_execute", execute_result)
    execute_output = execute_result.stdout + "\n" + execute_result.stderr
    if execute_result.returncode != 0:
        emit("error", "backfill_execute", f"回填失败：\n{safe_tail(execute_output)}", returncode=execute_result.returncode)
        return execute_result.returncode or 1

    updated = count_updated_lines(execute_output)
    verify_result = run_command(backfill_base_argv + ["--sample", "3"], cwd=workdir, timeout_s=300)
    write_job_log(log_dir, job_id, "05_backfill_verify", verify_result)
    verify_output = verify_result.stdout + "\n" + verify_result.stderr
    if verify_result.returncode != 0:
        emit("error", "backfill_verify", f"回填复核失败：\n{safe_tail(verify_output)}", returncode=verify_result.returncode)
        return verify_result.returncode or 1

    remaining = parse_planned_patches(verify_output)
    emit(
        "final",
        "complete",
        final_full_summary_message(
            project,
            metrics_summary,
            base_records,
            valid_rows,
            skipped_rows,
            skipped_items,
            xlsx_path,
        ),
        xlsx_path=str(xlsx_path),
        planned=planned,
        updated=updated or planned,
        remaining=remaining,
        valid_rows=valid_rows,
        base_records=base_records,
        skipped_rows=skipped_rows,
        metrics_summary=metrics_summary.__dict__,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = args.project
    job_id = args.job_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    workdir = args.workdir.resolve()
    log_dir = args.log_dir.resolve()
    workflow_id = args.workflow_id or MODE_TO_WORKFLOW_ID[args.mode]
    spec = WORKFLOW_REGISTRY[workflow_id]
    args.mode = spec.mode

    if workflow_id == "cdp_cache_cleanup":
        return run_cdp_cache_cleanup_workflow(job_id, log_dir, dry_run=args.dry_run)

    def workflow_func() -> int:
        return run_workflow_body(args, project, workflow_id, job_id, workdir, log_dir)

    if workflow_id in SOCIAL_CDP_CLEANUP_WORKFLOWS:
        return run_with_silent_cdp_cleanup(workflow_func, log_dir, job_id)

    return workflow_func()


if __name__ == "__main__":
    raise SystemExit(main())
