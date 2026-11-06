"""Router Logger (16 S-66..S-69, 2026-11-05 user): on a router problem press start in the
background, ignore every other problem while it logs for the record time and while it
saves after stop ("stop이 정확히 마무리될때까지는 start를 눌러선 안돼. 오류나"), then
be ready again. A fake window and clock; nothing real is pressed."""

from __future__ import annotations

from types import SimpleNamespace

from controller import run as ctl
from controller.router_logger import SLOW_SAVE_S, STABLE_S, START, STOP, RouterLogger, WinWindow
from modules.iptv import run as rules


class FakeWindow:
    def __init__(self) -> None:
        self.start_on, self.stop_on, self.there = True, False, True
        self.file: tuple[str, int] | None = ("log_1.bin", 100)
        self.written = 0  # the program's disk writes so far
        self.old: list[tuple[str, int]] = []  # its other log files
        self.cpu_s = 0.0  # its CPU seconds so far
        self.pressed: list[int] = []

    def state(self) -> tuple[bool, bool] | None:
        return (self.start_on, self.stop_on) if self.there else None

    def press(self, command: int) -> bool:
        self.pressed.append(command)
        if command == START:
            self.start_on, self.stop_on = False, True
        return True

    def newest(self) -> tuple[str, int] | None:
        return self.file

    def writes(self) -> int | None:
        return self.written

    def files(self) -> list[tuple[str, int]]:
        return sorted({*self.old, *([self.file] if self.file else [])})

    def cpu(self) -> float | None:
        return self.cpu_s


def _logger() -> tuple[RouterLogger, FakeWindow, list[float], list[tuple[str, str]]]:
    win, now, said = FakeWindow(), [0.0], []
    lg = RouterLogger(lambda code, level, text, device: said.append((level, text)),
                      window_of=lambda program: win, clock=lambda: now[0])  # fmt: skip
    lg.configure({"jitter_ms": 50.0, "record_s": 30, "program": r"C:\tools\logger.exe"})
    return lg, win, now, said


def test_start_then_stop_after_the_record_time_then_wait_for_the_save() -> None:
    lg, win, now, said = _logger()
    assert lg.trigger("lost 5", "tablet-1") and win.pressed == [START]
    assert lg.state == "logging" and "Router Logger started: tablet-1 lost 5" in said[-1][1]
    now[0] = 10.0
    assert not lg.trigger("lost 9", "t460-rx")  # logging: not again
    lg.step()
    assert win.pressed == [START]  # not before the record time
    now[0] = 30.0
    lg.step()
    assert win.pressed == [START, STOP] and lg.state == "saving"
    win.stop_on = False  # the menu says idle at once, the file still grows
    win.start_on = True
    for t, size in ((32.0, 5_000), (36.0, 9_000), (41.0, 9_000)):
        now[0], win.file = t, ("log_1.bin", size)
        lg.step()
        assert lg.state == "saving"  # file grew or not yet unchanged STABLE_S
    assert not lg.trigger("freeze", "tablet-2")  # saving: never start (the program errs)
    now[0] = 36.0 + STABLE_S
    lg.step()
    assert lg.state == "idle" and win.pressed == [START, STOP]
    assert "saved log_1.bin (9,000 B)" in said[-1][1] and "2 more problem(s)" in said[-1][1]
    assert lg.trigger("lost 1", "tablet-1") and win.pressed[-1] == START  # ready again


def test_never_pressed_when_busy_or_gone_and_a_slow_save_only_waits() -> None:
    lg, win, now, said = _logger()
    win.start_on, win.stop_on = False, True  # started by hand
    assert not lg.trigger("lost 1", "rx") and win.pressed == []
    win.there = False
    assert not lg.trigger("lost 1", "rx") and not lg.trigger("lost 2", "rx")
    assert sum("window is not open" in t for _, t in said) == 1  # said once
    win.there, win.start_on, win.stop_on = True, True, False
    assert lg.trigger("lost 1", "rx")
    now[0] = 30.0
    lg.step()  # Stop; the program stays busy saving
    now[0] = 30.0 + SLOW_SAVE_S + 1
    lg.step()
    lg.step()
    assert lg.state == "saving" and win.pressed == [START, STOP]
    assert sum("still saving" in t for _, t in said) == 1
    assert WinWindow(r"C:\nope.exe").press(33) is False  # never the settings dialog


