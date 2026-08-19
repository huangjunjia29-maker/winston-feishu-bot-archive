#!/usr/bin/env python3
"""Find a Feishu group jielong Base link and export it as xlsx.

This script delegates auth and API calls to lark-cli. Configure lark-cli once
with your app id/secret, then run this script from Codex or a terminal.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


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

URL_RE = re.compile(r"https?://[^\s\"'<>\\]+")
TRAILING_URL_CHARS = ".,;:!?)]}" + "\uff0c\u3002\uff1b\uff1a\uff01\uff1f\uff09\u3011"
WINDOWS_FILENAME_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Candidate:
    url: str
    token: str
    doc_type: str
    message_id: str = ""
    create_time: str = ""
    keyword_match: bool = False
    table_id: str = ""
    view_id: str = ""


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
        if not key:
            continue
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

    script_path = Path(__file__).resolve()
    return str(script_path.parent.parent / ".env")


def lark_cli_bin() -> str:
    configured = os.getenv("LARK_CLI_BIN", "lark-cli").strip() or "lark-cli"
    resolved = shutil.which(configured)
    if resolved:
        return resolved
    for candidate in ("lark-cli.cmd", "lark-cli.exe", "lark-cli.ps1"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return configured


def run_lark(args: list[str]) -> Any:
    cmd = [lark_cli_bin()]
    profile = os.getenv("FEISHU_LARK_PROFILE") or os.getenv("LARK_CLI_PROFILE")
    if profile:
        cmd.extend(["--profile", profile])
    cmd.extend(args)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=utf8_child_env(),
    )
    if proc.returncode != 0:
        joined = " ".join(cmd)
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {joined}\n"
            f"STDERR:\n{proc.stderr.strip()}\n"
            f"STDOUT:\n{proc.stdout.strip()}"
        )

    stdout = proc.stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"raw": stdout}


def is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or "xxx" in lowered or lowered.startswith("cli_x")


def setup_lark_cli_from_env() -> dict[str, Any]:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if is_placeholder(app_id) or is_placeholder(app_secret):
        raise RuntimeError(
            "FEISHU_APP_ID and FEISHU_APP_SECRET must be filled before setup."
        )

    cmd = [
        lark_cli_bin(),
        "config",
        "init",
        "--app-id",
        app_id,
        "--app-secret-stdin",
        "--brand",
        os.getenv("FEISHU_BRAND", "feishu"),
    ]
    profile = os.getenv("FEISHU_LARK_PROFILE", "").strip()
    if profile:
        cmd.extend(["--name", profile])

    proc = subprocess.run(
        cmd,
        input=app_secret + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=utf8_child_env(),
    )
    if proc.returncode != 0:
        safe_cmd = " ".join(cmd)
        raise RuntimeError(
            f"lark-cli setup failed ({proc.returncode}): {safe_cmd}\n"
            f"STDERR:\n{proc.stderr.strip()}\n"
            f"STDOUT:\n{proc.stdout.strip()}"
        )
    return {"configured": True, "profile": profile or "default"}


def deep_strings(value: Any, seen: set[str] | None = None) -> list[str]:
    if seen is None:
        seen = set()
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
        s = value.strip()
        if s and s not in seen and s[:1] in "[{":
            seen.add(s)
            try:
                out.extend(deep_strings(json.loads(s), seen))
            except json.JSONDecodeError:
                pass
        return out
    if isinstance(value, dict):
        for item in value.values():
            out.extend(deep_strings(item, seen))
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(deep_strings(item, seen))
    return out


def extract_urls(value: Any) -> list[str]:
    urls: list[str] = []
    for text in deep_strings(value):
        for match in URL_RE.findall(text):
            urls.append(match.rstrip(TRAILING_URL_CHARS))
    return urls


def parse_doc_url(url: str) -> tuple[str, str, str, str] | None:
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)

    for index, part in enumerate(parts):
        if part in {"base", "bitable"} and index + 1 < len(parts):
            return (
                "bitable",
                parts[index + 1],
                query.get("table", [""])[0],
                query.get("view", [""])[0],
            )
        if part == "wiki" and index + 1 < len(parts):
            return ("wiki", parts[index + 1], "", "")
    return None


def find_list_by_key(value: Any, keys: set[str]) -> list[Any] | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, list):
                return item
        for item in value.values():
            found = find_list_by_key(item, keys)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = find_list_by_key(item, keys)
            if found is not None:
                return found
    return None


def message_list(data: Any) -> list[dict[str, Any]]:
    messages = find_list_by_key(data, {"messages", "items"})
    if not messages:
        return []
    return [msg for msg in messages if isinstance(msg, dict)]


def has_more(data: Any) -> bool:
    if isinstance(data, dict):
        if data.get("has_more") is True:
            return True
        for item in data.values():
            if has_more(item):
                return True
    return False


def next_page_token(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("page_token", "next_page_token"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        for item in data.values():
            found = next_page_token(item)
            if found:
                return found
    return ""


def import_lark_api_helper() -> Any:
    module_name = "export_jielong_lark_api"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = Path(__file__).resolve().parent / "base_backfill.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot import cached-TAT API helper: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def scan_messages(
    chat_id: str,
    identity: str,
    start: str,
    end: str,
    pages: int,
) -> list[dict[str, Any]]:
    helper = import_lark_api_helper()
    api_path = "/open-apis/im/v1/messages"
    params: dict[str, Any] = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "page_size": 50,
        "sort_type": "ByCreateTimeDesc",
    }
    if start:
        params["start_time"] = start
    if end:
        params["end_time"] = end
    all_messages: list[dict[str, Any]] = []
    page_token = ""
    for _ in range(max(1, pages)):
        if page_token:
            params["page_token"] = page_token
        payload = helper.request_lark_api("GET", api_path, params=params)
        data = payload.get("data") or {}
        items = data.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError("Feishu message-list response has invalid items.")
        all_messages.extend(item for item in items if isinstance(item, dict))
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "")
        if not page_token:
            raise RuntimeError("Feishu message-list omitted page_token while has_more=true.")
    return all_messages


def normalize_keyword(value: str) -> str:
    """Ignore separators users commonly add when typing a card title."""
    return re.sub(r"[_\s]+", "", value)


def collect_candidates(messages: list[dict[str, Any]], keyword: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    normalized_keyword = normalize_keyword(keyword)
    for msg in messages:
        strings = deep_strings(msg)
        text_blob = "\n".join(strings)
        keyword_match = bool(
            normalized_keyword
            and normalized_keyword in normalize_keyword(text_blob)
        )
        for url in extract_urls(msg):
            parsed = parse_doc_url(url)
            if not parsed:
                continue
            doc_type, token, table_id, view_id = parsed
            candidates.append(
                Candidate(
                    url=url,
                    token=token,
                    doc_type=doc_type,
                    message_id=str(msg.get("message_id", "")),
                    create_time=str(msg.get("create_time", "")),
                    keyword_match=keyword_match,
                    table_id=table_id,
                    view_id=view_id,
                )
            )
    return candidates


def resolve_wiki(candidate: Candidate, identity: str) -> Candidate:
    if candidate.doc_type != "wiki":
        return candidate

    data = run_lark(
        [
            "wiki",
            "spaces",
            "get_node",
            "--as",
            identity,
            "--params",
            json.dumps({"token": candidate.token}, ensure_ascii=True),
            "--format",
            "json",
        ]
    )
    node = data.get("node") if isinstance(data, dict) else None
    if not isinstance(node, dict):
        node = data.get("data", {}).get("node") if isinstance(data, dict) else None
    if not isinstance(node, dict):
        raise RuntimeError(f"Could not resolve wiki URL: {candidate.url}")

    obj_type = str(node.get("obj_type", ""))
    obj_token = str(node.get("obj_token", ""))
    if obj_type != "bitable" or not obj_token:
        raise RuntimeError(
            f"Wiki URL is {obj_type or 'unknown'}, not bitable: {candidate.url}"
        )

    return Candidate(
        url=candidate.url,
        token=obj_token,
        doc_type="bitable",
        message_id=candidate.message_id,
        create_time=candidate.create_time,
        keyword_match=candidate.keyword_match,
        table_id=candidate.table_id,
        view_id=candidate.view_id,
    )


def create_time_sort_value(candidate: Candidate) -> int:
    digits = "".join(ch for ch in candidate.create_time if ch.isdigit())
    return int(digits) if digits else 0


def choose_candidate(
    candidates: list[Candidate],
    prefer: str,
    require_keyword_match: bool = False,
    keyword: str = "",
) -> Candidate:
    if not candidates:
        raise RuntimeError("No Base or Wiki URL was found in scanned messages.")

    if require_keyword_match and keyword:
        candidates = [candidate for candidate in candidates if candidate.keyword_match]
        if not candidates:
            raise RuntimeError(
                "No Base or Wiki URL was found in scanned messages containing "
                f"keyword: {keyword}"
            )

    def sort_key(item: Candidate) -> tuple[bool, int, bool, bool]:
        time_value = create_time_sort_value(item)
        if prefer == "newest":
            time_value = -time_value
        return (
            not item.keyword_match,
            time_value,
            not bool(item.table_id),
            not bool(item.view_id),
        )

    candidates.sort(
        key=sort_key,
    )
    return candidates[0]


def output_dir_for_cli(output_dir: str) -> str:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    if not output_path.is_absolute():
        return output_dir

    cwd = Path.cwd().resolve()
    resolved = output_path.resolve()
    try:
        relative = resolved.relative_to(cwd)
    except ValueError as exc:
        raise RuntimeError(
            "lark-cli only allows output paths inside the current working "
            "directory; cd to the target directory or pass a relative "
            "--output-dir."
        ) from exc
    return "." if str(relative) == "." else str(relative)


def safe_xlsx_name(topic: str) -> str:
    name = WINDOWS_FILENAME_UNSAFE_RE.sub("_", topic).strip().rstrip(".")
    if not name:
        name = "jielong"
    if len(name) > 120:
        name = name[:120].rstrip(" .")
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    return name


def normalize_xlsx_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        match = re.fullmatch(r"\[(https?://[^\]]+)\]\((https?://[^)]+)\)", value.strip())
        return match.group(2) if match else value
    if isinstance(value, list):
        normalized = [normalize_xlsx_cell(item) for item in value]
        return "\n".join(str(item) for item in normalized if item != "")
    if isinstance(value, dict):
        for key in ("name", "text", "url", "link"):
            if value.get(key):
                return normalize_xlsx_cell(value[key])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def resolve_base_table(candidate: Candidate, identity: str) -> str:
    if candidate.table_id:
        return candidate.table_id
    payload = run_lark(
        [
            "base",
            "+table-list",
            "--as",
            identity,
            "--base-token",
            candidate.token,
            "--offset",
            "0",
            "--limit",
            "100",
            "--format",
            "json",
        ]
    )
    tables = payload.get("data", {}).get("tables", [])
    jielong = [item for item in tables if str(item.get("name") or "").strip() == "接龙表"]
    if len(jielong) == 1:
        return str(jielong[0].get("id") or "")
    if len(tables) == 1:
        return str(tables[0].get("id") or "")
    raise RuntimeError("Base API fallback could not determine the jielong table.")


def export_bitable_via_base_api(
    candidate: Candidate,
    identity: str,
    output_dir: str,
    file_name: str,
    overwrite: bool,
) -> dict[str, Any]:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("Base API fallback requires openpyxl.") from exc

    table_id = resolve_base_table(candidate, identity)
    offset = 0
    rows: list[list[Any]] = []
    headers: list[str] = []
    while True:
        args = [
            "base",
            "+record-list",
            "--as",
            identity,
            "--base-token",
            candidate.token,
            "--table-id",
            table_id,
            "--offset",
            str(offset),
            "--limit",
            "200",
            "--format",
            "json",
        ]
        if candidate.view_id:
            args.extend(["--view-id", candidate.view_id])
        payload = run_lark(args).get("data", {})
        page_headers = [str(item) for item in payload.get("fields", [])]
        if not headers:
            headers = page_headers
        elif page_headers != headers:
            raise RuntimeError("Base record pages returned inconsistent fields.")
        rows.extend(payload.get("data", []))
        if not payload.get("has_more"):
            break
        page_size = len(payload.get("data", []))
        if page_size <= 0:
            raise RuntimeError("Base record pagination returned no rows while has_more=true.")
        offset += page_size

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    target = output_path / (file_name or "jielong.xlsx")
    if target.exists() and not overwrite:
        raise RuntimeError(f"Output file already exists: {target}")

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "接龙表"
    worksheet.append(headers)
    for row in rows:
        worksheet.append([normalize_xlsx_cell(value) for value in row])
    workbook.save(target)
    return {
        "data": {
            "saved_path": str(target.resolve()),
            "source": "base_api_fallback",
            "table_id": table_id,
            "view_id": candidate.view_id,
            "row_count": len(rows),
        }
    }


def export_bitable(
    candidate: Candidate,
    identity: str,
    output_dir: str,
    file_name: str,
    overwrite: bool,
    dry_run: bool,
) -> Any:
    cli_output_dir = output_dir_for_cli(output_dir)
    args = [
        "drive",
        "+export",
        "--as",
        identity,
        "--token",
        candidate.token,
        "--doc-type",
        "bitable",
        "--file-extension",
        "xlsx",
        "--output-dir",
        cli_output_dir,
    ]
    if file_name:
        args.extend(["--file-name", file_name])
    if overwrite:
        args.append("--overwrite")

    if dry_run:
        return {
            "dry_run_method": "base_api",
            "base_token": candidate.token,
            "table_id": candidate.table_id,
            "view_id": candidate.view_id,
            "output_dir": output_dir,
            "file_name": file_name,
        }
    return export_bitable_via_base_api(
        candidate,
        identity,
        output_dir,
        file_name,
        overwrite,
    )
def main() -> int:
    env_file = preparse_env_file(sys.argv[1:])
    load_env_file(env_file)

    parser = argparse.ArgumentParser(
        description="Export the latest Feishu group jielong Base as xlsx."
    )
    parser.add_argument("--env-file", default=env_file)
    parser.add_argument(
        "--setup-cli",
        action="store_true",
        default=str_to_bool(os.getenv("FEISHU_SETUP_LARK_CLI", "")),
        help="configure lark-cli from FEISHU_APP_ID/FEISHU_APP_SECRET before export",
    )
    parser.add_argument("--chat-id", default=os.getenv("FEISHU_CHAT_ID", ""))
    parser.add_argument("--base-url", default=os.getenv("FEISHU_BASE_URL", ""))
    parser.add_argument("--base-token", default=os.getenv("FEISHU_BASE_TOKEN", ""))
    parser.add_argument("--keyword", default=os.getenv("FEISHU_JIELONG_KEYWORD", "\u63a5\u9f99"))
    parser.add_argument("--start", default=os.getenv("FEISHU_SCAN_START", ""))
    parser.add_argument("--end", default=os.getenv("FEISHU_SCAN_END", ""))
    parser.add_argument("--pages", type=int, default=int(os.getenv("FEISHU_SCAN_PAGES", "3")))
    parser.add_argument(
        "--as-identity",
        choices=["bot", "user"],
        default=os.getenv("FEISHU_IDENTITY", "bot"),
    )
    parser.add_argument("--output-dir", default=os.getenv("FEISHU_OUTPUT_DIR", "exports"))
    parser.add_argument("--file-name", default="")
    parser.add_argument(
        "--prefer",
        choices=["newest", "oldest"],
        default=os.getenv("FEISHU_PREFER", "newest"),
    )
    parser.add_argument(
        "--require-keyword-match",
        action="store_true",
        default=str_to_bool(os.getenv("FEISHU_REQUIRE_KEYWORD_MATCH", "")),
        help="fail instead of falling back to another Base link when keyword is absent",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=str_to_bool(os.getenv("FEISHU_OVERWRITE", "")),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dump-messages", default=os.getenv("FEISHU_DUMP_MESSAGES", ""))
    args = parser.parse_args()

    setup_result = None
    if args.setup_cli:
        setup_result = setup_lark_cli_from_env()

    if not args.file_name and args.keyword:
        args.file_name = safe_xlsx_name(args.keyword)
    elif not args.file_name and os.getenv("FEISHU_FILE_NAME"):
        args.file_name = os.environ["FEISHU_FILE_NAME"]

    if args.base_token:
        candidate = Candidate(url="", token=args.base_token, doc_type="bitable")
    elif args.base_url:
        parsed = parse_doc_url(args.base_url)
        if not parsed:
            raise RuntimeError("--base-url is not a recognized Base/Wiki URL")
        doc_type, token, table_id, view_id = parsed
        candidate = Candidate(args.base_url, token, doc_type, table_id=table_id, view_id=view_id)
    else:
        if not args.chat_id:
            raise RuntimeError("Pass --chat-id or set FEISHU_CHAT_ID.")
        messages = scan_messages(
            chat_id=args.chat_id,
            identity=args.as_identity,
            start=args.start,
            end=args.end,
            pages=args.pages,
        )
        if args.dump_messages:
            Path(args.dump_messages).write_text(
                json.dumps(messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        candidate = choose_candidate(
            collect_candidates(messages, args.keyword),
            prefer=args.prefer,
            require_keyword_match=args.require_keyword_match,
            keyword=args.keyword,
        )

    candidate = resolve_wiki(candidate, args.as_identity)
    result = export_bitable(
        candidate=candidate,
        identity=args.as_identity,
        output_dir=args.output_dir,
        file_name=args.file_name,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )

    print(
        json.dumps(
            {
                "selected": candidate.__dict__,
                "setup": setup_result,
                "export": result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
