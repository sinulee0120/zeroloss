"""Process runner (docs/03-features.md child cleanup, 12 4-3, SYS-006, SYS-007)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks.process_runner import (
    TOKEN_ENV,
    ProcessRunner,
    ProcState,
    StartError,
    stop_leftovers,
)

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux path")
PY = sys.executable
IGNORE_TERM = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def wait_until(cond: object, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():  # type: ignore[operator]
            return True
        time.sleep(0.02)
    return False


def tokened_pids() -> set[int]:
    """Every live process started by any runner (carries the token variable)."""
    found = set()
    for entry in os.scandir("/proc"):
        if entry.name.isdigit():
            try:
                with open(f"/proc/{entry.name}/environ", "rb") as f:
                    if f"{TOKEN_ENV}=".encode() in f.read():
                        found.add(int(entry.name))
            except OSError:
                pass
    return {p for p in found if alive(p)}


def children_of(pid: int) -> list[int]:
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    return [int(x) for x in out.stdout.split()]


def test_start_and_polite_stop(tmp_path: Path) -> None:
    r = ProcessRunner("sleeper", ["sleep", "60"], tmp_path)
    r.start()
    assert r.state is ProcState.RUNNING and (tmp_path / "sleeper.json").exists()
    pid = r.pid
    assert pid is not None
    res = r.stop()
    assert not res.forced and res.exit_code == -signal.SIGTERM
    assert r.poll() is ProcState.IDLE and not alive(pid)
    assert not (tmp_path / "sleeper.json").exists()


def test_ignores_sigterm_is_force_killed(tmp_path: Path) -> None:
    r = ProcessRunner("stubborn", [PY, "-c", IGNORE_TERM], tmp_path, stop_timeout_s=0.5)
    r.start(ready=lambda: True)
    time.sleep(0.3)  # let it install the handler
    res = r.stop()
    assert res.forced and res.exit_code == -signal.SIGKILL


def test_grandchildren_are_stopped_too(tmp_path: Path) -> None:
    r = ProcessRunner("shell", ["sh", "-c", "sleep 60 & sleep 60"], tmp_path)
    r.start()
    assert r.pid is not None
    assert wait_until(lambda: len(children_of(r.pid)) >= 2)
    kids = children_of(r.pid)
    r.stop()
    assert not any(alive(k) for k in kids)


def test_unexpected_exit_is_fault_then_cleanup(tmp_path: Path) -> None:
    r = ProcessRunner("crasher", [PY, "-c", "import sys; sys.exit(3)"], tmp_path)
    r.start()
    assert wait_until(lambda: r.poll() is ProcState.FAULT)
    assert r.exit_code == 3
    with pytest.raises(RuntimeError):
        r.start()  # FAULT must be cleaned up first
    r.stop()
    assert r.state is ProcState.IDLE


def test_ready_check(tmp_path: Path) -> None:
    flag = tmp_path / "ready"
    prog = (
        f"import pathlib,time; time.sleep(0.2); pathlib.Path({str(flag)!r}).touch(); time.sleep(60)"
    )
    r = ProcessRunner("slowstart", [PY, "-c", prog], tmp_path)
    r.start(ready=flag.exists, ready_timeout_s=5)
    assert r.state is ProcState.RUNNING
    r.stop()


def test_not_ready_in_time_is_rolled_back(tmp_path: Path) -> None:
    r = ProcessRunner("never", ["sleep", "60"], tmp_path)
    with pytest.raises(StartError, match="not ready"):
        r.start(ready=lambda: False, ready_timeout_s=0.3)
    assert r.state is ProcState.IDLE
    assert r.pid is not None and not alive(r.pid)
    assert not (tmp_path / "never.json").exists()


def test_exit_during_start_and_missing_program(tmp_path: Path) -> None:
    r = ProcessRunner("quick", [PY, "-c", "pass"], tmp_path)
    with pytest.raises(StartError, match="exited"):
        r.start(ready=lambda: False, ready_timeout_s=5)
    assert r.state is ProcState.IDLE
    missing = ProcessRunner("missing", [str(tmp_path / "no-such-tool")], tmp_path)
    with pytest.raises(StartError):
        missing.start()
    assert missing.state is ProcState.IDLE


def test_commands_outside_their_state_are_refused(tmp_path: Path) -> None:
    r = ProcessRunner("s", ["sleep", "60"], tmp_path)
    with pytest.raises(RuntimeError):
        r.stop()
    r.start()
    with pytest.raises(RuntimeError):
        r.start()
    r.stop()
    with pytest.raises(RuntimeError):
        r.stop()


AGENT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from core.blocks.process_runner import ProcessRunner
    r = ProcessRunner("player", ["sh", "-c", "sleep 60 & sleep 60"], Path(sys.argv[1]))
    r.start()
    print(r.pid, flush=True)
    time.sleep(60)
    """
)


