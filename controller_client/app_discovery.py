from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.7


@dataclass(frozen=True)
class AppCandidate:
    display_name: str
    exec_path: str


def compute_match_score(query: str, candidate: AppCandidate) -> float:
    q = query.lower()
    display = candidate.display_name.lower()
    exec_name = Path(candidate.exec_path).stem.lower()

    if q == display:
        return 1.0
    if q == exec_name:
        return 0.95
    if display.startswith(q):
        return 0.9
    if q in display:
        coverage = len(q) / len(display) if display else 0.0
        return 0.7 + 0.2 * coverage
    if q in exec_name:
        coverage = len(q) / len(exec_name) if exec_name else 0.0
        return 0.5 + 0.2 * coverage
    return 0.0


def find_best_match(
    query: str, candidates: list[AppCandidate]
) -> tuple[AppCandidate | None, float]:
    best: AppCandidate | None = None
    best_score = 0.0

    for candidate in candidates:
        score = compute_match_score(query, candidate)
        if score > best_score:
            best_score = score
            best = candidate

    return best, best_score


def discover_apps() -> list[AppCandidate]:
    system = platform.system()
    if system == "Darwin":
        return _discover_macos_apps()
    if system == "Windows":
        return _discover_windows_apps()
    return _discover_linux_apps()


def _discover_macos_apps() -> list[AppCandidate]:
    candidates: list[AppCandidate] = []
    seen_paths: set[str] = set()

    for app_dir in ("/Applications", str(Path.home() / "Applications")):
        _scan_macos_app_dir(Path(app_dir), candidates, seen_paths)

    _supplement_macos_with_mdfind(candidates, seen_paths)
    return candidates


def _scan_macos_app_dir(
    directory: Path,
    candidates: list[AppCandidate],
    seen_paths: set[str],
) -> None:
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.suffix == ".app" and entry.is_dir():
            path_str = str(entry)
            if path_str not in seen_paths:
                seen_paths.add(path_str)
                candidates.append(AppCandidate(entry.stem, path_str))


def _supplement_macos_with_mdfind(
    candidates: list[AppCandidate],
    seen_paths: set[str],
) -> None:
    try:
        result = subprocess.run(
            ["mdfind", "kMDItemKind == 'Application'"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line.endswith(".app") and line not in seen_paths:
                seen_paths.add(line)
                name = Path(line).stem
                candidates.append(AppCandidate(name, line))
    except (subprocess.TimeoutExpired, OSError):
        logger.debug("mdfind supplemental discovery failed", exc_info=True)


def _discover_linux_apps() -> list[AppCandidate]:
    candidates: list[AppCandidate] = []
    seen_names: set[str] = set()

    desktop_dirs = [
        Path("/usr/share/applications"),
        Path.home() / ".local" / "share" / "applications",
    ]

    for d in desktop_dirs:
        _scan_desktop_files(d, candidates, seen_names)

    return candidates


def _scan_desktop_files(
    directory: Path,
    candidates: list[AppCandidate],
    seen_names: set[str],
) -> None:
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.suffix != ".desktop" or not entry.is_file():
            continue
        parsed = _parse_desktop_file(entry)
        if parsed is not None and parsed.display_name not in seen_names:
            seen_names.add(parsed.display_name)
            candidates.append(parsed)


def _parse_desktop_file(path: Path) -> AppCandidate | None:
    name: str | None = None
    exec_cmd: str | None = None
    no_display = False

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("Name=") and name is None:
                    name = stripped[5:]
                elif stripped.startswith("Exec=") and exec_cmd is None:
                    exec_cmd = stripped[5:]
                elif stripped == "NoDisplay=true":
                    no_display = True
    except OSError:
        return None

    if no_display or not name or not exec_cmd:
        return None

    return AppCandidate(name, exec_cmd)


def _discover_windows_apps() -> list[AppCandidate]:
    candidates: list[AppCandidate] = []
    seen_names: set[str] = set()

    _discover_windows_start_apps(candidates, seen_names)
    _scan_windows_start_menu(candidates, seen_names)
    _scan_windows_desktop(candidates, seen_names)
    _scan_windows_program_files(candidates, seen_names)

    return candidates


def _discover_windows_start_apps(
    candidates: list[AppCandidate],
    seen_names: set[str],
) -> None:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-StartApps | ConvertTo-Json",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return

        apps = json.loads(result.stdout)
        if isinstance(apps, dict):
            apps = [apps]
        for app in apps:
            name = app.get("Name", "")
            app_id = app.get("AppID", "")
            if name and app_id and name not in seen_names:
                seen_names.add(name)
                candidates.append(AppCandidate(name, app_id))
    except (subprocess.TimeoutExpired, OSError, ValueError):
        logger.warning("PowerShell Get-StartApps failed", exc_info=True)


def _scan_windows_start_menu(
    candidates: list[AppCandidate],
    seen_names: set[str],
) -> None:
    start_menu_dirs = [
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs",
        Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData"))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs",
    ]
    for d in start_menu_dirs:
        _scan_lnk_dir(d, candidates, seen_names)


def _scan_windows_desktop(
    candidates: list[AppCandidate],
    seen_names: set[str],
) -> None:
    desktop_dirs = [
        Path.home() / "Desktop",
        Path(os.environ.get("PUBLIC", "C:\\Users\\Public")) / "Desktop",
    ]
    for d in desktop_dirs:
        _scan_lnk_dir(d, candidates, seen_names)


def _scan_lnk_dir(
    directory: Path,
    candidates: list[AppCandidate],
    seen_names: set[str],
) -> None:
    if not directory.is_dir():
        return
    for entry in directory.rglob("*.lnk"):
        name = entry.stem
        if name not in seen_names:
            seen_names.add(name)
            candidates.append(AppCandidate(name, str(entry)))


def _scan_windows_program_files(
    candidates: list[AppCandidate],
    seen_names: set[str],
) -> None:
    program_dirs = [
        Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")),
        Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")),
    ]
    for d in program_dirs:
        if not d.is_dir():
            continue
        _scan_exe_dir(d, candidates, seen_names, max_depth=2, current_depth=0)


def _scan_exe_dir(
    directory: Path,
    candidates: list[AppCandidate],
    seen_names: set[str],
    max_depth: int,
    current_depth: int,
) -> None:
    if current_depth > max_depth:
        return
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".exe":
                name = entry.stem
                if name not in seen_names:
                    seen_names.add(name)
                    candidates.append(AppCandidate(name, str(entry)))
            elif entry.is_dir() and current_depth < max_depth:
                _scan_exe_dir(
                    entry, candidates, seen_names, max_depth, current_depth + 1
                )
    except OSError:
        pass