def test_while_the_program_still_writes_after_stop_start_is_never_pressed() -> None:
    """2026-11-06 user ("stop후 saving동안 다시 start가 안눌린다는것만 확실하면"): the menu
    turns idle the moment Stop is pressed and the newest file may not change for a while,
    but the program is still writing its buffer: no Start until its writes have stopped."""
    lg, win, now, said = _logger()
    assert lg.trigger("lost 1", "rx")
    now[0] = 30.0
    lg.step()  # Stop
    win.start_on, win.stop_on = True, False  # idle at once
    for t in range(31, 400):  # minutes of saving: the file the same, the writes rising
        now[0], win.written = float(t), win.written + 4096
        lg.step()
        assert lg.state == "saving"
        assert not lg.trigger("lost 2", "rx")
    assert win.pressed == [START, STOP]
    now[0] = 400.0 + STABLE_S  # it has stopped writing for STABLE_S
    lg.step()
    assert lg.state == "idle" and "in 380 s" in said[-1][1]
    assert "saved log_1.bin (100 B)" in said[-1][1]
    assert lg.trigger("lost 3", "rx") and win.pressed == [START, STOP, START]


def test_the_saved_message_names_every_split_file_of_that_logging() -> None:
    """2026-11-06 user: "보통 파일이두개 정도로 나뉘어서 생성되더라고" — both parts, and
    not the files of an earlier logging."""
    lg, win, now, said = _logger()
    win.old, win.file = [("fw_log_20261106_200000_1.bin", 7)], None  # an earlier one
    assert lg.trigger("lost 1", "rx")
    win.old.append(("fw_log_20261106_210000_1.bin", 1_000))
    win.file = ("fw_log_20261106_210000_2.bin", 500)
    now[0] = 30.0
    lg.step()
    win.start_on, win.stop_on = True, False
    now[0] = 30.0 + STABLE_S
    lg.step()
    assert lg.state == "idle"
    assert ("saved fw_log_20261106_210000_1.bin, fw_log_20261106_210000_2.bin (1,500 B)"
            in said[-1][1])  # fmt: skip


def test_a_busy_program_writing_nothing_yet_is_still_saving() -> None:
    """A save may work in memory before it writes: the program's CPU keeps the wait."""
    lg, win, now, _said = _logger()
    assert lg.trigger("lost 1", "rx")
    now[0] = 30.0
    lg.step()
    win.start_on, win.stop_on = True, False
    for t in range(31, 100):  # a whole core busy, no file change, no writes
        now[0], win.cpu_s = float(t), win.cpu_s + 1.0
        lg.step()
        assert lg.state == "saving"
    for t in range(100, 99 + int(STABLE_S)):  # idle again (Tk's own 1 %) since 99 s
        now[0], win.cpu_s = float(t), win.cpu_s + 0.01
        lg.step()
    assert lg.state == "saving"
    now[0], win.cpu_s = 99.0 + STABLE_S, win.cpu_s + 0.01
    lg.step()
    assert lg.state == "idle" and win.pressed == [START, STOP]


def test_many_split_files_are_counted() -> None:
    """A small split size in the program (its own setting): counted, not all named."""
    lg, win, now, said = _logger()
    win.file = None
    assert lg.trigger("lost 1", "rx")
    win.old = [(f"fw_log_20261106_210000_{i}.bin", 100) for i in range(1, 8)]
    now[0] = 30.0
    lg.step()
    win.start_on, win.stop_on = True, False
    now[0] = 30.0 + STABLE_S
    lg.step()
    assert ("saved 7 files (fw_log_20261106_210000_1.bin … fw_log_20261106_210000_7.bin) "
            "(700 B)") in said[-1][1]  # fmt: skip


def test_off_or_without_a_program_does_nothing() -> None:
    assert rules.logger_settings(None) is None  # default Off
    assert rules.logger_settings({"S-66": "On"}) is None  # no program named
    got = rules.logger_settings({"S-66": "On", "S-69": r" C:\tools\logger.exe ", "S-67": 80})
    assert got == {"jitter_ms": 80.0, "record_s": 30, "program": r"C:\tools\logger.exe"}
    # 2026-11-06 user: the jitter limit's default is 200 ms (was 50)
    got = rules.logger_settings({"S-66": "On", "S-69": r"C:\tools\logger.exe"})
    assert got is not None and got["jitter_ms"] == 200.0


