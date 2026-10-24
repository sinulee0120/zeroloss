"""Experience DB: every situation, every action taken, and its outcome (docs/14-autonomy.md 1).

Phase 1 only records (automatic response level 0). The later prediction and
recommendation models read this; fixed pass criteria are never learned.

- ``situations``: one row per event from the event flow (code, device, module, stage,
  values). ``record_event`` is an event-bus subscriber.
- ``actions``: one row per response (button, automatic, human task, CLI), always with a
  standard ``action_id``, linked to the situation it answers when known.
- Actions still ``pending`` when the program died are marked ``unknown`` at next start.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.events.bus import Event, utc_now
from core.records.sqlite_schema import open_db

Actor = Literal["auto", "human", "claude", "remote"]
Outcome = Literal["resolved", "not_resolved", "made_worse", "pending", "unknown"]
FINAL: frozenset[str] = frozenset({"resolved", "not_resolved", "made_worse"})
ACTION_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")


def _v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE situations (
            id INTEGER PRIMARY KEY,
            ts_utc TEXT NOT NULL,
            code TEXT NOT NULL,
            level TEXT NOT NULL,
            text TEXT NOT NULL,
            device TEXT NOT NULL DEFAULT '',
            device_kind TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT '',
            module TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            elapsed_s REAL,
            build TEXT NOT NULL,
            dut TEXT NOT NULL DEFAULT '',
            values_json TEXT NOT NULL DEFAULT '{}'
        )"""
    )
    conn.execute("CREATE INDEX situations_code ON situations(code)")
    conn.execute(
        """CREATE TABLE actions (
            id INTEGER PRIMARY KEY,
            situation_id INTEGER REFERENCES situations(id),
            action_id TEXT NOT NULL,
            actor TEXT NOT NULL,
            actor_name TEXT NOT NULL DEFAULT '',
            started_utc TEXT NOT NULL,
            finished_utc TEXT,
            duration_s REAL,
            outcome TEXT NOT NULL DEFAULT 'pending',
            note TEXT NOT NULL DEFAULT '',
            build TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX actions_action ON actions(action_id)")


MIGRATIONS = [_v1]


@dataclass(frozen=True)
class Context:
    """Facts that do not change during a run, stored with each row."""

    build: str
    run_id: str = ""
    dut: str = ""  # DUT model and firmware as the user entered them
    stage: str = ""  # IDLE / STARTING / RUNNING / STOPPING / FAULT


class ExperienceDb:
    def __init__(self, path: Path, context: Context) -> None:
        self.context = context
        self._db = open_db(path, MIGRATIONS, context.build)
        self._lock = threading.Lock()
        self._started: dict[int, float] = {}
        with self._lock:
            self.interrupted = self._db.execute(
                "UPDATE actions SET outcome = 'unknown' WHERE outcome = 'pending'"
            ).rowcount

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record_event(self, event: Event) -> None:
        """Event-bus subscriber: every event becomes a situation."""
        d = event.data or {}
        self.record_situation(
            event.code,
            event.level,
            event.text,
            device=event.device,
            device_kind=str(d.get("device_kind", "")),
            role=str(d.get("role", "")),
            module=str(d.get("module", "")),
            elapsed_s=event.elapsed_s,
            values={k: v for k, v in d.items() if k not in ("device_kind", "role", "module")},
            ts_utc=event.ts_utc,
        )

    def record_situation(
        self,
        code: str,
        level: str,
        text: str,
        *,
        device: str = "",
        device_kind: str = "",
        role: str = "",
        module: str = "",
        elapsed_s: float | None = None,
        values: dict[str, Any] | None = None,
        ts_utc: str | None = None,
    ) -> int:
        c = self.context
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO situations (ts_utc, code, level, text, device, device_kind,
                   role, module, stage, run_id, elapsed_s, build, dut, values_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ts_utc or utc_now(),
                    code,
                    level,
                    text,
                    device,
                    device_kind,
                    role,
                    module,
                    c.stage,
                    c.run_id,
                    elapsed_s,
                    c.build,
                    c.dut,
                    json.dumps(values or {}, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
            return int(cur.lastrowid or 0)

    def start_action(
        self,
        action_id: str,
        actor: Actor,
        situation_id: int | None = None,
        actor_name: str = "",
        note: str = "",
    ) -> int:
        if not ACTION_ID.match(action_id):
            raise ValueError(f"action_id {action_id!r} is not a standard name")
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO actions (situation_id, action_id, actor, actor_name,
                   started_utc, note, build) VALUES (?,?,?,?,?,?,?)""",
                (situation_id, action_id, actor, actor_name, utc_now(), note, self.context.build),
            )
            row = int(cur.lastrowid or 0)
            self._started[row] = time.monotonic()
            return row

    def finish_action(self, action_row: int, outcome: Outcome, note: str = "") -> None:
        """Record the result once. A second finish of the same action is refused."""
        if outcome not in FINAL:
            raise ValueError(f"outcome must be one of {sorted(FINAL)}")
        with self._lock:
            started = self._started.pop(action_row, None)
            duration = round(time.monotonic() - started, 3) if started is not None else None
            cur = self._db.execute(
                """UPDATE actions SET outcome = ?, finished_utc = ?, duration_s = ?,
                   note = CASE WHEN ? = '' THEN note ELSE ? END
                   WHERE id = ? AND outcome = 'pending'""",
                (outcome, utc_now(), duration, note, note, action_row),
            )
            if cur.rowcount != 1:
                raise ValueError(f"action {action_row} is not pending")

    def action_stats(self, action_id: str) -> dict[str, int]:
        """Outcome counts for one action: the raw material for later weights."""
        with self._lock:
            rows = self._db.execute(
                "SELECT outcome, COUNT(*) FROM actions WHERE action_id = ? GROUP BY outcome",
                (action_id,),
            ).fetchall()
        return {str(o): int(n) for o, n in rows}
