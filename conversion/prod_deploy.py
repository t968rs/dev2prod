"""
prod_deploy.py - Production deployment config and file copier for fpm projects.

Project definition layout:
    project_definition/
        <project>.json   -- folder specs and file filter patterns
        <project>.env    -- SOURCE_ROOT and DEST_ROOT paths

Usage:
    cfg, paths = load_project("fbs")
    result = ProdCopier(cfg, paths, dry_run=True).run()
"""

from __future__ import annotations

import fnmatch
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from conversion.project_model import ProjectPaths, ProdConfig, FolderSpec


_HERE_DIR = Path(__file__).parent.resolve()
_PROJECT_DEF_DIR = _HERE_DIR.parent / "project_definitions"


# ------ Loading Helpers ---------------------------------------------------

def _parse_env(env_path: Path) -> dict:
    """
    Parse a simple KEY=VALUE env file.
    Ignores blank lines and lines starting with #.
    Strips surrounding quotes from values.
    """
    result: dict = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip("\"'")
    return result


def _load_paths(env_path: Path) -> ProjectPaths:
    data = _parse_env(env_path)
    missing = [k for k in ("SOURCE_ROOT", "DEST_ROOT") if k not in data]
    if missing:
        raise KeyError(f"missing keys in {env_path}: {missing}")
    return ProjectPaths(
        source_root=Path(data["SOURCE_ROOT"]),
        dest_root=Path(data["DEST_ROOT"]),
    )



# ---------------------------------------------------------------------------
# Project loader
# ---------------------------------------------------------------------------

def load_project(
    name: str,
    project_def_dir: Optional[Path] = None,
) -> Tuple[ProdConfig, ProjectPaths]:
    """
    Load config and paths for a named project.

    Resolves both files from project_def_dir (default: project_definition/
    next to this file):
        <name>.json
        <name>.env
    """
    base = project_def_dir or _PROJECT_DEF_DIR
    cfg = ProdConfig.from_json(base / f"{name}.json")
    paths = _load_paths(base / f"{name}.env")
    return cfg, paths


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_source(config: ProdConfig, paths: ProjectPaths) -> List[str]:
    """Returns one error string per missing required folder."""
    errors: List[str] = []
    for spec in config.folders:
        candidate = paths.source_root / spec.name
        if spec.required and not candidate.is_dir():
            errors.append(f"required folder absent: {candidate}")
    return errors


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CopyResult:
    copied: Tuple[Path, ...]
    skipped: Tuple[Path, ...]
    patched: Tuple[Path, ...]
    errors: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


# ---------------------------------------------------------------------------
# Pattern helpers
# ---------------------------------------------------------------------------

def _matches(name: str, rel_posix: str, pattern: str) -> bool:
    if "/" in pattern or "**" in pattern:
        return fnmatch.fnmatch(rel_posix, pattern)
    return fnmatch.fnmatch(name, pattern)


def _is_denied(
    name: str,
    rel_posix: str,
    local_denied: List[str],
    global_denied: List[str],
) -> bool:
    return any(
        _matches(name, rel_posix, p)
        for p in local_denied + global_denied
    )


def _is_allowed(name: str, rel_posix: str, allowed: List[str]) -> bool:
    if not allowed:
        return True
    return any(_matches(name, rel_posix, p) for p in allowed)


# ---------------------------------------------------------------------------
# Recursive walk (prunes denied directories before descending)
# ---------------------------------------------------------------------------

def _walk_folder(
    folder_root: Path,
    local_denied: List[str],
    global_denied: List[str],
    allowed: List[str],
) -> List[Path]:
    acc: List[Path] = []
    _recurse(folder_root, folder_root, local_denied, global_denied, allowed, acc)
    return acc


def _recurse(
    dir_path: Path,
    root: Path,
    local_denied: List[str],
    global_denied: List[str],
    allowed: List[str],
    acc: List[Path],
) -> None:
    for item in sorted(dir_path.iterdir()):
        rel_posix = item.relative_to(root).as_posix()
        if item.is_dir():
            if not _is_denied(item.name, rel_posix, local_denied, global_denied):
                _recurse(item, root, local_denied, global_denied, allowed, acc)
        elif item.is_file():
            if (
                not _is_denied(item.name, rel_posix, local_denied, global_denied)
                and _is_allowed(item.name, rel_posix, allowed)
            ):
                acc.append(item)


