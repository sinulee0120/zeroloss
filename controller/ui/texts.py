"""Screen text: the one place for words on the Controller screens (docs/11-ui.md 1.7).

Terms follow the 11-ui 1.7-1 table (a test checks the banned words). A Korean edition
later changes only this file.
"""

from __future__ import annotations

TEXT: dict[str, str] = {
    "app_title": "ZeroLoss",
    "group_test": "Test",
    "group_devices": "Devices",
    "group_cells": "5-min results",
    "group_status": "Status",
    "reports": "Reports",
    "live_view": "Live view",  # 02 12 /live (GAP-119)
    "tip_live_view": "The test every second in your browser: values, 10-minute charts,"
    " seconds with trouble and the 5-minute boxes with their reasons. From another PC:"
    " http://<this PC>:{port}/live",
    "last_report": "Last report",
    "reset_all": "Reset All",
    "fault_title": "The test was stopped by a fault",
    "fault_what": "{device} stopped answering ({reason}). Last answer: {last_ok}.",
    "fault_what_other": "The test stopped: {reason}.",
    "fault_data": "Report written for {run_id} ({verdict}). Data after {last_ok} is missing.",
    "fault_no_report": "No report was written.",
    "fault_restart": "Restart",
    "fault_restart_tip": "Restart the ZeroLoss service on this PC (through its Deployer)",
    "fault_shutdown": "Shut Down All",
    "fault_shutdown_tip": "Stop the ZeroLoss node on every PC; start them again with "
    "'zeroloss nodes start'",
    "reset_title": "Reset All?",
    "reset_body": "Every device stops everything and checks that nothing is left. "
    "Then a new test can start.",
    "shutdown_title": "Shut down all nodes?",
    "shutdown_body": "The ZeroLoss node stops on every PC. The PCs stay on; "
    "'zeroloss nodes start' starts the nodes again.",
    "fault_state_stopped": "Stopped",
    "fault_state_silent": "Not answering",
    "fault_state_clean": "Clean",
    "fault_state_not_clean": "Not clean: {why}",
    "fault_state_no_answer": "No answer — {fix}",
    "follow_live": "Follow live",
    "test_type": "Test type",
    "video_quality": "Video quality",
    "duration": "Duration",
    "send_reports": "Send quality reports",
    "advanced": "Advanced settings",
    "tip_test_type": "Which test to run. IPTV test: pick the receivers in the device rows, a "
    "PC and up to three tablets on its USB, on the same or different TV channels.",
    "tip_quality": "Video quality the sender sends. Example: HD · 1920×1080 · 10 Mbps.",
    "tip_duration": "How long to measure: pick one or type it (40m, 1h 30m, 100h). The test"
    " ends by itself after it.",
    "duration_title": "Duration",
    "duration_bad": '"{text}" is not a test length. Type it like 40m, 1h 30m or 100h'
    " (10 s to 200 h).",
    "tip_send_reports": "Receivers send 1-second values while testing, for the live screen and "
    "the 1-second graphs.",
    "tip_advanced": "Show the less used settings. Their values stay when hidden.",
    "setup_loading": "Reading the test devices…",
    "setup_saving": "Saving…",
    "role_sender": "Sender",
    "role_receiver": "Receiver",
    "role_unused": "Not used",
    # Tablet test (11-ui 3, 2026-10-23): a row per tablet on a PC's USB
    "tablet_new": "Tablet",
    "relays_tablets": "relays {n} tablet(s) by USB",
    "usb_unseen": "{n} tablet(s) on its USB not seen by adb: plug in again, try another "
    "cable, straight into the PC (not a hub or dock), then Refresh",
    "tablet_on": "on {host} USB",
    "tablet_battery": "battery {n}%{how}",
    "tablet_charging": " charging",
    "tablet_wifi": "Wi-Fi {ip}",
    "tablet_no_wifi": "no Wi-Fi yet",
    "tablet_app": "app {v}",
    "tablet_no_app": "The ZeroLoss app is not on this tablet: press Update PCs.",
    "tablet_low_battery": "Battery {n}% and not charging: plug in a charger for a long test.",
    "tablets_full": "Three tablets at most: set another one to Not used first.",
    "tablet_wifi_keep": "Wi-Fi: keep {ssid}",
    "tablet_wifi_keep_none": "Wi-Fi: keep as it is (on none now)",
    "tablet_wifi_unseen": "{ssid} (not seen by the tablet now)",
    "tip_tablet_wifi": "The router Wi-Fi this tablet joins at Start, by itself (adb). Keep: it "
    "stays on the Wi-Fi it is on.",
    # the same choice for a test without video (throughput): what each PC does there
    "role_sender_iperf": "WAN side PC",
    "this_pc_iperf": "this PC · measured as a LAN-side PC",
    "this_pc_video": "this PC · runs the test",
    "update_pcs": "Update PCs to build {build}",
    "update_pcs_busy": "Updating the PCs… (a few minutes; ZeroLoss restarts on each PC)",
    "update_pcs_tip": "Installs this program's build on every PC that has another build, "
    "from the packages folder next to this program.",
    # S-65 "Wi-Fi to test" (2026-10-17 user: the person picks the card and each band)
    "wifi_card": "{pc} Wi-Fi card",
    "wifi_card_none": "— not tested",
    "wifi_card_item": "{nic} ({where})",
    "wifi_band": "{band}",
    "wifi_band_none": "— not tested",
    "wifi_band_item": "{ssid} · {bssid} · {signal} % · {security}",
    "wifi_band_kept": "{ssid} · {bssid} · picked before (Scan to see it now)",
    # the box beside a band's SSID (2026-10-17 user: "옆에 칸 만들어 SSID 고르고 암호 넣게",
    # "암호가 없으면 빈칸 또는 open", "빈칸이면 암호 없음을 의미")
    "wifi_pw_open": "open",
    "wifi_pw_empty": "password (empty = none)",
    "wifi_pw_saved": "password saved",
    "wifi_pw_tip": "The Wi-Fi password of the network picked on the left. Empty = joined "
    "without a password. Kept encrypted on this PC; never shown again.",
    "wifi_scan": "Scan",
    "card_not_tickable": "Not measured: the WAN card, or a card that is not on the router "
    "LAN side.",
    "wifi_scanning": "Scanning…",
    "wifi_scan_tip": "Look again for the Wi-Fi networks this card sees (about 10 s).",
    "wifi_not_seen": "{band}: none seen (scan again, or this card cannot use it)",
    "wifi_scan_failed": "Scan failed: {why}",
    "wifi_no_band": "No network picked for a band: this card is measured only on the Wi-Fi it "
    "is on now. Pick the router's network for each band to test 2.4 / 5 / 6 GHz.",
    "wifi_scan_old": "Scan failed: this PC has an older ZeroLoss. Press Update PCs, then Scan.",
    "role_receiver_iperf": "LAN side PC",
    "side_wan": "router WAN side",
    "side_lan": "router LAN side",
    "side_direct": "cable to the other PC, no router",  # S-54 baseline (GAP-115)
    "card_wired": "Wired {nic}{speed}",  # the test's card on a row (GAP-127)
    "card_wireless": "Wi-Fi {nic}{ssid}",
    "card_wifi_test": "Wi-Fi (test Wi-Fi)",
    "card_ip": "Card {ip}",
    "card_tip": "The network card this PC sends or receives the test on.",
    "card_auto": "Card: Auto",  # DSC-006, GAP-115: the person can pick the test card
    "card_gone": "(not on this PC now)",  # its cards are known and this is not one
    "card_unseen": "(cards not seen yet)",  # nothing known about its cards (GAP-121)
    "tip_card": "The network card this PC sends or receives the test on. Auto lets the"
    " Controller pick it; pick one yourself when the check says the test card is wrong or"
    " unknown. Example: enp0s31f6 · 10.0.0.100.",
    "side_unknown": "not answering",
    # GAP-153 / 07 5: a PC still not found after the automatic search gets its address
    # typed once (it shows it on its own screen under "My IP")
    "tip_address": "The address the Controller uses for this PC. Pick one of its addresses"
    " (in use, found now, its cards), or type the one its ZeroLoss window shows under My IP"
    " and press Enter (example: 10.0.0.1 or 10.0.0.1:10992). Used only when that PC answers"
    " there.",
    "address_placeholder": "IP address",
    "address_title": "Address of {name}",
    "address_bad": "{text!r} is not an address. Type it like 10.0.0.1 or 10.0.0.1:10992.",
    "address_failed_title": "Address not used",
    "address_failed_body": "{what}\n\n{why}",
    "tip_role": "Sender: the PC on the router WAN side (it plays the ISP). Receiver: a PC on "
    "the router LAN or Wi-Fi. A LAN sender makes a comparison test.",
    "buffer_default": "Buffer: test's",
    "buffer_ms": "Buffer {s:g} s",
    "tip_buffer": "How much video this receiver's player keeps ahead. Each PC and player "
    'differs: a slow PC may need more. "Test\'s" uses Player buffer (S-25).',
    "tip_network": "Does the receiver PC get the video over its cable or its Wi-Fi? (S-20)",
    "tip_channel": "The TV channel this receiver watches. Example: CH2. Default: a different "
    "TV channel each (S-04).",
    "screen_too_small": "Screen too small, some parts need scrolling",
    "broadcast_on": "Broadcast: ON ({n} TV channels)",
    "broadcast_tip": "Always-on (S-13): the sender keeps sending between tests, like an ISP. "
    "Stop ends the test only. Since {since}.",
    "stop_broadcast": "Stop broadcast",
    "stop_broadcast_title": "Stop the broadcast?",
    "stop_broadcast_body": "The sender stops sending all TV channels.",
    "tip_channel_live": "Change this receiver to another TV channel now (Leave, then Join). "
    "Only TV channels being sent are listed. Locked while automatic TV channel change "
    "(S-21) is on.",
    "advanced_changed": "Advanced settings ({n} changed)",
    "no_devices": "No devices yet. Add one with 'zeroloss devices add'.",
    "no_cells": "No 5-min results yet.",
    # 2026-10-17 user: a test that is not IPTV shows its cases there, not 5-minute boxes
    "group_cases": "Results (one line per case)",
    "no_cases": "No case has ended yet. Each one shows here when it ends.",
    "case_device": "PC",
    "case_why": "Why",
    "reconnecting": "Reconnecting… The Controller service is not reachable.",
    "idle": "Idle",
    "not_in_test": "not in this test (no card of it ticked for this test type)",
    "warming_up": "Warming up",
    "last_update": "Last update {s} s ago",
    "rssi": "RSSI {v} dBm",
    "wifi_band_item_tablet": "{ssid} · {security}",
    "cells_merged": "Hours {a}-{b} of the test (one box, its worst result)",
    "rssi_none": "RSSI —",
    "rssi_none_tip": "Wi-Fi signal not available",
    "rssi_weak_tip": "Weak Wi-Fi signal (below {v} dBm)",
    "wifi": "Wi-Fi {ch} · {w} MHz",
    "wifi_no_width": "Wi-Fi {ch}",  # some drivers (rtl8814au) do not report the width
    "wifi_rate_width": "Wi-Fi {ch} · ≥{w} MHz",
    "wifi_rate_width_tip": "This card does not report its Wi-Fi channel width; {mbps:g} Mbps is "
    "more than any narrower width carries with 1 or 2 streams, so it is at least {w} MHz.",
    "wifi_changed": "Wi-Fi {c0}→{c1} · {w0}→{w1} MHz ({t})",
    "link": "{label} link {speed} {duplex}",
    "link_down": "{label} link down",
    "link_short": "{label} link {speed}",
    "link_need_tip": "this test needs about {n} Mbps",
    "link_usual_tip": "usually {s}",
    "status_link_short": "{label} link dropped to {speed} at {at} — losses after this point "
    "are likely from the link.",
    "status_link_down": "{label} link down since {at} — check the cable and port.",
    "status_link_slow": "{label} link at {speed} since {at} (usually {usual}) — still enough "
    "for this test, but often a cable or port problem.",
    "status_ready": "Ready. Press Start.",
    "status_not_ready": "Not ready — {why}",
    "status_cancelled": "Start cancelled. The sender PC goes on making the video; press Start"
    " when you like — it waits only for what is left.",  # 09 USR-006 (GAP-125)
    "step_cancelled": "cancelled by you",
    "status_not_started": "Did not start — {why}. Fix it, then press Start again.",
    "status_need_plan": "Choose the test devices.",
    "status_preparing": "Starting the test…",
    "status_preparing_videos": "Preparing videos: {text}",
    "status_running": "Running normally. Next 5-min result in {next}.",
    "status_running_cases": "Running normally. Each case's result shows in Results when it ends.",
    "status_running_end": "Running normally. The test ends and gives its result in {end}.",
    "status_running_problem": "{device}: {why}",
    "status_stopping": "Stopping and writing the report…",
    "status_stop_waiting": "Stopping… {s} s — the test ends its current step, then writes"
    " the report. Please wait.",
    "stop_failed_title": "Stop did not finish",
    "stop_failed_body": "Stop did not finish: {what}. {why}\n\nThe test may still be running."
    " Press Stop again; if it still does not stop, close ZeroLoss and open it again.",
    "status_fault": "A fault stopped the test. Press Reset All.",
    "status_finished": "Finished: {verdict}. Open Last report for details.",
    "source_video": "Source video",  # S-48 on the Test area (GAP-115)
    "tip_source_video": "Video the sender sends on each TV channel: Generated (default) or a movie"
    " from the sender PC's Videos folder. Example: CH1 = movie.mp4.",
    "source_generated": "Generated (default)",  # S-48 list (GAP-110)
    "source_checking": "{name} · checking…",
    "source_checking_tip": "Asking the sender PC for its videos.",
    "source_missing": "{name} · not on the sender PC",
    "source_missing_tip": "The sender PC does not have this file now; Start will say so.",
    "start_over_broadcast_title": "The broadcast sends something else",
    "start_over_broadcast_body": "The Always-on broadcast is still sending the earlier video"
    " or channels, so this test cannot use it. Stop the broadcast and start this test?",
    "making_now": "Making the CH{ch} video: {pct}%{left}",  # iptv 1-1-2 (GAP-116)
    "making_left": " · about {n} min left",
    "making_queued": "The CH{ch} video waits its turn",
    "making_checking": "Checking the CH{ch} video",
    "making_ready": "CH{ch} video ready",
    "making_failed": "The CH{ch} video could not be made: {why}",
    "source_file": "{name} · {width}×{height}{length}",
    "source_minutes": " · {n} min",
    # 2026-10-17 user: which videos start at once, which must be made first
    # in front of the name (2026-10-24 user: "바로 가능이 있고 변환 필요(시간 걸림)")
    "source_ready": "✔ Ready now · ",
    "source_to_make": "⏳ Needs converting (takes time) · ",
    "source_ready_tip": "Already made for this TV channel, quality and bitrate: Start uses it.",
    "source_to_make_tip": "Not made yet for this TV channel, quality and bitrate: the sender PC "
    "makes it first (a UHD movie can take hours). It starts as soon as you pick it.",
    # 11-ui 3 (GAP-106): the verdict first, then what it rests on; notes keep the verdict
    "finished_pass": "PASS — {loss}, max jitter {jitter} ms.",
    "finished_fail": "FAIL — {loss}: {why}.",
    "finished_other": "{verdict} — {why}.",
    "finished_no_loss": "no packet lost",
    "finished_cases": "{n} cases: {ok} passed, {check} check needed, {bad} failed",
    "finished_pass_cases": "PASS — {cases}.",
    "finished_fail_cases": "FAIL — {cases}: {why}.",
    "finished_lost": "{n} packets lost",
    "finished_notes": " {n} note(s), the verdict stands: {why}.",
    "status_failed": "{what}",
    "why": "Why",
    "fix": "Fix",
    "progress": "{elapsed} / {planned} ({pct}%)",
    "refresh": "Refresh",
    "help": "?",  # narrow: the top bar fits 1280 px while a test runs
    "tip_help": "Help: the install and use guide (in the browser), at the part for this test.",
    "refreshing": "Reading…",
    "refresh_tip": "Read the PCs and the tablets on their USB again now (they are read every few "
    "seconds anyway). A tablet that never shows: its USB cable or port (the PC does not see it).",
    "start_title": "Start the test?",
    # 2026-10-31 (a black window and typed commands were the only way): a Linux PC is
    # registered from here, as 2_리눅스PC_등록.bat does it (06 11.3)
    "add_linux": "Add a Linux PC",
    "add_linux_busy": "Installing on {host}… (a few minutes)",
    "add_linux_tip": "Install ZeroLoss on an Ubuntu PC over the network (SSH) and add it to "
    "Devices. Prepare that PC first with zeroloss-linux-setup.sh from the kit.",
    "linux_title": "Add a Linux PC",
    # 06 7 (2026-10-31, the remove buttons): ZeroLoss off a Linux PC, the app off a tablet
    "remove": "Remove…",
    "remove_tip": "Remove ZeroLoss from this PC (over SSH), or the ZeroLoss app from this "
    "tablet. Only between tests.",
    "remove_pc_title": "Remove ZeroLoss from {name}",
    "remove_purge": "Also delete its test data and settings (/var/lib/zeroloss)",
    "remove_tablet_title": "Remove the ZeroLoss app?",
    "remove_tablet_body": "The ZeroLoss app comes off tablet {serial} (on {host}). Nothing "
    "else on the tablet changes. Update PCs puts it back.",
    "removed_title": "Removed",
    "removed_pc": "ZeroLoss is off {name} and out of Devices.",
    "removed_tablet": "The ZeroLoss app is off tablet {serial}.",
    "remove_failed_title": "Not removed",
    "remove_failed_body": "{what}\n\n{why}\n\n{fix}",
    "linux_host": "IP address",
    "linux_user": "User name",
    "linux_password": "Password",
    "linux_name": "Name (sender, receiver…)",
    "linux_auto_login": "Log this user in after a reboot (receiving PC: its picture shows)",
    "linux_need": "Type the IP address, the user name and the password.",
    "linux_done_title": "Linux PC added",
    "linux_done_body": "{name} ({host}) is installed and in Devices.{missing}",
    "linux_missing": "\n\nNot installed on it yet: {tools}. Run zeroloss-linux-setup.sh there.",
    "linux_failed_title": "Linux PC not added",
    "linux_failed_body": "{what}\n\n{why}\n\n{fix}",
    "start_sender": "Sender: {pc}",
    "start_wan": "WAN side PC: {pc}",
    "start_wifi": "Wi-Fi {ssid} (kept)",
    "this_pc": "this PC",
    "start_body": "{what}\n\nThe test starts on every PC now.",
    "stop_title": "Stop the test?",
    "stop_body": "The test stops now and the report is written from the data so far. "
    "A stopped test is never PASS.",
    "close_title": "The test keeps running",
    "close_body": "The test keeps running in the background. Open ZeroLoss again to see it.",
    "no_report": "No report yet. Run a test first.",
    "problems_none": "No problems",
    "actions_title": "Action Required",
    "actions_badge": "🖐 {n} action{s} needed",
    "actions_none": "Nothing to do",
    "actions_cols": "Where|What|How|Why|State",
    "actions_waiting": "⏳ Waiting…",
    "actions_done_auto": "✔ Done (seen by ZeroLoss)",
    "actions_done_human": "✔ Done",
    "actions_done_button": "Done",
    "actions_close_button": "Close",
    "actions_all_done": "✔ All done — nothing to do now.",
    "actions_done_tip": "Mark the selected action done (ZeroLoss also closes it when it"
    " sees the fix).",
    "actions_choose": "Choose:",
    "actions_choose_tip": "Use this one (the same as 'zeroloss actions choose')",
    "problems_badge": "{n} problem{s}",
    "problems_title": "Problems",
    "problems_flood": "Many problems at once ({n} in 10 min) — only errors are shown",
    "problems_stale": "Open for more than 24 h",
    "problems_repeat": "{what} (×{n})",
    "problems_resolved": "Resolved {t}",
    "problems_cols": "Time|Device|Code|What|Why|What to do",
    "problems_detail": "{time}  {device}  {code}\nWhat: {what}\nWhy: {why}\nWhat to do: {fix}",
    "finished_title": "Test finished — {verdict} ({why})",
    "reason_device_lost": "a device stopped answering: {detail}",
    "no_red": "No red boxes so far.",
    "no_red_end": "No red boxes.",  # the test ran to its end (GAP-115)
    "red_boxes": "{n} red boxes.",
    "ran_of": "Ran {ran} of {planned}. {red}",
    "report_saved": "Report saved to:",
    "see_now": "Do you want to see the report now?",
    "show_report": "Show report",
    "open_folder": "Open folder",
    "later": "Later",
    "copy_path": "Copy path",
    "reports_title": "Reports",
    "reports_cols": "Started|Run|Test|Time|Result|Why",
    "reports_where": "Reports are saved in {path}",
    "reports_none": "No reports yet. Run a test first.",
    "reports_loading": "Reading the reports…",
    "reports_failed": "Could not read the reports: {what}",
    "reports_show": "Show",
    "reports_folder": "Folder",
    "reports_no_report_tip": "This run has no report",
    "reports_pick_tip": "Choose a report first",
    # 20 GAP-82: Manage storage (S14) and diagnostics
    "storage_title": "Manage storage",
    "storage_low_tip": "Less than 20% or 50 GB free on: {pcs}",
    "storage_cols": "Run|Size|Raw data|Where",
    "storage_running": "running now",
    "storage_refresh": "Refresh",
    "storage_loading": "Reading how much each run takes…",
    "storage_empty": "No runs stored",
    "storage_free": "Free: {free}",
    "storage_not_from": "Not read from {device}: {why}",
    "storage_raw_title": "Delete the raw data of {run}?",
    "storage_raw_body": "The 1-second originals, captures and clips of {run} are deleted on "
    "every PC ({size}). The report stays.",
    "storage_run_title": "Delete {run}?",
    "storage_run_body": "Everything of {run} is deleted on every PC ({size}), the report too. "
    "This cannot be undone.",
    "storage_deleted": "Deleted {size} of {run}.",
    "storage_kept": "Some files stayed: {what}",
    "export_title": "Diagnostics",
    "export_done": "Saved: {path} ({size})",
    "export_not_from": "Not from {device}: {why}",
    "export_failed": "Could not make the file: {what}",
}

QUALITY_LABELS: dict[str, str] = {  # name · Video resolution · bitrate (1.7-1)
    "UHD": "UHD · 3840×2160 · 35 Mbps",
    "HD": "HD · 1920×1080 · 10 Mbps",
}
# 2026-10-24 user ("메뉴가 두개 있을 필요없는거잖아 … 4대중에 골라서 테스트하는거니까"):
# one IPTV test; its rows pick the receivers among the PC and the tablets
TEST_TYPES: dict[str, str] = {
    "iptv": "IPTV test", "all": "Throughput · All",
    "wired": "Throughput · Wired", "wifi": "Throughput · Wi-Fi",
}  # fmt: skip
# a report's test_type (02 10): runs before and after the merge read the same way
TEST_LABELS: dict[str, str] = {**TEST_TYPES, "pc": "IPTV test", "tablet": "IPTV test"}
DURATIONS_S: tuple[int, ...] = (600, 3600, 10 * 3600, 24 * 3600, 100 * 3600)

# Words never used alone on screens (11-ui 1.7-1): checked by a test.
BANNED = ("Resolution", "Channel", "DUT", "Client", "STB", "Server", "Streamer", "Zoom")
