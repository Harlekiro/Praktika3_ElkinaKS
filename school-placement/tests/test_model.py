# -*- coding: utf-8 -*-
"""Проверки расчётов (раздел «Проверки» задания)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from school_app.model import (
    AutoSearchJob,
    PlacementError,
    SchoolPlacementModel,
    rank_candidates,
)


def test_matrix_ids_match_blocks(model, data_dir):
    matrix = pd.read_parquet(data_dir / "accessibility_matrix_walk.parquet")
    ids = list(model.block_ids)
    assert sorted(int(i) for i in matrix.index) == ids
    assert sorted(int(c) for c in matrix.columns) == ids
    assert model.validation["matrix_ids_match"]


def test_base_reproduces_practice2(model, data_dir):
    """Без новых школ расчёт воспроизводит сохранённый результат практики 2."""
    v = model.validation
    assert v["status"] == "ok", v
    assert v["compared_blocks"] == v["populated_blocks"] == 24
    assert v["max_abs_diff"] == 0.0
    assert v["links_equal"] is True

    blocks = json.loads((data_dir / "blocks.json").read_text(encoding="utf-8"))["blocks"]
    saved = {b["id"]: b["saved_provision"] for b in blocks}
    for i, block_id in enumerate(model.block_ids):
        if saved[block_id] is None:
            assert model.base.demand[i] == 0
        else:
            assert model.base.provision[i] == pytest.approx(saved[block_id], abs=1e-12)

    report = pd.read_csv(data_dir / "source" / "provision_report.csv", encoding="utf-8-sig")
    school = report[report["сервис"] == "school"].iloc[0]
    assert round(model.base_totals["provision"], 3) == pytest.approx(float(school["обеспеченность (сильная)"]))
    assert len(model.base.links) == int(school["связей"])


def test_empty_scenario_equals_base(model):
    result = model.scenario([])
    assert result["added_capacity"] == 0
    assert result["deficit_reduction_places"] == 0
    assert result["scenario"] == model.base_totals
    for row in result["blocks"]:
        assert row["scenario_deficit_places"] == row["base_deficit_places"]
        assert row["scenario_provision"] == row["base_provision"]


def test_deficit_is_spatial_not_subtraction(model):
    """Вместимости больше, чем потребности, но дефицит есть — из-за доступности."""
    totals = model.base_totals
    assert totals["capacity_places"] > totals["demand_places"]
    assert totals["deficit_places"] == totals["demand_places"] - totals["served_places"] > 0


def test_capacity_growth_is_counted(model):
    placements = [
        {"block_id": 22, "capacity": 300},
        {"block_id": 22, "capacity": 200},
        {"block_id": 100, "capacity": 150},
    ]
    result = model.scenario(placements)
    assert result["added_capacity"] == 650
    assert result["scenario"]["capacity_places"] == model.base_totals["capacity_places"] + 650
    rows = {row["id"]: row for row in result["blocks"]}
    assert rows[22]["added_capacity"] == 500
    assert rows[22]["scenario_capacity"] == rows[22]["base_capacity"] + 500
    assert rows[100]["scenario_capacity"] == rows[100]["base_capacity"] + 150
    untouched = [row for row in result["blocks"] if row["id"] not in (22, 100)]
    assert all(row["scenario_capacity"] == row["base_capacity"] for row in untouched)
    assert result["deficit_reduction_places"] == model.base_totals["deficit_places"] - result["scenario"]["deficit_places"]


@pytest.mark.parametrize("capacity", [0, -1, -600, 1.5, 600.0, "1.5", "abc", "", " ", "-5", "6e2", None, True, [600]])
def test_capacity_must_be_positive_integer(model, capacity):
    with pytest.raises(PlacementError):
        model.scenario([{"block_id": 22, "capacity": capacity}])


@pytest.mark.parametrize("capacity", [1, 600, "600", " 300 "])
def test_capacity_valid_values(model, capacity):
    assert model.scenario([{"block_id": 22, "capacity": capacity}])["added_capacity"] == int(str(capacity).strip())


@pytest.mark.parametrize("bad", [[{"block_id": 99999, "capacity": 600}], [{"capacity": 600}], [600], "22"])
def test_bad_placements(model, bad):
    with pytest.raises(PlacementError):
        model.scenario(bad)


def test_auto_capacity_validation(model):
    for bad in (0, -1, 2.5, "x"):
        with pytest.raises(PlacementError):
            model.auto_search(bad)


def test_scenarios_do_not_change_source_data(model, data_dir):
    population = model.population
    capacity = model.base_capacity
    matrix = model.matrix
    totals = dict(model.base_totals)
    base_deficit = model.base.deficit.copy()
    files = {p: p.read_bytes() for p in data_dir.rglob("*") if p.is_file()}

    model.scenario([{"block_id": 22, "capacity": 600}, {"block_id": 5, "capacity": 300}])
    model.evaluate_single(10, 1000)
    model.population[:] = 0  # копия — исходные данные не меняются
    model.base_capacity[:] = 0

    np.testing.assert_array_equal(model.population, population)
    np.testing.assert_array_equal(model.base_capacity, capacity)
    pd.testing.assert_frame_equal(model.matrix, matrix)
    assert model.base_totals == totals
    np.testing.assert_array_equal(model.base.deficit, base_deficit)
    assert model.scenario([])["scenario"] == totals
    assert {p: p.read_bytes() for p in data_dir.rglob("*") if p.is_file()} == files


def test_auto_checks_every_block_independently(model, auto_run):
    solves, result = auto_run
    base = model.base_capacity
    assert len(solves) == len(model.block_ids)
    touched = []
    for capacity in solves:
        diff = capacity - base
        changed = np.flatnonzero(diff)
        assert len(changed) == 1, "между проверками накопился прирост вместимости"
        assert diff[changed[0]] == 600
        touched.append(model.block_ids[changed[0]])
    assert sorted(touched) == list(model.block_ids)
    assert result["complete"] and result["checked"] == result["total"] == len(model.block_ids)
    assert len(result["candidates"]) == len(model.block_ids)
    reductions = [c["deficit_reduction"] for c in result["candidates"]]
    assert reductions == sorted(reductions, reverse=True)


def test_auto_considers_blocks_beyond_max_own_deficit(model, auto_run):
    _, result = auto_run
    best = result["best"][0]
    worst_own = int(model.block_ids[int(np.argmax(model.base.deficit))])
    # лучший квартал определяется влиянием на всю территорию, а не собственным дефицитом
    assert best["deficit_reduction"] >= next(c for c in result["candidates"] if c["block_id"] == worst_own)["deficit_reduction"]
    assert any(c["population"] == 0 for c in result["candidates"])


def test_manual_placement_reproduces_auto(model, auto_run):
    _, result = auto_run
    for candidate in result["best"] + result["candidates"][-3:]:
        manual = model.scenario([{"block_id": candidate["block_id"], "capacity": 600}])
        assert manual["scenario"]["deficit_places"] == candidate["scenario_deficit"]
        assert manual["deficit_reduction_places"] == candidate["deficit_reduction"]
        assert manual["scenario"]["provision"] == candidate["scenario_provision"]


def test_auto_result_for_practice2(auto_run):
    _, result = auto_run
    assert result["ties"] == 1
    assert result["best"][0]["block_id"] == 22
    assert result["best_reduction"] == 88
    assert "Проверены все 255 кварталов" in result["message"]


def _cand(block_id, reduction):
    return {"block_id": block_id, "deficit_reduction": reduction, "scenario_deficit": 100 - reduction}


def test_ties_are_all_reported():
    result = rank_candidates([_cand(3, 10), _cand(1, 10), _cand(2, 4), _cand(4, 10)], [1, 2, 3, 4])
    assert result["complete"]
    assert result["ties"] == 3
    assert [c["block_id"] for c in result["best"]] == [1, 3, 4]
    assert "Равноценных лучших вариантов: 3" in result["message"]
    assert all(c["is_best"] for c in result["candidates"][:3]) and not result["candidates"][3]["is_best"]


def test_single_best():
    result = rank_candidates([_cand(1, 5), _cand(2, 6)], [1, 2])
    assert result["ties"] == 1 and result["best"][0]["block_id"] == 2


def test_incomplete_search_does_not_claim_best():
    errors = [{"block_id": 3, "error": "RuntimeError: тест"}]
    result = rank_candidates([_cand(1, 5), _cand(2, 6)], [1, 2, 3], errors=errors)
    assert not result["complete"]
    assert result["checked"] == 3 and result["evaluated"] == 2
    assert "неполный" in result["message"] and "не определён" in result["message"]
    assert "Проверены все" not in result["message"]

    cancelled = rank_candidates([_cand(1, 5)], [1, 2, 3], cancelled=True)
    assert not cancelled["complete"] and "остановлен" in cancelled["message"]


def test_error_in_one_block_is_reported(model, monkeypatch):
    original = model.solve
    target = model.block_ids[5]

    def flaky(capacity):
        if capacity[5] != model.base_capacity[5]:
            raise RuntimeError("сбой расчёта")
        return original(capacity)

    monkeypatch.setattr(model, "solve", flaky)
    calls = {"n": 0}

    def stop_early():
        calls["n"] += 1
        return calls["n"] > 8

    result = model.auto_search(600, cancel=stop_early)
    assert {e["block_id"] for e in result["errors"]} == {target}
    assert not result["complete"]
    assert "Проверены все" not in result["message"]


def test_job_can_be_cancelled(model):
    job = AutoSearchJob("t", model, 600).start()
    job.cancel()
    job.wait(30)
    snap = job.snapshot()
    assert snap["state"] == "cancelled"
    assert not snap["result"]["complete"]


def test_missing_data_reported(tmp_path):
    with pytest.raises(Exception) as info:
        SchoolPlacementModel(tmp_path)
    assert "blocks.json" in str(info.value) and "prepare_data.py" in str(info.value)
