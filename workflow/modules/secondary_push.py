#!/usr/bin/env python3
"""Secondary push workflow helper for Feishu Base records."""

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
SECONDARY_AUTOMATION_SETTLE_SECONDS = 10.0

PROJECT_FIELD = "project"
OWNER_FIELD = "owner"
PUBLISH_STATUS_FIELD = "发布状态"
SECONDARY_PUSH_FIELD = "二次推送"
AUTO_FIELD = "自动素材编号"
SECONDARY_PUSH_TEXT = "二次推送"

TEXT_LIKE_TYPES = {"text", "url", "phone", "email", "barcode"}
SINGLE_SELECT_TYPES = {"select", "single_select", "singleSelect", "single-select"}
MULTI_SELECT_TYPES = {"multi_select", "multiSelect", "multi-select"}


@dataclass
class SecondaryPushContext:
    module: Any
    skill_root: Path
    identity: str
    base_token: str
    table_id: str
    fields_by_name: dict[str, Any]
    project_field: Any
    owner_field: Any
    publish_status_field: Any
    secondary_push_field: Any
    platform_fields: dict[str, dict[str, Any]]
    auto_field: Any | None = None


@dataclass
class SecondaryPushStats:
    project_records: int
    owner_nonempty: int
    owner_nonempty_publish_status_nonempty: int
    secondary_push_nonempty: int


@dataclass
class SecondaryPushPlan:
    project: str
    xlsx_path: Path
    xlsx_entries: int
    link_plans: list[Any]
    link_skipped: list[str]
    marker_records: list[Any]
    marker_skipped_existing: list[Any]
    marker_skipped_owner_blank: list[Any]
    marker_skipped_planned_link: list[Any]
    stats_before: SecondaryPushStats

    @property
    def link_plan_count(self) -> int:
        return len(self.link_plans)

    @property
    def marker_plan_count(self) -> int:
        return len(self.marker_records)

    @property
    def link_plan_record_ids(self) -> set[str]:
        return {str(plan.record_id) for plan in self.link_plans}


@dataclass
class SecondaryPushExecuteResult:
    link_written: int
    marker_written: int
    stats_after: SecondaryPushStats
    lines: list[str] = field(default_factory=list)


def import_backfill_module() -> Any:
    spec = importlib.util.spec_from_file_location("feishu_secondary_push_base_helper", BACKFILL_SCRIPT)
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
        f"Field {getattr(field_info, 'name', SECONDARY_PUSH_FIELD)} has unsupported writable type: {field_type}"
    )


def load_context(
    *,
    env_file: Path = BASES_ENV_FILE,
    target_base_url: str | None = None,
    target_base_token: str | None = None,
    target_table_id: str | None = None,
) -> SecondaryPushContext:
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
    publish_status_field = resolve_field_case_insensitive(fields_by_name, PUBLISH_STATUS_FIELD)
    secondary_push_field = resolve_field_case_insensitive(fields_by_name, SECONDARY_PUSH_FIELD)
    try:
        auto_field = resolve_field_case_insensitive(fields_by_name, AUTO_FIELD)
    except RuntimeError:
        auto_field = None

    platform_fields = helper.build_field_requirements(fields_by_name)
    return SecondaryPushContext(
        module=helper,
        skill_root=SKILL_ROOT,
        identity=identity,
        base_token=base_token,
        table_id=table_id,
        fields_by_name=fields_by_name,
        project_field=project_field,
        owner_field=owner_field,
        publish_status_field=publish_status_field,
        secondary_push_field=secondary_push_field,
        platform_fields=platform_fields,
        auto_field=auto_field,
    )


def project_select_fields(ctx: SecondaryPushContext) -> list[Any]:
    fields = [
        ctx.project_field,
        ctx.owner_field,
        ctx.publish_status_field,
        ctx.secondary_push_field,
    ]
    if ctx.auto_field:
        fields.insert(0, ctx.auto_field)
    for mapping in ctx.platform_fields.values():
        for field_info in mapping.values():
            if field_info not in fields:
                fields.append(field_info)
    return fields


