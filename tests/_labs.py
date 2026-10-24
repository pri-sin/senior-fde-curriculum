"""Load a module's lab.py by directory name (the labs are scripts, not importable packages)."""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

MODULES = Path(__file__).resolve().parent.parent / "modules"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"lab_{name}", MODULES / name / "lab.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    for attr in vars(mod).values():  # keep test output readable: silence the lab's JSON logger
        if isinstance(attr, logging.Logger):
            attr.setLevel(logging.CRITICAL)
    return mod
