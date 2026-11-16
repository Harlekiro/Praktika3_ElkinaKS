#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Расчёт обеспеченности школами на собственном наборе данных по Высоцку.

Повторяет шаги ноутбука практики 2 (examples/vysotsk/vysotsk_study.ipynb) теми же функциями BlocksNet,
но берёт исходные слои из папки пользователя, а не из OpenStreetMap:

* кварталы — ``blocks.geojson`` как есть (мелкие кварталы НЕ объединяются);
* население — ``building.geojson``, столбец ``population`` (уже задан по зданиям, поэтому
  ``impute_buildings`` не нужен), агрегация ``aggregate_objects`` — раздел 6.2 ноутбука;
* школы — ``Cервисы/school.geojson``, столбец ``capacity``, агрегация ``aggregate_objects`` — раздел 7.2;
* матрица времени в пути — ``calculate_accessibility_matrix`` по пешеходному графу — раздел 8.1: собственный граф
  по границе набора (``--download-graph`` скачивает его один раз, как ноутбук) или граф практики 2;
* обеспеченность — ``competitive_provision(table, matrix, 15, 120)`` — раздел 9.5.

Папка ``Сценарий/`` не используется. Исходные файлы (папка пользователя и практика 2) только читаются.

Результат записывается в структуру, совместимую с практикой 2, после чего данные приложения
собирает ``tools/prepare_data.py --source <выход> --output data_user``.

Запуск из корня репозитория:

    .\\.venv\\Scripts\\python.exe school-placement\\tools\\build_user_dataset.py --source "D:\\...\\Высоцкое городское поселение"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from loguru import logger


APP_DIR = Path(__file__).resolve().parents[1]
PRACTICE2_DATA = APP_DIR.parent / "examples" / "vysotsk" / "data"
DEFAULT_OUTPUT = APP_DIR / "sources" / "user_vysotsk"
TARGET_CRS = "EPSG:32635"  # как в практике 2
SERVICE = "school"
PROVISION_MODE = "walk"
FAR_FROM_GRAPH_M = 300  # квартал дальше от ближайшего узла графа — предупреждение

# Как в ноутбуке практики 2 (раздел 1.1).
OVERPASS_SERVERS = [
    "https://overpass-api.de/api",
    "https://overpass.private.coffee/api",
    "https://overpass.kumi.systems/api",
    "https://maps.mail.ru/osm/tools/overpass/api",
    "https://overpass.osm.jp/api",
]
FETCH_ATTEMPTS = 2
FETCH_PAUSE_S = 15
OVERPASS_TIMEOUT_S = 300


