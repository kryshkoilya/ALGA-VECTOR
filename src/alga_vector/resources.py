"""Resolve immutable resources in source and PyInstaller builds."""

from __future__ import annotations

import sys
from pathlib import Path


def bundle_root() -> Path:
    """Return the executable extraction root or installed package root."""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root is not None:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parent


def assets_root() -> Path:
    """Return immutable assets in source, wheel, and frozen layouts."""

    root = bundle_root()
    if getattr(sys, "_MEIPASS", None) is not None:
        return root / "alga_vector" / "assets"
    return root / "assets"


def asset_path(*parts: str) -> Path:
    return assets_root().joinpath(*parts)


def default_config_path() -> Path:
    """Return the bundled, read-only default configuration."""

    return asset_path("config", "default.yaml")


__all__ = ["asset_path", "assets_root", "bundle_root", "default_config_path"]
