"""Every child starts on the one long-lived spawner thread (2026-10-09, senderPC: a
tsp started inside an HTTP request thread got the parent-death SIGKILL when that thread
ended). Runs on every platform; the Linux kill itself is in test_process_runner."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from core.blocks.process_runner import SPAWNER_NAME, ProcessRunner


def test_children_start_on_the_spawner_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real = subprocess.Popen

    def popen(argv: Any, **kw: Any) -> Any:
        seen.append(threading.current_thread().name)
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", popen)

    def ask() -> None:
        r = ProcessRunner("quick", [sys.executable, "-c", "pass"], tmp_path)
        r.start()
        r.stop()

    for _ in range(2):  # from two short-lived threads
        t = threading.Thread(target=ask)
        t.start()
        t.join()
    assert seen == [SPAWNER_NAME, SPAWNER_NAME]


def test_a_failed_start_comes_back_to_the_caller(tmp_path: Path) -> None:
    from core.blocks.process_runner import StartError

    r = ProcessRunner("missing", [str(tmp_path / "no-such-program")], tmp_path)
    with pytest.raises((StartError, OSError)):
        r.start()
