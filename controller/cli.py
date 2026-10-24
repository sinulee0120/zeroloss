"""``zeroloss`` command line (docs/13-ai-loop.md 2): a thin window onto the Controller
service at 127.0.0.1:10994. Same commands as the screen app.

Output: a sentence for people, or with ``--json`` always
``{"ok", "exit_code", "codes", "data"}``. Exit codes are fixed so Claude can act on them:
0 success/PASS, 1 FAIL, 2 INCOMPLETE, 3 start refused, 4 needs --yes, 5 cannot connect,
6 bad command or plan file, 10 internal error.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from core.events.bus import utc_now
from core.node import LOCAL_PORT
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

P = "/v1/controller"
LONG_RUN_S = 24 * 3600  # 13-ai-loop 3: long tests need --yes
DEPLOY_TIMEOUT_S = 1080.0  # upload + install + node restart; a node update 15 min (06 11.5)
INSTALL_LINUX_TIMEOUT_S = 1800.0  # upload, dpkg lock wait up to 10 min (INS-011), modules
START_TIMEOUT_S = 12 * 3600  # a start may make videos first (iptv 1-1-2); stalls fail on the node
VERDICT_EXIT = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}
BAD_INPUT = {"ZL-DSC-012", "ZL-RUN-014"}
UNREACHABLE = {"ZL-DSC-014", "ZL-INS-014"}  # node or its Deployer does not answer


def exit_code_for(code: str) -> int:
    if code in UNREACHABLE:
        return 5
    if code in BAD_INPUT:
        return 6
    if code.startswith("ZL-SYS-"):
        return 10
    return 3  # the Controller refused or could not start what was asked


class Cli:
    def __init__(self, port: int, as_json: bool) -> None:
        self.as_json = as_json
        self.client = NodeClient("127.0.0.1", port, f"cli-{uuid.uuid4()}", "Controller service")

    def command(self, path: str, timeout_s: float = 5.0, **params: Any) -> dict[str, Any]:
        gen = int(self.client.get(f"{P}/status").get("generation", 0))
        return self.client.command(path, gen, timeout_s=timeout_s, **params)

    def out(self, exit_code: int, data: Any, text: str, codes: list[str] | None = None) -> int:
        if self.as_json:
            print(
                json.dumps(
                    {
                        "ok": exit_code == 0,
                        "exit_code": exit_code,
                        "codes": codes or [],
                        "data": data,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(text)
        return exit_code


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="zeroloss", description="ZeroLoss Controller command line")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--port", type=int, default=LOCAL_PORT, help="Controller local port")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Controller and current test state")
    dev = sub.add_parser("devices", help="device list")
    dsub = dev.add_subparsers(dest="action", required=True)
    dsub.add_parser("list")
    add = dsub.add_parser("add")
    add.add_argument("name")
    add.add_argument("host")
    add.add_argument("--node-port", type=int, default=10992)
    add.add_argument("--deployer-port", type=int, default=10991)
    rm = dsub.add_parser("remove")
    rm.add_argument("name")
    wan = dsub.add_parser("wan", help="the sender PC as the router WAN's ISP (07 3.2 B)")
    wan.add_argument("name")
    onoff = wan.add_mutually_exclusive_group()
    onoff.add_argument("--on", action="store_true", help="give the router WAN its address")
    onoff.add_argument("--off", action="store_true", help="stop and take our address off")
    auto_help = "back to the default: starts by itself on a router WAN port"
    onoff.add_argument("--auto", action="store_true", help=auto_help)
    wan.add_argument("--nic", help="the WAN network card (default: the one free wired card)")
    run = sub.add_parser("run", help="start a test from a plan file")
    run.add_argument("--plan", type=Path, help="a plan file (default: the saved screen choice)")
    run.add_argument("--wait", action="store_true", help="wait for the verdict")
    run.add_argument("--yes", action="store_true", help="confirm a test longer than 24 h")
    sub.add_parser("stop", help="stop the current test and make the report")
    ins = sub.add_parser("install", help="first install of a node")
    isub = ins.add_subparsers(dest="action", required=True)
    lin = isub.add_parser("linux", help="install a Linux node over SSH (06 11.3)")
    lin.add_argument("--host", required=True)
    lin.add_argument("--user", required=True)
    lin.add_argument("--name", default="", help="device name (default: from the IP)")
    lin.add_argument("--ssh-port", type=int, default=22)
    lin.add_argument("--node-port", type=int, default=10992)
    lin.add_argument("--deployer-port", type=int, default=10991)
    lin.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    lin.add_argument("--accept-new-host-key", action="store_true", help="after a reinstall")
    lin.add_argument("--package", type=Path, help="the .deb (default: newest in packages/)")
    lin.add_argument("--module", type=Path, action="append", help="a .zlm (repeatable)")
    lin.add_argument("--auto-login", action="store_true",
                     help="log this account in after a reboot (GAP-67)")  # fmt: skip
    lin.add_argument("--yes", action="store_true", help="confirm the SSH install")
    dep = sub.add_parser("deploy", help="send a package to a node's Deployer (10991)")
    dep.add_argument("--node", required=True)
    dep.add_argument("--package", required=True, type=Path)
    mods = sub.add_parser("modules", help="modules on a node")
    msub = mods.add_subparsers(dest="action", required=True)
    ml = msub.add_parser("list")
    ml.add_argument("--node", required=True)
    mr = msub.add_parser("remove")
    mr.add_argument("id")
    mr.add_argument("--node", required=True)
    mr.add_argument("--purge", action="store_true", help="also delete the module's settings")
    mr.add_argument("--dry-run", action="store_true", help="only list what would be removed")
    mr.add_argument("--yes", action="store_true", help="confirm the removal")
    src = sub.add_parser("sources", help="source videos in the sender PC's media folder")
    src.add_argument("--node", required=True, help="the sender device")
    src.add_argument("--module", default="iptv")
    dc = sub.add_parser("discover", help="find nodes (broadcast, sender addresses, a range)")
    dc.add_argument("--scan", help="also ask every address of a range, e.g. 192.168.1.0/24")
    dc.add_argument("--no-try-sender", action="store_true", help="skip 10.0.0.1 / 10.0.0.2")
    dc.add_argument("--node-port", type=int, default=10992)
    rs = sub.add_parser("reset", help="Reset All: every device stops and checks it is clean")
    rs.add_argument("--yes", action="store_true", help="confirm the reset")
    nd = sub.add_parser("nodes", help="restart one node, stop or start every node (F11)")
    ndsub = nd.add_subparsers(dest="action", required=True)
    nr = ndsub.add_parser("restart")
    nr.add_argument("--node", required=True)
    ns = ndsub.add_parser("stop", help="Shut Down All: node services stop, Deployers stay")
    ns.add_argument("--yes", action="store_true", help="confirm")
    ndsub.add_parser("start", help="start every node service again")
    wifi = sub.add_parser("wifi", help="the test Wi-Fi (S-51) for tablets and Wi-Fi PCs")
    wsub = wifi.add_subparsers(dest="action", required=True)
    wsub.add_parser("show")
    ws = wsub.add_parser("set")
    ws.add_argument("--ssid", required=True)
    ws.add_argument("--security", required=True, choices=["Open", "WPA2", "WPA3"])
    ws.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    wsub.add_parser("clear")
    pre = sub.add_parser("precheck", help="check a plan before a test (F17 1-5, no test)")
    pre.add_argument("--plan", type=Path, help="a plan file (default: the saved screen choice)")
    su = sub.add_parser("setup", help="the saved test choice the screen and Start use (F14)")
    susub = su.add_subparsers(dest="action", required=True)
    susub.add_parser("show")
    ss = susub.add_parser("set", help="e.g. quality=HD S-49=Off sender=tx nic=tx:10.0.0.1")
    ss.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    prep = sub.add_parser("prepare", help="make the generated videos before a test")
    prep.add_argument("--sender", required=True, help="sender device name")
    prep.add_argument("--quality", choices=["UHD", "HD"])
    prep.add_argument("--bitrate", type=int, help="Mbps (default: the quality's default)")
    prep.add_argument("--ch", default="1,2,3", help="channels, e.g. 1,2,3")
    prep.add_argument("--module", default="iptv")
    prep.add_argument("--wait", action="store_true", help="wait until every video is made")
    prep.add_argument("--status", action="store_true", help="only show the progress")
    sub.add_parser("problems", help="open and resolved problems (F21)")
    act = sub.add_parser("actions", help="what a person must do (F22)")
    asub = act.add_subparsers(dest="action")
    done = asub.add_parser("done", help="mark an action that nothing can detect as done")
    done.add_argument("key")
    pick = asub.add_parser("choose", help="answer an action that offers choices (20 W-1)")
    pick.add_argument("key")
    pick.add_argument("choice", help="one of the ids the action lists")
    sub.add_parser("reports", help="past reports, newest first (F15)")
    rep = sub.add_parser("report", help="verdict and summary of a run")
    rep.add_argument("--run", required=True)
    diag = sub.add_parser("diagnostics", help="every node's logs, settings and events in one zip")
    diag.add_argument("--run", help="the run (default: the last one)")
    ag = sub.add_parser("agent", help="this PC's own node, without the Controller")
    agsub = ag.add_subparsers(dest="action", required=True)
    agsub.add_parser("stop", help="stop this PC's test or sending only (USR-007)")
    ver = sub.add_parser("verify", help="the product's own checks (accuracy, judgement)")
    ver.add_argument("--suite", action="append", choices=["accuracy", "judgement", "loopback"],
                     help="repeat for more (default: accuracy and judgement)")  # fmt: skip
    ver.add_argument("--seconds", type=int, default=60, help="loopback test length")
    sub.add_parser("baselines", help="the last baseline test of each receiver (05 4.6.3)")
    bc = sub.add_parser("broadcast", help="the Always-on broadcast (F18)")
    bc.add_argument("action", choices=["stop"])
    chn = sub.add_parser("channel", help="change a receiver's channel during the test (F19)")
    chn.add_argument("device")
    chn.add_argument("ch", type=int, choices=[1, 2, 3])
    rec = sub.add_parser("records", help="fetch a run's 1-second records from every device")
    rec.add_argument("--run", required=True)
    st = sub.add_parser("storage", help="space used by results; delete a run (F15)")
    stsub = st.add_subparsers(dest="action", required=True)
    stsub.add_parser("list", help="each run's size, biggest first")
    sdel = stsub.add_parser("delete", help="delete one run on every PC")
    sdel.add_argument("run")
    sdel.add_argument("--raw-only", action="store_true",
                      help="only 1-second originals, captures, clips; report stays")  # fmt: skip
    sdel.add_argument("--yes", action="store_true", help="confirm the deletion")
    srv = sub.add_parser("serve", help="run the Controller service or an agent node (foreground)")
    srv.add_argument("--role", choices=["controller", "agent"], required=True)
    srv.add_argument("--data", type=Path, default=Path.home() / "ZeroLoss" / "data")
    srv.add_argument("--reports", type=Path, default=Path.home() / "ZeroLoss" / "Reports")
    srv.add_argument(
        "--modules", type=Path, help="module folder (installed: the Deployer's, 06 12)"
    )
    srv.add_argument("--node-port", type=int, default=10992, help="node API port")
    srv.add_argument("--player-host", choices=["self", "desktop"], default="self",
                     help="who starts the PC player (03 F20)")  # fmt: skip
    return ap


def _console_text() -> None:
    """The CLI prints "—" and "✔": a Windows console on an old code page (cp949) cannot
    show them and stopped the program (GAP-111). A sign it cannot show becomes "?";
    the console's own settings are left as they are."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["pace"]:  # GAP-160: the sender's even spacing, run as its own process
        from core.blocks.udp_pacer import main as pace

        return pace(raw[1:])
    _console_text()
    try:
        args = _parser().parse_args(argv)
    except SystemExit as e:  # argparse already printed the reason
        return 6 if e.code else 0
    if args.cmd == "serve":
        return _serve(args)
    cli = Cli(args.port, args.json)
    try:
        return _dispatch(cli, args)
    except ZlError as e:
        return cli.out(
            exit_code_for(e.code),
            e.to_dict(),
            f"{e.code}: {e.what}\n  {e.why}\n  {e.fix}".rstrip(),
            [e.code],
        )
    except Exception as e:  # never a bare traceback: code 10 with the reason
        return cli.out(10, {"error": repr(e)}, f"Internal error: {e!r}", ["ZL-SYS-008"])
    finally:
        cli.client.close()


