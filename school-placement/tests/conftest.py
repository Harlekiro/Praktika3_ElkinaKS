# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from school_app.model import SchoolPlacementModel  # noqa: E402


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return APP_DIR / "data"


@pytest.fixture(scope="session")
def model(data_dir) -> SchoolPlacementModel:
    return SchoolPlacementModel(data_dir)


@pytest.fixture(scope="session")
def auto_run(model):
    """Полный перебор для школы на 600 мест с записью всех вариантов вместимости."""
    solves: list[np.ndarray] = []
    original = model.solve

    def recording(capacity):
        solves.append(np.array(capacity, copy=True))
        return original(capacity)

    model.solve = recording
    try:
        result = model.auto_search(600)
    finally:
        del model.solve
    return solves, result
