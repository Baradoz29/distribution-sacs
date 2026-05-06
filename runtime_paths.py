from __future__ import annotations

import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_BASE_DIR = Path(getattr(sys, "_MEIPASS", PROJECT_DIR)).resolve()
APP_HOME_DIR = (Path(sys.executable).resolve().parent if IS_FROZEN else PROJECT_DIR).resolve()
STATIC_DIR = RESOURCE_BASE_DIR / "static"
BUNDLED_DATA_DIR = RESOURCE_BASE_DIR / "data"
BUNDLED_TOOL_DATA_DIR = RESOURCE_BASE_DIR / "outils" / "data"
DATA_DIR = APP_HOME_DIR / "data"
TOOL_DATA_DIR = PROJECT_DIR / "outils" / "data"


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def get_runtime_data_path(relative_path: str | Path, *, copy_if_missing: bool = False) -> Path:
    relative_path = Path(relative_path)
    target_path = DATA_DIR / relative_path

    if not copy_if_missing:
        return target_path

    ensure_data_dir()
    source_path = BUNDLED_DATA_DIR / relative_path

    if target_path.exists():
        return target_path

    if source_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source_path.resolve() != target_path.resolve():
                shutil.copy2(source_path, target_path)
        except FileNotFoundError:
            pass

    return target_path


def get_tool_data_path(relative_path: str | Path) -> Path:
    relative_path = Path(relative_path)
    bundled_path = BUNDLED_TOOL_DATA_DIR / relative_path
    if bundled_path.exists():
        return bundled_path
    return TOOL_DATA_DIR / relative_path
