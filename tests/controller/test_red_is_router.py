"""Red is the router's only (2026-10-31 user: "702 lost라고 빨갛게 lost라고 적으면 라우터가 놓친것도
아닌데 라우터 이슈처럼 보임", "Peak loss 0.0704 % 이게 공유기 잘못인가? 리포트에 빨갛게 나와"):
the live page and the report color a loss by the box it sits in, never red by itself."""

from __future__ import annotations

from pathlib import Path

from controller import live_page, report_page
from modules.iptv import record_keys as k
from modules.iptv.run import LIVE_COLUMNS


def test_the_report_draws_no_loss_red_by_itself() -> None:
    s = report_page.SCRIPT
    assert "fill='#c62828'" not in s
    assert s.count("#c62828") == 1  # lossInk's own red for a red or unjudged box
    assert ".card.side .v" in report_page.STYLE and "__SIDE__" not in report_page.STYLE


def test_the_live_page_draws_no_loss_red_by_itself() -> None:
    page = live_page.page()
    assert 'fill:"var(--red)"' not in page
    assert "lossCls(" in page and "secCls(" in page and "td.side{" in page


def test_the_receivers_own_player_columns_are_never_the_routers() -> None:
    own = {c["key"] for c in LIVE_COLUMNS if c.get("receiver")}
    assert own == {k.PLAYBACK, k.PLAY_DROPPED}


def test_the_results_web_serves_the_guide_with_the_build(tmp_path: Path) -> None:
    """08 2: the screen's Help opens /guide; the build is in it, no "{BUILD}" left."""
    from controller.web import ResultsWeb, Traffic

    web = ResultsWeb(tmp_path, lambda: {}, lambda: {}, lambda: "Any", lambda: [], lambda: None,
                     Traffic(), guide=lambda: "<h1>guide 20990101.0000</h1>")  # fmt: skip
    code, kind, body = web.page("/guide")
    assert code == 200 and "html" in kind and b"20990101.0000" in body
