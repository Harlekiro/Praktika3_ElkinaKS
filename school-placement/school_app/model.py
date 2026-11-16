# -*- coding: utf-8 -*-
"""Расчёт обеспеченности населения местами в школах.

Метод совпадает с практикой 2 (ноутбук ``examples/vysotsk/vysotsk_study.ipynb``, раздел 9.5):

* BlocksNet 1.0.0a9, ``blocksnet.analysis.provision.competitive_provision``;
* параметры ``blocksnet.config.service_types_config["school"]``: 120 мест на 1000 жителей, 15 минут;
* матрица времени в пути пешком (``walk``), минуты;
* таблица кварталов ``population`` + ``capacity``, вызов
  ``competitive_provision(table, matrix, accessibility, demand)`` — ровно как в ноутбуке.

BlocksNet сам считает потребность квартала ``ceil(население / 1000 × 120)``, сначала закрывает её
собственной вместимостью квартала, затем распределяет остаток вместимости между кварталами
в пределах 15 минут, решая задачу линейного программирования (конкуренция кварталов за места).

Дефицит мест квартала = потребность − удовлетворённый в пределах доступности спрос
(``demand - demand_within``). Это не «потребность минус вместимость».
"""

from __future__ import annotations

import json
import math
import numbers
import threading
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd
from loguru import logger

from blocksnet.analysis.provision import competitive_provision, provision_strong_total, provision_weak_total
from blocksnet.config import service_types_config


# BlocksNet пишет служебные сообщения на каждый расчёт; при переборе кварталов их сотни.
logger.disable("blocksnet")

SERVICE = "school"
PROVISION_MODE = "walk"
REQUIRED_BLOCKSNET = "1.0.0a9"
DEMAND_PER_1000 = int(service_types_config[SERVICE]["demand"])
ACCESSIBILITY_MIN = int(service_types_config[SERVICE]["accessibility"])
DEFAULT_AUTO_CAPACITY = 600
MAX_CAPACITY = 100_000
# Дефицит — целое число мест (переменные задачи ЛП целочисленные), поэтому равенство
# результатов определяется с точностью до 0,5 места.
TIE_TOLERANCE = 0.5


class DataValidationError(RuntimeError):
    """Данные не позволяют воспроизвести расчёт практики 2."""


class PlacementError(ValueError):
    """Некорректный вариант размещения школы."""


@dataclass(frozen=True)
class Placement:
    block_id: int
    capacity: int


def parse_positive_int(value: Any, field: str, maximum: int = MAX_CAPACITY) -> int:
    """Принимает только положительное целое число (int или строку из цифр)."""
    message = f"{field}: укажите положительное целое число."
    if isinstance(value, bool) or value is None:
        raise PlacementError(message)
    if isinstance(value, numbers.Integral):
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise PlacementError(message)
    if number <= 0:
        raise PlacementError(message)
    if number > maximum:
        raise PlacementError(f"{field}: не больше {maximum:,} мест.".replace(",", " "))
    return number


def _parse_block_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise PlacementError(f"{field}: нужен номер квартала.")
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise PlacementError(f"{field}: нужен целый номер квартала.")


def _finite_or_none(value: float) -> float | None:
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass
class ProvisionResult:
    demand: np.ndarray
    capacity: np.ndarray
    served: np.ndarray
    served_outside: np.ndarray
    deficit: np.ndarray
    provision: np.ndarray
    provision_weak: np.ndarray
    links: pd.DataFrame
    provision_total: float
    provision_weak_total: float

    def totals(self, population: float) -> dict[str, Any]:
        return {
            "population": float(population),
            "demand_places": int(self.demand.sum()),
            "capacity_places": int(self.capacity.sum()),
            "served_places": int(self.served.sum()),
            "deficit_places": int(self.deficit.sum()),
            "provision": float(self.provision_total),
            "provision_percent": float(self.provision_total * 100.0),
            "provision_weak": float(self.provision_weak_total),
        }