def _dispatch(cli: Cli, args: argparse.Namespace) -> int:
    if args.cmd == "status":
        s = cli.client.get(f"{P}/status")
        r = s["run"]
        return cli.out(
            0,
            s,
            f"Controller build {s['build']} · test {r['state']}"
            + (f" · run {r['run_id']} · {r['elapsed_s']} s" if r["run_id"] else ""),
        )
    if args.cmd == "devices":
        if args.action == "list":
            d = cli.client.get(f"{P}/devices")
            lines = [f"{x['name']:<16} {x['host']}:{x['port']}  {x['role']}" for x in d["devices"]]
            return cli.out(
                0, d, "\n".join(lines) or "No devices. Add one with 'zeroloss devices add'."
            )
        if args.action == "add":
            d = cli.command(
                f"{P}/devices/add", name=args.name, host=args.host, port=args.node_port,
                deployer_port=args.deployer_port,
            )  # fmt: skip
            return cli.out(0, d, f"Added {d['name']} ({d['role']})")
        if args.action == "wan":
            return _wan(cli, args)
        d = cli.command(f"{P}/devices/remove", name=args.name)
        return cli.out(0, d, f"Removed {args.name}")
    if args.cmd == "problems":
        p = cli.client.get(f"{P}/problems")
        lines = [
            f"{'OPEN ' if r['active'] else 'done '}{r['level']:<5} {r['code']} {r['device']}: "
            f"{r['what']} — {r['fix']}"
            for r in p["problems"]
        ]
        return cli.out(0, p, "\n".join(lines) or "No problems")
    if args.cmd == "actions":
        if args.action == "done":
            d = cli.command(f"{P}/actions/done", key=args.key)
            return cli.out(0, d, f"Done: {args.key}")
        if args.action == "choose":
            d = cli.command(f"{P}/actions/choose", timeout_s=30, key=args.key, choice=args.choice)
            return cli.out(0, d, f"Chose {args.choice}: {d['wan'].get('state')}")
        a = cli.client.get(f"{P}/actions")
        lines = [
            f"{'WAIT ' if x['state'] == 'waiting' else 'done '}{x['device']}: {x['what']} — "
            f"{x['how']}"
            + ("" if x["auto_done"] or x["state"] != "waiting" else f" [done: {x['key']}]")
            + (
                f" [choose: {x['key']} " + " | ".join(c["id"] for c in x["choices"]) + "]"
                if x.get("choices") and x["state"] == "waiting"
                else ""
            )
            for x in a["actions"]
        ]
        return cli.out(0, a, "\n".join(lines) or "Nothing to do")
    if args.cmd == "agent":  # 13: `agent stop` talks to this PC's node (10994), no Controller
        d = cli.client.command("/v1/stop", 0, timeout_s=AGENT_STOP_TIMEOUT_S)  # a report route
        files = ", ".join(f"{f['name']} ({f['bytes'] // 1024} KB)" for f in d["files"]) or "none"
        text = f"{'Stopped' if d['stopped'] else 'Did not stop cleanly'}: {d['state']}" + (
            f"; run {d['run']} files: {files}" if d["run"] else "")  # fmt: skip
        return cli.out(0 if d["stopped"] else 3, d, text)
    if args.cmd == "diagnostics":  # 02 2-13 (20 GAP-78)
        params = {"run": args.run} if args.run else {}
        d = cli.command(f"{P}/diagnostics", timeout_s=DIAG_TIMEOUT_S, **params)
        bad = {k: v for k, v in d["nodes"].items() if v != "ok"}
        text = f"{d['path']} ({d['bytes'] // 1024} KB)" + "".join(
            f"\n  not from {k}: {v}" for k, v in bad.items())  # fmt: skip
        return cli.out(0, d, text)
    if args.cmd == "storage":
        return _storage(cli, args)
    if args.cmd == "reports":  # 02 2-13 (20 GAP-83)
        d = cli.client.get(f"{P}/reports")
        lines = [d["reports_dir"]] + [
            f"  {r['run']}  {r['started']}  {r['verdict'] or r['why']}"
            + (f"  {r['test_type']} {r['quality']} {r['actual_s']}/{r['planned_s']} s"
               if r["verdict"] else "")
            for r in d["reports"]
        ]  # fmt: skip
        return cli.out(0, d, "\n".join(lines))
    if args.cmd == "verify":  # 02 2-13 (20 GAP-81)
        want: dict[str, Any] = {"seconds": args.seconds}
        if args.suite:
            want["suites"] = list(dict.fromkeys(args.suite))
        d = cli.command(f"{P}/verify", timeout_s=args.seconds + VERIFY_MARGIN_S, **want)
        lines = [f"{'ok  ' if c['ok'] else 'FAIL'} {c['suite']}: {c['name']} - {c['detail']}"
                 for c in d["checks"]]  # fmt: skip
        lines.append(f"{d['passed']} passed, {d['failed']} failed")
        return cli.out(1 if d["failed"] else 0, d, "\n".join(lines))
    if args.cmd == "baselines":  # 02 2-13 (05 4.6.3)
        b = cli.client.get(f"{P}/baselines")
        lines = [
            f"{k}: {'verified' if v.get('verified') else 'NOT verified'} {v.get('date_utc')} "
            f"({v.get('run_id')}, loss {v.get('loss')}, playback issues {v.get('playback_issues')})"
            for k, v in sorted(b["baselines"].items())
        ]
        return cli.out(0, b, "\n".join(lines) or "No baseline test yet (S-54 Baseline)")
    if args.cmd == "broadcast":  # 02 2-13 (03 F18)
        d = cli.command(f"{P}/broadcast", timeout_s=60, on=False)
        chans = ", ".join(f"CH{c}" for c in d["stopped"])
        return cli.out(0, d, f"Broadcast stopped: {chans}" if chans else "No broadcast was on")
    if args.cmd == "channel":  # 02 2-13 (03 F19)
        d = cli.command(f"{P}/channel", timeout_s=30, device=args.device, ch=args.ch)
        z = d.get("zap") or {}
        if d["zap"] is None:
            return cli.out(0, d, f"{d['device']} is on CH{d['to']} already")
        took = (
            ""
            if z.get("t_first_packet") is None or z.get("t_cmd") is None
            else (f", first packet after {(z['t_first_packet'] - z['t_cmd']) * 1000:.0f} ms")
        )
        failed = z.get("join_failed") is True
        text = f"{d['device']}: CH{d['from']} -> CH{d['to']} (join {d['join_method']}){took}" + (
            "\n  no packet within 5 s: Join failed (NET-003)" if failed else ""
        )
        return cli.out(3 if failed else 0, d, text)
    if args.cmd == "records":  # 02 2-13 (20 GAP-80)
        d = cli.command(f"{P}/records", timeout_s=RECORDS_TIMEOUT_S, run=args.run)
        got = sum(1 for f in d["files"] if f["state"] == "got")
        text = f"{d['run']}: {got} fetched, {len(d['files']) - got} already here" + "".join(
            f"\n  not from {k}: {v}" for k, v in d["missing"].items())  # fmt: skip
        return cli.out(3 if d["missing"] else 0, d, text)
    if args.cmd == "run":
        return _run(cli, args)
    if args.cmd == "prepare":
        return _prepare(cli, args)
    if args.cmd == "precheck":
        return _precheck(cli, args)
    if args.cmd == "setup":
        return _setup(cli, args)
    if args.cmd == "wifi":
        return _wifi(cli, args)
    if args.cmd == "reset":
        return _reset(cli, args)
    if args.cmd == "discover":
        return _discover(cli, args)
    if args.cmd == "sources":
        r = cli.client.get(f"{P}/sources", timeout_s=90, node=args.node, module=args.module)
        lines = []
        for f in r["files"]:
            if f.get("error"):
                lines.append(f"✖ {f['name']}: {f['error']}")
                continue
            fit = ", ".join(f"{q} {'⚠ ' + v['why'] if v.get('why') else 'ok'}"
                            for q, v in f["fits"].items())  # fmt: skip
            lines.append(f"{f['name']}  {f['width']}x{f['height']} {f['fps']} fps "
                         f"{f['duration_s']} s  ({fit})")  # fmt: skip
        text = "\n".join(lines) or f"No videos in {r['folder']} (put .mp4 files there)."
        return cli.out(0, r, text)
    if args.cmd == "nodes":
        return _nodes(cli, args)
    if args.cmd == "install":
        return _install_linux(cli, args)
    if args.cmd == "deploy":
        r = cli.command(f"{P}/deploy", timeout_s=DEPLOY_TIMEOUT_S, node=args.node,
                        package=str(args.package.resolve()))  # fmt: skip
        return cli.out(0, r, f"{args.node}: {r['id']} {r['build']} {r['result']}")
    if args.cmd == "modules":
        return _modules(cli, args)
    if args.cmd == "stop":
        r = cli.command(f"{P}/stop", timeout_s=60)
        if r.get("cancelled"):  # the start was making videos (USR-006)
            return cli.out(0, r, "Start cancelled; the sender PC goes on making the videos")
        return cli.out(VERDICT_EXIT.get(r["verdict"], 10), r, f"Stopped: {r['verdict']}")
    rep = cli.client.get(f"{P}/report", run=args.run)
    return cli.out(VERDICT_EXIT.get(rep["verdict"], 10), rep, f"{args.run}: {rep['verdict']}")


