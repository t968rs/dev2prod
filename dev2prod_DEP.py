#!/usr/bin/env python3
"""
dev2prod.py - Convert Python repository from development to production mode

This module handles the transition from development to production for Python projects by:
1. Copying the repository to a target location
2. Adjusting logging levels
3. Removing development artifacts
4. Configuring environment-specific settings
5. Performing other production-readiness tasks

Usage:
    from dev2prod import DevToProdConverter
    
    converter = DevToProdConverter(
        source_dir="path/to/dev/repo",
        target_dir="path/to/prod/repo",
        config={"log_level": "WARNING"}
    )
    converter.convert()
"""

import os
import re
import sys
import shutil
import logging
import argparse
import configparser
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Union, Set, Tuple


def _to_posix_lower(p: Path | str) -> PurePosixPath:
    """
    Normalize a path to a lowercase, forward-slash PurePosixPath with no leading/trailing slashes.
    Keeps only the parts; does not attempt to resolve on disk.
    """
    if not isinstance(p, Path):
        p = Path(p)
    parts = [part.lower() for part in p.parts if part not in ("/", "\\")]
    # Strip any accidental leading/trailing slashes via parts filtering above
    return PurePosixPath(*parts)


def _starts_with(a: PurePosixPath, prefix: PurePosixPath) -> bool:
    """True if 'a' path starts with 'prefix' path (component-wise, case already normalized)."""
    ap, pp = a.parts, prefix.parts
    return len(ap) >= len(pp) and ap[:len(pp)] == pp


@dataclass(frozen=True)
class ProductionTree:
    """
    NOTE on base_root:
      - All *relative* entries in `roots`, `include_files`, and `exclude_rel`
        will be resolved against `base_root` at init time.
      - For best results, pass the same folder later as `source_root` to the methods.
    """
    roots: Tuple[str, ...] = ("calcs", "reference", "scripts")
    include_files: Tuple[str, ...] = ("README.md", "LICENSE", "floodplain_mapping.atbx")
    exclude_rel: Tuple[str, ...] = (
        "scripts/_pre_tbx",
        "scripts/_admin_utils",
        "scripts/pyt_scripts",
        "reference_modules",
        "toolbox_figures",
        "results",
    )

    # Where to resolve relative inputs (default = current working directory)
    base_root: Path = field(default_factory=lambda: Path.cwd())

    # normalized caches (as Path/PurePosixPath, not strings)
    _roots_norm: Tuple[PurePosixPath, ...] = field(init=False, repr=False)
    _inc_norm: Tuple[PurePosixPath, ...]   = field(init=False, repr=False)
    _ex_norm: Tuple[PurePosixPath, ...]    = field(init=False, repr=False)

    # absolute, resolved counterparts (regular Path objects)
    _roots_abs: Tuple[Path, ...] = field(init=False, repr=False)
    _inc_abs: Tuple[Path, ...]   = field(init=False, repr=False)
    _ex_abs: Tuple[Path, ...]    = field(init=False, repr=False)

    def __post_init__(self):
        # Resolve everything to absolute Paths first (relative -> base_root)
        base = self.base_root.resolve()

        def _absify(s: str) -> Path:
            p = Path(s)
            return p if p.is_absolute() else (base / p)
        roots_abs = tuple(_absify(r).resolve() for r in self.roots)
        inc_abs   = tuple(_absify(f).resolve() for f in self.include_files)
        ex_abs    = tuple(_absify(e).resolve() for e in self.exclude_rel)

        # Build normalized (lower + posix) versions *relative to base_root*
        def _rel_norm(p: Path) -> PurePosixPath:
            try:
                rel = p.relative_to(base)
            except ValueError:
                # If not under base, fall back to a lowercase posix form of the absolute path.
                # (Won't match relative relpaths during traversal unless you also traverse from the same base.)
                rel = p
            return _to_posix_lower(rel)

        roots_norm = tuple(_rel_norm(p) for p in roots_abs)
        inc_norm   = tuple(_rel_norm(p) for p in inc_abs)
        ex_norm    = tuple(_rel_norm(p) for p in ex_abs)

        object.__setattr__(self, "_roots_abs", roots_abs)
        object.__setattr__(self, "_inc_abs", inc_abs)
        object.__setattr__(self, "_ex_abs", ex_abs)

        object.__setattr__(self, "_roots_norm", roots_norm)
        object.__setattr__(self, "_inc_norm", inc_norm)
        object.__setattr__(self, "_ex_norm", ex_norm)

    # --- helpers used during traversal ---

    @staticmethod
    def _rel(source_root: Path, p: Path) -> Path:
        return p.resolve().relative_to(source_root.resolve())

    @staticmethod
    def _norm_rel(rel: Path) -> PurePosixPath:
        """Normalized, lowercase, posix form of a *relative* path object."""
        # do not resolve here; just normalize the path parts given
        return _to_posix_lower(rel)

    def _is_under_any_root(self, rel: Path) -> bool:
        rel_norm = self._norm_rel(rel)
        # Check whether the first component of rel is one of the roots' first parts
        if not rel_norm.parts:
            return False
        first = rel_norm.parts[0]
        roots_first = {r.parts[0] for r in self._roots_norm if r.parts}
        return first in roots_first

    def _is_excluded(self, rel: Path) -> bool:
        rel_norm = self._norm_rel(rel)
        return any(rel_norm == ex or _starts_with(rel_norm, ex) for ex in self._ex_norm)

    # --- public API ---

    def allows_dir(self, source_root: Path, abs_dir: Path) -> bool:
        rel = self._rel(source_root, abs_dir)
        if len(rel.parts) == 0:
            return True  # repo root
        return self._is_under_any_root(rel) and not self._is_excluded(rel)

    def allows_file(self, source_root: Path, abs_file: Path) -> bool:
        rel = self._rel(source_root, abs_file)
        if self._is_under_any_root(rel) and not self._is_excluded(rel):
            return True
        # Allow explicitly included files by normalized relative match
        rel_norm = self._norm_rel(rel)
        return rel_norm in self._inc_norm

    def should_descend(self, source_root: Path, abs_dir: Path) -> bool:
        rel = self._rel(source_root, abs_dir)
        if len(rel.parts) == 0:
            return True
        rel_norm = self._norm_rel(rel)
        roots_first = {r.parts[0] for r in self._roots_norm if r.parts}
        return rel_norm.parts[0] in roots_first


