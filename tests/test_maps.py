from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from alga_vector.location import GeoPoint
from alga_vector.maps import (
    MapCatalog,
    MBTilesPackage,
    MBTilesValidationError,
    OfflineMapService,
    latlon_to_tile,
    latlon_to_world,
    world_to_latlon,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

# Deliberately synthetic test-only coordinate; it does not represent a deployed site.
_SYNTHETIC_BASE = GeoPoint(12.3456, 65.4321)


def _build_mbtiles(
    path: Path,
    *,
    corrupt_format: bool = False,
    bounds: str | None = "-180,-85,180,85",
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata (name TEXT NOT NULL, value TEXT NOT NULL);
            CREATE TABLE tiles (
                zoom_level INTEGER NOT NULL,
                tile_column INTEGER NOT NULL,
                tile_row INTEGER NOT NULL,
                tile_data BLOB NOT NULL
            );
            CREATE UNIQUE INDEX tile_index
                ON tiles (zoom_level, tile_column, tile_row);
            """
        )
        metadata = [
            ("name", "Test offline map"),
            ("format", "pbf" if corrupt_format else "png"),
            ("minzoom", "0"),
            ("maxzoom", "1"),
            ("center", "0,0,1"),
            ("attribution", "Test data"),
            ("version", "1"),
        ]
        if bounds is not None:
            metadata.append(("bounds", bounds))
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", metadata)
        # MBTiles stores TMS rows. XYZ (0, 0, 0) is also TMS row 0 at z0.
        connection.execute(
            "INSERT INTO tiles VALUES (?, ?, ?, ?)",
            (0, 0, 0, _PNG),
        )
        # XYZ (1, 0, 0) maps to TMS row 1.
        connection.execute(
            "INSERT INTO tiles VALUES (?, ?, ?, ?)",
            (1, 0, 1, _PNG),
        )
        connection.commit()
    finally:
        connection.close()


def test_web_mercator_round_trip_and_tile_range() -> None:
    point = _SYNTHETIC_BASE
    x, y = latlon_to_world(point, 12)
    round_trip = world_to_latlon(x, y, 12)
    assert round_trip.latitude_deg == pytest.approx(point.latitude_deg, abs=1e-9)
    assert round_trip.longitude_deg == pytest.approx(point.longitude_deg, abs=1e-9)
    tile_x, tile_y = latlon_to_tile(point, 12)
    assert 0 <= tile_x < 4096
    assert 0 <= tile_y < 4096

    edge_x, edge_y = latlon_to_tile(GeoPoint(90.0, 180.0), 2)
    assert (edge_x, edge_y) == (3, 0)


def test_mbtiles_reads_metadata_and_xyz_tiles_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "map.mbtiles"
    _build_mbtiles(path)
    original_size = path.stat().st_size

    with MBTilesPackage(path) as package:
        assert package.metadata.name == "Test offline map"
        assert package.metadata.minimum_zoom == 0
        assert package.metadata.maximum_zoom == 1
        assert package.metadata.attribution == "Test data"
        assert package.get_tile(0, 0, 0) == _PNG
        assert package.get_tile(1, 0, 0) == _PNG
        assert package.get_tile(1, 1, 1) is None

    assert path.stat().st_size == original_size


def test_mbtiles_rejects_non_raster_and_invalid_schema(tmp_path: Path) -> None:
    vector_path = tmp_path / "vector.mbtiles"
    _build_mbtiles(vector_path, corrupt_format=True)
    with pytest.raises(MBTilesValidationError):
        MBTilesPackage(vector_path)

    invalid = tmp_path / "invalid.mbtiles"
    connection = sqlite3.connect(invalid)
    connection.execute("CREATE TABLE unrelated (value TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(MBTilesValidationError):
        MBTilesPackage(invalid)


def test_map_catalog_imports_content_addressed_package(tmp_path: Path) -> None:
    source = tmp_path / "source.mbtiles"
    _build_mbtiles(source)
    catalog = MapCatalog(tmp_path / "catalog")

    imported = catalog.import_package(
        source,
        license_name="ODbL-1.0",
        source_url="https://example.invalid/source",
    )
    assert imported.path.is_file()
    assert imported.path.name == f"{imported.sha256}.mbtiles"
    assert imported.attribution == "Test data"
    assert (catalog.root / f"{imported.package_id}.json").is_file()

    loaded = catalog.get(imported.package_id)
    assert loaded is not None
    assert loaded.sha256 == imported.sha256
    assert loaded.metadata == imported.metadata

    duplicate = catalog.import_package(source)
    assert duplicate.package_id == imported.package_id
    assert duplicate.license_name == "ODbL-1.0"
    assert duplicate.source_url == "https://example.invalid/source"
    assert len(tuple(catalog.root.glob("*.mbtiles"))) == 1


def test_catalog_detects_package_tampering(tmp_path: Path) -> None:
    source = tmp_path / "source.mbtiles"
    _build_mbtiles(source)
    catalog = MapCatalog(tmp_path / "catalog")
    imported = catalog.import_package(source)
    with imported.path.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(Exception, match="hash"):
        catalog.get(imported.package_id)


def test_offline_map_service_reports_coverage_and_reads_only_local_tiles(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mbtiles"
    _build_mbtiles(source)
    service = OfflineMapService(source, cache_mib=16)

    snapshot = service.snapshot()
    assert snapshot.available
    assert snapshot.name == "Test offline map"
    assert snapshot.attribution == "Test data"
    assert service.contains(_SYNTHETIC_BASE)
    assert service.get_tile(0, 0, 0) == _PNG

    service.close()


def test_map_without_declared_bounds_never_claims_base_coverage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "without-bounds.mbtiles"
    _build_mbtiles(source, bounds=None)
    service = OfflineMapService(source, cache_mib=16)

    assert service.contains(_SYNTHETIC_BASE) is None
    assert service.snapshot().base_in_coverage is None
    service.close()
    assert not service.snapshot().available
