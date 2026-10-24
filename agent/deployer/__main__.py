"""``python -m agent.deployer`` / ``zeroloss-deployer`` (docs/06-install.md 4.3, 11).

- no sub-command: run the Deployer service (the installed unit passes
  ``--service systemd:zeroloss-agent`` or ``windows:ZeroLossAgent``);
- ``node-setup`` / ``node-remove [--stop-only]``: what the Linux package's
  maintainer scripts call (4.3-1). ``--root`` points at another root folder (tests).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

from agent.deployer import auto_login
from agent.deployer.agent_link import AgentLink, ServiceControl, SystemdService, WindowsService
from agent.deployer.modules import Layout
from agent.deployer.nodesetup import (
    NodePaths,
    System,
    node_remove,
    node_setup,
    turn_on_auto_login,
)
from agent.deployer.server import PORT, Deployer
from agent.deployer.update_hold import APT_DIR


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("node-setup", "node-remove"):
        return _node(args)
    if args and args[0] == "auto-login":
        return _auto_login(args[1:])
    ap = argparse.ArgumentParser(prog="zeroloss-deployer")
    ap.add_argument("--data", type=Path, required=True, help="node data folder")
    ap.add_argument("--modules", type=Path, required=True, help="module code folder")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--agent-port", type=int, default=10992)
    ap.add_argument("--service", required=True, help="systemd:<unit> | windows:<name>")
    ap.add_argument("--data-owner", help="account that owns module data folders (Linux)")
    ap.add_argument("--no-watch", action="store_true", help="do not restart a silent node")
    ns = ap.parse_args(args)
    service: ServiceControl
    kind, _, name = ns.service.partition(":")
    if kind == "systemd":
        service = SystemdService(name)
    elif kind == "windows":
        service = WindowsService(name)
    else:
        ap.error("--service must be systemd:<unit> or windows:<name>")
    dep = Deployer(Layout(ns.data, ns.modules), AgentLink(service, ns.agent_port),
                   ns.host, ns.port, watch=not ns.no_watch, owner=ns.data_owner,
                   apt_dir=APT_DIR, media_root=Path("/"))  # fmt: skip
    dep.start()
    print(f"ZeroLoss Deployer on {dep.port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        dep.stop()
    return 0


def _node(args: list[str]) -> int:
    ap = argparse.ArgumentParser(prog=f"zeroloss-deployer {args[0]}")
    ap.add_argument("--root", type=Path, default=Path("/"))
    ap.add_argument("--stop-only", action="store_true")
    ns = ap.parse_args(args[1:])
    paths = NodePaths(ns.root)
    if args[0] == "node-setup":
        out = node_setup(paths, System())
    else:
        out = node_remove(paths, System(), stop_only=ns.stop_only)
    print(json.dumps(out))
    return 1 if out.get("left") else 0


def _auto_login(args: list[str]) -> int:
    """0 on, 3 not supported here (no GDM: the person does it), 2 bad user name."""
    ap = argparse.ArgumentParser(prog="zeroloss-deployer auto-login")
    ap.add_argument("--user", required=True)
    ap.add_argument("--root", type=Path, default=Path("/"))
    ns = ap.parse_args(args)
    try:
        out = turn_on_auto_login(NodePaths(ns.root), ns.user)
    except auto_login.NotSupported as e:
        print(json.dumps({"error": str(e)}))
        return 3
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
