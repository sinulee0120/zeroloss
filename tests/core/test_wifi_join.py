"""wifi_join block (03 F17 "무선 수신 PC"): nmcli steps, the password never on a
command line or in any text, never-default, clean failures. Never runs real nmcli."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from core.blocks.wifi_join import PROFILE, WifiJoin
from core.protocol.envelope import ZlError

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux path")
SECRET = "s3cret-Pass!"


class FakeNm:
    """nmcli / iw stand-in: records every command, joins when ``up`` succeeds."""

    def __init__(
        self, tmp: Path, up_rc: int = 0, gives_ip: bool = True,
        up_out: str = f"Error: Connection activation failed: bad secret {SECRET}",
        scan: str = "",
    ) -> None:  # fmt: skip
        self.calls: list[list[str]] = []
        self.secrets: list[str] = []  # passwd-file contents seen while ``up`` ran
        self.up_rc, self.gives_ip, self.up_out, self.scan = up_rc, gives_ip, up_out, scan
        self.ssid: str | None = None
        self.pending: str | None = None
        self.root = tmp / "net"
        for nic, wireless in (("enp0s31f6", False), ("wlp4s0", True)):
            (self.root / nic / "device").mkdir(parents=True)
            (self.root / nic / "address").write_text("aa:bb:cc:00:11:22\n")
            if wireless:
                (self.root / nic / "wireless").mkdir()

    def run(self, argv: list[str], timeout_s: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        if argv[:2] == ["iw", "dev"]:
            return 0, f"Connected to 11:22:33:44:55:66\n\tSSID: {self.ssid}\n" if self.ssid else ""
        if argv[4:7] == ["device", "wifi", "list"]:  # nmcli -t -f SSID,SECURITY device wifi list
            return 0, self.scan
        if "up" in argv:
            if "passwd-file" in argv:
                self.secrets.append(Path(argv[argv.index("passwd-file") + 1]).read_text())
            if self.up_rc:
                return self.up_rc, self.up_out
            self.ssid = self.pending
            return 0, "Connection successfully activated"
        if "add" in argv:
            self.pending = argv[argv.index("ssid") + 1]
            return 0, "added"
        return 0, ""

    def ip(self, nic: str) -> str | None:
        return "192.168.50.23" if nic == "wlp4s0" and self.ssid and self.gives_ip else None


def joiner(fake: FakeNm, tmp: Path) -> WifiJoin:
    return WifiJoin(fake.run, fake.root, fake.ip, tmp, sleep=lambda s: None,
                    rfkill_root=tmp / "no-rfkill")  # fmt: skip


def texts(e: ZlError) -> str:
    return " ".join([e.what, e.why, e.fix, e.detail])


def test_join_wpa2_password_only_in_a_file_removed_at_once(tmp_path: Path) -> None:
    fake = FakeNm(tmp_path)
    j = joiner(fake, tmp_path)
    assert j.status() == {"supported": True, "rfkill": [], "radio": None, "nics": [
        {"nic": "wlp4s0", "mac": "aa:bb:cc:00:11:22", "ssid": None, "ip": None,
         "operstate": None, "nm_state": None}]}  # fmt: skip
    before = len(fake.calls)  # status() reads nmcli too (GAP-157)
    got = j.join("ZL-TEST", "WPA2", SECRET)
    assert got == {"nic": "wlp4s0", "ssid": "ZL-TEST", "ip": "192.168.50.23", "joined": True}
    assert not any(SECRET in " ".join(c) for c in fake.calls)  # never on a command line
    assert fake.secrets == [f"802-11-wireless-security.psk:{SECRET}\n"]
    assert not list(tmp_path.glob("zl-wifi-*"))  # the secret file is gone
    nm = [c for c in fake.calls[before:] if c[0] == "nmcli"]
    assert nm[0] == ["nmcli", "connection", "delete", "id", PROFILE]  # the same profile again
    add = nm[1]
    assert add[add.index("ifname") + 1] == "wlp4s0" and add[add.index("con-name") + 1] == PROFILE
    for key in ("ipv4.never-default", "ipv6.never-default"):  # the wired link keeps the route
        assert add[add.index(key) + 1] == "yes"
    assert add[add.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
    assert not any("enp0s31f6" in " ".join(c) for c in nm)  # the wired NIC is not touched


def test_wpa3_and_open(tmp_path: Path) -> None:
    fake = FakeNm(tmp_path)
    joiner(fake, tmp_path).join("ZL-6G", "WPA3", SECRET)
    assert "sae" in next(c for c in fake.calls if "add" in c)
    fake2 = FakeNm(tmp_path / "b")
    joiner(fake2, tmp_path).join("OPEN-NET", "Open", "")
    up = next(c for c in fake2.calls if "up" in c)
    assert "passwd-file" not in up and "wifi-sec.key-mgmt" not in next(
        c for c in fake2.calls if "add" in c
    )


def test_failures_are_rcv_023_without_the_password(tmp_path: Path) -> None:
    fake = FakeNm(tmp_path, up_rc=4)
    with pytest.raises(ZlError) as e:
        joiner(fake, tmp_path).join("ZL-TEST", "WPA2", SECRET)
    assert e.value.code == "ZL-RCV-023" and SECRET not in texts(e.value)
    assert "***" in e.value.why and not list(tmp_path.glob("zl-wifi-*"))
    fake = FakeNm(tmp_path / "c", gives_ip=False)
    with pytest.raises(ZlError) as e:
        joiner(fake, tmp_path).join("ZL-TEST", "WPA2", SECRET, timeout_s=0.01)
    assert e.value.code == "ZL-RCV-023" and "No address" in e.value.what
    with pytest.raises(ZlError) as e:
        joiner(FakeNm(tmp_path / "d"), tmp_path).join("ZL-TEST", "WPA2", SECRET, nic="wlan9")
    assert e.value.code == "ZL-RCV-023" and "wlan9" in e.value.what


NO_ANSWER = "Error: Connection activation failed: 802.1X supplicant took too long to authenticate"
NOT_FOUND = "Error: Connection activation failed: The Wi-Fi network could not be found"


def test_profile_also_finds_hidden_ssids(tmp_path: Path) -> None:
    """GAP-101: an IPTV SSID is often hidden; the profile asks for the name itself."""
    for security, password in (("Open", ""), ("WPA2", SECRET)):
        fake = FakeNm(tmp_path / security)
        joiner(fake, tmp_path).join("ZL_WIFI_IPTV_TEST", security, password)
        add = next(c for c in fake.calls if "add" in c)
        assert add[add.index("802-11-wireless.hidden") + 1] == "yes"


def test_open_wifi_the_router_does_not_answer_says_no_word_of_a_password(tmp_path: Path) -> None:
    """GAP-101 (real case): Open hidden SSID, auth ok, association timed out 3/3."""
    fake = FakeNm(tmp_path, up_rc=4, up_out=NO_ANSWER)
    with pytest.raises(ZlError) as e:
        joiner(fake, tmp_path).join("ZL_WIFI_IPTV_TEST", "Open", "")
    err = e.value
    assert err.code == "ZL-RCV-023"
    assert "did not answer" in err.why and "password" not in (err.why + err.fix).lower()
    assert "MAC" in err.fix and "aa:bb:cc:00:11:22" in err.fix  # the PC's Wi-Fi MAC to allow


def test_secured_wifi_no_answer_also_points_at_the_password(tmp_path: Path) -> None:
    fake = FakeNm(tmp_path, up_rc=4, up_out=NO_ANSWER + f" {SECRET}")
    with pytest.raises(ZlError) as e:
        joiner(fake, tmp_path).join("ZL-TEST", "WPA2", SECRET)
    assert "password" in e.value.fix.lower() and SECRET not in texts(e.value)


def test_router_security_differs_from_s51_is_named(tmp_path: Path) -> None:
    """GAP-101 (real case): saved Open, the router still had WPA2/WPA3 → 'not found'."""
    scan = "ZL_TEST_2.4G:WPA2 WPA3\nZL_TEST_5G:WPA2 WPA3\nSK\\:odd\\:name:\n:--\n"
    fake = FakeNm(tmp_path, up_rc=10, up_out=NOT_FOUND, scan=scan)
    with pytest.raises(ZlError) as e:
        joiner(fake, tmp_path).join("ZL_TEST_5G", "Open", "")
    assert "WPA2 WPA3" in e.value.why and "Open" in e.value.why
    assert "zeroloss wifi set" in e.value.fix
    fake = FakeNm(tmp_path / "b", up_rc=10, up_out=NOT_FOUND, scan="SK\\:odd\\:name:\n")
    with pytest.raises(ZlError) as e:  # an Open network saved as WPA2, name with colons
        joiner(fake, tmp_path).join("SK:odd:name", "WPA2", SECRET)
    assert "Open" in e.value.why and "WPA2" in e.value.why


def test_a_busy_card_is_scanned_again_before_saying_not_seen(tmp_path: Path) -> None:
    """Real case (2026-10-03): the first scan right after the failed attempt did not name
    the SSID; a fresh one did, with WPA2 WPA3 while S-51 said Open."""
    fake = FakeNm(tmp_path, up_rc=10, up_out=NOT_FOUND, scan="")
    scans = iter(["", "ZL_WIFI_IPTV_TEST:WPA2 WPA3\n"])
    plain = fake.run

    def run(argv: list[str], timeout_s: float) -> tuple[int, str]:
        if argv[4:7] == ["device", "wifi", "list"]:
            fake.calls.append(list(argv))
            return 0, next(scans)
        return plain(argv, timeout_s)

    fake.run = run  # type: ignore[method-assign]
    with pytest.raises(ZlError) as e:
        joiner(fake, tmp_path).join("ZL_WIFI_IPTV_TEST", "Open", "")
    assert "WPA2 WPA3" in e.value.why and "Open" in e.value.why
    rescans = [c[c.index("--rescan") + 1] for c in fake.calls if "--rescan" in c]
    assert rescans == ["auto", "yes"]


def test_not_seen_does_not_blame_the_name_of_a_hidden_ssid(tmp_path: Path) -> None:
    fake = FakeNm(tmp_path, up_rc=10, up_out=NOT_FOUND, scan="OTHER:WPA2\n")
    with pytest.raises(ZlError) as e:
        joiner(fake, tmp_path).join("ZL_WIFI_IPTV_TEST", "Open", "")
    assert "not seen" in e.value.why and "hidden" in e.value.fix


def test_a_failed_join_leaves_no_profile_behind(tmp_path: Path) -> None:
    """GAP-103 (real case): the profile stayed with autoconnect on after a refused join
    and kept taking the card. Every failure path deletes it (G-11)."""
    drop = ["nmcli", "connection", "delete", "id", PROFILE]
    fake = FakeNm(tmp_path, up_rc=4, up_out=NO_ANSWER)
    with pytest.raises(ZlError):
        joiner(fake, tmp_path).join("ZL_WIFI_IPTV_TEST", "WPA2", SECRET)
    assert fake.calls[-1] == drop
    fake = FakeNm(tmp_path / "b", gives_ip=False)
    with pytest.raises(ZlError):
        joiner(fake, tmp_path).join("ZL-TEST", "WPA2", SECRET, timeout_s=0.01)
    assert drop in fake.calls[2:]  # after the add, not only the delete before it
    fake = FakeNm(tmp_path / "c")  # success keeps it (reconnects after a drop)
    joiner(fake, tmp_path).join("ZL-TEST", "WPA2", SECRET)
    assert fake.calls.count(drop) == 1


def test_nmcli_timeout_says_the_router_refused_and_names_the_mac(tmp_path: Path) -> None:
    """Real case 2026-10-03: WPA2 hidden SSID, ASSOC-REJECT status 1 every try → nmcli
    only said "Timeout expired (30 seconds)"."""
    fake = FakeNm(tmp_path, up_rc=3, up_out="Error: Timeout expired (30 seconds)")
    with pytest.raises(ZlError) as e:
        joiner(fake, tmp_path).join("ZL_WIFI_IPTV_TEST", "WPA2", SECRET)
    assert "did not finish in 30 s" in e.value.why and "refused" in e.value.why
    assert "aa:bb:cc:00:11:22" in e.value.fix and "password" in e.value.fix.lower()


def test_no_wifi_card_and_other_os(tmp_path: Path, monkeypatch: Any) -> None:
    empty = tmp_path / "none"
    empty.mkdir()
    j = WifiJoin(lambda a, t: (0, ""), empty, lambda n: None, tmp_path, rfkill_root=empty)
    assert j.status() == {"supported": True, "nics": [], "rfkill": [], "radio": None}
    with pytest.raises(ZlError) as e:
        j.join("ZL-TEST", "WPA2", SECRET)
    assert e.value.code == "ZL-RCV-023"
    monkeypatch.setattr(sys, "platform", "win32")
    assert j.status() == {"supported": False, "nics": [], "rfkill": [], "radio": None}
    with pytest.raises(ZlError) as e:
        j.join("ZL-TEST", "WPA2", SECRET)
    assert "not supported" in e.value.what


def test_status_says_what_holds_a_card_off(tmp_path: Path) -> None:
    """GAP-157: the sender PC's Wi-Fi would not come on; the Controller reads why over the
    cable: rfkill (hard = the key or the BIOS), the NetworkManager radio, the card's state."""
    net, rf = tmp_path / "net", tmp_path / "rfkill"
    (net / "wlo1" / "wireless").mkdir(parents=True)
    (net / "wlo1" / "address").write_text("02:00:5e:50:00:aa")
    (net / "wlo1" / "operstate").write_text("down")
    for name, kind, soft, hard in (("phy0", "wlan", "0", "1"), ("hci0", "bluetooth", "0", "0"),
                                   ("x", "nfc", "1", "1")):  # fmt: skip
        d = rf / f"rfkill{name}"
        d.mkdir(parents=True)
        for k, v in (("name", name), ("type", kind), ("soft", soft), ("hard", hard)):
            (d / k).write_text(v)

    def run(argv: list[str], t: float) -> tuple[int, str]:
        if argv[:3] == ["nmcli", "-t", "radio"]:
            return 0, "enabled\n"
        return 0, "enp37s0:connected:zeroloss-wan\nwlo1:unavailable:\n"

    st = WifiJoin(run, net, lambda n: None, tmp_path, rfkill_root=rf).status()
    assert st["radio"] == "enabled"
    wlan = {"name": "phy0", "type": "wlan", "soft": False, "hard": True}
    bt = {"name": "hci0", "type": "bluetooth", "soft": False, "hard": False}
    assert st["rfkill"] == [bt, wlan]
    assert st["nics"][0]["operstate"] == "down" and st["nics"][0]["nm_state"] == "unavailable"