def svc_build(svc: Any) -> str:
    node = getattr(svc, "node", svc)
    return str(getattr(node, "build", "?"))


def _serve(args: argparse.Namespace) -> int:
    """Development stand-in for the installed service (P3 installer registers it)."""
    import threading

    from controller.service import ControllerService
    from core.node import Node

    modules = args.modules or Path(__file__).resolve().parent.parent / "modules"
    svc: Any
    if args.role == "controller":
        svc = ControllerService(
            args.data, args.reports, modules, port=args.node_port, local_port=args.port
        )
    else:
        svc = Node(
            args.data,
            "agent",
            modules_dir=modules,
            port=args.node_port,
            local_port=args.port,
            player_host=args.player_host,
        )
    svc.start()
    print(f"{utc_now()} ZeroLoss {args.role} running (build {svc_build(svc)}). Ctrl+C stops it.",
          flush=True)  # fmt: skip
    try:  # until Ctrl+C, or the Controller is asked to quit while idle (GAP-116)
        quit_requested = getattr(svc, "quit_requested", None) or threading.Event()
        while not quit_requested.wait(1.0):
            pass
    except KeyboardInterrupt:
        print(f"{utc_now()} stopping: Ctrl+C or the console closed", flush=True)
    else:
        print(f"{utc_now()} stopping: asked to quit while idle", flush=True)
    finally:
        svc.stop()
        print(f"{utc_now()} stopped", flush=True)
    return 0


