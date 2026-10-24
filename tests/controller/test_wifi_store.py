"""S-51 test Wi-Fi store and its 10994 / CLI paths (02 2-7): allowed values, owner-only
file, the password never shown, logged or put in any other file."""

from __future__ import annotations

import io
import json
import stat
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.cli import main
from controller.service import ControllerService
from controller.wifi_store import WifiConfig, WifiStore, check
from core.protocol.envelope import ZlError

REPO = Path(__file__).resolve().parents[2]
SECRET = "Pa55-w0rd-xyz"


@pytest.mark.parametrize(
    ("ssid", "sec", "pw"),
    [("", "WPA2", SECRET), ("x" * 33, "WPA2", SECRET), ("가" * 11, "WPA2", SECRET),
     ("ZL", "WEP", SECRET), ("ZL", "WPA2", "short"), ("ZL", "WPA2", "x" * 64),
     ("ZL", "Open", SECRET), ("ZL", "WPA3", None), ("ZL", "WPA2", "비밀번호비밀번호")],
)  # fmt: skip
def test_values_outside_the_table_are_refused(ssid: str, sec: str, pw: Any) -> None:
    with pytest.raises(ZlError) as e:
        check(ssid, sec, pw)
    assert e.value.code == "ZL-RUN-014" and (not pw or pw not in str(e.value.to_dict()))


def test_save_read_clear_owner_only(tmp_path: Path) -> None:
    s = WifiStore(tmp_path / "test-wifi.json")
    assert s.public() == {"ssid": None, "security": None, "has_password": False}
    assert s.secret() is None
    got = s.save(check("ZL-TEST", "WPA2", SECRET))
    assert got == {"ssid": "ZL-TEST", "security": "WPA2", "has_password": True}
    assert SECRET not in json.dumps(got) and SECRET not in repr(s.secret())
    assert s.secret() == WifiConfig("ZL-TEST", "WPA2", SECRET)
    if sys.platform != "win32":
        assert stat.S_IMODE((tmp_path / "test-wifi.json").stat().st_mode) == 0o600
    assert s.save(check("가나다", "Open", "")) == {"ssid": "가나다", "security": "Open",
                                                  "has_password": False}  # fmt: skip
    assert s.clear()["ssid"] is None and not (tmp_path / "test-wifi.json").exists()
    (tmp_path / "test-wifi.json").write_text("{broken")
    with pytest.raises(ZlError) as e:
        s.public()
    assert e.value.code == "ZL-SYS-008"


@pytest.fixture
def svc(tmp_path: Path) -> Iterator[ControllerService]:
    s = ControllerService(tmp_path / "ctl", tmp_path / "rep", REPO / "modules",
                          host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    s.start()
    yield s
    s.stop()


def test_cli_set_show_clear_never_prints_the_password(
    svc: ControllerService, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    port = ["--json", "--port", str(svc.node.local.port)]
    monkeypatch.setattr(sys, "stdin", io.StringIO(SECRET + "\n"))
    assert main([*port, "wifi", "set", "--ssid", "ZL-TEST", "--security", "WPA2",
                 "--password-stdin"]) == 0  # fmt: skip
    assert main([*port, "wifi", "show"]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
    bad = [*port, "wifi", "set", "--ssid", "ZL", "--security", "WPA2", "--password-stdin"]
    assert main(bad) == 6  # RUN-014 is bad input (13)
    out = capsys.readouterr().out
    shown = json.loads(out.strip().splitlines()[1])["data"]
    assert shown == {"ssid": "ZL-TEST", "security": "WPA2", "has_password": True}
    assert SECRET not in out
    assert svc.run._wifi() == WifiConfig("ZL-TEST", "WPA2", SECRET)  # the start uses it
    for f in (tmp_path / "ctl").rglob("*"):  # events, logs, experience DB: no password
        if f.is_file() and f.name != "test-wifi.json":
            assert SECRET.encode() not in f.read_bytes(), f
    assert main([*port, "wifi", "clear"]) == 0
    assert svc.wifi.public()["ssid"] is None
