"""netem loss check (docs/10-verification.md 1.3, 04 P2 completion): ZeroLoss's lost
count must equal the packets netem dropped, exactly.

Run once as root (tc needs it):  sudo .venv/bin/python tools/netem_check.py

- netem goes only on UDP port 5004 of the loopback device (prio qdisc + u32 filter),
  so nothing else on this PC is touched; it is removed at the end, also on errors.
- Loss is switched on only after the receiver is measuring and off again before Stop,
  and the check waits past the 1 s reorder window, so warm-up and tail drops cannot blur
  the count: equal means equal.
- Playback is off here (VLC refuses to run as root); the loss count is the analyzer's.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from controller.service import ControllerService  # noqa: E402
from core.node import Node  # noqa: E402
from core.protocol.client import NodeClient  # noqa: E402
from modules.iptv.sender.commands import QUALITIES, prepare_command  # noqa: E402

DEV, PORT = "lo", 5004
LOSS = sys.argv[1] if len(sys.argv) > 1 else "0.1%"
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 60


def tc(*args: str, check: bool = True) -> str:
    out = subprocess.run(["tc", *args], capture_output=True, text=True, check=check)
    return out.stdout


def netem_dropped() -> int:
    text = tc("-s", "qdisc", "show", "dev", DEV)
    part = text[text.index("qdisc netem 30:") :]
    m = re.search(r"dropped (\d+)", part)
    assert m, text
    return int(m.group(1))


def set_loss(loss: str) -> None:
    tc("qdisc", "change", "dev", DEV, "parent", "1:3", "handle", "30:", "netem", "loss", loss)


def setup() -> None:
    tc("qdisc", "del", "dev", DEV, "root", check=False)
    tc("qdisc", "add", "dev", DEV, "root", "handle", "1:", "prio")
    tc("qdisc", "add", "dev", DEV, "parent", "1:3", "handle", "30:", "netem", "loss", "0%")
    tc(
        "filter", "add", "dev", DEV, "parent", "1:0", "protocol", "ip", "prio", "1", "u32",
        "match", "ip", "protocol", "17", "0xff", "match", "ip", "dport", str(PORT), "0xffff",
        "flowid", "1:3",
    )  # fmt: skip


def lost_total(c: NodeClient) -> int | None:
    dev = c.get("/v1/controller/status")["run"]["devices"].get("local") or {}
    recv = ((dev.get("module_data") or {}).get("receivers")) or []
    return recv[0].get("lost_total") if recv else None


def main() -> int:
    if not shutil.which("tc") or not shutil.which("tsp"):
        print("needs tc and tsp")
        return 2
    work = Path(tempfile.mkdtemp(prefix="zl-netem-"))
    work.chmod(0o755)  # run as root; the result must stay readable by the user
    hd = work / "hd.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, hd, seconds=6, preset="ultrafast"),
        check=True,
        capture_output=True,
    )
    svc = ControllerService(
        work / "ctl", work / "rep", ROOT / "modules", host="127.0.0.1", port=0, local_port=0
    )
    node = Node(work / "agent", "agent", host="127.0.0.1", port=0, local_port=0,
                modules_dir=ROOT / "modules")  # fmt: skip
    result: dict[str, Any] = {}
    setup()
    svc.start()
    node.start()
    try:
        c = NodeClient("127.0.0.1", svc.node.local.port, "netem", "svc")

        def gen() -> int:
            return int(c.get("/v1/controller/status")["generation"])

        c.command("/v1/controller/devices/add", gen(), name="local", host="127.0.0.1",
                  port=node.api.port)  # fmt: skip
        plan = {
            "schema_version": 1, "module": "iptv", "test_type": "pc", "quality": "HD",
            "duration_s": SECONDS + 60,
            "devices": {"sender": "local", "receivers": [{"device": "local", "ch": 1}]},
            "settings": {"S-49": "Off", "S-14": {"groups": ["239.255.95.1", "239.255.95.2",
                                                           "239.255.95.3"], "port": PORT}},
            "nics": {"local": "127.0.0.1"}, "files": {"1": str(hd)},
        }  # fmt: skip
        c.command("/v1/controller/run", gen(), timeout_s=120, plan=plan)
        base = lost_total(c)
        assert base == 0, f"loss before netem: {base}"
        d0 = netem_dropped()
        set_loss(LOSS)
        print(f"netem loss {LOSS} on UDP {PORT} for {SECONDS} s …", flush=True)
        time.sleep(SECONDS)
        set_loss("0%")
        dropped = netem_dropped() - d0
        time.sleep(3)  # past the 1 s reorder window: every gap is final
        counted = lost_total(c)
        c.command("/v1/controller/stop", gen(), timeout_s=120)
        result = {"loss": LOSS, "seconds": SECONDS, "netem_dropped": dropped,
                  "zeroloss_lost": counted, "equal": dropped == counted}  # fmt: skip
    finally:
        svc.stop()
        node.stop()
        tc("qdisc", "del", "dev", DEV, "root", check=False)
    print(json.dumps(result))
    (work / "netem_result.json").write_text(json.dumps(result))
    return 0 if result.get("equal") and result.get("netem_dropped") else 1


if __name__ == "__main__":
    sys.exit(main())
