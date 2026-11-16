# -*- coding: utf-8 -*-
"""Проверки на собственном наборе данных (data_user, собран tools/build_user_dataset.py)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from school_app.model import SchoolPlacementModel


USER_DIR = pytest.importorskip("conftest").APP_DIR / "data_user"
pytestmark = pytest.mark.skipif(not (USER_DIR / "blocks.json").exists(), reason="data_user не подготовлен")


@pytest.fixture(scope="module")
def user_model():
    return SchoolPlacementModel(USER_DIR)


@pytest.fixture(scope="module")
def user_auto(user_model):
    solves = []
    original = user_model.solve

    def recording(capacity):
        solves.append(np.array(capacity, copy=True))
        return original(capacity)

    user_model.solve = recording
    try:
        result = user_model.auto_search(600)
    finally:
        del user_model.solve
    return solves, result


def test_inputs_match_user_files(user_model):
    meta = user_model.metadata
    assert meta["dataset"]["name"].startswith("Собственный")
    assert len(user_model.block_ids) == 473  # мелкие кварталы не объединялись
    assert user_model.base_totals["population"] == pytest.approx(1135)
    capacity = user_model.base_capacity
    assert capacity.sum() == 340 and np.count_nonzero(capacity) == 1
    assert "Сценарий" not in json.dumps(meta["dataset"]["input_hashes"], ensure_ascii=False)


def test_user_base_reproduced(user_model):
    v = user_model.validation
    assert not v["problems"]
    assert v["compared_blocks"] == v["populated_blocks"] == 56
    assert v["max_abs_diff"] == 0.0 and v["links_equal"] is True
    assert any("пешеходного графа" in w for w in v["warnings"])  # предупреждение о покрытии графа
    assert user_model.scenario([])["scenario"] == user_model.base_totals


def test_user_matrix_ids(user_model):
    matrix = pd.read_parquet(USER_DIR / "accessibility_matrix_walk.parquet")
    assert list(map(int, matrix.index)) == list(user_model.block_ids) == list(map(int, matrix.columns))


def test_user_auto_independent_and_ties(user_model, user_auto):
    solves, result = user_auto
    base = user_model.base_capacity
    changed = [int(np.flatnonzero(c - base)[0]) for c in solves]
    assert all(np.count_nonzero(c - base) == 1 for c in solves)
    assert sorted(changed) == list(range(len(user_model.block_ids)))
    assert result["complete"] and result["ties"] > 1
    assert f"Равноценных лучших вариантов: {result['ties']}" in result["message"]
    assert "Проверены все 473 квартала" in result["message"]
    best = {c["deficit_reduction"] for c in result["best"]}
    assert len(best) == 1
    assert all(c["deficit_reduction"] < result["best_reduction"] for c in result["candidates"][result["ties"]:])


def test_user_manual_reproduces_every_tie(user_model, user_auto):
    _, result = user_auto
    for candidate in result["best"]:
        manual = user_model.scenario([{"block_id": candidate["block_id"], "capacity": 600}])
        assert manual["scenario"]["deficit_places"] == candidate["scenario_deficit"]
        assert manual["deficit_reduction_places"] == candidate["deficit_reduction"]
