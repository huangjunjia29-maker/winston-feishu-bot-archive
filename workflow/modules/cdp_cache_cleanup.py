#!/usr/bin/env python3
"""Clear Chrome CDP cache files while preserving login/session state."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


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

WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CDP_PROFILE = WORKFLOW_ROOT / "assets" / "chrome-cdp-profile"

# Allowlist only cache-like Chrome artifacts. Do not add Cookies, Local Storage,
# IndexedDB, Session Storage, Login Data, Preferences, or History here.
CACHE_RELATIVE_TARGETS = (
    "ShaderCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    "GPUPersistentCache",
    "component_crx_cache",
    "extensions_crx_cache",
    "BrowserMetrics",
    "DeferredBrowserMetrics",
    "Crashpad/reports",
    "Crashpad/attachments",
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/Media Cache",
    "Default/Network/Cache",
    "Default/Service Worker/CacheStorage",
    "Default/Service Worker/ScriptCache",
    "Default/Shared Dictionary",
)


@dataclass
class TargetCleanupStats:
    path: str
    exists: bool = False
    removed_files: int = 0
    removed_dirs: int = 0
    removed_bytes: int = 0
    skipped_files: int = 0
    skipped_dirs: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ProfileCleanupStats:
    profile: str
    valid_profile: bool
    dry_run: bool = False
    targets: list[TargetCleanupStats] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def removed_files(self) -> int:
        return sum(target.removed_files for target in self.targets)

    @property
    def removed_dirs(self) -> int:
        return sum(target.removed_dirs for target in self.targets)

    @property
    def removed_bytes(self) -> int:
        return sum(target.removed_bytes for target in self.targets)

    @property
    def skipped_files(self) -> int:
        return sum(target.skipped_files for target in self.targets)

    @property
    def skipped_dirs(self) -> int:
        return sum(target.skipped_dirs for target in self.targets)

    @property
    def error_count(self) -> int:
        return sum(len(target.errors) for target in self.targets)


@dataclass
class CleanupReport:
    profiles: list[ProfileCleanupStats]
    dry_run: bool = False

    @property
    def removed_files(self) -> int:
        return sum(profile.removed_files for profile in self.profiles)

    @property
    def removed_dirs(self) -> int:
        return sum(profile.removed_dirs for profile in self.profiles)

    @property
    def removed_bytes(self) -> int:
        return sum(profile.removed_bytes for profile in self.profiles)

    @property
    def skipped_files(self) -> int:
        return sum(profile.skipped_files for profile in self.profiles)

    @property
    def skipped_dirs(self) -> int:
        return sum(profile.skipped_dirs for profile in self.profiles)

    @property
    def error_count(self) -> int:
        return sum(profile.error_count for profile in self.profiles)


def human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GiB"


def normalize_profile_path(path: Path) -> Path:
    return path.expanduser().resolve()


def unique_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = normalize_profile_path(path)
        key = str(resolved).casefold() if os.name == "nt" else str(resolved)
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


def configured_cdp_profiles(default_profile: Path = DEFAULT_CDP_PROFILE) -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("SOCIAL_CDP_PROFILE", "WEEKLY_ACCOUNT_CDP_PROFILE", "WEEKLY_ACCOUNT_X_CDP_PROFILE"):
        raw = os.getenv(env_name, "").strip()
        if raw:
            candidates.append(Path(raw))
    candidates.append(default_profile)
    return unique_paths(candidates)


def looks_like_chrome_profile(profile: Path) -> bool:
    if not profile.exists() or not profile.is_dir():
        return False
    markers = (
        profile / "Default",
        profile / "Local State",
        profile / "First Run",
    )
    return profile.name == "chrome-cdp-profile" or any(marker.exists() for marker in markers)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def walk_target(path: Path) -> tuple[list[Path], list[Path]]:
    if not path.exists():
        return [], []
    if path.is_symlink() or path.is_file():
        return [path], []

    files: list[Path] = []
    dirs: list[Path] = []
    for root, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
        root_path = Path(root)
        for filename in filenames:
            files.append(root_path / filename)
        for dirname in dirnames:
            dirs.append(root_path / dirname)
    dirs.append(path)
    return files, dirs


def clean_target(path: Path, *, dry_run: bool = False) -> TargetCleanupStats:
    stats = TargetCleanupStats(path=str(path), exists=path.exists())
    if not stats.exists:
        return stats

    files, dirs = walk_target(path)
    sizes = {file_path: file_size(file_path) for file_path in files}
    if dry_run:
        stats.removed_files = len(files)
        stats.removed_dirs = len(dirs)
        stats.removed_bytes = sum(sizes.values())
        return stats

    for file_path in files:
        try:
            file_path.unlink()
            stats.removed_files += 1
            stats.removed_bytes += sizes.get(file_path, 0)
        except FileNotFoundError:
            continue
        except OSError as exc:
            stats.skipped_files += 1
            stats.errors.append(f"file: {file_path}: {exc}")

    for dir_path in dirs:
        try:
            dir_path.rmdir()
            stats.removed_dirs += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            stats.skipped_dirs += 1
            stats.errors.append(f"dir: {dir_path}: {exc}")

    return stats


def clean_cdp_profile(
    profile: Path,
    *,
    dry_run: bool = False,
    relative_targets: Iterable[str] = CACHE_RELATIVE_TARGETS,
) -> ProfileCleanupStats:
    profile = normalize_profile_path(profile)
    valid = looks_like_chrome_profile(profile)
    stats = ProfileCleanupStats(profile=str(profile), valid_profile=valid, dry_run=dry_run)
    if not valid:
        stats.warnings.append("Skipped: path is not an existing Chrome CDP profile.")
        return stats

    for relative in relative_targets:
        stats.targets.append(clean_target(profile / relative, dry_run=dry_run))
    return stats


def clean_cdp_profiles(profiles: Iterable[Path] | None = None, *, dry_run: bool = False) -> CleanupReport:
    selected = list(profiles) if profiles is not None else configured_cdp_profiles()
    return CleanupReport(
        profiles=[clean_cdp_profile(profile, dry_run=dry_run) for profile in unique_paths(selected)],
        dry_run=dry_run,
    )


def format_cleanup_report(report: CleanupReport) -> str:
    mode = "DRY-RUN" if report.dry_run else "EXECUTE"
    lines = [
        f"F010 CDP cache cleanup {mode}",
        f"removed_files={report.removed_files}",
        f"removed_dirs={report.removed_dirs}",
        f"removed_bytes={report.removed_bytes} ({human_bytes(report.removed_bytes)})",
        f"skipped_files={report.skipped_files}",
        f"skipped_dirs={report.skipped_dirs}",
        f"errors={report.error_count}",
    ]
    for profile in report.profiles:
        lines.append("")
        lines.append(f"profile={profile.profile}")
        lines.append(f"valid_profile={profile.valid_profile}")
        if profile.warnings:
            for warning in profile.warnings:
                lines.append(f"warning={warning}")
        lines.append(
            "profile_removed="
            f"files:{profile.removed_files} dirs:{profile.removed_dirs} bytes:{profile.removed_bytes}"
        )
        if profile.skipped_files or profile.skipped_dirs or profile.error_count:
            lines.append(
                "profile_skipped="
                f"files:{profile.skipped_files} dirs:{profile.skipped_dirs} errors:{profile.error_count}"
            )
        for target in profile.targets:
            if not target.exists and not target.errors:
                continue
            lines.append(
                "target="
                f"{target.path} files:{target.removed_files} dirs:{target.removed_dirs} "
                f"bytes:{target.removed_bytes} skipped_files:{target.skipped_files} "
                f"skipped_dirs:{target.skipped_dirs}"
            )
            for error in target.errors[:5]:
                lines.append(f"target_error={error}")
            if len(target.errors) > 5:
                lines.append(f"target_error_more={len(target.errors) - 5}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clear cache data from the project Chrome CDP profile.")
    parser.add_argument("--profile", type=Path, action="append", help="Chrome user-data-dir to clean.")
    parser.add_argument("--dry-run", action="store_true", help="Report removable cache files without deleting them.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = clean_cdp_profiles(args.profile, dry_run=args.dry_run)
    sys.stdout.write(format_cleanup_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