def search_project_records(ctx: SecondaryPushContext, project: str) -> list[Any]:
    return ctx.module.search_project_records(
        ctx.skill_root,
        ctx.identity,
        ctx.base_token,
        ctx.table_id,
        project,
        ctx.project_field,
        project_select_fields(ctx),
    )


def auto_id_for(ctx: SecondaryPushContext, record: Any) -> str:
    if ctx.auto_field:
        return normalize(ctx.module, record.values.get(ctx.auto_field.id)) or record.record_id
    return record.record_id


def stats_for_records(ctx: SecondaryPushContext, records: list[Any]) -> SecondaryPushStats:
    owner_nonempty_records = [
        record
        for record in records
        if not is_blank(ctx.module, record.values.get(ctx.owner_field.id))
    ]
    return SecondaryPushStats(
        project_records=len(records),
        owner_nonempty=len(owner_nonempty_records),
        owner_nonempty_publish_status_nonempty=sum(
            1
            for record in owner_nonempty_records
            if not is_blank(ctx.module, record.values.get(ctx.publish_status_field.id))
        ),
        secondary_push_nonempty=sum(
            1
            for record in records
            if not is_blank(ctx.module, record.values.get(ctx.secondary_push_field.id))
        ),
    )


def read_link_only_xlsx_entries(ctx: SecondaryPushContext, xlsx_path: Path) -> tuple[list[Any], dict[str, Any]]:
    helper = ctx.module
    worksheet = os.getenv("BACKFILL_WORKSHEET", "")
    link_header = os.getenv("BACKFILL_XLSX_LINK_HEADER", "接龙信息")
    owner_header = os.getenv("BACKFILL_XLSX_OWNER_HEADER", "接龙人")
    load_workbook = helper.import_openpyxl_load_workbook()
    # Feishu exports can carry a stale worksheet dimension (for example A1:A1).
    # Normal mode scans actual cells, so the 接龙人 column remains visible.
    wb = load_workbook(xlsx_path, read_only=False, data_only=True)
    ws = helper.select_xlsx_worksheet(wb, worksheet, link_header, owner_header)

    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    headers = [helper.normalize_text(value) for value in header_row]
    header_index = helper.resolve_xlsx_header_indices(headers, link_header, owner_header)
    missing = [key for key in ("link", "owner") if key not in header_index]
    if missing:
        labels = {"link": link_header, "owner": owner_header}
        raise RuntimeError("XLSX missing required column(s): " + ", ".join(labels[key] for key in missing))

    entries: list[Any] = []
    ignored_non_platform_rows: list[str] = []
    ignored_blank_owner_rows: list[str] = []
    for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        owner = helper.normalize_text(row[header_index["owner"]])
        link_cell = row[header_index["link"]]
        link = helper.extract_first_url(link_cell)
        platform = helper.platform_from_url(link)
        if not platform:
            ignored_non_platform_rows.append(
                f"row {row_num} {owner or '<blank owner>'}: No supported social URL in {link_header}: {helper.preview_text(link_cell)}"
            )
            continue
        if not owner:
            ignored_blank_owner_rows.append(f"row {row_num}: XLSX blank Owner for {link_header}: {helper.preview_text(link_cell)}")
            continue
        entries.append(helper.XlsxEntry(row_num=row_num, owner=owner, platform=platform, link=link, metrics={}))

    meta = {
        "sheet": ws.title,
        "rows": ws.max_row,
        "ignored_non_platform_rows": ignored_non_platform_rows,
        "ignored_blank_owner_rows": ignored_blank_owner_rows,
        "columns": {key: index + 1 for key, index in header_index.items()},
    }
    return entries, meta