# ---------------------------------------------------------------------------
# Copier
# ---------------------------------------------------------------------------

class ProdCopier:
    """
    Copies production files from source to dest per a ProdConfig + ProjectPaths.

    dry_run:    Walk and filter without writing anything.
    clean_dest: Delete dest_root before copying (ensures no stale files).
    """

    _DEBUG_PATTERN = re.compile(r"(_debug_override\s*=\s*)True")

    def __init__(
        self,
        config: ProdConfig,
        paths: ProjectPaths,
        dry_run: bool = False,
        clean_dest: bool = False,
    ) -> None:
        self._cfg = config
        self._paths = paths
        self._dry_run = dry_run
        self._clean_dest = clean_dest

    def run(self) -> CopyResult:
        errors = validate_source(self._cfg, self._paths)
        if errors:
            return CopyResult((), (), (), tuple(errors))

        if self._clean_dest and not self._dry_run:
            if self._paths.dest_root.exists():
                shutil.rmtree(self._paths.dest_root)

        copied: List[Path] = []
        skipped: List[Path] = []
        patched: List[Path] = []
        errors_acc: List[str] = []

        for spec in self._cfg.folders:
            c, s, e = self._copy_folder(spec)
            copied.extend(c)
            skipped.extend(s)
            errors_acc.extend(e)

        c, s, e = self._copy_top_level()
        copied.extend(c)
        skipped.extend(s)
        errors_acc.extend(e)

        p, e = self._patch_debug_override()
        patched.extend(p)
        errors_acc.extend(e)

        return CopyResult(
            tuple(copied), tuple(skipped), tuple(patched), tuple(errors_acc)
        )

    def _copy_folder(
        self, spec: FolderSpec
    ) -> Tuple[List[Path], List[Path], List[str]]:
        src_root = self._paths.source_root / spec.name
        files = _walk_folder(
            src_root,
            list(spec.denied),
            list(self._cfg.global_denied),
            list(spec.allowed),
        )
        copied: List[Path] = []
        skipped: List[Path] = []
        errors: List[str] = []
        for src_file in files:
            rel = src_file.relative_to(self._paths.source_root)
            dst = self._paths.dest_root / rel
            ok, err = self._copy_file(src_file, dst)
            if err:
                errors.append(err)
                print(f'ERROR: {err}')
            elif ok:
                copied.append(dst)
            else:
                skipped.append(src_file)
        return copied, skipped, errors

    def _copy_top_level(self) -> Tuple[List[Path], List[Path], List[str]]:
        copied: List[Path] = []
        skipped: List[Path] = []
        errors: List[str] = []
        for item in sorted(self._paths.source_root.iterdir()):
            if not item.is_file():
                continue
            if not any(fnmatch.fnmatch(item.name, p) for p in self._cfg.top_level_files):
                skipped.append(item)
                continue
            if _is_denied(item.name, item.name, [], list(self._cfg.global_denied)):
                skipped.append(item)
                continue
            dst = self._paths.dest_root / item.name
            ok, err = self._copy_file(item, dst)
            if err:
                errors.append(err)
            elif ok:
                copied.append(dst)
            else:
                skipped.append(item)
        return copied, skipped, errors

    def _patch_debug_override(self) -> Tuple[List[Path], List[str]]:
        patched: List[Path] = []
        errors: List[str] = []
        target = self._paths.dest_root / self._cfg.debug_override_rel
        if not target.is_file() and not self._dry_run:
            errors.append(f"debug_override_rel not found in dest: {target}")
            return patched, errors
        elif self._dry_run:
            return patched, errors
        try:
            content = target.read_text(encoding="utf-8")
            new_content, count = self._DEBUG_PATTERN.subn(r"\g<1>False", content)
            if count:
                if not self._dry_run:
                    target.write_text(new_content, encoding="utf-8")
                patched.append(target)
        except Exception as exc:
            errors.append(f"patch failed {target}: {exc}")
        return patched, errors

    def _copy_file(self, src: Path, dst: Path) -> Tuple[bool, Optional[str]]:
        if self._dry_run:
            return True, None
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return True, None
        except Exception as exc:
            return False, f"copy failed {src} -> {dst}: {exc}"
