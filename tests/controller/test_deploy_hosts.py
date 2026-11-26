"""The wait for a node update asks every address of that PC (2026-11-26 review): the
Deployer may answer only on its other card (the ProBook, 2026-10-17)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from controller.deploy import Deploy
from core.protocol.envelope import ZlError


class FakeClient:
    def __init__(self, host: str, answers: dict[str, dict[str, Any]]) -> None:
        self.host, self.answers = host, answers

    def get(self, path: str, timeout_s: float = 0) -> dict[str, Any]:
        if self.host not in self.answers:
            raise ZlError("DSC-014", "Cannot reach the Deployer")
        return self.answers[self.host]

    def close(self) -> None:
        pass


def _deploy(monkeypatch: pytest.MonkeyPatch, answers: dict[str, dict[str, Any]]) -> Deploy:
    d = Deploy.__new__(Deploy)
    d.update_wait_s, d.poll_s = 2.0, 0.01
    monkeypatch.setattr(d, "hosts", lambda node: ["192.168.1.50", "10.0.0.1"])
    monkeypatch.setattr(d, "_client", lambda node, host=None: FakeClient(str(host), answers))
    return d


def test_the_update_is_followed_on_the_other_card(monkeypatch: pytest.MonkeyPatch) -> None:
    up = {"id": "u1", "state": "done", "from_build": "a", "to_build": "b"}
    d = _deploy(monkeypatch, {"10.0.0.1": {"update": up}})
    r = d._wait_update("probook", {"update_id": "u1"})
    assert r["result"] == "updated" and r["build"] == "b"


def test_no_card_answers_is_waited_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda s: None)
    d = _deploy(monkeypatch, {})
    d.update_wait_s = 0.05
    with pytest.raises(ZlError) as e:
        d._wait_update("probook", {"update_id": "u1"})
    assert e.value.code == "ZL-INS-013"
