"""F16 time range view in report.html (03 F16 and its implementation rules, 20 GAP-86),
in a real browser from file:// without a server: quick ranges, From/To, red only,
devices, the range in the URL, 1-second graphs loaded from data files, CSV, and a
100-hour report that opens quickly. The overall verdict never changes with the range.
Runs where Playwright and a Chromium are installed."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.report import write_report

sync_api = pytest.importorskip("playwright.sync_api")
CELL = 300


def make_run(tmp: Path, hours: float = 2.5, devices: tuple[str, ...] = ("a", "b"),
             records_h: int | None = 1, red_at: int = 14) -> Path:  # fmt: skip
    d = tmp / "R20260927-080000"
    d.mkdir(parents=True)
    n = int(hours * 3600 // CELL)
    judged, lines = [], []
    for k, dev in enumerate(devices, start=1):
        cells = []
        for i in range(n):
            red = dev == "a" and i == red_at
            lines.append({"device": dev, "ch": k, "index": i, "start_elapsed_s": i * CELL,
                          "duration_s": CELL, "loss": {"lost": 7 if red else 0, "expected": 1000},
                          "jitter": {"max_ms": 9.0 if red else 2.0, "avg_ms": 1.0}})  # fmt: skip
            color, why = ("red", ["lost"]) if red else ("green", [])
            cells.append({"index": i, "start_elapsed_s": i * CELL, "duration_s": CELL,
                          "color": color, "reasons": why})  # fmt: skip
        judged.append({"device": dev, "ch": k, "cells": cells, "loss_total": 7 if dev == "a" else 0,
                       "jitter_max_ms": 9.0, "colors": {"green": n}})  # fmt: skip
    (d / "summaries.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    (d / "timeline.json").write_text(json.dumps({f"{dev}|{k}": 10.0 for k, dev in
                                                 enumerate(devices, start=1)}))  # fmt: skip
    (d / "run-state.json").write_text(json.dumps({"started_utc": "2026-09-27T08:00:00Z"}))
    if records_h is not None:  # the 1-second records of the first hour(s) are here
        for k, dev in enumerate(devices, start=1):
            (d / "raw" / dev).mkdir(parents=True)
            rec = [{"type": "s", "e": e, "ch": k, "lost": 0, "jit": 1.5, "rx": 900}
                   for e in range(records_h * 3600 - 10)]  # fmt: skip
            (d / "raw" / dev / f"{d.name}_h000.jsonl").write_text(
                "\n".join(json.dumps(x) for x in rec))  # fmt: skip
    result: dict[str, Any] = {
        "run_id": d.name, "verdict": "FAIL", "with_warnings": False, "verdict_details": [],
        "verdict_reason": "planned_time_reached", "planned_s": int(hours * 3600),
        "actual_s": int(hours * 3600), "test_type": "pc", "quality": "HD", "not_run": [],
        "devices": judged, "problems": [],
        "analysis": [{"rule": "R-05", "category": "Router", "confidence": "high",
                      "text": "together", "devices": ["a"], "at_s": 14 * CELL + 10.0},
                     {"rule": "R-16", "category": "Router", "confidence": "medium",
                      "text": "dups", "devices": ["b"], "at_s": None}],
    }  # fmt: skip
    write_report(d, result, {"module": "iptv"})
    return d


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            try:  # the PC's own Chrome, when Playwright's Chromium is not downloaded
                b = p.chromium.launch(channel="chrome")
            except Exception as e:  # no browser here
                pytest.skip(f"no Chromium: {e}")
        yield b
        b.close()


def cells(page: Any, dev: str | None = None) -> int:
    sel = "#grid .c" if dev is None else f"#grid .c[data-k='{dev}']"
    return int(page.locator(sel).count())


def test_quick_ranges_devices_and_the_verdict_stays(browser: Any, tmp_path: Path) -> None:
    d = make_run(tmp_path)
    page = browser.new_page()
    page.goto((d / "report.html").as_uri())
    shown = page.inner_text("#shown")
    assert "Overall result: FAIL (unchanged)" in shown and "of 2h 30m" in shown
    assert cells(page) == 60 and "(" in shown  # the real time next to the elapsed time
    page.click("button[data-q=first]")
    assert cells(page, "a|1") == 12 and "Showing 0h 00m" in page.inner_text("#shown")
    assert "FAIL (unchanged)" in page.inner_text("#shown")
    page.click("button[data-q=red]")
    assert cells(page) == 1 and page.locator("#grid .c").get_attribute("title").startswith("1h 10m")
    page.click("button[data-q=red1]")  # 30 min around the first red (at 70 min + offset)
    assert page.input_value("#from") == "0h 40m" and page.input_value("#to") == "1h 40m"
    assert "R-05" in page.inner_text("#causes") and "R-16" in page.inner_text("#causes")
    page.click("button[data-q=last]")
    assert "R-05" not in page.inner_text("#causes")  # only what happened in the range
    page.fill("#from", "1h 00m")
    page.fill("#to", "1h 30m")
    page.dispatch_event("#to", "change")
    assert cells(page, "a|1") == 7  # cells touching 1h00m-1h30m (run clock, +10 s)
    lost = page.locator("#sum tr").nth(1).inner_text()
    assert "\t7\t" in lost  # recomputed for the range: the red cell's 7 packets
    page.uncheck("#devs input[value='b|2']")
    assert cells(page, "b|2") == 0 and "dev=a%7C1" in page.url


def test_the_url_keeps_the_range(browser: Any, tmp_path: Path) -> None:
    d = make_run(tmp_path)
    page = browser.new_page()
    page.goto((d / "report.html").as_uri() + "#from=3600&to=7200&dev=b%7C2")
    assert cells(page, "a|1") == 0 and cells(page, "b|2") == 13
    assert page.input_value("#from") == "1h 00m"


def test_a_cell_opens_its_seconds_or_says_they_are_not_here(browser: Any, tmp_path: Path) -> None:
    d = make_run(tmp_path, records_h=1)
    assert (d / "data" / "data_h000.js").is_file() and not (d / "data" / "data_h001.js").exists()
    page = browser.new_page()
    page.goto((d / "report.html").as_uri())
    page.locator("#grid .c[data-k='a|1'][data-i='2']").click()
    page.wait_for_selector("#second[data-rows]")
    assert page.get_attribute("#second", "data-rows") == "300"
    assert page.locator("#second svg polyline").count() == 2
    page.locator("#grid .c[data-k='a|1'][data-i='20']").click()  # second hour: no records
    page.wait_for_function("document.getElementById('second').textContent.includes('not here')")
    page.click("#csv")
    out = page.evaluate("window.ZL_CSV")
    assert out.startswith("device,ch,from_s,to_s,color,lost,jitter_max_ms,why\n")  # GAP-116
    assert "1-second,run_s,lost,jitter_ms" in out


def test_before_the_records_the_page_works_from_the_cells(browser: Any, tmp_path: Path) -> None:
    d = make_run(tmp_path, records_h=None)
    assert not (d / "data").exists()
    page = browser.new_page()
    page.goto((d / "report.html").as_uri())
    assert page.locator("#graph polyline").count() >= 2  # 5-minute points
    page.locator("#grid .c").first.click()
    page.wait_for_function("document.getElementById('second').textContent.includes('not here')")


def test_a_100_hour_report_opens_quickly(browser: Any, tmp_path: Path) -> None:
    d = make_run(tmp_path, hours=100, devices=("a", "b", "c"), records_h=None)
    page = browser.new_page()
    start = time.monotonic()
    page.goto((d / "report.html").as_uri())
    page.wait_for_selector("#grid .c")
    assert time.monotonic() - start < 5.0  # 03 F16 completion
    assert cells(page) == 3600


@pytest.mark.parametrize(("width", "height"), [(360, 740), (390, 844), (768, 1024), (3840, 2160)])
def test_the_report_fits_a_phone_and_a_4k_screen(
    browser: Any, tmp_path: Path, width: int, height: int
) -> None:
    """11-ui 7 "태블릿·보고서": no sideways scrolling of the whole page and nothing cut;
    the 5-min cells wrap inside the page (a 100 h run has 1,200 per device)."""
    d = make_run(tmp_path, hours=100)
    page = browser.new_page(viewport={"width": width, "height": height})
    page.goto((d / "report.html").as_uri())
    page.wait_for_load_state("load")
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert over <= 1, f"{width} px wide: the page is {over} px wider than the screen"
    page.screenshot(path=str(tmp_path / f"report-{width}x{height}.png"))
    page.close()