class SchoolPlacementModel:
    """Модель размещения школ на сохранённых данных практики 2.

    Исходные массивы (население, вместимость, матрица) хранятся только для чтения:
    каждый сценарий строит собственную копию вместимости.
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).resolve()
        self._lock = threading.Lock()

        blocks_path = self.data_dir / "blocks.json"
        matrix_path = self.data_dir / "accessibility_matrix_walk.parquet"
        metadata_path = self.data_dir / "metadata.json"
        missing = [path for path in (blocks_path, matrix_path, metadata_path) if not path.exists()]
        if missing:
            raise DataValidationError(
                "Не найдены файлы данных приложения: "
                + ", ".join(str(path) for path in missing)
                + ". Подготовьте их командой: python school-placement/tools/prepare_data.py"
            )

        with blocks_path.open(encoding="utf-8") as file:
            blocks_payload = json.load(file)
        with metadata_path.open(encoding="utf-8") as file:
            self.metadata = json.load(file)

        raw_blocks = sorted(blocks_payload.get("blocks", []), key=lambda item: int(item["id"]))
        if not raw_blocks:
            raise DataValidationError("В blocks.json нет кварталов.")
        self.blocks: list[dict[str, Any]] = raw_blocks
        self.view_box = blocks_payload.get("view_box", [0, 0, 1000, 1000])

        ids = tuple(int(block["id"]) for block in raw_blocks)
        if len(set(ids)) != len(ids):
            raise DataValidationError("В blocks.json повторяются идентификаторы кварталов.")
        self._ids = ids
        self._index = {block_id: position for position, block_id in enumerate(ids)}

        population = np.array([float(block["population"]) for block in raw_blocks], dtype=float)
        if not np.isfinite(population).all() or (population < 0).any():
            raise DataValidationError("Население кварталов содержит отрицательные или нечисловые значения.")
        base_capacity = np.array([int(block["base_capacity"]) for block in raw_blocks], dtype=np.int64)
        if (base_capacity < 0).any():
            raise DataValidationError("Вместимость существующих школ содержит отрицательные значения.")
        population.setflags(write=False)
        base_capacity.setflags(write=False)
        self._population = population
        self._base_capacity = base_capacity

        self._matrix = self._load_matrix(matrix_path)
        self._saved_links_path = self.data_dir / "source" / "provision_links_school.parquet"

        self.base = self.solve(self._base_capacity)
        self.base_totals = self.base.totals(self._population.sum())
        self.validation = self._validate_base()

    # ------------------------------------------------------------------
    # Загрузка и проверки
    # ------------------------------------------------------------------

    def _load_matrix(self, path: Path) -> pd.DataFrame:
        matrix = pd.read_parquet(path)
        try:
            matrix.index = pd.Index([int(value) for value in matrix.index])
            matrix.columns = pd.Index([int(value) for value in matrix.columns])
        except (TypeError, ValueError) as error:
            raise DataValidationError("Идентификаторы матрицы времени в пути не являются целыми числами.") from error
        if matrix.index.has_duplicates or matrix.columns.has_duplicates:
            raise DataValidationError("В матрице времени в пути повторяются идентификаторы кварталов.")
        expected = set(self._ids)
        for axis, labels in (("строках", matrix.index), ("столбцах", matrix.columns)):
            missing = sorted(expected - set(labels))
            extra = sorted(set(labels) - expected)
            if missing or extra:
                raise DataValidationError(
                    f"Идентификаторы кварталов не совпадают с {axis} матрицы времени в пути. "
                    f"Нет в матрице: {missing[:20]}. Лишние: {extra[:20]}."
                )
        matrix = matrix.loc[list(self._ids), list(self._ids)]
        values = matrix.to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise DataValidationError("Матрица времени в пути содержит пустые или отрицательные значения.")
        if not np.allclose(np.diag(values), 0.0):
            raise DataValidationError("На диагонали матрицы времени в пути есть ненулевые значения.")
        return matrix

    def _validate_base(self) -> dict[str, Any]:
        problems: list[str] = []
        warnings: list[str] = []

        try:
            installed = version("blocksnet")
        except PackageNotFoundError:
            installed = None
        if installed != REQUIRED_BLOCKSNET:
            warnings.append(f"установлен BlocksNet {installed}, в практике 2 использовался {REQUIRED_BLOCKSNET}")
        warnings.extend((self.metadata.get("dataset") or {}).get("warnings", []))

        report = self.metadata.get("saved_provision_report") or {}
        if report:
            if int(report.get("норматив на 1000 жителей", DEMAND_PER_1000)) != DEMAND_PER_1000:
                problems.append("норматив в provision_report.csv отличается от service_types_config")
            if int(report.get("радиус, мин", ACCESSIBILITY_MIN)) != ACCESSIBILITY_MIN:
                problems.append("радиус доступности в provision_report.csv отличается от service_types_config")
            if str(report.get("граф", PROVISION_MODE)) != PROVISION_MODE:
                problems.append("в практике 2 использовался другой граф")

        saved = np.array(
            [np.nan if block.get("saved_provision") is None else float(block["saved_provision"]) for block in self.blocks]
        )
        saved_weak = np.array(
            [
                np.nan if block.get("saved_provision_weak") is None else float(block["saved_provision_weak"])
                for block in self.blocks
            ]
        )
        populated = self.base.demand > 0
        saved_mask = np.isfinite(saved)
        if not np.array_equal(saved_mask, populated):
            problems.append("набор кварталов с сохранённой обеспеченностью не совпадает с населёнными кварталами")
        compared = int((saved_mask & populated).sum())
        mask = saved_mask & populated
        diffs = np.abs(self.base.provision[mask] - saved[mask]) if compared else np.array([np.inf])
        weak_mask = np.isfinite(saved_weak) & populated
        weak_diffs = np.abs(self.base.provision_weak[weak_mask] - saved_weak[weak_mask])
        max_diff = float(diffs.max())
        max_weak_diff = float(weak_diffs.max()) if weak_diffs.size else 0.0
        mismatch_ids = [self._ids[i] for i in np.where(mask & (np.abs(self.base.provision - saved) > 1e-9))[0]]
        if compared == 0:
            problems.append("в данных нет сохранённой обеспеченности кварталов")
        if mismatch_ids:
            problems.append(f"обеспеченность кварталов {mismatch_ids[:20]} отличается от сохранённой")
        if max_weak_diff > 1e-9:
            problems.append("слабая обеспеченность отличается от сохранённой")

        reported_total = self.metadata.get("saved_provision_total")
        total_diff = None
        if reported_total is None:
            warnings.append("в provision_report.csv нет итоговой обеспеченности школами")
        else:
            total_diff = abs(round(self.base_totals["provision"], 3) - float(reported_total))
            if total_diff > 1e-9:
                problems.append(
                    f"итоговая обеспеченность {self.base_totals['provision']:.4f} не совпадает с сохранённой {reported_total}"
                )

        links_checked = False
        links_equal = None
        if self._saved_links_path.exists():
            saved_links = pd.read_parquet(self._saved_links_path)
            saved_set = {(int(r.source), int(r.target), round(float(r.value), 6)) for r in saved_links.itertuples()}
            links = self.base.links.reset_index()
            computed_set = {(int(r.source), int(r.target), round(float(r.value), 6)) for r in links.itertuples()}
            links_checked = True
            links_equal = saved_set == computed_set
            if not links_equal:
                problems.append("связи «квартал → квартал со школой» отличаются от provision_links.parquet")
        else:
            warnings.append("нет файла сохранённых связей source/provision_links_school.parquet")

        if int(round(self._population.sum())) != int(round(float(self.metadata.get("total_population", -1)))):
            problems.append("население не совпадает с metadata.json")
        if int(self._base_capacity.sum()) != int(self.metadata.get("total_capacity_places", -1)):
            problems.append("суммарная вместимость школ не совпадает с metadata.json")

        result = {
            "status": "error" if problems else ("warning" if warnings else "ok"),
            "method": (
                f"BlocksNet {installed} · competitive_provision · граф {PROVISION_MODE} · "
                f"{ACCESSIBILITY_MIN} мин · {DEMAND_PER_1000} мест на 1000 жителей"
            ),
            "blocksnet_version": installed,
            "compared_blocks": compared,
            "populated_blocks": int(populated.sum()),
            "max_abs_diff": max_diff,
            "max_abs_diff_weak": max_weak_diff,
            "reported_total": float(reported_total) if reported_total is not None else None,
            "computed_total": float(self.base_totals["provision"]),
            "total_diff": total_diff,
            "links_checked": links_checked,
            "links_equal": links_equal,
            "links_count": int(len(self.base.links)),
            "matrix_shape": [int(self._matrix.shape[0]), int(self._matrix.shape[1])],
            "matrix_ids_match": True,
            "problems": problems,
            "warnings": warnings,
        }
        if problems:
            raise DataValidationError(
                "Исходный расчёт не воспроизводит сохранённые результаты практики 2: "
                + "; ".join(problems)
                + ". Проверьте версию BlocksNet (нужна 1.0.0a9) и выполните tools/prepare_data.py заново."
            )
        return result

    # ------------------------------------------------------------------
    # Свойства только для чтения
    # ------------------------------------------------------------------

    @property
    def block_ids(self) -> tuple[int, ...]:
        return self._ids

    @property
    def population(self) -> np.ndarray:
        return self._population.copy()

    @property
    def base_capacity(self) -> np.ndarray:
        return self._base_capacity.copy()

    @property
    def matrix(self) -> pd.DataFrame:
        return self._matrix.copy()

    # ------------------------------------------------------------------
    # Расчёт
    # ------------------------------------------------------------------

    def solve(self, capacity: Sequence[int] | np.ndarray) -> ProvisionResult:
        """Расчёт обеспеченности для заданной вместимости школ по кварталам (как в практике 2)."""
        capacity = np.array(capacity, dtype=np.int64)
        if capacity.shape != (len(self._ids),):
            raise ValueError("Число значений вместимости не совпадает с числом кварталов.")
        if (capacity < 0).any():
            raise ValueError("Вместимость не может быть отрицательной.")

        table = pd.DataFrame({"population": self._population, "capacity": capacity}, index=list(self._ids))
        with self._lock:
            provision_df, links_df = competitive_provision(
                table,
                self._matrix,
                ACCESSIBILITY_MIN,
                DEMAND_PER_1000,
            )
        provision_df = provision_df.reindex(list(self._ids))

        demand = provision_df["demand"].to_numpy(dtype=np.int64)
        served_raw = provision_df["demand_within"].to_numpy(dtype=float)
        served = np.rint(served_raw).astype(np.int64)
        if np.abs(served_raw - served).max(initial=0.0) > 1e-6:
            raise DataValidationError("BlocksNet вернул нецелое число удовлетворённых мест.")
        outside = np.rint(provision_df["demand_without"].to_numpy(dtype=float)).astype(np.int64)
        deficit = demand - served
        if (deficit < 0).any():
            raise DataValidationError("Расчёт дал отрицательный дефицит мест.")
        with np.errstate(divide="ignore", invalid="ignore"):
            provision = np.where(demand > 0, served / np.where(demand > 0, demand, 1), np.nan)
            provision_weak = np.where(demand > 0, (served + outside) / np.where(demand > 0, demand, 1), np.nan)
        return ProvisionResult(
            demand=demand,
            capacity=provision_df["capacity"].to_numpy(dtype=np.int64),
            served=served,
            served_outside=outside,
            deficit=deficit,
            provision=provision,
            provision_weak=provision_weak,
            links=links_df,
            provision_total=provision_strong_total(provision_df),
            provision_weak_total=provision_weak_total(provision_df),
        )

    def normalize_placements(self, placements: Any) -> list[Placement]:
        if placements is None:
            return []
        if not isinstance(placements, (list, tuple)):
            raise PlacementError("Список размещений должен быть массивом.")
        normalized: list[Placement] = []
        for number, item in enumerate(placements, start=1):
            if not isinstance(item, dict) or "block_id" not in item or "capacity" not in item:
                raise PlacementError(f"Школа №{number}: нужны поля block_id и capacity.")
            block_id = _parse_block_id(item["block_id"], f"Школа №{number}")
            if block_id not in self._index:
                raise PlacementError(
                    f"Школа №{number}: квартал №{block_id} не найден "
                    f"(допустимы номера {min(self._ids)}–{max(self._ids)})."
                )
            capacity = parse_positive_int(item["capacity"], f"Вместимость школы №{number}")
            normalized.append(Placement(block_id=block_id, capacity=capacity))
        return normalized

    def added_capacity(self, placements: Sequence[Placement]) -> np.ndarray:
        added = np.zeros(len(self._ids), dtype=np.int64)
        for placement in placements:
            added[self._index[placement.block_id]] += placement.capacity
        return added

    def _block_rows(self, result: ProvisionResult, added: np.ndarray) -> list[dict[str, Any]]:
        base = self.base
        rows = []
        for i, block in enumerate(self.blocks):
            base_p = _finite_or_none(base.provision[i])
            scen_p = _finite_or_none(result.provision[i])
            rows.append(
                {
                    "id": self._ids[i],
                    "population": float(self._population[i]),
                    "demand_places": int(base.demand[i]),
                    "base_capacity": int(self._base_capacity[i]),
                    "added_capacity": int(added[i]),
                    "scenario_capacity": int(result.capacity[i]),
                    "base_served_places": int(base.served[i]),
                    "scenario_served_places": int(result.served[i]),
                    "base_deficit_places": int(base.deficit[i]),
                    "scenario_deficit_places": int(result.deficit[i]),
                    "deficit_reduction_places": int(base.deficit[i] - result.deficit[i]),
                    "base_provision": base_p,
                    "scenario_provision": scen_p,
                    "provision_change_pp": None if base_p is None or scen_p is None else (scen_p - base_p) * 100.0,
                    "land_use_label": block.get("land_use_label", "Не задано"),
                    "area_ha": float(block.get("area_ha", 0.0)),
                }
            )
        return rows

    def _compare(self, totals: dict[str, Any], added_total: int) -> dict[str, Any]:
        base = self.base_totals
        reduction = int(base["deficit_places"] - totals["deficit_places"])
        return {
            "added_capacity": int(added_total),
            "added_capacity_percent": 100.0 * added_total / base["capacity_places"] if base["capacity_places"] else None,
            "deficit_reduction_places": reduction,
            "deficit_reduction_percent": 100.0 * reduction / base["deficit_places"] if base["deficit_places"] else None,
            "provision_change_pp": (totals["provision"] - base["provision"]) * 100.0,
        }

    def scenario(self, placements: Any) -> dict[str, Any]:
        """Рассчитывает сценарий: исходная сеть школ + новые школы пользователя."""
        parsed = self.normalize_placements(placements)
        added = self.added_capacity(parsed)
        result = self.solve(self._base_capacity + added)
        totals = result.totals(self._population.sum())
        if totals["capacity_places"] != self.base_totals["capacity_places"] + int(added.sum()):
            raise DataValidationError("Суммарная вместимость сценария не равна исходной плюс новые места.")
        return {
            "placements": [{"block_id": p.block_id, "capacity": p.capacity} for p in parsed],
            "base": dict(self.base_totals),
            "scenario": totals,
            **self._compare(totals, int(added.sum())),
            "blocks": self._block_rows(result, added),
        }

    def evaluate_single(self, block_id: int, capacity: int) -> dict[str, Any]:
        """Один вариант автоподбора: исходная сеть + одна школа. Ничего не накапливается."""
        placement = self.normalize_placements([{"block_id": block_id, "capacity": capacity}])[0]
        position = self._index[placement.block_id]
        candidate_capacity = self._base_capacity.copy()
        candidate_capacity[position] += placement.capacity
        result = self.solve(candidate_capacity)
        totals = result.totals(self._population.sum())
        compare = self._compare(totals, placement.capacity)
        return {
            "block_id": placement.block_id,
            "capacity": placement.capacity,
            "population": float(self._population[position]),
            "base_capacity": int(self._base_capacity[position]),
            "block_base_deficit": int(self.base.deficit[position]),
            "scenario_capacity_total": totals["capacity_places"],
            "scenario_deficit": totals["deficit_places"],
            "deficit_reduction": compare["deficit_reduction_places"],
            "deficit_reduction_percent": compare["deficit_reduction_percent"],
            "scenario_provision": totals["provision"],
            "provision_change_pp": compare["provision_change_pp"],
            "land_use_label": self.blocks[position].get("land_use_label", "Не задано"),
        }

    def auto_search(
        self,
        capacity: Any = DEFAULT_AUTO_CAPACITY,
        progress: Callable[[int, int, int, dict | None], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Полный перебор всех кварталов. Каждый вариант считается от исходной сети школ."""
        capacity_int = parse_positive_int(capacity, "Вместимость новой школы")
        candidates: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        cancelled = False
        total = len(self._ids)
        for checked, block_id in enumerate(self._ids, start=1):
            if cancel is not None and cancel():
                cancelled = True
                break
            item = None
            try:
                item = self.evaluate_single(block_id, capacity_int)
                candidates.append(item)
            except Exception as error:  # ошибка одного квартала не останавливает перебор
                errors.append({"block_id": int(block_id), "error": f"{type(error).__name__}: {error}"})
            if progress is not None:
                progress(checked, total, int(block_id), item)
        return rank_candidates(candidates, self._ids, errors=errors, capacity=capacity_int, cancelled=cancelled)


