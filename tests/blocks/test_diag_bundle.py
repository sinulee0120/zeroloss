"""Diagnostic files (02 4 ``GET /v1/logs``, 02 2-13, 13 ``zeroloss diagnostics``, 20
GAP-78): logs, records of programs, settings and state in one zip — never a secret,
never 1-second records or media, at most 50 MB per node (newest first), and a node or
PC that does not answer is written down instead of failing the file."""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from core.blocks import diag_bundle
from core.node import Node
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

REPO = Path(__file__).resolve().parents[2]


def names(z: zipfile.ZipFile) -> set[str]:
    return set(z.namelist())


def test_secrets_are_hidden_at_any_depth() -> None:
    pairs = [{"api_token": "t"}, {"hmac_secret": "h", "ok": 1}]
    value = {"ssid": "ZL-TEST", "password": "pw-9", "pairing": {"key": "k1", "keys": ["x"]},
             "list": pairs, "device_id": "d"}  # fmt: skip
    clean, hidden = diag_bundle.redact(value)
    assert clean == {"ssid": "ZL-TEST", "password": "***", "pairing": {"key": "***", "keys": ["x"]},
                     "list": [{"api_token": "***"}, {"hmac_secret": "***", "ok": 1}],
                     "device_id": "d"}  # fmt: skip
    assert sorted(hidden) == ["api_token", "hmac_secret", "key", "password"]


def test_what_goes_in_newest_first_under_the_limit(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "logs").mkdir(parents=True)
    (root / "runs" / "R1").mkdir(parents=True)
    old, new = root / "logs" / "old.log", root / "logs" / "new.log"
    old.write_bytes(b"o" * 600)
    new.write_bytes(b"n" * 600)
    os.utime(old, (1_000, 1_000))  # older
    (root / "runs" / "R1" / "seconds.jsonl").write_text("{}\n")  # 1-second records: never
    (root / "runs" / "R1" / "ch1.ts").write_bytes(b"G")  # media: never
    (root / "wifi.json").write_text('{"ssid": "x", "password": "p"}')
    (root / "agent.json").write_text('{"wan_isp": {"on": null}, "psk": "s"}')
    out = diag_bundle.build(root, ["logs/**/*", "runs/**/*", "*.json"], {"state.json": {"a": 1}},
                            {"device_id": "d1"}, limit_bytes=1000, skip=("wifi.json",))  # fmt: skip
    try:
        with zipfile.ZipFile(out) as z:
            assert names(z) == {"logs/new.log", "agent.json", "state.json", "manifest.json"}
            assert json.loads(z.read("agent.json"))["psk"] == "***"
            m = json.loads(z.read("manifest.json"))
        assert m["device_id"] == "d1" and m["limit_bytes"] == 1000
        assert m["left_out"] == [
            {"name": "logs/old.log", "bytes": 600, "why": "over the size limit"}
        ]
        assert "agent.json:psk" in m["redacted"]
    finally:
        out.unlink()


@pytest.fixture
def agent(tmp_path: Path) -> Any:
    n = Node(tmp_path / "agent", "agent", host="127.0.0.1", port=0, local_port=0,
             modules_dir=REPO / "modules", lost_after_s=60)  # fmt: skip
    n.start()
    yield n
    n.stop()


def leftovers() -> list[str]:
    return sorted(p.name for p in Path(tempfile.gettempdir()).glob("zeroloss-diag-*"))


def test_node_sends_its_bundle_as_a_file(agent: Node, tmp_path: Path) -> None:
    d = agent.data_dir
    (d / "runs" / "R20260926-1").mkdir(parents=True)
    (d / "runs" / "R20260926-1" / "tsp.log").write_text("tsp says hi\n")
    (d / "runs" / "R20260926-1" / "records.jsonl").write_text("{}\n")
    (d / "agent.json").write_text(json.dumps({"pairing": {"key": "hmac-key-1"}}))
    before = leftovers()
    c = NodeClient("127.0.0.1", agent.api.port, "boot")
    try:
        got = c.download("/v1/logs", tmp_path / "n.zip")
        with zipfile.ZipFile(got) as z:
            n = names(z)
            assert {"runs/R20260926-1/tsp.log", "state.json", "manifest.json",
                    "agent.json", "device_id"} <= n  # fmt: skip
            assert "runs/R20260926-1/records.jsonl" not in n
            assert any(x.startswith("logs/") for x in n)  # events.log at least
            assert b"hmac-key-1" not in z.read("agent.json")
            m = json.loads(z.read("manifest.json"))
            assert m["device_id"] == agent.device_id and m["run"] == "R20260926-1"
            assert json.loads(z.read("state.json"))["health"]["role"] == "agent"
        with pytest.raises(ZlError) as e:
            c.download("/v1/logs", tmp_path / "bad.zip", run="../etc")
        assert e.value.code == "ZL-DSC-012" and not (tmp_path / "bad.zip").exists()
        assert not list(tmp_path.glob("*.part"))
    finally:
        c.close()
    assert leftovers() == before  # the node removed its temporary zip


def test_controller_gathers_every_node_and_writes_down_the_silent_ones(
    agent: Node, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from controller.cli import main
    from controller.service import ControllerService

    svc = ControllerService(tmp_path / "ctl", tmp_path / "reports", REPO / "modules",
                            host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    svc.start()
    try:
        (tmp_path / "ctl" / "wifi.json").write_text('{"ssid": "ZL", "password_enc": "zzz"}')
        svc.devices.add("rx", "127.0.0.1", agent.api.port, "b")
        (tmp_path / "ctl" / "devices.json").write_text(json.dumps({"schema_version": 1, "devices": [
            *svc.devices.to_dict()["devices"],
            {"name": "gone", "host": "127.0.0.1", "port": 9, "device_id": "x", "role": "agent",
             "added_utc": "2026-09-26T00:00:00Z"}]}))  # fmt: skip
        from controller.devices import DeviceList

        svc.devices = DeviceList(tmp_path / "ctl" / "devices.json")
        code = main(["--json", "--port", str(svc.node.local.port), "diagnostics"])
        doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert code == 0, doc
        d = doc["data"]
        assert d["nodes"]["rx"] == "ok" and d["nodes"]["gone"].startswith("ZL-DSC-014")
        path = Path(d["path"])
        assert (
            path.parent == tmp_path / "reports" / "diagnostics"
            and path.stat().st_size == d["bytes"]
        )
        with zipfile.ZipFile(path) as z:
            n = names(z)
            assert {"controller.zip", "rx.zip", "manifest.json", "controller/devices.json",
                    "controller/status.json", "controller/problems.json"} <= n  # fmt: skip
            assert json.loads(z.read("manifest.json"))["nodes"]["gone"] != "ok"
            inner = zipfile.ZipFile(io.BytesIO(z.read("controller.zip")))
            assert not any(x.endswith("wifi.json") for x in inner.namelist())
            assert all(b"zzz" not in z.read(x) for x in n)
    finally:
        svc.stop()
