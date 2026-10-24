"""Experience DB (docs/14-autonomy.md 1) and schema versions (02 11, INS-025)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.events.bus import Event, EventBus
from core.protocol.envelope import ZlError
from core.records.experience import Context, ExperienceDb
from core.records.sqlite_schema import open_db

CTX = Context(build="20260924.1300", run_id="R20260924-130000", dut="AX3000 v1.2")


def rows(path: Path, sql: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path) as c:
        result: list[tuple[Any, ...]] = c.execute(sql).fetchall()
        return result


def test_events_become_situations(tmp_path: Path) -> None:
    db = ExperienceDb(tmp_path / "experience.db", CTX)
    bus = EventBus()
    bus.subscribe(db.record_event)
    bus.publish(
        Event(
            "ZL-TAB-005",
            "warn",
            "Wi-Fi weak",
            device="tablet-2",
            elapsed_s=12.5,
            data={"device_kind": "tablet", "module": "iptv", "rssi": -71},
        )
    )
    db.close()
    (row,) = rows(
        tmp_path / "experience.db",
        "SELECT code, device, device_kind, module, run_id, dut, values_json FROM situations",
    )
    assert row == ("ZL-TAB-005", "tablet-2", "tablet", "iptv", CTX.run_id, CTX.dut, '{"rssi": -71}')


def test_action_lifecycle_and_stats(tmp_path: Path) -> None:
    db = ExperienceDb(tmp_path / "e.db", CTX)
    sit = db.record_situation("ZL-TAB-005", "warn", "Wi-Fi weak")
    a = db.start_action("tablet.wifi.reconnect", "auto", situation_id=sit)
    db.finish_action(a, "resolved")
    with pytest.raises(ValueError):
        db.finish_action(a, "made_worse")  # the result is written once
    b = db.start_action("tablet.wifi.reconnect", "human", actor_name="kim", note="moved closer")
    db.finish_action(b, "not_resolved")
    assert db.action_stats("tablet.wifi.reconnect") == {"resolved": 1, "not_resolved": 1}
    with pytest.raises(ValueError):
        db.start_action("Restart Agent", "human")  # not a standard name
    with pytest.raises(ValueError):
        db.finish_action(db.start_action("agent.restart", "auto"), "pending")
    db.close()
    ((dur, note),) = rows(tmp_path / "e.db", f"SELECT duration_s, note FROM actions WHERE id={b}")
    assert dur is not None and dur >= 0 and note == "moved closer"


def test_actions_left_pending_by_a_crash_become_unknown(tmp_path: Path) -> None:
    db = ExperienceDb(tmp_path / "e.db", CTX)
    db.start_action("agent.restart", "auto")
    db._db.close()  # crash: no finish
    again = ExperienceDb(tmp_path / "e.db", CTX)
    assert again.interrupted == 1
    assert again.action_stats("agent.restart") == {"unknown": 1}
    again.close()


def test_newer_file_is_refused_and_untouched(tmp_path: Path) -> None:
    path = tmp_path / "e.db"
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA user_version = 7")
    before = path.read_bytes()
    with pytest.raises(ZlError) as e:
        ExperienceDb(path, CTX)
    assert e.value.code == "ZL-INS-025" and "newer" in e.value.what
    assert path.read_bytes() == before


def test_failed_upgrade_restores_copy_and_refuses(tmp_path: Path) -> None:
    path = tmp_path / "x.db"

    def v1(c: sqlite3.Connection) -> None:
        c.execute("CREATE TABLE t (a INTEGER)")
        c.execute("INSERT INTO t VALUES (1)")

    def v2_broken(c: sqlite3.Connection) -> None:
        c.execute("ALTER TABLE t ADD COLUMN b INTEGER")
        raise RuntimeError("bug in upgrade")

    open_db(path, [v1], "b1").close()
    with pytest.raises(ZlError) as e:
        open_db(path, [v1, v2_broken], "b2")
    assert e.value.code == "ZL-INS-025"
    assert rows(path, "PRAGMA user_version") == [(1,)]
    assert [r[1] for r in rows(path, "PRAGMA table_info(t)")] == ["a"]
    assert (tmp_path / "x.db.bak-b2").exists()

    def v2(c: sqlite3.Connection) -> None:
        c.execute("ALTER TABLE t ADD COLUMN b INTEGER")

    open_db(path, [v1, v2], "b3").close()
    assert rows(path, "SELECT a, b FROM t") == [(1, None)]


def test_first_create_failure_leaves_no_file(tmp_path: Path) -> None:
    def broken(c: sqlite3.Connection) -> None:
        raise RuntimeError("x")

    with pytest.raises(ZlError):
        open_db(tmp_path / "new.db", [broken], "b1")
    assert not (tmp_path / "new.db").exists()


def test_parallel_writers(tmp_path: Path) -> None:
    db = ExperienceDb(tmp_path / "e.db", CTX)

    def work(n: int) -> None:
        for i in range(50):
            s = db.record_situation("ZL-INFO", "info", f"{n}-{i}")
            db.finish_action(db.start_action("run.test_step", "auto", s), "resolved")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert db.action_stats("run.test_step") == {"resolved": 300}
    db.close()


@settings(max_examples=40, deadline=None)
@given(
    ops=st.lists(
        st.one_of(
            st.tuples(st.just("start"), st.sampled_from(["agent.restart", "tablet.app.restart"])),
            st.tuples(st.just("finish"), st.sampled_from(["resolved", "not_resolved"])),
            st.tuples(st.just("crash"), st.just("")),
        ),
        max_size=25,
    )
)
def test_random_actions_every_action_ends_with_one_outcome(
    tmp_path_factory: pytest.TempPathFactory, ops: list[tuple[str, str]]
) -> None:
    """Invariant: each started action has exactly one outcome; after a crash the
    unfinished ones are unknown, never lost and never resolved."""
    path = tmp_path_factory.mktemp("x") / "e.db"
    db = ExperienceDb(path, CTX)
    open_rows: list[int] = []
    expected: dict[str, int] = {}
    for op, arg in ops:
        if op == "start":
            open_rows.append(db.start_action(arg, "auto"))
        elif op == "finish" and open_rows:
            db.finish_action(open_rows.pop(0), arg)  # type: ignore[arg-type]
            expected[arg] = expected.get(arg, 0) + 1
        elif op == "crash":
            db._db.close()
            if open_rows:
                expected["unknown"] = expected.get("unknown", 0) + len(open_rows)
            open_rows = []
            db = ExperienceDb(path, CTX)
    db.close()
    got: dict[str, int] = dict(rows(path, "SELECT outcome, COUNT(*) FROM actions GROUP BY outcome"))
    if open_rows:
        expected["pending"] = len(open_rows)
    assert got == expected
