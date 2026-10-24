"""Action Required board (docs/03-features.md F22, 02 2-3): what a person must do.

Kept apart from Problems (F21): a problem says what went wrong, an action says what a
person has to do. Actions are opened by problem codes (``catalog.json``: ``opened_by``
maps a code to whether its clearing proves the action done). An action closes by
itself when that problem clears (02 8 ``clears``) or, when nothing can detect it, by
[Done]. One row per ``action_id`` + device; finished rows are kept up to ``KEEP_DONE``.
``grace_s`` (2026-10-09, user: "Done is not clickable"): an action is shown only once
it has waited that long, so a 2 s blip that closes itself never asks a person anything.
A Wi-Fi card that drops asks for the router's Wi-Fi, not a cable.
``on_done`` lets the service write the action to the experience DB (14 1).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.events.bus import Event, utc_now

_FILE = Path(__file__).with_name("catalog.json")
CATALOG: dict[str, dict[str, Any]] = json.loads(_FILE.read_text(encoding="utf-8"))["actions"]
KEEP_DONE = 200
_BY_CODE: dict[str, tuple[str, bool]] = {
    code: (aid, bool(auto)) for aid, a in CATALOG.items() for code, auto in a["opened_by"].items()
}


class ActionBoard:
    def __init__(
        self, on_done: Callable[[dict[str, Any], str], None] | None = None, grace: bool = True
    ) -> None:
        self._grace = grace  # False: every action shows at once (tests of other rules)
        self._on_done = on_done or (lambda action, actor: None)
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []  # insertion order (newest last)
        self._since: dict[str, float] = {}  # key -> monotonic time it was asked
        self.clock: Callable[[], float] = time.monotonic

    def __call__(self, e: Event) -> None:
        """Event bus subscriber. Never raises."""
        finished: list[tuple[dict[str, Any], str]] = []
        with self._lock:
            clears = (e.data or {}).get("clears")
            if isinstance(clears, list):
                for row in self._rows.values():
                    if (
                        row["state"] == "waiting"
                        and row["device"] == e.device
                        and row["opened_by"] in clears
                        and row["auto_done"]
                    ):
                        finished.append((self._finish(row, "auto"), "auto"))
            if e.level in ("warn", "error") and e.code in _BY_CODE:
                self._open(e)
        for row, actor in finished:
            self._on_done(row, actor)

    def done(self, key: str) -> bool:
        """[Done] from the screen or CLI. False when there is nothing waiting."""
        with self._lock:
            row = self._rows.get(key)
            if row is None or row["state"] != "waiting":
                return False
            copy = self._finish(row, "human")
        self._on_done(copy, "human")
        return True

    def waiting_row(self, key: str) -> dict[str, Any] | None:
        """A copy of a waiting action (for a choice), or None."""
        with self._lock:
            row = self._rows.get(key)
            return dict(row) if row is not None and row["state"] == "waiting" else None

    def _shown(self, row: dict[str, Any]) -> bool:
        """A row a person sees: it waited its ``grace_s``, or a person closed it."""
        grace = float(CATALOG[row["action_id"]].get("grace_s") or 0) if self._grace else 0.0
        if not grace or row["done_by"] == "human":
            return True
        waited = row.get("_waited_s")
        if waited is None:
            waited = self.clock() - self._since.get(row["key"], self.clock())
        return bool(waited >= grace)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            rows = [
                {k: v for k, v in self._rows[k].items() if k != "_waited_s"}
                for k in reversed(self._order)
                if self._shown(self._rows[k])
            ]
            waiting = [dict(r) for r in rows if r["state"] == "waiting"]
            finished = [dict(r) for r in rows if r["state"] != "waiting"]
            return {"waiting": len(waiting), "actions": waiting + finished}

    def run_report(self, run_id: str) -> list[dict[str, Any]]:
        """02 10 ``human_actions`` of one run, oldest first."""
        with self._lock:
            return [
                {
                    k: r[k]
                    for k in ("action_id", "device", "what", "requested_utc", "done_utc", "done_by")
                }
                for r in (self._rows[k] for k in self._order)
                if r["run_id"] == run_id and self._shown(r)
            ]

    # ---------------------------------------------------------------- inside the lock

    def _open(self, e: Event) -> None:
        aid, auto = _BY_CODE[e.code]
        if e.code == "ZL-NET-015" and _wifi_card(e):
            aid = "human.wifi.check"
        key = f"{aid}|{e.device}"
        row = self._rows.get(key)
        if row is not None and row["state"] == "waiting":
            return  # already asked; the first reason stays
        a = CATALOG[aid]
        if row is not None:
            self._order.remove(key)
        self._rows[key] = {
            "key": key,
            "action_id": aid,
            "device": e.device,
            "what": a["what"],
            "how": a["how"],
            "why": e.text,
            "opened_by": e.code,
            "state": "waiting",
            "done_by": None,
            "auto_done": auto,
            "repeat": a["repeat"],
            "requested_utc": e.ts_utc,
            "done_utc": None,
            "run_id": str((e.data or {}).get("run_id") or ""),
            # 20 W-1: a choice the person makes by a button instead of by hand (02 2-3)
            "choose": _text_or_none((e.data or {}).get("choose")),
            "choices": _choices((e.data or {}).get("choices")),
            # where the person's choice goes (a module's list waiting for it)
            "ask": _ask((e.data or {}).get("ask")),
        }
        self._order.append(key)
        self._since[key] = self.clock()

    def _finish(self, row: dict[str, Any], actor: str) -> dict[str, Any]:
        row["state"], row["done_by"], row["done_utc"] = "done", actor, utc_now()
        row["_waited_s"] = self.clock() - self._since.get(row["key"], self.clock())
        done = [k for k in self._order if self._rows[k]["state"] != "waiting"]
        for k in done[: max(0, len(done) - KEEP_DONE)]:
            self._order.remove(k)
            del self._rows[k]
            self._since.pop(k, None)
        return {k: v for k, v in row.items() if k != "_waited_s"}


def _wifi_card(e: Event) -> bool:
    """NET-015 of a Wi-Fi card: its reading before the drop had Wi-Fi values."""
    d = e.data or {}
    return any(isinstance((d.get(k) or {}).get("wifi"), dict) for k in ("before", "after"))


def _ask(v: Any) -> dict[str, Any] | None:
    return dict(v) if isinstance(v, dict) else None


def _text_or_none(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _choices(v: Any) -> list[dict[str, str]] | None:
    """``[{"id", "label"}]`` from the event, or None (anything else is dropped)."""
    if not isinstance(v, list):
        return None
    out = [{"id": str(c["id"]), "label": str(c.get("label") or c["id"])}
           for c in v if isinstance(c, dict) and c.get("id")]  # fmt: skip
    return out or None
