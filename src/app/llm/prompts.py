"""Versioned prompt files under prompts/.

Each file starts with a front-matter block:
    ---
    version: 3
    ---
The version is recorded in traces and eval reports so a regression can be
tied to the prompt change that caused it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import yaml

from app.config import get_settings


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


@lru_cache(maxsize=None)
def load_prompt(name: str) -> Prompt:
    path = get_settings().prompts_dir / f"{name}.md"
    raw = path.read_text(encoding="utf-8")
    version = "0"
    if raw.startswith("---"):
        _, meta, body = raw.split("---", 2)
        version = str((yaml.safe_load(meta) or {}).get("version", "0"))
        raw = body.lstrip("\n")
    return Prompt(name=name, version=version, text=raw)