def download_walk_graph(territory: gpd.GeoDataFrame):
    """Скачивает пешеходный граф по границе — так же, как раздел 8.1 ноутбука."""
    import time

    import iduedu as ie
    from blocksnet.relations.accessibility import get_accessibility_graph

    ie.config.set_timeout(OVERPASS_TIMEOUT_S)
    last_error = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        for server in OVERPASS_SERVERS:
            host = server.split("/")[2]
            try:
                ie.config.set_overpass_url(server + "/interpreter")
                graph = get_accessibility_graph(territory, "walk")
                print("   граф скачан, сервер:", host)
                return graph
            except Exception as error:
                last_error = error
                print("   сервер", host, "не ответил:", type(error).__name__)
        if attempt < FETCH_ATTEMPTS:
            print("   пауза", FETCH_PAUSE_S, "с и новая попытка…")
            time.sleep(FETCH_PAUSE_S)
    raise SystemExit(f"Не удалось скачать пешеходный граф: {type(last_error).__name__}: {last_error}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_services_dir(source: Path) -> Path:
    # В наборе папка называется «Cервисы» с латинской C; ищем без учёта этой особенности.
    for child in source.iterdir():
        if child.is_dir() and child.name.lower().replace("c", "с") == "сервисы":
            return child
    raise FileNotFoundError(f"В {source} нет папки «Сервисы».")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Расчёт обеспеченности школами на собственных данных.")
    parser.add_argument("--source", type=Path, required=True, help="папка «Высоцкое городское поселение»")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--graph", type=Path,
        help="пешеходный граф (pickle). По умолчанию: собственный граф набора <output>/data/graph_walk.pickle, "
        "если он скачан, иначе граф практики 2",
    )
    parser.add_argument("--download-graph", action="store_true",
                        help="скачать пешеходный граф по границе набора (boundary.geojson) и сохранить его")
    args = parser.parse_args()
    logger.disable("blocksnet")
    own_graph = args.output.expanduser().resolve() / "data" / "graph_walk.pickle"
    if args.download_graph:
        boundary_path = args.source.expanduser().resolve() / "boundary.geojson"
        territory = gpd.read_file(boundary_path).to_crs(TARGET_CRS)[["geometry"]]
        print("Скачиваю пешеходный граф по границе", boundary_path.name, f"({territory.area.sum() / 1e4:.0f} га)…")
        graph = download_walk_graph(territory)
        own_graph.parent.mkdir(parents=True, exist_ok=True)
        with own_graph.open("wb") as file:
            pickle.dump(graph, file)
        print("   сохранён:", own_graph, f"(узлов {graph.number_of_nodes()}, рёбер {graph.number_of_edges()})")
    if args.graph is None:
        args.graph = own_graph if own_graph.exists() else PRACTICE2_DATA / "graph_walk.pickle"
    graph_label = "собственный граф по границе набора" if args.graph.resolve() == own_graph else "граф практики 2"

    from blocksnet.analysis.provision import competitive_provision, provision_strong_total, provision_weak_total
    from blocksnet.blocks.aggregation import aggregate_objects
    from blocksnet.config import service_types_config
    from blocksnet.relations.accessibility import calculate_accessibility_matrix

    source = args.source.expanduser().resolve()
    services_dir = find_services_dir(source)
    files = {
        "blocks": source / "blocks.geojson",
        "buildings": source / "building.geojson",
        "school": services_dir / "school.geojson",
        "landuse": source / "landuse.geojson",
        "boundary": source / "boundary.geojson",
        "graph_walk": args.graph.expanduser().resolve(),
    }
    missing = [f"  - {name}: {path}" for name, path in files.items() if not path.exists()]
    if missing:
        raise SystemExit(
            "Не хватает обязательных файлов:\n" + "\n".join(missing) + "\n"
            "graph_walk.pickle создаётся ноутбуком практики 2 (раздел 8.1); остальные файлы — из набора пользователя."
        )

    settings = service_types_config[SERVICE]
    accessibility, demand = int(settings["accessibility"]), int(settings["demand"])
    report_lines: list[str] = []

    def note(text: str) -> None:
        print(text)
        report_lines.append(text)

    # --- Кварталы -----------------------------------------------------------------------------
    blocks = gpd.read_file(files["blocks"]).to_crs(TARGET_CRS)[["geometry"]].reset_index(drop=True)
    if blocks.geometry.isna().any() or blocks.is_empty.any():
        raise SystemExit("В blocks.geojson есть пустые геометрии.")
    if (~blocks.is_valid).any():
        raise SystemExit(f"В blocks.geojson есть невалидные геометрии: {int((~blocks.is_valid).sum())}.")
    note(f"Кварталы: {len(blocks)} (порядковый номер строки файла = _id), без объединения мелких; "
         f"меньше 0,3 га: {int((blocks.area < 3000).sum())}.")

    # --- Землепользование: тип зоны с наибольшей площадью пересечения ---------------------------
    landuse = gpd.read_file(files["landuse"]).to_crs(TARGET_CRS)
    landuse["label"] = landuse["Тип"].fillna(landuse["Зона"]).astype(str).str.strip()
    landuse.loc[landuse["label"].isin(["", "-", "None", "nan"]), "label"] = landuse["Зона"]
    pieces = gpd.overlay(blocks.reset_index(names="bid"), landuse[["label", "geometry"]], how="intersection",
                         keep_geom_type=True)
    pieces["area"] = pieces.area
    dominant = pieces.sort_values("area", ascending=False).drop_duplicates("bid").set_index("bid")
    share = dominant["area"] / blocks.area.loc[dominant.index]
    land_use_label = pd.Series("Не задано", index=blocks.index, dtype=object)
    covered = share[share >= 0.25].index
    land_use_label.loc[covered] = dominant.loc[covered, "label"]
    note(f"Землепользование: тип зоны определён для {len(covered)} кварталов (доля зоны ≥ 25 %).")

    # --- Население (раздел 6.2) ---------------------------------------------------------------
    buildings = gpd.read_file(files["buildings"]).to_crs(TARGET_CRS)
    buildings["population"] = pd.to_numeric(buildings["population"], errors="coerce")
    if buildings["population"].isna().any() or (buildings["population"] < 0).any():
        raise SystemExit("В building.geojson есть пустое или отрицательное население.")
    buildings_blocks, excess = aggregate_objects(blocks, buildings[["population", "geometry"]])
    population = buildings_blocks["population"].astype(float)
    total_pop = float(buildings["population"].sum())
    note(f"Население: по зданиям {total_pop:.0f} чел., по кварталам {population.sum():.0f} чел.; "
         f"зданий вне кварталов: {len(excess)} (население {excess['population'].sum():.0f}); "
         f"населённых кварталов: {int((population > 0).sum())}.")
    if abs(population.sum() - total_pop + excess["population"].sum()) > 0.5:
        note("ВНИМАНИЕ: часть зданий попала в несколько кварталов сразу (точка на границе).")

    # --- Школы (раздел 7.2) -------------------------------------------------------------------
    schools = gpd.read_file(files["school"]).to_crs(TARGET_CRS)
    schools["capacity"] = pd.to_numeric(schools["capacity"], errors="coerce")
    if schools["capacity"].isna().any():
        raise SystemExit("У школы в school.geojson не указана вместимость (capacity).")
    school_blocks, school_excess = aggregate_objects(blocks, schools[["capacity", "geometry"]])
    if len(school_excess):
        raise SystemExit(f"Школ вне кварталов: {len(school_excess)} — проверьте school.geojson и blocks.geojson.")
    capacity = school_blocks["capacity"].fillna(0).round().astype(int)
    names = ", ".join(str(n) for n in schools.get("NAME", pd.Series(dtype=str)).tolist())
    note(f"Школы: {len(schools)} ({names}); вместимость {int(capacity.sum())} мест в кварталах "
         f"{[int(i) for i in capacity[capacity > 0].index]}.")

    # --- Матрица времени в пути (раздел 8.1) ---------------------------------------------------
    with files["graph_walk"].open("rb") as file:
        graph = pickle.load(file)
    matrix = calculate_accessibility_matrix(blocks, graph)
    matrix.index = matrix.index.astype(int)
    matrix.columns = matrix.columns.astype(int)
    values = matrix.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise SystemExit("Матрица времени в пути содержит недостижимые пары кварталов (inf/NaN).")
    nodes = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy([d["x"] for _, d in graph.nodes(data=True)], [d["y"] for _, d in graph.nodes(data=True)]),
        crs=graph.graph.get("crs", TARGET_CRS),
    ).to_crs(TARGET_CRS)
    points = gpd.GeoDataFrame(geometry=blocks.representative_point(), crs=TARGET_CRS)
    snap = gpd.sjoin_nearest(points, nodes, distance_col="dist")["dist"].groupby(level=0).min()
    far = snap[snap > FAR_FROM_GRAPH_M]
    far_pop = far[population.loc[far.index] > 0]
    note(f"Матрица: {matrix.shape[0]} × {matrix.shape[1]}, время {values.min():.1f}–{values.max():.1f} мин; "
         f"{graph_label}: {args.graph} ({graph.number_of_nodes()} узлов).")
    note(f"Расстояние от квартала до ближайшего узла графа: медиана {snap.median():.0f} м; дальше {FAR_FROM_GRAPH_M} м — "
         f"{len(far)} кварталов, из них населённых {len(far_pop)}"
         + (f": {[(int(i), round(float(d))) for i, d in far_pop.items()]}" if len(far_pop) else "") + ".")

    warnings: list[str] = []
    if len(far_pop):
        far_people = float(population.loc[far_pop.index].sum())
        warnings.append(
            f"Населённые кварталы {[int(i) for i in far_pop.index]} ({far_people:.0f} чел.) лежат дальше "
            f"{FAR_FROM_GRAPH_M} м от пешеходного графа ({graph_label}; до {far_pop.max():.0f} м). Дороги рядом с ними "
            "в OSM не связаны с основной пешеходной сетью, а BlocksNet/iduedu оставляет только связную сеть. "
            "Время в пути считается от ближайшего узла графа без учёта подхода к нему и может быть занижено."
        )

    # --- Обеспеченность (раздел 9.5) -----------------------------------------------------------
    table = blocks[["geometry"]].join(population.rename("population")).join(capacity.rename("capacity"))
    table["capacity"] = table["capacity"].fillna(0).astype(int)
    prov_df, links_df = competitive_provision(table, matrix, accessibility, demand)
    strong, weak = provision_strong_total(prov_df), provision_weak_total(prov_df)
    deficit = int(prov_df["demand"].sum() - prov_df["demand_within"].sum())
    note(f"Обеспеченность школами: потребность {int(prov_df['demand'].sum())} мест, вместимость {int(capacity.sum())}, "
         f"удовлетворено {int(prov_df['demand_within'].sum())}, дефицит {deficit} мест, "
         f"обеспеченность {strong:.3f} (слабая {weak:.3f}), связей {len(links_df)}.")

    # --- Запись в структуре практики 2 ---------------------------------------------------------
    output = args.output.expanduser().resolve()
    data_dir, results_dir = output / "data", output / "data" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    summary = blocks.copy()
    summary["population"] = population
    summary["school_capacity"] = capacity
    summary["school_provision"] = prov_df["provision_strong"]
    summary["school_provision_weak"] = prov_df["provision_weak"]
    summary["land_use_label"] = land_use_label
    summary["_id"] = summary.index
    summary.to_crs("EPSG:4326").to_file(results_dir / "vysotsk_blocks_summary.geojson", driver="GeoJSON")
    matrix.to_parquet(data_dir / "accessibility_matrix_walk.parquet")
    pd.DataFrame([{
        "сервис": SERVICE,
        "название": settings.get("name_ru", SERVICE),
        "граф": PROVISION_MODE,
        "радиус, мин": accessibility,
        "норматив на 1000 жителей": demand,
        "обеспеченность (сильная)": round(strong, 3),
        "обеспеченность (слабая)": round(weak, 3),
        "связей": int(len(links_df)),
    }]).to_csv(results_dir / "provision_report.csv", index=False, encoding="utf-8-sig")
    links_df.reset_index().assign(сервис=SERVICE).to_parquet(results_dir / "provision_links.parquet", index=False)
    (data_dir / "version.txt").write_text("user-1", encoding="utf-8")

    dataset = {
        "name": "Собственный набор данных по Высоцку",
        "territory": "Высоцкое городское поселение, Ленинградская область",
        "source_folder": str(source),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_hashes": {name: sha256(path) for name, path in files.items()},
        "sources": [
            f"Кварталы — blocks.geojson пользователя ({len(blocks)} шт., без объединения мелких); "
            "_id — порядковый номер строки файла.",
            f"Население — building.geojson пользователя, столбец population ({total_pop:.0f} чел.), "
            "агрегация по кварталам BlocksNet aggregate_objects.",
            f"Вместимость школ — Сервисы/school.geojson пользователя: {names}, {int(capacity.sum())} мест.",
            f"Матрица времени в пути пешком — BlocksNet calculate_accessibility_matrix; {graph_label} "
            f"(BlocksNet get_accessibility_graph, OpenStreetMap; {graph.number_of_nodes()} узлов).",
            "Тип землепользования квартала — landuse.geojson пользователя (поле «Тип» зоны с наибольшей площадью).",
            f"Норматив и радиус — blocksnet.config.service_types_config['school']: {demand} мест на 1000 жителей, "
            f"{accessibility} минут.",
            "Слой «Сценарий» набора пользователя не используется.",
        ],
        "log": report_lines,
        "warnings": warnings,
    }
    (output / "dataset.json").write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Записано:", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
