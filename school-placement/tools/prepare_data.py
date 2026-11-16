#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Подготовка локальных данных приложения из результатов практики 2.

Скрипт только читает исходный проект и складывает производные файлы в
``school-placement/data``. Исходные файлы практики 2 не изменяются.

Запуск из корня репозитория:

    .\\.venv\\Scripts\\python.exe school-placement\\tools\\prepare_data.py

Если исходный проект лежит в другом месте, укажите путь:

    .\\.venv\\Scripts\\python.exe school-placement\\tools\\prepare_data.py ^
        --source C:\\путь\\к\\python-package-boilerplate\\examples\\vysotsk
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd


APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = APP_DIR.parent / "examples" / "vysotsk"
TARGET_CRS = "EPSG:32635"
SOURCE_CRS = "EPSG:4326"
SVG_WIDTH = 1000.0
SVG_PADDING = 18.0
SIMPLIFY_M = 6.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plain(value):
    """Приводит numpy/pandas-значения к JSON-совместимым."""
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (str, int, bool)):
        return value
    if pd.isna(value):
        return None
    return str(getattr(value, "value", value))


def geometry_rings(geometry):
    """Возвращает внешние и внутренние кольца полигона/мультиполигона."""
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        polygons = [geometry]
    elif geometry.geom_type == "MultiPolygon":
        polygons = list(geometry.geoms)
    else:
        polygons = [part for part in geometry.geoms if part.geom_type == "Polygon"]
    rings = []
    for polygon in polygons:
        if polygon.is_empty:
            continue
        rings.append(list(polygon.exterior.coords))
        for interior in polygon.interiors:
            rings.append(list(interior.coords))
    return rings


def build_svg_paths(metric_blocks: gpd.GeoDataFrame, simplify_m: float = SIMPLIFY_M):
    """Строит SVG-пути кварталов в локальных координатах схемы."""
    bounds = metric_blocks.total_bounds
    xmin, ymin, xmax, ymax = (float(value) for value in bounds)
    if not all(math.isfinite(value) for value in (xmin, ymin, xmax, ymax)):
        raise RuntimeError("Не удалось определить границы кварталов для схемы.")
    width = max(xmax - xmin, 1.0)
    height = max(ymax - ymin, 1.0)
    scale = (SVG_WIDTH - 2 * SVG_PADDING) / width
    view_height = round(height * scale + 2 * SVG_PADDING, 1)

    def to_svg(x, y):
        return (
            round((float(x) - xmin) * scale + SVG_PADDING, 1),
            round((ymax - float(y)) * scale + SVG_PADDING, 1),
        )

    paths = {}
    centroids = {}
    for block_id, geometry in metric_blocks.geometry.items():
        display = geometry.simplify(simplify_m, preserve_topology=True)
        if display.is_empty:
            display = geometry
        pieces = []
        for ring in geometry_rings(display):
            if len(ring) < 3:
                continue
            commands = []
            for index, (x, y) in enumerate(ring):
                sx, sy = to_svg(x, y)
                commands.append(("M" if index == 0 else "L") + f"{sx:.1f},{sy:.1f}")
            pieces.append(" ".join(commands) + " Z")
        paths[int(block_id)] = " ".join(pieces)

        point = geometry.representative_point()
        if point.is_empty:
            point = geometry.centroid
        centroids[int(block_id)] = list(to_svg(point.x, point.y))

    return [0.0, 0.0, SVG_WIDTH, view_height], paths, centroids


def load_matrix(path: Path, block_ids: list[int]) -> tuple[pd.DataFrame, dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"Не найдена матрица времени в пути: {path}\n"
            "Она должна лежать в examples/vysotsk/data/accessibility_matrix_walk.parquet."
        )
    matrix = pd.read_parquet(path)
    try:
        matrix.index = pd.Index([int(value) for value in matrix.index], name=matrix.index.name)
        matrix.columns = pd.Index([int(value) for value in matrix.columns], name=matrix.columns.name)
    except (TypeError, ValueError) as error:
        raise RuntimeError("Идентификаторы строк/столбцов матрицы не являются целыми числами.") from error

    if matrix.shape != (len(block_ids), len(block_ids)):
        raise RuntimeError(
            f"Размер матрицы {matrix.shape} не совпадает с числом кварталов ({len(block_ids)})."
        )
    if len(set(matrix.index)) != len(matrix.index) or len(set(matrix.columns)) != len(matrix.columns):
        raise RuntimeError("В матрице времени в пути есть повторяющиеся идентификаторы кварталов.")
    if list(matrix.index) != list(matrix.columns):
        raise RuntimeError("Строки и столбцы матрицы времени в пути имеют разный порядок идентификаторов.")

    expected = set(block_ids)
    actual = set(int(value) for value in matrix.index)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise RuntimeError(
            "Идентификаторы кварталов не совпадают с матрицей времени в пути. "
            f"Нет в матрице: {missing[:20]}{'…' if len(missing) > 20 else ''}. "
            f"Лишние в матрице: {extra[:20]}{'…' if len(extra) > 20 else ''}."
        )

    reordered = list(matrix.index) != block_ids
    matrix = matrix.loc[block_ids, block_ids].copy()
    values = matrix.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise RuntimeError("В матрице времени в пути есть пустые или бесконечные значения.")
    if (values < 0).any():
        raise RuntimeError("В матрице времени в пути есть отрицательное время.")

    report = {
        "rows": int(matrix.shape[0]),
        "columns": int(matrix.shape[1]),
        "reordered": bool(reordered),
        "min_minutes": float(values.min()),
        "max_minutes": float(values.max()),
        "diagonal_zero": int(np.isclose(np.diag(values), 0.0).sum()),
        "has_nan": False,
        "ids_match_blocks": True,
    }
    return matrix, report


