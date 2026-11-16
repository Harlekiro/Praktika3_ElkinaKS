# -*- coding: utf-8 -*-
"""Проверки HTTP-API и раздачи интерфейса."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from school_app.server import create_server


@pytest.fixture(scope="module")
def base_url(model):
    httpd = create_server(model, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(base_url, path, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base_url + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, response.headers.get("Content-Type"), raw
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type"), error.read()


def call_json(base_url, path, body=None):
    status, _, raw = call(base_url, path, body)
    return status, json.loads(raw.decode("utf-8"))


def test_static_files(base_url):
    for path, kind in (("/", "text/html"), ("/static/app.js", "text/javascript"), ("/static/style.css", "text/css")):
        status, content_type, raw = call(base_url, path)
        assert status == 200 and content_type.startswith(kind)
    status, _, raw = call(base_url, "/")
    html = raw.decode("utf-8")
    for text in ("Рассчитать сценарий", "Разместить автоматически", "Вместимость новой школы, мест",
                 "Исходное состояние", "Сценарий", "Изменение", "градостроительного обоснования"):
        assert text in html
    assert call(base_url, "/static/../server.py")[0] in (400, 404)


def test_meta_and_blocks(base_url, model):
    status, meta = call_json(base_url, "/api/meta")
    assert status == 200 and meta["validation"]["status"] == "ok"
    assert meta["config"]["default_auto_capacity"] == 600
    assert meta["sources"] and meta["assumptions"]
    status, blocks = call_json(base_url, "/api/blocks")
    assert status == 200 and len(blocks["blocks"]) == len(model.block_ids)
    assert all(b["path"] and b["scenario_capacity"] == b["base_capacity"] for b in blocks["blocks"])


def test_scenario_api(base_url, model):
    status, result = call_json(base_url, "/api/scenario", {"placements": [{"block_id": 22, "capacity": 600}]})
    assert status == 200
    assert result["scenario"]["capacity_places"] == model.base_totals["capacity_places"] + 600
    assert result["scenario"]["deficit_places"] == 5


@pytest.mark.parametrize("capacity", [0, -5, 2.5, "abc", None])
def test_scenario_api_rejects_bad_capacity(base_url, capacity):
    status, result = call_json(base_url, "/api/scenario", {"placements": [{"block_id": 22, "capacity": capacity}]})
    assert status == 400 and "положительное целое" in result["error"]


def test_auto_api_rejects_bad_capacity(base_url):
    status, result = call_json(base_url, "/api/auto", {"capacity": 0})
    assert status == 400


def test_auto_api_cancel_gives_incomplete(base_url):
    status, job = call_json(base_url, "/api/auto", {"capacity": 600})
    assert status == 202
    call_json(base_url, f"/api/auto/{job['id']}/cancel", {})
    for _ in range(100):
        status, snap = call_json(base_url, f"/api/auto/{job['id']}")
        if snap["state"] not in ("running", "cancelling"):
            break
        time.sleep(0.1)
    assert snap["state"] == "cancelled"
    assert snap["result"]["complete"] is False
    assert "неполный" in snap["result"]["message"]


def test_unknown_job(base_url):
    status, result = call_json(base_url, "/api/auto/nope")
    assert status == 404 and "заново" in result["error"]
