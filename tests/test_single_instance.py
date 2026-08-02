from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from alga_vector.single_instance import SingleInstanceGuard, isolated_smoke_mutex_name


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_second_guard_for_same_name_is_rejected() -> None:
    name = rf"Local\ALGA_VECTOR_TEST_{uuid4().hex}"
    with SingleInstanceGuard(name) as first, SingleInstanceGuard(name) as second:
        assert first.acquired is True
        assert second.acquired is False


def test_isolated_smoke_mutex_is_stable_and_data_dir_scoped(tmp_path: Path) -> None:
    first = isolated_smoke_mutex_name(tmp_path / "first")
    same = isolated_smoke_mutex_name(tmp_path / "first" / ".")
    second = isolated_smoke_mutex_name(tmp_path / "second")

    assert first == same
    assert first != second
    assert first.startswith("Local\\ALGA_VECTOR_SMOKE_")
