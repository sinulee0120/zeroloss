"""02 2-11: a setup edit between tests is saved at once, even during the idle check."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from controller.run import RunController, RunState
from controller.service import ControllerService
from controller.setup import SetupStore
from core.protocol.envelope import Request, ZlError


def test_a_setup_edit_does_not_wait_for_the_idle_check(tmp_path: Path) -> None:
    """2026-11-27: the screen's idle check holds the run lock up to 60 s; an edit (the
    screen waits 5 s) said "Cannot reach Controller service" though it was saved later."""
    run: Any = SimpleNamespace(state=RunState.IDLE, _lock=threading.RLock())
    run.not_in_test = lambda what: RunController.not_in_test(run, what)
    svc: Any = SimpleNamespace(run=run, setup=SetupStore(tmp_path / "controller.json", {}))
    req = Request(command_id="c", generation=0, run_id=None,
                  params={"changes": {"duration_s": 10800}})  # fmt: skip
    held, done = threading.Event(), threading.Event()

    def idle_check() -> None:
        with run._lock:
            held.set()
            done.wait(10)

    t = threading.Thread(target=idle_check, daemon=True)
    t.start()
    held.wait(5)
    began = time.monotonic()
    got = ControllerService.setup_set(svc, req)
    assert time.monotonic() - began < 1.0 and got["setup"]["duration_s"] == 10800
    done.set()
    t.join(5)
    run.state = RunState.RUNNING
    with pytest.raises(ZlError) as e:
        ControllerService.setup_set(svc, req)
    assert e.value.code == "ZL-RUN-012"