def _blocks_word(count: int) -> str:
    """Согласование: 1 квартал, 3 квартала, 255 кварталов."""
    if count % 10 == 1 and count % 100 != 11:
        return "квартал"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "квартала"
    return "кварталов"


def rank_candidates(
    candidates: Sequence[dict[str, Any]],
    all_block_ids: Sequence[int],
    errors: Sequence[dict[str, Any]] = (),
    capacity: int | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Сортирует варианты по сокращению дефицита и выделяет все равноценные лучшие."""
    ordered = sorted(candidates, key=lambda c: (-c["deficit_reduction"], int(c["block_id"])))
    best: list[dict[str, Any]] = []
    best_reduction = None
    if ordered:
        best_reduction = ordered[0]["deficit_reduction"]
        best = [c for c in ordered if abs(c["deficit_reduction"] - best_reduction) <= TIE_TOLERANCE]
    best_ids = {int(c["block_id"]) for c in best}
    for rank, item in enumerate(ordered, start=1):
        item["rank"] = rank
        item["is_best"] = int(item["block_id"]) in best_ids

    total = len(all_block_ids)
    evaluated = {int(c["block_id"]) for c in ordered}
    complete = (not cancelled) and not errors and evaluated == {int(b) for b in all_block_ids}
    checked = len(ordered) + len(errors)

    if not ordered:
        message = "Ни один вариант не рассчитан."
    elif complete:
        if len(best) == 1:
            message = (
                f"Проверены все {total} {_blocks_word(total)}. Наибольшее сокращение дефицита — квартал №{best[0]['block_id']}: "
                f"на {best_reduction} мест."
            )
        else:
            numbers_text = ", ".join(f"№{c['block_id']}" for c in best)
            message = (
                f"Проверены все {total} {_blocks_word(total)}. Равноценных лучших вариантов: {len(best)} ({numbers_text}); "
                f"у каждого дефицит сокращается на {best_reduction} мест."
            )
    else:
        reasons = []
        if cancelled:
            reasons.append("перебор остановлен")
        if errors:
            reasons.append(f"ошибок расчёта: {len(errors)}")
        message = (
            f"Перебор неполный ({', '.join(reasons)}): успешно рассчитано {len(ordered)} из {total} кварталов. "
            "Лучший вариант среди всех кварталов не определён; показан лучший среди рассчитанных."
        )

    return {
        "capacity": capacity,
        "total": total,
        "checked": checked,
        "evaluated": len(ordered),
        "complete": bool(complete),
        "cancelled": bool(cancelled),
        "errors": list(errors),
        "best": best,
        "best_reduction": best_reduction,
        "ties": len(best),
        "candidates": ordered,
        "message": message,
    }


class AutoSearchJob:
    """Фоновый перебор кварталов с прогрессом и возможностью остановки."""

    def __init__(self, job_id: str, model: SchoolPlacementModel, capacity: int):
        self.id = job_id
        self.model = model
        self.capacity = capacity
        self.total = len(model.block_ids)
        self.checked = 0
        self.current_block: int | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.result: dict[str, Any] | None = None
        self.failure: str | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._errors: list[dict[str, Any]] = []
        self._thread = threading.Thread(target=self._run, name=f"auto-search-{job_id}", daemon=True)

    def start(self) -> "AutoSearchJob":
        self._thread.start()
        return self

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _progress(self, checked: int, total: int, block_id: int, item: dict | None) -> None:
        with self._lock:
            self.checked = checked
            self.current_block = block_id
            if item is None:
                self._errors.append({"block_id": block_id})

    def _run(self) -> None:
        try:
            result = self.model.auto_search(self.capacity, progress=self._progress, cancel=self._cancel.is_set)
            with self._lock:
                self.result = result
        except Exception as error:  # pragma: no cover - защитная ветка
            with self._lock:
                self.failure = f"{type(error).__name__}: {error}"
        finally:
            with self._lock:
                self.finished = time.time()

    @property
    def state(self) -> str:
        if self.finished is None:
            return "cancelling" if self._cancel.is_set() else "running"
        if self.failure:
            return "failed"
        return "cancelled" if self.result and self.result["cancelled"] else "done"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "state": self.state,
                "capacity": self.capacity,
                "checked": self.checked,
                "total": self.total,
                "current_block": self.current_block,
                "errors_so_far": len(self._errors),
                "elapsed_s": round((self.finished or time.time()) - self.started, 1),
                "failure": self.failure,
                "result": self.result,
            }