def test_what_in_a_second_is_a_problem() -> None:
    assert rules.logger_reasons({"lost": 0, "cc": 0, "p1": 0, "jit": 3.0, "pb": "ok"}, 50) == []
    assert rules.logger_reasons({"lost": 1}, 50) == ["lost 1"]  # even one packet
    assert rules.logger_reasons({"cc": 2, "p1": 1}, 50) == ["glitch (CC 2, P1 1)"]
    assert rules.logger_reasons({"pb": "glitch"}, 50) == ["glitch (player)"]
    assert rules.logger_reasons({"pb": "freeze"}, 50) == ["picture freeze"]
    assert rules.logger_reasons({"jit": 80.5}, 50) == ["jitter 80.5 ms > 50 ms"]
    assert rules.logger_reasons({"jit": 50}, 50) == []  # the limit itself is fine
    assert rules.logger_reasons({"rx": 0}, 50) == ["no video (0 packets)"]
    assert rules.logger_reasons({"rx": 3300}, 50) == []


def test_only_the_routers_problems_start_it_after_the_judging_wait() -> None:
    r = ctl.RunController.__new__(ctl.RunController)
    r._rules = rules.create()
    r._plan = SimpleNamespace(sender="tx")
    r._windows = {"tx": {"sender": [[100.0, 110.0, "ZL-SND-026"]]}}
    r._logger_wait = []
    r.state = ctl.RunState.RUNNING
    got: list[tuple[str, str]] = []

    def trigger(why: str, dev: str) -> bool:
        got.append((why, dev))
        return True

    r.router_logger = SimpleNamespace(cfg={"jitter_ms": 50.0}, trigger=trigger)  # type: ignore[assignment]
    r._node = lambda device: "rx"  # type: ignore[method-assign]
    r._note_logger("tablet-1", {"lost": 40}, 105.0)  # the sender's noise
    r._note_logger("tablet-1", {"lost": 0}, 105.0 + ctl.LOGGER_JUDGE_S)
    assert got == []
    r._note_logger("tablet-1", {"lost": 3}, 200.0)  # nothing of the sender's then
    r._note_logger("tablet-1", {"lost": 0}, 201.0)
    assert got == []  # waits LOGGER_JUDGE_S for a late sender event
    r._note_logger("tablet-1", {"lost": 0}, 200.0 + ctl.LOGGER_JUDGE_S)
    assert got == [("lost 3", "tablet-1")]


def test_a_pc_with_video_goes_on_when_its_tablets_have_none() -> None:
    """2026-11-05 user ("PC부터 해결해"): the tablets got no video and the start was called
    off, stopping the PC that played. Now the test goes on; the tablets are named."""
    assert rules.warmup_split(
        {"receivers": [{"measuring": True}],
         "tablets": [{"id": "tablet-1", "measuring": False}, {"id": "tablet-2"}]}, "rx"
    ) == (["rx"], ["tablet-1", "tablet-2"])  # fmt: skip
    r = ctl.RunController.__new__(ctl.RunController)
    r._rules = rules.create()
    r._plan = SimpleNamespace(warmup_max_s=-10)  # time is up at once
    r._watch = {"rx": SimpleNamespace(last_error=None)}  # type: ignore[dict-item]
    r._nodes = lambda: {"rx"}  # type: ignore[method-assign]
    tabs = [{"id": "tablet-1", "measuring": False}]
    hb = {"module_data": {"iptv": {"receivers": [{"measuring": True}], "tablets": tabs}}}
    r._asked = lambda name: hb  # type: ignore[method-assign]
    said: list[tuple[str, str]] = []
    r._event = lambda code, level, text, device="", **d: said.append((code, text))  # type: ignore[method-assign]
    r.router_logger = SimpleNamespace(trigger=lambda why, dev: False)  # type: ignore[assignment]
    r._wait_measuring()  # no error: the PC's test goes on
    assert said == [("ZL-NET-003", "tablet-1: no video after warm-up — the test goes on; "
                                   "it counts as nothing received")]  # fmt: skip