def load_report(path: Path) -> dict:
    if not path.exists():
        return {}
    frame = pd.read_csv(path, encoding="utf-8-sig")
    if "сервис" not in frame.columns:
        return {}
    rows = frame[frame["сервис"].astype(str).str.strip() == "school"]
    if rows.empty:
        return {}
    return {str(key): plain(value) for key, value in rows.iloc[0].to_dict().items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Подготовка данных приложения из практики 2.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Папка examples/vysotsk")
    parser.add_argument("--output", type=Path, default=APP_DIR / "data", help="Папка данных приложения")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    summary_path = source / "data" / "results" / "vysotsk_blocks_summary.geojson"
    matrix_path = source / "data" / "accessibility_matrix_walk.parquet"
    report_path = source / "data" / "results" / "provision_report.csv"
    links_path = source / "data" / "results" / "provision_links.parquet"

    required = {
        summary_path: "сводный слой кварталов (геометрия, _id, population, school_capacity, school_provision) — "
        "ноутбук vysotsk_study.ipynb, разделы 6–10 (сводная таблица кварталов)",
        matrix_path: "матрица времени в пути пешком — ноутбук, раздел 8.1 (calculate_accessibility_matrix, walk)",
        report_path: "сохранённая обеспеченность по сервисам — ноутбук, раздел 9.5 (provision_report.csv)",
        links_path: "сохранённые связи «квартал → школа» — ноутбук, раздел 9.5 (provision_links.parquet)",
    }
    missing = [(path, hint) for path, hint in required.items() if not path.exists()]
    if missing:
        lines = ["Не хватает обязательных файлов практики 2:"]
        for path, hint in missing:
            lines.append(f"  - {path}\n    {hint}")
        lines.append(
            "Восстановление: откройте examples/vysotsk/vysotsk_study.ipynb и выполните все ячейки "
            "(Run All). Ноутбук берёт данные из кэша data/ и скачивает из OpenStreetMap только отсутствующие слои."
        )
        raise SystemExit("\n".join(lines))

    print("Источник данных:", source)
    print("Папка приложения:", output)

    blocks = gpd.read_file(summary_path)
    if blocks.crs is None:
        raise RuntimeError("У сводного слоя кварталов не задана система координат.")
    blocks = blocks.to_crs(SOURCE_CRS)
    if "_id" not in blocks.columns:
        raise RuntimeError("В сводном слое нет столбца _id — невозможно сопоставить кварталы и матрицу.")
    blocks["_id"] = blocks["_id"].astype(int)
    if blocks["_id"].duplicated().any():
        duplicates = sorted(blocks.loc[blocks["_id"].duplicated(), "_id"].tolist())
        raise RuntimeError(f"В сводном слое повторяются идентификаторы кварталов: {duplicates[:20]}")
    blocks = blocks.set_index("_id").sort_index()
    block_ids = [int(value) for value in blocks.index]

    for column in ("population", "school_capacity"):
        if column not in blocks.columns:
            raise RuntimeError(
                f"В сводном слое нет обязательного столбца {column!r}. "
                "Он нужен для расчёта обеспеченности местами в школах."
            )

    population = pd.to_numeric(blocks["population"], errors="coerce").fillna(0.0)
    if (population < 0).any() or not np.isfinite(population.to_numpy(dtype=float)).all():
        raise RuntimeError("Население кварталов содержит отрицательные или нечисловые значения.")
    capacity_raw = pd.to_numeric(blocks["school_capacity"], errors="coerce").fillna(0.0)
    if (capacity_raw < 0).any() or not np.isfinite(capacity_raw.to_numpy(dtype=float)).all():
        raise RuntimeError("Вместимость школ содержит отрицательные или нечисловые значения.")
    capacity = capacity_raw.round().astype(int)

    matrix, matrix_report = load_matrix(matrix_path, block_ids)

    metric = blocks.to_crs(TARGET_CRS)
    area_ha = (metric.area / 10_000).round(3)
    view_box, paths, centroids = build_svg_paths(metric)

    saved_provision = pd.to_numeric(blocks.get("school_provision"), errors="coerce")
    saved_provision_weak = pd.to_numeric(blocks.get("school_provision_weak"), errors="coerce")
    land_use = blocks.get("land_use_label", pd.Series(index=blocks.index, dtype=object)).fillna("Не задано")

    block_records = []
    for block_id in block_ids:
        record = {
            "id": int(block_id),
            "population": float(population.loc[block_id]),
            "base_capacity": int(capacity.loc[block_id]),
            "saved_provision": plain(saved_provision.loc[block_id]),
            "saved_provision_weak": plain(saved_provision_weak.loc[block_id]),
            "area_ha": float(area_ha.loc[block_id]),
            "land_use_label": str(land_use.loc[block_id]),
            "path": paths[block_id],
            "centroid": centroids[block_id],
            "existing_school": bool(int(capacity.loc[block_id]) > 0),
        }
        if not record["path"]:
            raise RuntimeError(f"Не удалось построить контур квартала {block_id} для схемы.")
        block_records.append(record)

    output.mkdir(parents=True, exist_ok=True)
    (output / "source").mkdir(parents=True, exist_ok=True)

    shutil.copy2(summary_path, output / "source" / summary_path.name)
    shutil.copy2(report_path, output / "source" / report_path.name)
    links = pd.read_parquet(links_path)
    service_column = links.columns[-1]
    school_links = links[links[service_column].astype(str).str.strip() == "school"][["source", "target", "value"]]
    school_links.reset_index(drop=True).to_parquet(output / "source" / "provision_links_school.parquet", index=False)
    shutil.copy2(matrix_path, output / "accessibility_matrix_walk.parquet")

    blocks_payload = {
        "view_box": view_box,
        "blocks": block_records,
    }
    with (output / "blocks.json").open("w", encoding="utf-8") as file:
        json.dump(blocks_payload, file, ensure_ascii=False, separators=(",", ":"))

    report = load_report(report_path)
    version_path = source / "data" / "version.txt"
    metadata = {
        "title": "Оценка вариантов размещения школ",
        "territory": "Высоцкое городское поселение, Ленинградская область",
        "source_project": str(source),
        "data_version": version_path.read_text(encoding="utf-8").strip() if version_path.exists() else None,
        "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "block_count": len(block_records),
        "populated_blocks": int((population > 0).sum()),
        "total_population": float(population.sum()),
        "total_capacity_places": int(capacity.sum()),
        "saved_provision_total": plain(report.get("обеспеченность (сильная)")),
        "saved_provision_weak_total": plain(report.get("обеспеченность (слабая)")),
        "saved_provision_report": report,
        "matrix": matrix_report,
        "source_files": {
            "blocks_summary": str(summary_path),
            "accessibility_matrix_walk": str(matrix_path),
            "provision_report": str(report_path),
            "provision_links": str(links_path),
        },
        "source_hashes": {
            "blocks_summary": sha256(summary_path),
            "accessibility_matrix_walk": sha256(matrix_path),
            "provision_report": sha256(report_path),
            "provision_links": sha256(links_path),
        },
        "saved_school_links": int(len(school_links)),
        "svg": {
            "width": SVG_WIDTH,
            "height": view_box[3],
            "simplify_m": SIMPLIFY_M,
        },
        "crs": {"source": SOURCE_CRS, "metric": TARGET_CRS},
        "provision_mode": "walk",
    }
    # Описание набора данных, если источник собран tools/build_user_dataset.py.
    dataset_path = source / "dataset.json"
    if dataset_path.exists():
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
        metadata["dataset"] = dataset
        metadata["territory"] = dataset.get("territory", metadata["territory"])
    else:
        metadata["dataset"] = {"name": "Данные практики 2 (OpenStreetMap + BlocksNet)"}
    with (output / "metadata.json").open("w", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)

    readme = f"""# Данные приложения

Файлы в этой папке подготовлены скриптом `tools/prepare_data.py` из проекта практики 2.
Исходные файлы не изменялись.

- `blocks.json` — кварталы, население, существующая вместимость школ, сохранённая
  обеспеченность и SVG-контуры для схемы.
- `accessibility_matrix_walk.parquet` — матрица времени в пути пешком, минуты.
- `metadata.json` — источники, контрольные суммы, параметры и проверка матрицы.
- `source/` — копии исходных результатов практики 2 для сверки: сводный слой,
  `provision_report.csv` и связи «квартал → школа» (`provision_links_school.parquet`).

Источник: `{source}`.
Подготовлено: {metadata['prepared_at']}.
"""
    (output / "README.md").write_text(readme, encoding="utf-8")

    print(f"Кварталов: {len(block_records)}; население: {metadata['total_population']:.0f} чел.;")
    print(f"существующая вместимость школ: {metadata['total_capacity_places']} мест.")
    print("Матрица времени в пути:", matrix_report)
    print("Записано:", output / "blocks.json")
    print("Записано:", output / "accessibility_matrix_walk.parquet")
    print("Записано:", output / "metadata.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
