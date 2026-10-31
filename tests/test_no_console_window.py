"""No console window flashes on the Controller PC (2026-10-24 user: "cmd창이 계속 떴다 사라져").

A windowed program starting a console tool opens a visible console for it unless the
call passes CREATE_NO_WINDOW. Every subprocess call that can run on Windows passes
``creationflags``; the files below run only on Linux.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CALLS = {"run", "Popen", "check_output", "check_call", "call"}
LINUX_ONLY = {
    "agent/deployer/node_update.py", "agent/deployer/nodesetup.py",
    "agent/deployer/server.py", "agent/deployer/update_hold.py",
    "core/blocks/link_monitor.py", "core/blocks/nic_speed.py", "core/blocks/sleep_guard.py",
    "core/blocks/time_sync.py", "core/blocks/wifi_join.py", "core/blocks/wifi_power.py",
    "modules/iptv/receiver/perf.py",
}  # fmt: skip


def _missing(path: Path) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                and node.func.attr in CALLS
                and not any(k.arg in ("creationflags", None) for k in node.keywords)):  # fmt: skip
            lines.append(node.lineno)
    return lines


def test_every_windows_subprocess_call_hides_its_console() -> None:
    bad = []
    for top in ("core", "controller", "modules", "agent"):
        for f in sorted((REPO / top).rglob("*.py")):
            rel = f.relative_to(REPO).as_posix()
            if rel not in LINUX_ONLY:
                bad += [f"{rel}:{n}" for n in _missing(f)]
    assert not bad, bad
