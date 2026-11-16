# -*- coding: utf-8 -*-
"""HTTP-сервер приложения оценки вариантов размещения школ.

Только стандартная библиотека Python. Интерфейс — статические HTML/CSS/JS,
карта — SVG-схема кварталов, поэтому приложение работает без интернета.

API
---
GET  /api/meta                  параметры, проверка исходного расчёта, источники, допущения
GET  /api/blocks                кварталы, контуры и исходные показатели
POST /api/scenario              {"placements": [{"block_id": 22, "capacity": 600}, ...]}
POST /api/auto                  {"capacity": 600} → запуск перебора всех кварталов
GET  /api/auto/<id>             ход перебора и результат
POST /api/auto/<id>/cancel      остановить перебор
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .model import (
    ACCESSIBILITY_MIN,
    DEFAULT_AUTO_CAPACITY,
    DEMAND_PER_1000,
    MAX_CAPACITY,
    PROVISION_MODE,
    AutoSearchJob,
    DataValidationError,
    PlacementError,
    SchoolPlacementModel,
    parse_positive_int,
)


APP_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 256 * 1024
MAX_JOBS = 8
DATASETS = {"user": APP_DIR / "data_user", "practice2": APP_DIR / "data"}

SOURCES = [
    "Кварталы и их идентификаторы (_id) — BlocksNet 1.0.0a9 по данным OpenStreetMap; "
    "файл практики 2 examples/vysotsk/data/results/vysotsk_blocks_summary.geojson.",
    "Численность населения кварталов — столбец population того же слоя (BlocksNet impute_buildings + "
    "aggregate_objects по зданиям OSM, раздел 6.2 ноутбука).",
    "Вместимость существующих школ — столбец school_capacity (школы OSM amenity=school, вместимость "
    "дозаполнена BlocksNet impute_services, раздел 7.2): одна школа, 250 мест, квартал №24.",
    "Матрица времени в пути пешком — examples/vysotsk/data/accessibility_matrix_walk.parquet "
    "(255 × 255 кварталов, минуты; раздел 8.1).",
    "Норматив и радиус — blocksnet.config.service_types_config['school']: 120 мест на 1000 жителей, 15 минут.",
    "Сохранённые результаты для сверки — provision_report.csv, provision_links.parquet и столбец "
    "school_provision (раздел 9.5 ноутбука).",
]

ASSUMPTIONS = [
    "Численность населения, матрица времени в пути и норматив потребности во всех сценариях остаются исходными.",
    "Новая школа увеличивает суммарную вместимость школ квартала, в котором она размещена; "
    "несколько школ в одном квартале складываются.",
    "Расчётная потребность квартала: ceil(население / 1000 × 120) мест (так считает BlocksNet).",
    "Обеспеченность — доля потребности, закрытая местами в школах в пределах 15 минут пешком с учётом "
    "конкуренции кварталов за места (competitive_provision, задача линейного программирования).",
    "Дефицит мест — потребность минус удовлетворённый в пределах доступности спрос. "
    "Это не разность «потребность − вместимость».",
    "Автоподбор проверяет все кварталы, включая незаселённые: школа может обслуживать соседние кварталы. "
    "Каждый вариант считается от исходной сети школ; прирост вместимости между проверками не накапливается.",
    "Варианты с одинаковым сокращением дефицита (дефицит — целое число мест) считаются равноценными.",
    "Приложение не выбирает земельный участок и не оценивает возможность строительства: для этого нужно "
    "отдельное градостроительное обоснование.",
]


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Нельзя сериализовать {type(value).__name__}")


class JobRegistry:
    def __init__(self, model: SchoolPlacementModel):
        self.model = model
        self._jobs: dict[str, AutoSearchJob] = {}
        self._lock = threading.Lock()

    def start(self, capacity: int) -> AutoSearchJob:
        with self._lock:
            for job in self._jobs.values():
                if job.finished is None:
                    job.cancel()  # новый перебор заменяет незавершённый
            while len(self._jobs) >= MAX_JOBS:
                self._jobs.pop(next(iter(self._jobs)))
            job = AutoSearchJob(uuid.uuid4().hex[:12], self.model, capacity)
            self._jobs[job.id] = job
        return job.start()

    def get(self, job_id: str) -> AutoSearchJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ApiError("Перебор не найден (сервер мог быть перезапущен). Запустите автоподбор заново.", 404)
        return job


def meta_payload(model: SchoolPlacementModel) -> dict:
    metadata = dict(model.metadata)
    dataset = metadata.get("dataset") or {}
    return {
        "title": "Оценка вариантов размещения школ",
        "territory": metadata.get("territory"),
        "dataset_name": dataset.get("name", "Данные практики 2"),
        "dataset_log": dataset.get("log", []),
        "block_count": len(model.block_ids),
        "data_prepared_at": metadata.get("prepared_at"),
        "config": {
            "demand_per_1000": DEMAND_PER_1000,
            "accessibility_min": ACCESSIBILITY_MIN,
            "provision_mode": PROVISION_MODE,
            "default_auto_capacity": DEFAULT_AUTO_CAPACITY,
            "max_capacity": MAX_CAPACITY,
        },
        "base": model.base_totals,
        "validation": model.validation,
        "sources": dataset.get("sources") or SOURCES,
        "assumptions": ASSUMPTIONS,
        "files": {
            "source_files": metadata.get("source_files", {}),
            "source_hashes": metadata.get("source_hashes", {}),
            "data_dir": str(model.data_dir),
        },
    }


def blocks_payload(model: SchoolPlacementModel) -> dict:
    rows = model.scenario([])["blocks"]
    blocks = []
    for block, row in zip(model.blocks, rows):
        blocks.append({**row, "path": block["path"], "centroid": block["centroid"]})
    return {"view_box": model.view_box, "blocks": blocks}


class SchoolPlacementHandler(BaseHTTPRequestHandler):
    server_version = "SchoolPlacement/2.0"
    protocol_version = "HTTP/1.1"

    model: SchoolPlacementModel
    jobs: JobRegistry
    verbose = False

    def log_message(self, fmt, *args):
        if self.verbose:
            super().log_message(fmt, *args)

    def _send_bytes(self, body: bytes, status: int = 200, content_type: str = "application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=_json_default).encode("utf-8")
        self._send_bytes(body, status, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise ApiError("Некорректная длина тела запроса.") from error
        if length > MAX_BODY_BYTES:
            raise ApiError("Слишком большой запрос.", 413)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError("Тело запроса должно быть корректным JSON.") from error
        if not isinstance(payload, dict):
            raise ApiError("Тело запроса должно быть JSON-объектом.")
        return payload

    def _dispatch(self, handler):
        try:
            handler()
        except PlacementError as error:
            self._send_json({"error": str(error)}, 400)
        except ApiError as error:
            self._send_json({"error": error.message}, error.status)
        except DataValidationError as error:
            self._send_json({"error": str(error)}, 500)
        except Exception as error:  # pragma: no cover - защитная ветка
            traceback.print_exc()
            self._send_json({"error": f"Внутренняя ошибка: {type(error).__name__}: {error}"}, 500)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        def handle():
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/") :])
            elif path == "/api/meta":
                self._send_json(meta_payload(self.model))
            elif path == "/api/blocks":
                self._send_json(blocks_payload(self.model))
            elif path.startswith("/api/auto/"):
                self._send_json(self.jobs.get(path.split("/")[3]).snapshot())
            else:
                raise ApiError("Страница не найдена.", 404)

        self._dispatch(handle)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)

        def handle():
            payload = self._read_json()
            if path == "/api/scenario":
                self._send_json(self.model.scenario(payload.get("placements", [])))
            elif path == "/api/auto":
                capacity = parse_positive_int(payload.get("capacity"), "Вместимость новой школы")
                job = self.jobs.start(capacity)
                self._send_json(job.snapshot(), 202)
            elif path.startswith("/api/auto/") and path.endswith("/cancel"):
                job = self.jobs.get(path.split("/")[3])
                job.cancel()
                self._send_json(job.snapshot())
            else:
                raise ApiError("API-метод не найден.", 404)

        self._dispatch(handle)

    def _serve_static(self, relative: str):
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError as error:
            raise ApiError("Недопустимый путь.", 400) from error
        if not target.is_file():
            raise ApiError("Файл не найден.", 404)
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript"
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self._send_bytes(target.read_bytes(), 200, content_type)


def create_server(model: SchoolPlacementModel, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (SchoolPlacementHandler,),
        {"model": model, "jobs": JobRegistry(model)},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Оценка вариантов размещения школ (практика 2, Высоцк).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="начальный порт; если занят, берётся следующий")
    parser.add_argument(
        "--dataset",
        choices=sorted(DATASETS),
        help="набор данных: user — собственные данные (data_user), practice2 — данные практики 2 (data); "
        "по умолчанию user, если он подготовлен",
    )
    parser.add_argument("--data-dir", type=Path, help="произвольная папка данных (вместо --dataset)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--verbose", action="store_true", help="печатать HTTP-запросы")
    args = parser.parse_args(argv)
    if args.data_dir is None:
        name = args.dataset or ("user" if (DATASETS["user"] / "blocks.json").exists() else "practice2")
        args.data_dir = DATASETS[name]

    try:
        model = SchoolPlacementModel(args.data_dir)
    except Exception as error:
        print(f"Не удалось загрузить данные: {type(error).__name__}: {error}", file=sys.stderr)
        print("Подготовьте данные: .venv\\Scripts\\python.exe school-placement\\tools\\prepare_data.py", file=sys.stderr)
        return 1

    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = create_server(model, args.host, port)
            break
        except OSError:
            continue
    if httpd is None:
        print(f"Не удалось занять порт {args.port}–{args.port + 19}.", file=sys.stderr)
        return 1
    httpd.RequestHandlerClass.verbose = args.verbose

    host, port = httpd.server_address[:2]
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    totals, check = model.base_totals, model.validation
    print("Оценка вариантов размещения школ")
    print(f"  Адрес:            {url}")
    print(f"  Набор данных:     {(model.metadata.get('dataset') or {}).get('name', '—')} ({model.data_dir})")
    print(f"  Кварталов:        {len(model.block_ids)}, население {totals['population']:.0f} чел.")
    print(f"  Потребность:      {totals['demand_places']} мест, вместимость школ {totals['capacity_places']} мест")
    print(f"  Исходный дефицит: {totals['deficit_places']} мест, обеспеченность {totals['provision']:.3f}")
    print(
        f"  Сверка с практикой 2: {check['status']} — {check['compared_blocks']} кварталов, "
        f"max |Δ| = {check['max_abs_diff']:.1e}, связей {check['links_count']} (совпадают: {check['links_equal']})"
    )
    for warning in check["warnings"]:
        print(f"  Предупреждение: {warning}")
    print("  Остановка: Ctrl+C", flush=True)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
