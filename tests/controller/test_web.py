"""Results web on 10993 (02 12, 07 7, 16 S-44, 09 CTL-006, 20 GAP-86): read only,
report folders only (never outside, hidden files or zips), who may look by S-44, bytes
counted during a test, and a busy port only turns the web off."""

from __future__ import annotations

import http.client
import json
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.service import ControllerService
from controller.web import ResultsWeb, Traffic, allowed
from core.events.bus import Event
from core.protocol.http_server import Query

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("client", "mode", "ok"), [
    ("127.0.0.1", "This PC only", True), ("192.168.1.20", "This PC only", False),
    ("192.168.1.20", "Same subnet", True), ("10.9.9.9", "Same subnet", False),
    ("::ffff:192.168.1.20", "Same subnet", True), ("::1", "This PC only", True),
    ("127.0.0.1", "Off", False), ("not-an-ip", "Same subnet", False),
])  # fmt: skip
def test_who_may_look(client: str, mode: str, ok: bool) -> None:
    assert allowed(client, mode, ["192.168.1.10/255.255.255.0"]) is ok


def get(port: int, path: str, method: str = "GET") -> tuple[int, str, bytes]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request(method, path)
        r = c.getresponse()
        return r.status, r.getheader("Content-Type") or "", r.read()
    finally:
        c.close()


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[ResultsWeb, dict[str, Any]]]:
    rep = tmp_path / "rep"
    (rep / "R1").mkdir(parents=True)
    (rep / "R1" / "report.html").write_text("<p>report R1</p>")
    (rep / "R1" / "diagnostics-x.zip").write_bytes(b"PK")
    (rep / "R1" / ".hidden").write_text("no")
    (tmp_path / "secret.txt").write_text("outside")
    state: dict[str, Any] = {"mode": "This PC only", "running": None}
    w = ResultsWeb(
        rep,
        listing=lambda: {"reports": [{"run": "R1", "started": "2026-09-27 10:00",
                                      "verdict": "PASS", "test_type": "pc", "quality": "HD"}]},
        live=lambda: {"run": {"state": "RUNNING", "run_id": "R2", "devices": {
            "rx": {"row": {"color": "green", "items": [{"label": "Jitter", "value": 2.1,
                                                        "unit": "ms"}]}}},
            "cells": {"rx": [{"color": "red"}]}}},
        access=lambda: str(state["mode"]), networks=lambda: [],
        running=lambda: state["running"], traffic=Traffic(), port=0,
    )  # fmt: skip
    w.start()
    yield w, state
    w.stop()


def test_pages_and_files(web: tuple[ResultsWeb, dict[str, Any]]) -> None:
    w, _ = web
    code, kind, body = get(w.port, "/")
    assert code == 200 and kind.startswith("text/html") and b"R1" in body and b"PASS" in body
    assert b"/reports/R1/report.html" in body
    code, _, body = get(w.port, "/live")
    assert (
        code == 200 and b"Jitter 2.1ms" in body and b"refresh" in body and b"\xe2\x9c\x96" in body
    )
    assert get(w.port, "/reports/R1/report.html")[2] == b"<p>report R1</p>"
    for bad in ("/reports/R1/../../secret.txt", "/reports/%2e%2e/secret.txt",
                "/reports/R1/.hidden", "/reports/R1/diagnostics-x.zip", "/reports/R9/x.html",
                "/nothing"):  # fmt: skip
        assert get(w.port, bad)[0] == 404, bad
    assert get(w.port, "/", "POST")[0] == 405  # read only


def test_access_and_traffic_during_a_test(web: tuple[ResultsWeb, dict[str, Any]]) -> None:
    w, state = web
    state["mode"] = "Off"
    assert get(w.port, "/")[0] == 403
    state["mode"] = "This PC only"
    get(w.port, "/")
    assert w.traffic.take("R2") == {"requests": 0, "bytes": 0}  # no test: not counted
    state["running"] = "R2"
    _, _, body = get(w.port, "/live")
    assert b"use a wired PC" in body  # 07 7: viewing over the tested Wi-Fi mixes in
    got = w.traffic.take("R2")
    assert got["requests"] == 1 and got["bytes"] == len(body)
    assert w.traffic.take("R2") == {"requests": 0, "bytes": 0}  # taken once, for its report


def test_web_opens_once_the_busy_port_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-09: a test run held 10993 while the real Controller started; its live page
    stayed closed for the whole 2 h run. Now it tries again and opens when free."""
    import controller.service as service

    monkeypatch.setattr(service, "WEB_RETRY_S", 0.2)
    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))
    busy.listen()
    port = busy.getsockname()[1]
    seen: list[Event] = []
    svc = ControllerService(tmp_path / "ctl", tmp_path / "rep", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0, web_port=port)  # fmt: skip
    svc.node.bus.subscribe(seen.append)
    try:
        svc.start()
        assert svc.web is None
        busy.close()
        end = time.monotonic() + 10
        while svc.web is None and time.monotonic() < end:
            time.sleep(0.1)
        assert svc.web is not None and get(port, "/")[0] == 200
        assert [e.code for e in seen].count("ZL-CTL-006") == 1  # told once, not per retry
        assert any("open on port" in e.text for e in seen)
    finally:
        svc.stop()
        busy.close()


def test_service_opens_the_web_or_says_the_port_is_busy(tmp_path: Path) -> None:
    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))
    busy.listen()
    port = busy.getsockname()[1]
    seen: list[Event] = []
    svc = ControllerService(tmp_path / "ctl", tmp_path / "rep", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0, web_port=port)  # fmt: skip
    svc.node.bus.subscribe(seen.append)
    try:
        svc.start()
        assert svc.web is None and [e.code for e in seen if e.code == "ZL-CTL-006"]
    finally:
        svc.stop()
        busy.close()
    svc = ControllerService(tmp_path / "ctl2", tmp_path / "rep", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0, web_port=0)  # fmt: skip
    try:
        svc.start()
        assert svc.web is not None
        code, _, body = get(svc.web.port, "/")
        assert code == 200 and b"No reports yet" in body
        code, _, body = get(svc.web.port, "/live")  # the live monitor page (GAP-119)
        assert code == 200 and b"/live.json" in body
        assert b'id="ffrom"' in body and b"problems only" in body  # GAP-129 box filter
        code, kind, body = get(svc.web.port, "/live.json")  # its values, every second
        assert kind.startswith("application/json")
        live = json.loads(body)
        assert code == 200 and live["state"] == "IDLE" and live["receivers"] == []
    finally:
        svc.stop()
    assert "storage_low" in json.loads(json.dumps(svc.status(Query({}, "", ""))))
