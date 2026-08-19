#!/usr/bin/env python3
"""Confirm push workflow helper for Feishu Base records."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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

MODULE_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = MODULE_ROOT.parent
BASES_ENV_FILE = SKILL_ROOT / "env" / "bases.env"
BACKFILL_SCRIPT = MODULE_ROOT / "base_backfill.py"

PROJECT_FIELD = "project"
OWNER_FIELD = "owner"
PUSH_FIELD = "推送通知"
STATUS_FIELD = "通知状态"
AUTO_FIELD = "自动素材编号"
CONFIRM_TEXT = "确认推送"
NOTIFIED_TEXT = "已通知"
DEFAULT_WAIT_SECONDS = 60

TEXT_LIKE_TYPES = {"text", "url", "phone", "email", "barcode"}
SINGLE_SELECT_TYPES = {"select", "single_select", "singleSelect", "single-select"}
MULTI_SELECT_TYPES = {"multi_select", "multiSelect", "multi-select"}


@dataclass
class ConfirmPushContext:
    module: Any
    skill_root: Path
    identity: str
    base_token: str
    table_id: str
    fields_by_name: dict[str, Any]
    project_field: Any
    owner_field: Any
    push_field: Any
    status_field: Any
    auto_field: Any | None = None


@dataclass
class ConfirmPushRecord:
    record_id: str
    auto_id: str
    owner: str
    push_value: str
    status_value: str


@dataclass
class ConfirmPushPlan:
    project: str
    total_project_records: int
    candidates: list[ConfirmPushRecord] = field(default_factory=list)
    skipped_owner_blank: list[ConfirmPushRecord] = field(default_factory=list)
    skipped_push_existing: list[ConfirmPushRecord] = field(default_factory=list)

    @property
    def ready_count(self) -> int:
        return len(self.candidates)


@dataclass
class ConfirmPushWriteResult:
    written: list[ConfirmPushRecord]
    lines: list[str]


@dataclass
class ConfirmPushVerifyResult:
    notified: list[ConfirmPushRecord]
    pending: list[ConfirmPushRecord]
    missing_record_ids: list[str]


def import_backfill_module() -> Any:
    spec = importlib.util.spec_from_file_location("feishu_confirm_push_base_helper", BACKFILL_SCRIPT)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import Base helper: {BACKFILL_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def resolve_field_case_insensitive(fields_by_name: dict[str, Any], expected: str) -> Any:
    expected_key = expected.strip().casefold()
    matches = [
        field_info
        for name, field_info in fields_by_name.items()
        if str(name).strip().casefold() == expected_key
    ]
    if not matches:
        raise RuntimeError(f"Base missing required field: {expected}")
    if len(matches) > 1:
        names = ", ".join(field.name for field in matches)
        raise RuntimeError(f"Base field name is not unique ignoring case for {expected}: {names}")
    return matches[0]


def normalize(module: Any, value: Any) -> str:
    return module.normalize_text(value)


def is_blank(module: Any, value: Any) -> bool:
    return module.is_blank(value)


def field_write_value(field_info: Any, value: str) -> Any:
    field_type = str(getattr(field_info, "type", "") or "")
    if field_type in TEXT_LIKE_TYPES or field_type in SINGLE_SELECT_TYPES:
        return value
    if field_type in MULTI_SELECT_TYPES:
        return [value]
    raise RuntimeError(
        f"Field {getattr(field_info, 'name', PUSH_FIELD)} has unsupported writable type: {field_type}"
    )


def load_context(
    *,
    env_file: Path = BASES_ENV_FILE,
    target_base_url: str | None = None,
    target_base_token: str | None = None,
    target_table_id: str | None = None,
) -> ConfirmPushContext:
    helper = import_backfill_module()
    helper.load_env_file(str(env_file))

    base_url = target_base_url or os.getenv("FEISHU_BASE_URL", "")
    parsed_base_url = helper.parse_base_url(base_url) if base_url else {}
    base_token = target_base_token or os.getenv("FEISHU_BASE_TOKEN", "") or parsed_base_url.get("base_token", "")
    table_id = target_table_id or os.getenv("FEISHU_TABLE_ID", "") or parsed_base_url.get("table_id", "")
    if not base_token:
        raise RuntimeError(f"Set FEISHU_BASE_URL or FEISHU_BASE_TOKEN in {env_file}.")
    if not table_id:
        raise RuntimeError(f"Set FEISHU_BASE_URL with ?table=... or set FEISHU_TABLE_ID in {env_file}.")

    identity = os.getenv("FEISHU_IDENTITY", "bot")
    fields_by_name = helper.get_fields(SKILL_ROOT, identity, base_token, table_id)
    project_field = resolve_field_case_insensitive(fields_by_name, PROJECT_FIELD)
    owner_field = resolve_field_case_insensitive(fields_by_name, OWNER_FIELD)
    push_field = resolve_field_case_insensitive(fields_by_name, PUSH_FIELD)
    status_field = resolve_field_case_insensitive(fields_by_name, STATUS_FIELD)
    try:
        auto_field = resolve_field_case_insensitive(fields_by_name, AUTO_FIELD)
    except RuntimeError:
        auto_field = None

    return ConfirmPushContext(
        module=helper,
        skill_root=SKILL_ROOT,
        identity=identity,
        base_token=base_token,
        table_id=table_id,
        fields_by_name=fields_by_name,
        project_field=project_field,
        owner_field=owner_field,
        push_field=push_field,
        status_field=status_field,
        auto_field=auto_field,
    )


def search_project_records(ctx: ConfirmPushContext, project: str) -> list[Any]:
    select_fields = [ctx.project_field, ctx.owner_field, ctx.push_field, ctx.status_field]
    if ctx.auto_field:
        select_fields.insert(0, ctx.auto_field)
    return ctx.module.search_project_records(
        ctx.skill_root,
        ctx.identity,
        ctx.base_token,
        ctx.table_id,
        project,
        ctx.project_field,
        select_fields,
    )


def record_summary(ctx: ConfirmPushContext, record: Any) -> ConfirmPushRecord:
    auto_id = record.record_id
    if ctx.auto_field:
        auto_id = normalize(ctx.module, record.values.get(ctx.auto_field.id)) or record.record_id
    return ConfirmPushRecord(
        record_id=record.record_id,
        auto_id=auto_id,
        owner=normalize(ctx.module, record.values.get(ctx.owner_field.id)),
        push_value=normalize(ctx.module, record.values.get(ctx.push_field.id)),
        status_value=normalize(ctx.module, record.values.get(ctx.status_field.id)),
    )


def build_plan(ctx: ConfirmPushContext, project: str) -> ConfirmPushPlan:
    records = search_project_records(ctx, project)
    plan = ConfirmPushPlan(project=project, total_project_records=len(records))
    for record in records:
        summary = record_summary(ctx, record)
        if not summary.owner:
            plan.skipped_owner_blank.append(summary)
        elif summary.push_value:
            plan.skipped_push_existing.append(summary)
        else:
            plan.candidates.append(summary)
    return plan


def execute_confirm_push(ctx: ConfirmPushContext, candidates: list[ConfirmPushRecord]) -> ConfirmPushWriteResult:
    patch_value = field_write_value(ctx.push_field, CONFIRM_TEXT)
    records = [
        {"record_id": record.record_id, "fields": {ctx.push_field.id: patch_value}}
        for record in candidates
    ]
    ctx.module.update_records_individually(
        ctx.identity,
        ctx.base_token,
        ctx.table_id,
        records,
    )
    lines = [
        f"[{index}/{len(candidates)}] confirmed {record.auto_id} {record.owner}"
        for index, record in enumerate(candidates, 1)
    ]
    return ConfirmPushWriteResult(written=list(candidates), lines=lines)


def verify_notification_status(
    ctx: ConfirmPushContext,
    project: str,
    written: list[ConfirmPushRecord],
) -> ConfirmPushVerifyResult:
    current_records = search_project_records(ctx, project)
    current_by_id = {record.record_id: record_summary(ctx, record) for record in current_records}
    notified: list[ConfirmPushRecord] = []
    pending: list[ConfirmPushRecord] = []
    missing: list[str] = []
    for record in written:
        current = current_by_id.get(record.record_id)
        if current is None:
            missing.append(record.record_id)
        elif current.status_value == NOTIFIED_TEXT:
            notified.append(current)
        else:
            pending.append(current)
    return ConfirmPushVerifyResult(notified=notified, pending=pending, missing_record_ids=missing)


def plan_lines(plan: ConfirmPushPlan) -> list[str]:
    return [
        f"project: {plan.project}",
        f"base project records: {plan.total_project_records}",
        f"ready to confirm push: {plan.ready_count}",
        f"skipped blank owner: {len(plan.skipped_owner_blank)}",
        f"skipped existing push notice: {len(plan.skipped_push_existing)}",
    ]


def run_cli(args: argparse.Namespace) -> int:
    ctx = load_context(
        env_file=Path(args.env_file),
        target_base_url=args.target_base_url,
        target_base_token=args.target_base_token,
        target_table_id=args.target_table_id,
    )
    plan = build_plan(ctx, args.project)
    print("Confirm push plan")
    print("\n".join(plan_lines(plan)))
    if not args.execute:
        print("Dry-run only. Re-run with --execute to write confirmation.")
        return 0
    if not plan.candidates:
        print("No records need confirmation.")
        return 0
    write_result = execute_confirm_push(ctx, plan.candidates)
    print("\n".join(write_result.lines))
    if args.wait_seconds > 0:
        print(f"Waiting {args.wait_seconds}s before verification.")
        time.sleep(args.wait_seconds)
    verify = verify_notification_status(ctx, args.project, write_result.written)
    print(f"notified: {len(verify.notified)}")
    print(f"pending manual check: {len(verify.pending) + len(verify.missing_record_ids)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Confirm push notices for Feishu Base project records.")
    parser.add_argument("project")
    parser.add_argument("--env-file", default=str(BASES_ENV_FILE))
    parser.add_argument("--target-base-url")
    parser.add_argument("--target-base-token")
    parser.add_argument("--target-table-id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_cli(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