def link_only_plans(ctx: SecondaryPushContext, xlsx_path: Path, project_records: list[Any]) -> tuple[list[Any], list[str], int]:
    entries, xlsx_meta = read_link_only_xlsx_entries(ctx, xlsx_path)
    owner_buckets = ctx.module.build_owner_record_buckets(project_records, ctx.owner_field)
    link_fields: dict[str, dict[str, Any]] = {}
    for platform, mapping in ctx.platform_fields.items():
        if "link" in mapping:
            link_fields[platform] = {"link": mapping["link"]}
    plans, skipped = ctx.module.plan_updates(
        entries=entries,
        platform_fields=link_fields,
        owner_buckets=owner_buckets,
        auto_field=ctx.auto_field,
        overwrite=False,
    )
    skipped = xlsx_meta.get("ignored_non_platform_rows", []) + xlsx_meta.get("ignored_blank_owner_rows", []) + skipped
    return plans, skipped, len(entries)


def marker_records(
    ctx: SecondaryPushContext,
    project_records: list[Any],
    *,
    exclude_record_ids: set[str] | None = None,
) -> tuple[list[Any], list[Any], list[Any], list[Any]]:
    exclude_record_ids = exclude_record_ids or set()
    to_mark: list[Any] = []
    skipped_existing: list[Any] = []
    skipped_owner_blank: list[Any] = []
    skipped_planned_link: list[Any] = []
    for record in project_records:
        if is_blank(ctx.module, record.values.get(ctx.owner_field.id)):
            skipped_owner_blank.append(record)
            continue
        if str(record.record_id) in exclude_record_ids:
            skipped_planned_link.append(record)
            continue
        if not is_blank(ctx.module, record.values.get(ctx.secondary_push_field.id)):
            skipped_existing.append(record)
            continue
        if is_blank(ctx.module, record.values.get(ctx.publish_status_field.id)):
            to_mark.append(record)
    return to_mark, skipped_existing, skipped_owner_blank, skipped_planned_link


def build_plan(ctx: SecondaryPushContext, project: str, xlsx_path: Path) -> SecondaryPushPlan:
    project_records = search_project_records(ctx, project)
    if not project_records:
        raise RuntimeError(f"No Base records found for project={project}.")
    link_plans, link_skipped, xlsx_entries = link_only_plans(ctx, xlsx_path, project_records)
    markers, marker_existing, marker_owner_blank, marker_planned_link = marker_records(
        ctx,
        project_records,
        exclude_record_ids={str(plan.record_id) for plan in link_plans},
    )
    return SecondaryPushPlan(
        project=project,
        xlsx_path=xlsx_path,
        xlsx_entries=xlsx_entries,
        link_plans=link_plans,
        link_skipped=link_skipped,
        marker_records=markers,
        marker_skipped_existing=marker_existing,
        marker_skipped_owner_blank=marker_owner_blank,
        marker_skipped_planned_link=marker_planned_link,
        stats_before=stats_for_records(ctx, project_records),
    )


def execute_link_plans(ctx: SecondaryPushContext, plans: list[Any], write_limiter: Any | None = None) -> list[str]:
    records = [
        {"record_id": plan.record_id, "fields": plan.patch_by_id}
        for plan in plans
    ]
    ctx.module.update_records_individually(
        ctx.identity,
        ctx.base_token,
        ctx.table_id,
        records,
        write_limiter=write_limiter,
    )
    return [
        f"[link {index}/{len(plans)}] updated {plan.auto_id} {plan.owner} {plan.platform}"
        for index, plan in enumerate(plans, 1)
    ]


