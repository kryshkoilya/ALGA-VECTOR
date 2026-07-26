"""Narrow retention policy for finalized ALGA spectrum captures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(slots=True, frozen=True)
class RetentionResult:
    removed_files: int
    removed_bytes: int
    skipped_partial_files: int


def prune_spectrum_captures(
    directory: Path,
    *,
    retention_days: int,
    now: datetime,
) -> RetentionResult:
    """Delete only expired finalized captures and their exact checksum sidecars.

    Active or recovered ``.partial`` files are never removed by retention.
    Unknown files are never touched.
    """

    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    root = Path(directory).resolve()
    if not root.exists():
        return RetentionResult(0, 0, 0)
    if not root.is_dir():
        raise NotADirectoryError(root)
    cutoff = (now - timedelta(days=retention_days)).timestamp()
    removed_files = 0
    removed_bytes = 0
    partial_count = sum(
        1
        for path in root.glob("alga-spectrum-*.jsonl.partial")
        if path.is_file()
    )
    for path in root.glob("alga-spectrum-*.jsonl"):
        resolved = path.resolve()
        if resolved.parent != root or not resolved.is_file():
            continue
        stat = resolved.stat()
        if stat.st_mtime >= cutoff:
            continue
        removed_bytes += stat.st_size
        resolved.unlink()
        removed_files += 1
        checksum = resolved.with_suffix(resolved.suffix + ".sha256")
        if checksum.parent == root and checksum.is_file():
            removed_bytes += checksum.stat().st_size
            checksum.unlink()
            removed_files += 1
    return RetentionResult(removed_files, removed_bytes, partial_count)


__all__ = ["RetentionResult", "prune_spectrum_captures"]
