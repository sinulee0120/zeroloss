"""Common API envelope (docs/02-data.md 3) and error form (docs/09-exceptions.md 0.2).

Request body:  {"run_id", "command_id", "generation", "params"}
Response:      {"ok": true, "data": {...}}
               {"ok": false, "error": {code, what, why, fix, detail}}

``CommandGuard`` applies the rules every command endpoint shares:
- a ``command_id`` seen before returns the earlier result, never runs twice (0.3-4);
- a command from an older ``generation`` is refused (DSC-011); the newest accepted
  generation becomes the current one;
- a Controller boot ID not seen before means the Controller restarted: the caller must
  safe-stop the old run first (CTL-008).
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
BOOT_HEADER = "X-ZL-Controller-Boot"


class ZlError(Exception):
    """An exception with its code from docs/09-exceptions.md, shown as What / Why / Fix."""

    def __init__(
        self, code: str, what: str, why: str = "", fix: str = "", detail: str = ""
    ) -> None:
        super().__init__(f"{code}: {what}")
        self.code = code if code.startswith("ZL-") else f"ZL-{code}"
        self.what = what
        self.why = why
        self.fix = fix
        self.detail = detail

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "what": self.what,
            "why": self.why,
            "fix": self.fix,
            "detail": self.detail,
        }


def ok(data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": dict(data or {})}


def fail(err: ZlError) -> dict[str, Any]:
    return {"ok": False, "error": err.to_dict()}


@dataclass(frozen=True)
class Request:
    command_id: str
    generation: int
    run_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def parse(body: object) -> Request:
        """Validate a decoded JSON body. Raises ZlError DSC-012."""
        if not isinstance(body, dict):
            raise _bad("body must be a JSON object")
        command_id = body.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            raise _bad("command_id is required")
        generation = body.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise _bad("generation must be a non-negative integer")
        run_id = body.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            raise _bad("run_id must be a string")
        params = body.get("params", {})
        if not isinstance(params, dict):
            raise _bad("params must be an object")
        return Request(command_id, generation, run_id, params)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "command_id": self.command_id,
            "generation": self.generation,
            "params": self.params,
        }


def new_command(generation: int, run_id: str | None = None, **params: Any) -> Request:
    return Request(str(uuid.uuid4()), generation, run_id, dict(params))


def _bad(detail: str) -> ZlError:
    return ZlError(
        "DSC-012", "Request rejected", "The request is not in the expected form.", "", detail
    )


class CommandGuard:
    """Shared command rules. One per node; thread safe."""

    def __init__(self, generation: int = 0, remember: int = 1000) -> None:
        self.generation = generation
        self._remember = remember
        self._results: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._running: dict[str, threading.Event] = {}
        self._boot_ids: set[str] = set()
        self.last_run_id: str | None = None  # the run the node works for (02 4 /v1/storage)
        self._lock = threading.Lock()

    def controller_boot(self, boot_id: str | None) -> bool:
        """Record a Controller boot ID. True when it is new while an older one was known
        (the Controller restarted: safe-stop the old run before handling the request)."""
        if not boot_id:
            return False
        with self._lock:
            if boot_id in self._boot_ids:
                return False
            restarted = bool(self._boot_ids)
            self._boot_ids = {boot_id}
            return restarted

    def bump_generation(self) -> int:
        """On Reset or restart: older commands are refused from now on."""
        with self._lock:
            self.generation += 1
            return self.generation

    def run(
        self, req: Request, handler: Callable[[Request], Mapping[str, Any] | None]
    ) -> dict[str, Any]:
        """Run ``handler`` once per command ID and return the response envelope.
        A repeat that arrives while the first is still running waits for its result.
        An unexpected exception is not remembered: it is raised again, and a waiting
        repeat then runs the command itself."""
        while True:
            with self._lock:
                if req.command_id in self._results:
                    self._results.move_to_end(req.command_id)
                    return self._results[req.command_id]
                waiting = self._running.get(req.command_id)
                if waiting is None:
                    if req.generation < self.generation:
                        return fail(
                            ZlError(
                                "DSC-011",
                                "Old command ignored",
                                f"It was sent before the last reset (generation "
                                f"{req.generation}, now {self.generation}).",
                                detail=req.command_id,
                            )
                        )
                    self.generation = max(self.generation, req.generation)
                    if req.run_id:
                        self.last_run_id = req.run_id
                    done = threading.Event()
                    self._running[req.command_id] = done
                    break
            waiting.wait()
        try:
            response = ok(handler(req))
        except ZlError as e:
            response = fail(e)
        except BaseException:
            with self._lock:
                self._running.pop(req.command_id)
            done.set()
            raise
        with self._lock:
            self._results[req.command_id] = response
            while len(self._results) > self._remember:
                self._results.popitem(last=False)
            self._running.pop(req.command_id)
        done.set()
        return response