def _read_plan(cli: Cli, path: Path) -> dict[str, Any] | int:
    """The plan file (02 11-1), or the exit code after telling what is wrong."""
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return cli.out(6, {"error": str(e)}, f"Plan file cannot be read: {e}", ["ZL-DSC-012"])
    if not isinstance(plan, dict):
        return cli.out(6, {}, "Plan file must be a JSON object", ["ZL-DSC-012"])
    return plan


WAN_TEXT = {
    "off": "ISP role not running (mode: {mode}; auto starts it on a router WAN port)",
    "ready": "ready: {nic} gives the router WAN its address (this PC is {server_ip})",
    "waiting_nic": "waiting: {why}",
    "conflict": "not started: {why}",
    "error": "error: {why}",
}


def _wan(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss devices wan <name> [--on|--off] [--nic X]`` (07 3.2 B, 02 2-9)."""
    params: dict[str, Any] = {"name": args.name}
    if args.auto:
        params["auto"] = True
    elif args.on or args.off:
        params["on"] = bool(args.on)
    if args.nic:
        params["nic"] = args.nic
    r = cli.command(f"{P}/devices/wan", timeout_s=30, **params)
    text = f"{args.name}: " + WAN_TEXT.get(r["state"], r["state"]).format(**{"mode": "?", **r})
    if r["state"] == "waiting_nic" and r.get("mode") == "on":
        text += "\nMove this PC's cable to the router WAN port; it goes on by itself."
    code = 0 if r["state"] in ("ready", "off", "waiting_nic") else 3
    return cli.out(code, r, text)


AGENT_STOP_TIMEOUT_S = 60  # modules stop in order, files close (F10)
DIAG_TIMEOUT_S = 1800  # every node may take up to 120 s (02 2-13)
VERIFY_MARGIN_S = 600  # checks, start, warm-up, stop and report (02 2-13)
RECORDS_TIMEOUT_S = 24 * 3600  # 100 h x devices, 600 s per file at most (02 2-13)
STORAGE_TIMEOUT_S = 600  # every device may take 10 s, deleting large runs longer (02 2-13)
DISCOVER_TIMEOUT_S = 60  # broadcast 1.5 s + sender tries + a /22 scan


def _discover(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss discover`` (06 10.2): lists what answered; ``devices add`` registers."""
    params: dict[str, Any] = {"try_sender": not args.no_try_sender, "port": args.node_port}
    if args.scan:
        params["scan"] = args.scan
    r = cli.command(f"{P}/discover", timeout_s=DISCOVER_TIMEOUT_S, **params)
    lines = [f"{'known' if d['known'] else 'NEW  '}  {d['name']:<20} {d['address']}:{d['port']}"
             f"  {d.get('role') or ''} {d.get('build') or ''} ({d['how']})"
             + ("  CONTROL PORT CLOSED (firewall, DSC-008)" if d.get("tcp_ok") is False else "")
             for d in r["found"]]  # fmt: skip
    for m in r.get("moved") or []:
        lines.append(f"{m['name']} moved: {m['from']} -> {m['to']} (address updated)")
    if r["sender_try"] == "not found":
        lines.append("Sender PC not at 10.0.0.1 / 10.0.0.2: add it with 'zeroloss devices add'"
                     " (its IP is shown on the sender PC).")  # fmt: skip
    text = "\n".join(lines) or "No device answered. Check cables, power and the firewall."
    return cli.out(0 if r["found"] else 3, r, text, r["codes"] or None)


RESET_TIMEOUT_S = 300  # every device's safe stop and clean check (F11)


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def _storage(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss storage list`` / ``delete <run> [--raw-only] --yes`` (F15, 02 2-13)."""
    if args.action == "list":
        d = cli.client.get(f"{P}/storage", timeout_s=STORAGE_TIMEOUT_S)
        lines = (
            [f"{d['reports_dir']} (free {d['free_mb']} MB)"]
            + [
                f"  {r['run']}: {_mb(r['bytes'])} (raw {_mb(r['raw_bytes'])})"
                + (" [running]" if r["in_use"] else "")
                + " - "
                + ", ".join(f"{k} {_mb(v['bytes'])}" for k, v in r["where"].items())
                for r in d["runs"]
            ]
            + [f"  not from {k}: {v}" for k, v in d["nodes"].items() if v != "ok"]
        )
        return cli.out(0, d, "\n".join(lines))
    what = "the raw data (1-second originals, captures, clips) of" if args.raw_only else "all of"
    if not args.yes:
        return cli.out(4, {}, f"This deletes {what} {args.run} on every PC. Add --yes to confirm.")
    d = cli.command(f"{P}/storage/delete", timeout_s=STORAGE_TIMEOUT_S, run=args.run,
                    raw_only=bool(args.raw_only))  # fmt: skip
    bad = [f"  not on {k}: {v}" for k, v in d["nodes"].items() if v != "ok"] + [
        f"  kept {e}" for e in d["errors"]]  # fmt: skip
    text = "\n".join([f"Deleted {_mb(d['deleted_bytes'])} of {d['run']}", *bad])
    return cli.out(0 if not bad else 3, d, text)


def _reset(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss reset --yes`` (13-ai-loop): exit 0 when every device is clean, else 3."""
    if not args.yes:
        return cli.out(4, {}, "Reset All stops everything on every device. Add --yes to confirm.")
    r = cli.command(f"{P}/reset", timeout_s=RESET_TIMEOUT_S)
    lines = [
        f"{'clean' if d['clean'] else 'NOT CLEAN'}  {d['device']}"
        + ("" if d["clean"] else f": {d.get('why') or ''} — {d.get('fix') or ''}")
        for d in r["devices"]
    ]
    head = "Reset done: ready for a new test" if r["ok"] else "Reset not complete"
    return cli.out(0 if r["ok"] else 3, r, "\n".join([head, *lines]))


def _nodes(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss nodes restart --node X`` / ``stop --yes`` / ``start`` (F11, 02 2-8)."""
    if args.action == "stop" and not args.yes:
        return cli.out(4, {}, "This stops the ZeroLoss node on every PC. Add --yes to confirm.")
    params = {"action": args.action} | ({"node": args.node} if args.action == "restart" else {})
    r = cli.command(f"{P}/nodes", timeout_s=RESET_TIMEOUT_S, **params)
    lines = [f"{'done' if d['done'] else 'FAILED'}  {d['device']}"
             + (f": {d['error']['what']}" if d["error"] else "") for d in r["devices"]]  # fmt: skip
    codes = [d["error"]["code"] for d in r["devices"] if d["error"]]
    return cli.out(0 if r["ok"] else 3, r, "\n".join(lines) or "No devices", codes or None)


def _wifi(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss wifi`` (02 2-7): the password only from stdin or a hidden prompt; it
    is never printed, not even in --json."""
    if args.action == "show":
        r = cli.client.get(f"{P}/wifi")
    elif args.action == "clear":
        r = cli.command(f"{P}/wifi/clear")
    else:
        password = ""
        if args.security != "Open":
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\n")
            else:
                import getpass

                password = getpass.getpass(f"Password for Wi-Fi {args.ssid}: ")
        r = cli.command(f"{P}/wifi", ssid=args.ssid, security=args.security, password=password)
    saved = ", password saved" if r["has_password"] else ""
    text = (
        f"Test Wi-Fi: {r['ssid']} ({r['security']}{saved})"
        if r.get("ssid")
        else "Test Wi-Fi: not set (tablets and Wi-Fi PCs are not connected automatically)"
    )
    return cli.out(0, r, text)


PRECHECK_TIMEOUT_S = 120  # 1-2 s test send per channel + node answers


def _plan_or_setup(cli: Cli, path: Path | None) -> dict[str, Any] | int:
    """A plan file, or the plan the saved screen choice makes (02 2-11)."""
    if path is not None:
        return _read_plan(cli, path)
    s = cli.client.get(f"{P}/setup", timeout_s=60)
    if s["plan"] is None:
        why = s["roles"]["why"]
        return cli.out(3, s, f"Not ready: {why} (zeroloss setup set sender=… receiver=…)")
    plan: dict[str, Any] = s["plan"]
    return plan


def _setup(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss setup show`` / ``setup set KEY=VALUE…`` (F14, 02 2-11)."""
    if args.action == "show":
        s = cli.client.get(f"{P}/setup", timeout_s=60)
        su, ro = s["setup"], s["roles"]
        rx = ", ".join(f"{r['device']} CH{r['ch']} {r.get('network', 'Wired')}"
                       for r in ro["receivers"]) or "none"  # fmt: skip
        lan = " (on LAN: comparison test)" if ro["lan_sender"] else ""
        text = (f"{su['test_type']} · {su['quality']} · {su['duration_s']} s\n"
                f"Sender: {ro['sender'] or '?'}{lan}"
                f"\nReceivers: {rx}\nChanged settings: {su['settings'] or 'none'}"
                f"\nTest cards: {su.get('nics') or 'Auto'}"
                + (f"\nNot ready: {ro['why']}" if ro["why"] else ""))  # fmt: skip
        return cli.out(0, s, text)
    changes: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    for pair in args.pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            return cli.out(6, {}, f"{pair}: use KEY=VALUE", ["ZL-DSC-012"])
        value = _value(raw)
        if key.startswith("S-"):
            settings[key] = value
        elif key == "nic":  # nic=name:address | nic=name:auto (DSC-006, GAP-115)
            name, _, ip = raw.partition(":")
            if "nics" not in changes:
                got = cli.client.get(f"{P}/setup", timeout_s=60)["setup"].get("nics")
                changes["nics"] = dict(got or {})
            if ip.lower() in ("", "auto"):
                changes["nics"].pop(name, None)
            else:
                changes["nics"][name] = ip
        elif key == "receiver":  # receiver=name[:ch[:Wired|Wireless]]
            name, _, rest = raw.partition(":")
            ch, _, net = rest.partition(":")
            changes.setdefault("receivers", []).append(
                {"device": name, "ch": int(ch or 1), "network": net or "Wired"})  # fmt: skip
        else:
            changes[key] = value
    if settings:
        current = cli.client.get(f"{P}/setup", timeout_s=60)["setup"]["settings"]
        changes["settings"] = {**current, **settings}
    r = cli.command(f"{P}/setup", changes=changes)
    return cli.out(0, r, "Saved")


def _value(raw: str) -> Any:
    try:
        return json.loads(raw)  # numbers, true/false, objects
    except ValueError:
        return raw


def _precheck(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss precheck`` (13-ai-loop, 02 2-6): exit 0 when no step failed, else 3."""
    plan = _plan_or_setup(cli, args.plan)
    if isinstance(plan, int):
        return plan
    r = cli.command(f"{P}/precheck", timeout_s=PRECHECK_TIMEOUT_S, plan=plan, full=True)
    return cli.out(0 if r["ok"] else 3, r, checklist_text(r), _checklist_codes(r))


MARKS = {"ok": "OK  ", "warn": "WARN", "failed": "FAIL", "skipped": "--  ", "waiting": "..  "}


def checklist_text(c: dict[str, Any]) -> str:
    lines = []
    for s in c.get("steps") or []:
        line = f"{MARKS.get(s['state'], s['state'])} {s['n']}. {s['name']}"
        if s.get("what"):
            line += f": {s['what']}" + (f"\n       Why: {s['why']}" if s.get("why") else "")
            line += f"\n       Fix: {s['fix']}" if s.get("fix") else ""
        elif s.get("detail"):
            line += f" — {s['detail']}"
        lines.append(line)
    return "\n".join(lines)


def _checklist_codes(c: dict[str, Any]) -> list[str] | None:
    return [s["code"] for s in c.get("steps") or [] if s.get("code")] or None


def _run(cli: Cli, args: argparse.Namespace) -> int:
    plan = _plan_or_setup(cli, args.plan)
    if isinstance(plan, int):
        return plan
    duration = plan.get("duration_s", 0)
    if isinstance(duration, int) and duration > LONG_RUN_S and not args.yes:
        return cli.out(
            4,
            {"duration_s": duration},
            f"This test runs {duration / 3600:g} h. Add --yes to confirm.",
        )
    started = cli.command(f"{P}/run", timeout_s=START_TIMEOUT_S, plan=plan)
    run_id = started["run_id"]
    if not args.wait:
        return cli.out(0, started, f"Started {run_id}")
    while True:
        time.sleep(1)
        r = cli.client.get(f"{P}/status")["run"]
        result = r.get("result")
        if r["state"] in ("IDLE", "FAULT") and result and result.get("run_id") == run_id:
            v = result["verdict"]
            return cli.out(VERDICT_EXIT.get(v, 10), result, f"{run_id}: {v}")


def _install_linux(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss install linux`` (13-ai-loop): needs --yes; the password is asked for or
    read from stdin, sent once to the local service and never stored (06 11.3)."""
    if not args.yes:
        return cli.out(4, {"host": args.host},
                       f"Installing on {args.host} over SSH. Add --yes to confirm.")  # fmt: skip
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        import getpass

        password = getpass.getpass(f"Password for {args.user}@{args.host}: ")
    params: dict[str, Any] = {
        "host": args.host,
        "user": args.user,
        "password": password,
        "name": args.name,
        "node_port": args.node_port,
        "deployer_port": args.deployer_port,
        "ssh_port": args.ssh_port,
        "accept_new_host_key": args.accept_new_host_key,
        "auto_login": args.auto_login,
    }
    if args.package:
        params["package"] = str(args.package.resolve())
    if args.module:
        params["modules"] = [str(m.resolve()) for m in args.module]
    r = cli.command(f"{P}/install/linux", timeout_s=INSTALL_LINUX_TIMEOUT_S, **params)
    missing = ", ".join(r.get("tools_missing") or []) or "none"
    return cli.out(0, r, f"Installed {r['name']} ({r['host']}), host key {r['host_key']['sha256']}"
                         f" ({r['host_key']['status']}), missing tools: {missing}")  # fmt: skip


def _modules(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss modules list|remove`` (13-ai-loop, 06 12). Removal needs --yes."""
    if args.action == "list":
        r = cli.client.get(f"{P}/modules", timeout_s=DEPLOY_TIMEOUT_S, node=args.node)  # retries
        lines = [f"{m['id']:<12} {m['build']}  {m['installed_utc']}" for m in r["modules"]]
        return cli.out(0, r, "\n".join(lines) or f"No modules on {args.node}")
    if not args.dry_run and not args.yes:
        return cli.out(4, {"id": args.id, "node": args.node},
                       f"Removing {args.id} from {args.node}. Add --yes to confirm.")  # fmt: skip
    r = cli.command(f"{P}/modules/remove", timeout_s=DEPLOY_TIMEOUT_S, node=args.node,
                    id=args.id, purge=args.purge, dry_run=args.dry_run)  # fmt: skip
    verb = "Would remove" if args.dry_run else "Removed"
    text = f"{verb} {len(r['removed'])} items" + (
        f", kept {', '.join(r['kept'])}" if r["kept"] else ""
    )
    return cli.out(0, r, text)


def _prepare(cli: Cli, args: argparse.Namespace) -> int:
    """``zeroloss prepare`` (13-ai-loop, 02 2-4, iptv 1-1-2)."""
    where = {"module": args.module, "sender": args.sender}
    if args.status:
        s = cli.client.get(f"{P}/prepare", **where)
        return cli.out(0, s, _prep_text(s))
    if not args.quality:
        return cli.out(6, {}, "Add --quality UHD or --quality HD", ["ZL-DSC-012"])
    try:
        chans = [int(x) for x in args.ch.split(",")]
    except ValueError:
        return cli.out(6, {}, "--ch must be channel numbers, e.g. 1,2,3", ["ZL-DSC-012"])
    params: dict[str, Any] = {**where, "quality": args.quality, "channels": chans}
    if args.bitrate is not None:
        params["bitrate_mbps"] = args.bitrate
    r = cli.command(f"{P}/prepare", timeout_s=30, **params)
    if not args.wait:
        return cli.out(0, r, _prep_text(r))
    while True:
        time.sleep(2)
        s = cli.client.get(f"{P}/prepare", **where)
        if not s.get("busy"):
            bad = [c for c in s.get("channels") or [] if c.get("state") != "ready"]
            code = exit_code_for("ZL-SND-020") if bad else 0
            return cli.out(code, s, _prep_text(s), ["ZL-SND-020"] if bad else None)


def _prep_text(s: dict[str, Any]) -> str:
    lines = []
    for c in s.get("channels") or []:
        line = f"CH{c.get('ch')} {c.get('quality', '')} {c.get('state')}".replace("  ", " ")
        if c.get("percent") is not None:
            line += f" {c['percent']:.0f}%"
        if c.get("error"):
            line += f": {c['error']}"
        lines.append(line)
    return "\n".join(lines) or "Nothing to prepare"


if __name__ == "__main__":
    sys.exit(main())