def execute_marker_records(ctx: SecondaryPushContext, records: list[Any], write_limiter: Any | None = None) -> list[str]:
    patch_value = field_write_value(ctx.secondary_push_field, SECONDARY_PUSH_TEXT)
    patches = [
        {"record_id": record.record_id, "fields": {ctx.secondary_push_field.id: patch_value}}
        for record in records
    ]
    ctx.module.update_records_individually(
        ctx.identity,
        ctx.base_token,
        ctx.table_id,
        patches,
        write_limiter=write_limiter,
    )
    return [
        f"[marker {index}/{len(records)}] marked {auto_id_for(ctx, record)} "
        f"{normalize(ctx.module, record.values.get(ctx.owner_field.id))}"
        for index, record in enumerate(records, 1)
    ]


def execute_plan(ctx: SecondaryPushContext, plan: SecondaryPushPlan) -> SecondaryPushExecuteResult:
    lines = []
    write_limiter = ctx.module.AutomationWriteRateLimiter()
    lines.extend(execute_link_plans(ctx, plan.link_plans, write_limiter))
    project_records_after_links = search_project_records(ctx, plan.project)
    marker_candidates, _, _, _ = marker_records(
        ctx,
        project_records_after_links,
        exclude_record_ids=plan.link_plan_record_ids,
    )
    if plan.link_plans:
        lines.append(f"[marker-plan] after link backfill selected {len(marker_candidates)} secondary push marks")
    lines.extend(execute_marker_records(ctx, marker_candidates, write_limiter))
    if plan.link_plans or marker_candidates:
        lines.append(f"[automation-settle] waiting {SECONDARY_AUTOMATION_SETTLE_SECONDS:g}s before final stats")
        time.sleep(SECONDARY_AUTOMATION_SETTLE_SECONDS)
    stats_after = stats_for_records(ctx, search_project_records(ctx, plan.project))
    return SecondaryPushExecuteResult(
        link_written=len(plan.link_plans),
        marker_written=len(marker_candidates),
        stats_after=stats_after,
        lines=lines,
    )


def plan_lines(plan: SecondaryPushPlan) -> list[str]:
    return [
        f"project: {plan.project}",
        f"xlsx: {plan.xlsx_path}",
        f"xlsx valid social rows: {plan.xlsx_entries}",
        f"planned link backfills: {plan.link_plan_count}",
        f"skipped link rows/fields: {len(plan.link_skipped)}",
        f"planned secondary push marks: {plan.marker_plan_count}",
        f"skipped existing secondary push: {len(plan.marker_skipped_existing)}",
        f"skipped planned link backfill records: {len(plan.marker_skipped_planned_link)}",
        f"base owner non-empty: {plan.stats_before.owner_nonempty}",
        f"base owner non-empty and publish status non-empty: {plan.stats_before.owner_nonempty_publish_status_nonempty}",
        f"base secondary push non-empty: {plan.stats_before.secondary_push_nonempty}",
    ]


def run_cli(args: argparse.Namespace) -> int:
    ctx = load_context(
        env_file=Path(args.env_file),
        target_base_url=args.target_base_url,
        target_base_token=args.target_base_token,
        target_table_id=args.target_table_id,
    )
    plan = build_plan(ctx, args.project, Path(args.xlsx))
    print("Secondary push plan")
    print("\n".join(plan_lines(plan)))
    if not args.execute:
        print("Dry-run only. Re-run with --execute to write changes.")
        return 0
    result = execute_plan(ctx, plan)
    print("\n".join(result.lines))
    print(f"link written: {result.link_written}")
    print(f"secondary push written: {result.marker_written}")
    print(f"base owner non-empty: {result.stats_after.owner_nonempty}")
    print(f"base owner non-empty and publish status non-empty: {result.stats_after.owner_nonempty_publish_status_nonempty}")
    print(f"base secondary push non-empty: {result.stats_after.secondary_push_nonempty}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run secondary push planning and writes for a Feishu Base project.")
    parser.add_argument("project")
    parser.add_argument("--xlsx", required=True)
    parser.add_argument("--env-file", default=str(BASES_ENV_FILE))
    parser.add_argument("--target-base-url")
    parser.add_argument("--target-base-token")
    parser.add_argument("--target-table-id")
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run_cli(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
