"""11-ui 7 "해상도·배율": the main screen at 1366x768 .. 3840x2160 and 100..200 % fits,
nothing is cut, text is never narrower than it needs, the narrow layout and the small
screen notice come at the right sizes, and the page scrolls only below 1024x600.

One process per display scale (Qt fixes it at start). Captures are kept for the 1.11
review in ``$ZEROLOSS_CAPTURE_DIR`` when set, else in the test's temporary folder.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.controller_ui.screen_sizes import SCALES, SCREENS, states

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("scale", SCALES)
def test_every_screen_size_at_this_scale(scale: float, tmp_path: Path) -> None:
    out = Path(os.environ.get("ZEROLOSS_CAPTURE_DIR") or tmp_path)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": str(scale)}
    run = subprocess.run(
        [sys.executable, "-m", "tests.controller_ui.screen_sizes", "--out", str(out)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert run.returncode == 0, run.stderr[-2000:]
    problems = [json.loads(x) for x in run.stdout.splitlines() if x.startswith("{")]
    assert not problems, "\n".join(
        f"{p['screen']}@{round(p['scale'] * 100)}% {p['state']}: {p['problem']}" for p in problems
    )
    pct = round(scale * 100)
    shots = {p.name for p in out.glob(f"*@{pct}_*.png")}
    # the states, and Throughput's own settings on the Test area (2026-10-17)
    assert len(shots) == len(SCREENS) * (len(list(states())) + 1), sorted(shots)