def test_agent_death_kills_child_and_restart_cleans_leftovers(tmp_path: Path) -> None:
    """The direct child dies with the agent (parent-death signal). A grandchild outlives
    it; the restarted agent stops it from the run record. Unrelated processes survive."""
    bystander = subprocess.Popen(["sleep", "60"])  # same kind of program, no token
    agent = subprocess.Popen(
        [PY, "-c", AGENT, str(tmp_path)],
        stdout=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    try:
        assert agent.stdout is not None
        child = int(agent.stdout.readline())
        assert wait_until(lambda: len(children_of(child)) >= 2)
        grandkids = children_of(child)
        agent.kill()
        agent.wait()
        assert wait_until(lambda: not alive(child))
        assert any(alive(g) for g in grandkids)  # orphans left behind
        stopped = stop_leftovers(tmp_path)
        assert [rec["name"] for rec in stopped] == ["player"]
        assert wait_until(lambda: not any(alive(g) for g in grandkids))
        assert alive(bystander.pid)
        assert stop_leftovers(tmp_path) == []
    finally:
        bystander.kill()
        bystander.wait()
        agent.kill()


def test_leftover_record_with_reused_pid_kills_nothing(tmp_path: Path) -> None:
    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        rec = f'{{"name":"old","pid":{bystander.pid},"token":"deadbeef","argv":["sleep"]}}'
        (tmp_path / "old.json").write_text(rec)
        assert stop_leftovers(tmp_path) == []
        assert alive(bystander.pid)
        assert not (tmp_path / "old.json").exists()
    finally:
        bystander.kill()
        bystander.wait()


@settings(max_examples=15, deadline=None)
@given(ops=st.lists(st.sampled_from(["start", "stop", "poll", "crash"]), max_size=10))
def test_random_commands_follow_state_flow_and_leave_nothing(ops: list[str]) -> None:
    """Invariants: refused commands change nothing; after cleanup no runner process is
    left and no run record remains."""
    import tempfile

    before = tokened_pids()
    with tempfile.TemporaryDirectory() as d:
        r = ProcessRunner("m", [PY, "-c", IGNORE_TERM], Path(d), stop_timeout_s=0.3)
        for op in ops:
            state = r.state
            if op == "start":
                if state is ProcState.IDLE:
                    r.start()
                else:
                    with pytest.raises(RuntimeError):
                        r.start()
                    assert r.state is state
            elif op == "stop":
                if state in (ProcState.RUNNING, ProcState.FAULT):
                    r.stop()
                    assert r.state is ProcState.IDLE
                else:
                    with pytest.raises(RuntimeError):
                        r.stop()
            elif op == "poll":
                r.poll()
            elif op == "crash" and state is ProcState.RUNNING and r.pid:
                os.kill(r.pid, signal.SIGKILL)
                assert wait_until(lambda: r.poll() is ProcState.FAULT)
        if r.state in (ProcState.RUNNING, ProcState.FAULT):
            r.stop()
        assert r.state is ProcState.IDLE
        assert list(Path(d).glob("*.json")) == []
    assert tokened_pids() <= before


def test_a_child_outlives_the_thread_that_asked_for_it(tmp_path: Path) -> None:
    """2026-10-09 (senderPC): tsp started inside an HTTP request thread got SIGKILL
    (PR_SET_PDEATHSIG) about 3 min later, when that thread ended."""
    import threading

    box: list[ProcessRunner] = []

    def ask() -> None:
        r = ProcessRunner("sleeper", ["sleep", "60"], tmp_path)
        r.start()
        box.append(r)

    t = threading.Thread(target=ask)
    t.start()
    t.join()
    time.sleep(0.5)
    r = box[0]
    try:
        assert r.poll() is ProcState.RUNNING
    finally:
        r.stop()