class DevToProdConverter:
    """Convert a Python repository from development to production mode."""

    DEFAULT_CONFIG = {
        "log_level": "WARNING",  # Default production log level
        "keep_tests": False,  # Remove test directories
        "clean_pycache": True,  # Remove __pycache__ directories
        "clean_logs": True,  # Remove log files
        "clean_dot_dirs": True,  # Remove .vscode, .idea, etc.
        "adjust_logging": True,  # Update logging level in code
        "ignore_patterns": [
            r"\.git",
            r"\.github",
            r"__pycache__",
            r"\.pytest_cache",
            r"\.ipynb_checkpoints",
            r".*__tests__",
            r".*_jupyter_snippets",
            r"\.coverage",
            r"\.mypy_cache",
            r"\.tox",
            r"\.nox",
            r"^venv($|/)",
            r"^\.venv($|/)",
            r"^\.env($|/)",
            r"^_admin_utils($|/)",
            r"^_pre_tbx($|/)",
            r"^reference_modules($|/)",
            r"^pyt_scripts($|/)",
            r"^dist($|/)",
            r".*\.egg-info($|/)?",
            r".*\.pyc$",
            r".*\.pyo$",
            r".*\.log$",
            r"(^|/)logs($|/)",
            r"(^|/)temp($|/)",
            r"(^|/)tests($|/)",
            r"(^|/)\.idea($|/)",
            r"(^|/)results($|/)",
            r"(^|/)toolbox_figures($|/)",
        ],

        "include_dirs": [
            "reference",
            "scripts"
        ],
        "dot_dirs_to_clean": [  # Dot directories to remove
            ".vscode",
            ".idea",
            ".ipynb_checkpoints",
            ".mypy_cache",
            ".pytest_cache",
            "results"
        ],
        "config_files": [  # Config files to update
            "config.py",
            "settings.py",
            ".env",
            "config.ini",
            "settings.ini",
        ],
    }

    def __init__(
            self,
            source_dir: Union[str, Path],
            target_dir: Union[str, Path],
            config: Optional[Dict] = None,
    ):
        """
        Initialize the converter.
        
        Args:
            source_dir: Path to source (development) repository
            target_dir: Path to target (production) repository
            config: Optional configuration overrides
        """
        self.source_dir = Path(source_dir).resolve()
        self.target_dir = Path(target_dir).resolve()
        self.config = self.DEFAULT_CONFIG.copy()
        self.included_dirs: list[Path] = []
        self.copied = []

        if config:
            self.config.update(config)

        self.logger = self._setup_logger()

        # Build a ProductionTree from config
        self.tree = ProductionTree(
                roots=tuple(self.config.get("include_dirs", [])),
                include_files=tuple(self.config.get("include_files", [])),
                exclude_rel=tuple(self.config.get("exclude", [])),
        )

    @staticmethod
    def _setup_logger() -> logging.Logger:
        """Set up and return a logger for the converter."""
        logger = logging.getLogger("dev2prod")
        logger.setLevel(logging.INFO)

        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        return logger

    def convert( self ) -> bool:
        """
        Convert the repository from development to production mode.
        Returns:
            bool: True if conversion was successful
        """
        try:
            self.logger.info(f"Starting conversion from {self.source_dir} to {self.target_dir}")

            # Ensure target directory exists or create it
            self.target_dir.mkdir(parents=True, exist_ok=True)

            # Copy repository with filtering
            self._copy_repository()
            self.logger.info(f'Copied {len(self.copied)} files to \n{self.target_dir}')

            # Clean up development artifacts
            if self.config["clean_pycache"]:
                self._clean_pycache()

            if self.config["clean_logs"]:
                self._clean_logs()

            if self.config["clean_dot_dirs"]:
                self._clean_dot_dirs()

            if not self.config["keep_tests"]:
                self._remove_tests()

            # Adjust settings for production
            if self.config["adjust_logging"]:
                self._adjust_logging_levels()

            # Update configuration files
            self._update_config_files()

            self.logger.info("Conversion completed successfully")
            return True

        except Exception as e:
            self.logger.error(f"Conversion failed: {str(e)}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False

    def _copy_repository( self ) -> None:
        self.logger.info("Copying repository files...")

        # case-insensitive regex
        ignore_patterns = [re.compile(p, re.IGNORECASE) for p in self.config["ignore_patterns"]]

        def norm_rel( p: Path ) -> str:
            return str(p.resolve().relative_to(self.source_dir)).replace("\\", "/").strip("/").lower()

        def should_ignore( path: Path ) -> bool:
            rel = norm_rel(path)
            return any(p.search(rel) for p in ignore_patterns)

        for root, dirs, files in os.walk(self.source_dir):
            root_path = Path(root)

            # prune traversal: only descend where both descend+allows_dir are true and not ignored
            dirs[:] = [
                d for d in dirs
                if not should_ignore(root_path / d)
                   and self.tree.should_descend(self.source_dir, root_path / d)
                   and self.tree.allows_dir(self.source_dir, root_path / d)
            ]

            for file in files:
                src = root_path / file
                if should_ignore(src):
                    continue
                if not self.tree.allows_file(self.source_dir, src):
                    continue
                rel = src.resolve().relative_to(self.source_dir)
                dst = self.target_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                self.copied.append(str(rel))


    def _clean_pycache( self ) -> None:
        """Remove __pycache__ directories from target repository."""
        self.logger.info("Cleaning __pycache__ directories...")
        self._remove_directories("__pycache__")

    def _clean_logs( self ) -> None:
        """Remove log files from target repository."""
        self.logger.info("Cleaning log files...")
        for log_file in self.target_dir.glob("**/*.log"):
            try:
                log_file.unlink()
            except (PermissionError, OSError) as e:
                self.logger.warning(f"Could not remove log file {log_file}: {str(e)}")

    def _clean_dot_dirs( self ) -> None:
        """Remove development dot directories (.vscode, .idea, etc.)."""
        self.logger.info("Cleaning development dot directories...")
        for dot_dir in self.config["dot_dirs_to_clean"]:
            self._remove_directories(dot_dir)

    def _remove_tests( self ) -> None:
        """Remove test directories from target repository."""
        self.logger.info("Removing test directories...")
        test_dirs = ["tests", "test"]
        for test_dir in test_dirs:
            self._remove_directories(test_dir)

    def _remove_directories( self, dir_name: str ) -> None:
        """Remove directories with the specified name from target repository using bottom-up approach."""
        for directory in self.target_dir.glob(f"**/{dir_name}"):
            if directory.is_dir():
                try:
                    # Bottom-up deletion approach: first delete all files
                    for file_path in directory.glob("**/*"):
                        if file_path.is_file():
                            try:
                                file_path.unlink()
                                self.logger.debug(f"Deleted file: {file_path}")
                            except (PermissionError, OSError) as e:
                                self.logger.warning(f"Could not remove file {file_path}: {str(e)}")

                    # Then delete empty directories bottom-up
                    for dir_path in sorted(directory.glob("**/*"), key=lambda x: len(str(x)), reverse=True):
                        if dir_path.is_dir():
                            try:
                                dir_path.rmdir()  # Only removes empty directories
                                self.logger.debug(f"Deleted directory: {dir_path}")
                            except (PermissionError, OSError) as e:
                                self.logger.warning(f"Could not remove directory {dir_path}: {str(e)}")

                    # Finally try to remove the root directory
                    try:
                        directory.rmdir()
                        self.logger.debug(f"Deleted root directory: {directory}")
                    except (PermissionError, OSError) as e:
                        self.logger.warning(f"Could not remove root directory {directory}: {str(e)}")

                except Exception as e:
                    self.logger.warning(f"Error during bottom-up deletion of {directory}: {str(e)}")

    def _adjust_logging_levels( self ) -> None:
        """Adjust logging levels in Python files to production level."""
        self.logger.info(f"Adjusting logging levels to {self.config['log_level']}...")

        prod_level = self.config["log_level"]
        log_level_num = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}.get(prod_level, 30)

        # Patterns to find logging level settings
        patterns = [
            # Standard logging patterns
            (r"logging\.basicConfig\(\s*.*?level\s*=\s*logging\.DEBUG",
             f"logging.basicConfig(level=logging.{prod_level}"),
            (r"setLevel\(\s*logging\.DEBUG\s*\)", f"setLevel(logging.{prod_level})"),
            (r"setLevel\(\s*logging\.INFO\s*\)", f"setLevel(logging.{prod_level})"),
            (r"setLevel\(\s*['\"]DEBUG['\"]\s*\)", f"setLevel('{prod_level}')"),
            (r"setLevel\(\s*['\"]INFO['\"]\s*\)", f"setLevel('{prod_level}')"),
            (r"LOG_LEVEL\s*=\s*['\"]DEBUG['\"]", f"LOG_LEVEL = '{prod_level}'"),
            (r"LOG_LEVEL\s*=\s*['\"]INFO['\"]", f"LOG_LEVEL = '{prod_level}'"),
            (r"LOG_LEVEL\s*=\s*logging\.DEBUG", f"LOG_LEVEL = logging.{prod_level}"),
            (r"LOG_LEVEL\s*=\s*logging\.INFO", f"LOG_LEVEL = logging.{prod_level}"),

            # Custom patterns for your specific logging style
            (r"DEBUG\s*=\s*True", "DEBUG = False"),
            (r"level=10(\s*if\s*DEBUG\s*else\s*\d+)", f"level={log_level_num}"),
            (r"level=20", f"level={log_level_num}"),
            (r"level=10", f"level={log_level_num}"),
            # Direct level specification without DEBUG flag - fixed the backref issue
            (r"getalogger\(\s*(.*?),\s*level=10", lambda m: f"getalogger({m.group(1)}, level={log_level_num}"),
            (r"getalogger\(\s*(.*?),\s*level=20", lambda m: f"getalogger({m.group(1)}, level={log_level_num}"),
        ]

        # Skip files that should not be modified
        skip_files = ["logging.py"]

        # Replace logging levels in Python files
        for py_file in self.target_dir.glob("**/*.py"):
            if py_file.name.lower() in skip_files:
                self.logger.info(f"Skipping logging module: {py_file}")
                continue
            self._replace_in_file(py_file, patterns)

    def _update_config_files( self ) -> None:
        """Update configuration files for production settings."""
        self.logger.info("Updating configuration files...")

        # Look for known configuration files
        for config_file_name in self.config["config_files"]:
            self.logger.info(f"Config: {config_file_name}...")
            for config_file in self.target_dir.glob(f"**/{config_file_name}"):
                self._process_config_file(config_file)

    def _process_config_file( self, config_file: Path ) -> None:
        """Process a configuration file based on its type."""
        extension = config_file.suffix.lower()

        # Handle different config file types
        if extension == ".py":
            patterns = [
                (r"DEBUG\s*=\s*True", "DEBUG = False"),
                (r"DEVELOPMENT(_MODE)?\s*=\s*True", r"DEVELOPMENT\1 = False"),
                (r"ENV\s*=\s*['\"]development['\"]", "ENV = 'production'"),
                (r"ENVIRONMENT\s*=\s*['\"]dev(elopment)?['\"]", "ENVIRONMENT = 'production'"),
                # Add more Python config patterns here
            ]
            self._replace_in_file(config_file, patterns)

        elif extension == ".ini" or config_file.name.endswith(".ini"):
            self._update_ini_config(config_file)

        elif config_file.name == ".env":
            patterns = [
                (r"DEBUG\s*=\s*True", "DEBUG=False"),
                (r"ENV\s*=\s*dev(elopment)?", "ENV=production"),
                (r"ENVIRONMENT\s*=\s*dev(elopment)?", "ENVIRONMENT=production"),
                (r"LOG_LEVEL\s*=\s*DEBUG", f"LOG_LEVEL={self.config['log_level']}"),
                (r"LOG_LEVEL\s*=\s*INFO", f"LOG_LEVEL={self.config['log_level']}"),
                # Add more .env patterns here
            ]
            self._replace_in_file(config_file, patterns)

        # Add more config file types as needed

    def _update_ini_config( self, config_file: Path ) -> None:
        """Update INI-style configuration files."""
        try:
            config = configparser.ConfigParser()
            config.read(config_file)

            # Update common configuration sections
            for section in config.sections():
                # Look for common development settings
                if "debug" in config[section]:
                    config[section]["debug"] = "False"

                if "log_level" in config[section]:
                    config[section]["log_level"] = self.config["log_level"]

                if "environment" in config[section]:
                    config[section]["environment"] = "production"

                if "env" in config[section]:
                    config[section]["env"] = "production"

            # Write updated config back to file
            with open(config_file, "w") as f:
                # noinspection PyTypeChecker
                config.write(f)

        except Exception as e:
            self.logger.warning(f"Error updating INI config {config_file}: {str(e)}")

    def _replace_in_file( self, file_path: Path, patterns: List[tuple] ) -> None:
        """
        Replace patterns in a file.
        
        Args:
            file_path: Path to the file
            patterns: List of (regex_pattern, replacement) tuples where replacement
                     can be a string or a function that takes a match object
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            modified = False

            for pattern, replacement in patterns:
                if callable(replacement):
                    # Function-based replacement
                    content_new = re.sub(pattern, replacement, content)
                    if content_new != content:
                        content = content_new
                        modified = True
                else:
                    # String-based replacement
                    new_content, count = re.subn(pattern, replacement, content)
                    if count > 0:
                        content = new_content
                        modified = True

            if modified:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)

        except Exception as e:
            self.logger.warning(f"Error processing file {file_path}: {str(e)}")


def main():
    """Command line entry point."""
    parser = argparse.ArgumentParser(description="Convert Python repository from development to production mode")
    parser.add_argument("source", help="Source (development) repository path")
    parser.add_argument("target", help="Target (production) repository path")
    parser.add_argument("--log-level", default="WARNING", help="Production log level")
    parser.add_argument("--keep-tests", action="store_true", help="Keep test directories")
    parser.add_argument("--no-clean-pycache", action="store_true", help="Don't clean __pycache__ directories")
    parser.add_argument("--no-clean-logs", action="store_true", help="Don't clean log files")
    parser.add_argument("--no-clean-dot-dirs", action="store_true", help="Don't clean dot directories (.vscode, etc.)")
    parser.add_argument("--no-adjust-logging", action="store_true", help="Don't adjust logging levels")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    args = parser.parse_args()

    # Configure based on command line arguments
    config = {
        "log_level": args.log_level,
        "keep_tests": args.keep_tests,
        "clean_pycache": not args.no_clean_pycache,
        "clean_logs": not args.no_clean_logs,
        "clean_dot_dirs": not args.no_clean_dot_dirs,
        "adjust_logging": not args.no_adjust_logging,
    }

    # Set up converter
    converter = DevToProdConverter(args.source, args.target, config)

    # Set logger level based on verbosity
    if args.verbose:
        converter.logger.setLevel(logging.DEBUG)

    # Run conversion
    success = converter.convert()
    print(f'Success: {"Yes" if success else "No"}')

    # Exit with appropriate status
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

    # test = Path(r"E:\automation\Production_Convert\fp_dev_04\scripts\_pre_tbx")
    # prod_tree = ProductionTree()
    # print("allows_dir?", prod_tree.allows_dir(Path(r"E:\automation\Production_Convert\fp_dev_04"), test))  # should be False
