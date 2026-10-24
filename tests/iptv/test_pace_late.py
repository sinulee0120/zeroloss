"""The sender PC's even-spacing sender running late is the sender's trouble (SND-025).

2026-10-22 R20261022-235424: the ProBook's Wi-Fi driver (ath9k) reset storm starved the pacer
for 20 s (late up to 790 ms, queue 15.9 k); the stream reached the T460 4.8 s late and 11
packets were lost, judged as network loss (red) until now.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modules.iptv.sender import session as s


def test_late_seconds_of_the_pacer_are_told(tmp_path: Path) -> None:
    log = tmp_path / "pace_ch1.log"
    lines = [{"sent": 0, "max_queue": 0, "late_us": 0, "listening": 5000},
             {"sent": 3325, "max_queue": 82, "late_us": 1000},
             {"sent": 1900, "dropped": 0, "max_queue": 3711, "late_us": 790000},
             {"sent": 3362, "dropped": 0, "max_queue": 200, "late_us": 49000},
             {"sent": 3000, "dropped": 325, "max_queue": 340, "late_us": 3000}]  # fmt: skip
    log.write_text("".join(json.dumps({"pace": d}) + "\n" for d in lines), encoding="utf-8")
    told: list[tuple[str, str]] = []
    sess: Any = object.__new__(s.SenderSession)
    sess.cfg = type("C", (), {"ch": 1})()
    sess._pace_log = s._PaceLog(log)
    sess._event = lambda code, level, text, clears=None: told.append((code, text))
    sess._watch_pace()
    assert told == [
        ("ZL-SND-025", "CH1 sending fell behind real time: 790 ms late, 0 datagrams too late "
                       "for live, not sent"),
        ("ZL-SND-025", "CH1 sending fell behind real time: 3 ms late, 325 datagrams too late "
                       "for live, not sent"),  # dropped as too late: the sender's loss
    ]  # fmt: skip
    sess._watch_pace()  # nothing new: nothing told
    assert len(told) == 2
