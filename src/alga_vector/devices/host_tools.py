"""Bounded subprocess boundary for vendor-provided receive-side host tools."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_MAX_STDERR_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class HostCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class HostToolError(RuntimeError):
    """Base error for the isolated host-tool boundary."""


class HostToolTimedOut(HostToolError):
    pass


class HostToolOutputTooLarge(HostToolError):
    pass


class HostTools(Protocol):
    """Injectable boundary used by discovery and the HackRF adapter."""

    def find(self, tool_name: str) -> str | None: ...

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        maximum_stdout_bytes: int,
    ) -> HostCommandResult: ...


class SubprocessHostTools:
    """Run exact executables without a shell, stdin, or unbounded runtime."""

    def find(self, tool_name: str) -> str | None:
        if not tool_name or Path(tool_name).name != tool_name:
            return None
        discovered = shutil.which(tool_name)
        if discovered:
            return str(Path(discovered).resolve())

        executable_name = tool_name
        if os.name == "nt" and not executable_name.casefold().endswith(".exe"):
            executable_name += ".exe"
        candidates = (
            Path(sys.executable).resolve().parent
            / "hardware-tools"
            / executable_name,
            Path(__file__).resolve().parents[1]
            / "hardware-tools"
            / executable_name,
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None

    def run(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float,
        maximum_stdout_bytes: int,
    ) -> HostCommandResult:
        if not command:
            raise ValueError("command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if maximum_stdout_bytes <= 0:
            raise ValueError("maximum_stdout_bytes must be positive")

        creation_flags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt"
            else 0
        )
        try:
            completed = subprocess.run(
                list(command),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
                shell=False,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise HostToolTimedOut(
                f"host tool exceeded {timeout_seconds:g} seconds"
            ) from exc
        except OSError as exc:
            raise HostToolError(f"host tool failed to start: {type(exc).__name__}") from exc

        stdout = bytes(completed.stdout)
        stderr = bytes(completed.stderr)
        if len(stdout) > maximum_stdout_bytes:
            raise HostToolOutputTooLarge(
                f"host tool stdout exceeded {maximum_stdout_bytes} bytes"
            )
        if len(stderr) > _MAX_STDERR_BYTES:
            raise HostToolOutputTooLarge(
                f"host tool stderr exceeded {_MAX_STDERR_BYTES} bytes"
            )
        return HostCommandResult(
            returncode=int(completed.returncode),
            stdout=stdout,
            stderr=stderr,
        )


__all__ = [
    "HostCommandResult",
    "HostToolError",
    "HostToolOutputTooLarge",
    "HostToolTimedOut",
    "HostTools",
    "SubprocessHostTools",
]
