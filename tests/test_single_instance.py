from __future__ import annotations

import os
from uuid import uuid4

import pytest

from alga_vector.single_instance import SingleInstanceGuard


@pytest.mark.skipif(os.name != "nt", reason="Windows named mutex")
def test_second_guard_for_same_name_is_rejected() -> None:
    name = rf"Local\ALGA_VECTOR_TEST_{uuid4().hex}"
    with SingleInstanceGuard(name) as first, SingleInstanceGuard(name) as second:
        assert first.acquired is True
        assert second.acquired is False
