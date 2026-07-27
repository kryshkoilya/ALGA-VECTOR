"""Per-user Windows single-instance guard."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_path

_ERROR_ALREADY_EXISTS = 183


def default_mutex_name() -> str:
    config_path = user_config_path(
        appname="ALGA VECTOR",
        appauthor="Буйвол и Задира",
        roaming=False,
    )
    identity = hashlib.sha256(str(config_path).casefold().encode("utf-8")).hexdigest()[:20]
    return rf"Global\ALGA_VECTOR_{identity}"


def isolated_smoke_mutex_name(data_dir: Path) -> str:
    """Return a mutex scoped to one explicit headless-smoke data directory.

    Production launches still use the per-user global mutex.  Build and CI
    smokes may coexist with a running operator instance only when they use a
    separate explicit data directory, so they cannot share journals or config.
    """

    resolved = data_dir.expanduser().resolve(strict=False)
    identity = hashlib.sha256(str(resolved).casefold().encode("utf-8")).hexdigest()[:20]
    return rf"Local\ALGA_VECTOR_SMOKE_{identity}"


@dataclass(slots=True)
class SingleInstanceGuard:
    """Keep the SQLite/config/log writer unique for the current Windows user."""

    name: str = ""
    acquired: bool = False
    _handle: int | None = None

    def __enter__(self) -> SingleInstanceGuard:
        if os.name != "nt":
            self.acquired = True
            return self

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE
        mutex_name = self.name or default_mutex_name()
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, mutex_name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = int(handle)
        self.acquired = ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
        if not self.acquired:
            self.close()
        return self

    def close(self) -> None:
        if self._handle is None or os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        close_handle(self._handle)
        self._handle = None

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = [
    "SingleInstanceGuard",
    "default_mutex_name",
    "isolated_smoke_mutex_name",
]
