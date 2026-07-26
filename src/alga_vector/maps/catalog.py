from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .mbtiles import MBTilesMetadata, MBTilesPackage, MBTilesValidationError


class MapCatalogError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class MapCatalogEntry:
    package_id: str
    path: Path
    metadata: MBTilesMetadata
    sha256: str
    imported_at: datetime
    attribution: str | None = None
    license_name: str | None = None
    source_url: str | None = None


class MapCatalog:
    """Import validated map packages into a local, content-addressed catalog."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def import_package(
        self,
        source: Path,
        *,
        attribution: str | None = None,
        license_name: str | None = None,
        source_url: str | None = None,
        imported_at: datetime | None = None,
    ) -> MapCatalogEntry:
        source_path = Path(source).resolve()
        with MBTilesPackage(source_path) as package:
            metadata = package.metadata
        timestamp = _utc_time(imported_at or datetime.now(UTC))
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".import-{uuid4().hex}.tmp"
        digest = hashlib.sha256()
        try:
            with source_path.open("rb") as source_handle, temporary.open("wb") as output:
                while chunk := source_handle.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            with MBTilesPackage(temporary) as copied:
                if copied.metadata != metadata:
                    raise MapCatalogError(
                        "map package changed while it was being imported"
                    )
            sha256 = digest.hexdigest()
            destination = self.root / f"{sha256}.mbtiles"
            previous = self.get(sha256) if destination.exists() else None
            if destination.exists():
                temporary.unlink()
                with MBTilesPackage(destination) as existing:
                    if existing.metadata != metadata:
                        raise MapCatalogError(
                            "content-addressed package metadata is inconsistent"
                        )
            else:
                os.replace(temporary, destination)
            effective_attribution = _optional_text(
                (
                    attribution
                    if attribution is not None
                    else (
                        previous.attribution
                        if previous is not None
                        else metadata.attribution
                    )
                ),
                "attribution",
            )
            entry = MapCatalogEntry(
                package_id=sha256,
                path=destination,
                metadata=metadata,
                sha256=sha256,
                imported_at=timestamp,
                attribution=effective_attribution,
                license_name=_optional_text(
                    (
                        license_name
                        if license_name is not None
                        else previous.license_name if previous is not None else None
                    ),
                    "license_name",
                ),
                source_url=_optional_text(
                    (
                        source_url
                        if source_url is not None
                        else previous.source_url if previous is not None else None
                    ),
                    "source_url",
                ),
            )
            self._write_manifest(entry)
            return entry
        except (OSError, MBTilesValidationError) as exc:
            raise MapCatalogError("map package import failed") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def get(self, package_id: str) -> MapCatalogEntry | None:
        _validate_package_id(package_id)
        manifest_path = self.root / f"{package_id}.json"
        package_path = self.root / f"{package_id}.mbtiles"
        if not manifest_path.exists() and not package_path.exists():
            return None
        if not manifest_path.is_file() or not package_path.is_file():
            raise MapCatalogError("catalog entry is incomplete")
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise MapCatalogError("catalog manifest root is invalid")
            if raw.get("package_id") != package_id or raw.get("sha256") != package_id:
                raise MapCatalogError("catalog manifest identity is invalid")
            if _sha256_file(package_path) != package_id:
                raise MapCatalogError("catalog package hash does not match manifest")
            with MBTilesPackage(package_path) as package:
                metadata = package.metadata
            imported_at = datetime.fromisoformat(_required_text(raw, "imported_at"))
            return MapCatalogEntry(
                package_id=package_id,
                path=package_path,
                metadata=metadata,
                sha256=package_id,
                imported_at=_utc_time(imported_at),
                attribution=_manifest_optional_text(raw, "attribution"),
                license_name=_manifest_optional_text(raw, "license_name"),
                source_url=_manifest_optional_text(raw, "source_url"),
            )
        except (OSError, ValueError, json.JSONDecodeError, MBTilesValidationError) as exc:
            raise MapCatalogError("catalog entry validation failed") from exc

    def _write_manifest(self, entry: MapCatalogEntry) -> None:
        manifest = {
            "format": "alga-vector-map-catalog",
            "version": 1,
            "package_id": entry.package_id,
            "sha256": entry.sha256,
            "imported_at": entry.imported_at.isoformat(),
            "attribution": entry.attribution,
            "license_name": entry.license_name,
            "source_url": entry.source_url,
            "map": {
                "name": entry.metadata.name,
                "format": entry.metadata.format,
                "minimum_zoom": entry.metadata.minimum_zoom,
                "maximum_zoom": entry.metadata.maximum_zoom,
                "bounds": entry.metadata.bounds,
                "center": entry.metadata.center,
                "version": entry.metadata.version,
            },
        }
        destination = self.root / f"{entry.package_id}.json"
        temporary = self.root / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    manifest,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_package_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("package_id must be a lowercase SHA-256 digest")


def _utc_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(UTC)


def _optional_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > 2048:
        raise ValueError(f"{name} is oversized")
    return cleaned


def _required_text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise MapCatalogError(f"manifest {name} is invalid")
    return value


def _manifest_optional_text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MapCatalogError(f"manifest {name} is invalid")
    return value


__all__ = ["MapCatalog", "MapCatalogEntry", "MapCatalogError"]
