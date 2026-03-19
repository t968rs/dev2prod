import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Project paths (from .env)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectPaths:
    source_root:    Path
    dest_root:      Path


# ---------------------------------------------------------------------------
# Config models (no path knowledge)
# ---------------------------------------------------------------------------

class FolderSpec(BaseModel):
    """
    Inclusion/exclusion rules for a single production folder.

    allowed:  Glob patterns matched against relative file paths within the folder.
              Empty list means allow everything not denied.
    denied:   Glob patterns that unconditionally exclude a file or directory,
              even if it matches allowed. Evaluated before allowed.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    required: bool = True
    allowed: List[str] = Field(default_factory=list)
    denied: List[str] = Field(default_factory=list)


class ProdConfig(BaseModel):
    """
    Structural deployment rules loaded from a project JSON file.

    global_denied applies across all folders and top-level files.
    Patterns without "/" are matched against the filename only.
    Patterns with "/" or "**" are matched against the full relative posix path.
    """

    model_config = ConfigDict(frozen=True)

    folders: List[FolderSpec]
    global_denied: List[str] = Field(default_factory=list)
    top_level_files: List[str] = Field(default_factory=list)

    # Relative path from dest_root to the file containing _debug_override.
    debug_override_rel: str = "fpm/__init__.py"

    def __str__( self ):
        return self.model_dump_json(indent=2, ensure_ascii=True)

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "ProdConfig":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(**data)

