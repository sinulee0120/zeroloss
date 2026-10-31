"""Short keys of the IPTV 1-second record (docs/02-data.md 7). The only definition."""

from __future__ import annotations

ELAPSED = "e"
CHANNEL = "ch"
RECEIVED = "rx"
LOST = "lost"
DUPLICATES = "dup"
REORDERED = "reo"
JITTER_MS = "jit"
BITRATE_BPS = "br"
HOST_DROPS = "hd"
CC_ERRORS = "cc"
P1_ERRORS = "p1"
DELAY_LOW_MS = "dlo"
DELAY_HIGH_MS = "dhi"
# delay variation of the last PDV_WINDOW_S seconds (max dhi - min dlo): a slow clock slew
# (NTP, <= 0.5 ms/s) stays out of it, a real delay burst does not (2026-10-22)
DELAY_WINDOW_MS = "dpw"
PAT_SEEN = "pat"
# Detail keys, written only when not zero (02 7)
LOSS_EVENTS = "lev"
MAX_BURST = "mb"
LATE_DISCARDED = "late"
SYNC_LOSS = "sync"
SYNC_BYTE_ERR = "sbe"
PAT_ERR = "pate"
PMT_ERR = "pmte"
PID_ERR = "pide"
RSSI_DBM = "rssi"
# Wi-Fi of the receiving card, wireless receivers only (02 7, GAP-104 3)
WIFI_MBPS = "wlm"
WIFI_CH = "wch"
WIFI_BW = "wbw"
WIFI_STD = "wst"  # Wi-Fi standard the receiver is joined with ("11ax"), tablets (node-read)
WIFI_BAND = "wbd"
WIFI_CH_CHANGED = "wcc"  # 1: channel, width or band differ from the second before
WIFI_BSSID_CHANGED = "wbc"  # 1: another access point
WIFI_SCANS = "wsc"  # Wi-Fi scans this receiver made this second (tablet app, TAB-018)
SCAN_LOST = "sl"  # of lost, lost in a second this receiver scanned (iptv R-25, node-made)
# Receiving card and host counters, per-second deltas, absent when not read (02 7, GAP-148)
NIC_PACKETS = "np"
NIC_MULTICAST = "nm"  # wired cards only: Wi-Fi drivers do not count it
NIC_DROPPED = "nd"
NIC_ERRORS = "ne"
UDP_IN_ERRORS = "ue"  # host-wide: shown, never judged
UDP_RCVBUF_ERRORS = "ub"
UDP_NO_PORTS = "unp"
M2U_MULTICAST = "fm"  # the stream's frames to the multicast MAC (Wi-Fi card)
M2U_UNICAST = "fu"  # ... to this PC's own MAC: the router converted them (M2U)
STATION_RETRIES = "wr"  # AP-side counters at the 10 s full read (05 1.9), over wsd s
STATION_FAILED = "wf"
STATION_BEACON_LOSS = "wbl"
STATION_RX_PACKETS = "wrp"
STATION_SPAN_S = "wsd"
SKEW_PPM = "sk"  # clock skew estimate (05 2.2)
TEI_ERR = "tei"  # TR 101 290 2.1, counted only
CRC_ERR = "crc"  # TR 101 290 2.2 (PAT/PMT), counted only
PLAYBACK = "pb"
PLAY_DROPPED = "pdr"  # written when measured, even 0 (02 7)
PLAY_DECODER_DROPPED = "pdd"  # of pdr, dropped by the decoder; written when measured (GAP-180)
PLAY_AUDIO_LOST = "pau"
PLAY_FREEZE_STARTED = "pfs"
PLAY_RESTARTED = "prs"
PLAY_DECODE_ERRORS = "pde"  # frames the player's decoder could not decode (GAP-152)
# Channel change (F19, 02 7): on the first 1-second line after a zap ended, 0 = not written
ZAP_COUNT = "zc"
ZAP_OK = "zok"
ZAP_JOIN_FAILED = "zjf"
ZAP_LEAVE_NOT_PROCESSED = "zlp"
ZAP_TOTAL_MS = "zt"
ZAP_JOIN_MS = "zj"
ZAP_PLAY_MS = "zp"
