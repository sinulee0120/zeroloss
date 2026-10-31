# 02. 데이터 형식과 통신 API

> Controller, Agent(Service·Desktop·Deployer), 태블릿 앱이 **같은 형식**을 쓰게 하는 기준 문서.
> Python 쪽은 `core/`(공통)와 각 모듈의 정의 파일(예: `modules/iptv/record_keys.py`) 한 곳에 형식을 정의하고 모든 구성 요소가 가져다 쓴다. Kotlin 앱은 이 문서의 필드 이름을 그대로 쓴다.

## 1. 공통 규칙
| 항목 | 규칙 |
|------|------|
| 인코딩 | JSON, UTF-8 |
| 필드 이름 | `snake_case`, 영어 |
| 시각 | `ts_utc`: UTC ISO 8601 밀리초(`2026-09-23T05:32:10.123Z`) / `mono_ns`: 장비 단조 시계 나노초 |
| 경과 시간 | `elapsed_s`: 시험 시작부터 초(Controller 기준 보정값) |
| 숫자 | 카운터는 64비트 정수. 비율은 0~1 실수. 시간 길이는 ms 실수 |
| ID | `lab_id`: 시험실 ID(기본 `lab-1`, 나중에 여러 시험실 대비) / `run_id`: `R` + `YYYYMMDD-HHMMSS` (예 `R20260923-143201`) / `device_id`: 설치 때 만든 UUID(IP가 바뀌어도 같음) / `command_id`: UUID |
| 버전 | `build`: `YYYYMMDD.HHMM` / `protocol_version`: 정수(현재 1) |
| 색 | `green` `yellow` `red` `purple` `grey` |
| 판정 | `PASS` `FAIL` `INCOMPLETE` |

## 2. 포트와 연결
| 포트 | 누가 열고 | 누가 연결 | 용도 |
|------|-----------|-----------|------|
| TCP 10991 | Deployer | Controller | 설치·업데이트 (15장) |
| TCP 10992 | Agent Service | Controller | 운영 API |
| UDP 10992 | Agent Service | Controller(방송) | 발견(설정 단계만) |
| TCP 10993 | Controller | 브라우저 | 결과 조회 웹 |
| TCP 10994 (127.0.0.1만) | Agent Service / **Controller 노드 서비스(지휘 역할)** | Agent Desktop / **Controller Desktop(화면 앱)·CLI** | 같은 PC 안 연결. 명령은 REST, 실시간 갱신은 같은 포트의 이벤트 스트림(SSE). 외부에서 접근 불가 |
| TCP 10995 (태블릿 안 localhost) | 태블릿 앱 | 수신 Agent (`adb forward`) | 태블릿 제어·자료 |

## 2-1. Controller 창구 상태 (10994 `GET /v1/controller/status`)
> 화면 앱(Controller Desktop)과 CLI `zeroloss status --json`이 읽는 값. 화면은 이것만 보고 그린다(11-ui 1.1). 2026-09-24 추가(20장 GAP-33).
```json
{ "build": "20260924.1200", "generation": 3, "reports_dir": "C:\\ZeroLoss\\Reports",
  "storage_low": [ { "device": "rx-pc", "free_mb": 30720, "total_mb": 476940 } ],
  "broadcast": { "on": false, "since_utc": null, "channels": [], "sender": null },
  "run": {
    "state": "RUNNING", "run_id": "R20260924-120000", "generation": 3,
    "elapsed_s": 5101.0, "planned_s": 360000, "cell_s": 300,
    "plan": { "test_type": "tablet", "quality": "UHD", "duration_s": 360000, "settings": { "S-48": { "1": "movie.mp4" } } },
    "devices": {
      "tablet-1": {
        "suspect": false, "age_s": 0.4,
        "module_data": {…},
        "links": […], "test_ip": "192.168.45.190",
        "link_checks": [ { "nic": "eth0", "label": "LAN", "level": "ok", "link_mbps": 2500, "duplex": "full",
                           "needed_mbps": 38.5, "usual_mbps": 2500, "since_elapsed_s": 0.0, "since_utc": "…" } ],
        "wifi_changes": [ { "elapsed_s": 754.2, "ts_utc": "…", "nic": "wlan0",
                            "from": { "channel": 36, "width_mhz": 80, "band": "5GHz", "bssid": "…" },
                            "to": { "channel": 149, "width_mhz": 40, "band": "5GHz", "bssid": "…" } } ],
        "row": { "color": "green", "why": "", "link_label": "LAN",
                 "items": [ { "label": "CH2", "value": null, "unit": "", "key": false },
                            { "label": "Jitter", "value": 2.1, "unit": "ms", "key": true } ],
                 "channel": 2 } } },
    "cells": { "tablet-1": [ { "index": 17, "partial": false, "color": "green", "reasons": [], "start_elapsed_s": 5100, "duration_s": 300, "values": [ { "key": "lost", "label": "Lost", "unit": "", "value": 0, "bad": true }, { "key": "jitter_max", "label": "Jitter max", "unit": "ms", "value": 2.4, "bad": false } ] } ] },
    "channels": { "on_air": [1, 2, 3], "auto": { "every_s": 600.0, "stagger_s": 10.0 }, "next_in_s": {} },
    "preparing": null,
    "checklist": { "kind": "start", "run_id": "R20260924-120000", "updated_utc": "…", "ok": true,
      "steps": [ { "n": 1, "name": "Devices connected", "state": "ok", "detail": "sender-pc, rx-pc",
                   "device": null, "code": null, "what": null, "why": null, "fix": null } ] },
    "precheck": null,
    "fault": null,
    "reset": null,
    "result": {…} } }
```
- `run.channels`(2026-10-01, F19, 20장 GAP-91): 시험 중(RUNNING)에만, 아니면 `null`. `{"on_air": [송출 중 채널], "auto": {"every_s", "stagger_s"} | null(S-21 Manual), "next_in_s": {"<장비>": 다음 자동 전환까지 초}}` — 화면 채널 상자는 `on_air`만 고르게 하고 `auto`가 있으면 잠근다(K-11). 지금 채널은 장비 `module_data`의 수신기 `ch`.
- `broadcast`(2026-09-27, F18): `{"on", "since_utc", "channels", "sender"}` — S-13 Always-on으로 켠 송출이 시험 밖에서도 도는지. 꺼져 있으면 `{"on": false, …null}`.
- `storage_low`(2026-09-27, 09 SYS-010): 여유가 20% 또는 50 GB 미만인 PC `[{"device", "free_mb", "total_mb"}]`(Controller PC는 `device` = `controller`), 없으면 `[]`. 화면 [Storage] 버튼 글자·Problems가 쓴다. 위 예시에는 줄였다.
- `reports_dir`: 보고서 폴더(F15). 회차 보고서는 `<reports_dir>/<run_id>/report.html`(P2는 폴더 이름 = run_id, F15 이름 규칙은 P3). 결과 팝업·[Last report]가 쓴다.
- `run.devices`: 시험 중인 장비만(IDLE이면 `{}`). 장비 목록 전체는 `GET /v1/controller/devices`(11-0).
- `age_s`: 마지막으로 heartbeat를 받은 뒤 지난 초. 3 s 이상이면 화면은 Stale(11-ui 1.5, 4장). `suspect`는 F11 의심(S-42).
- `module_data`: 그 장비 heartbeat의 모듈 묶음(5장) 그대로. `links`: heartbeat `links`(5장) 그대로.
- `link_checks`: 유선 시험 NIC마다 Controller의 링크 판단(05 1.8). `level`: `ok` / `slow`(기준 속도 `usual_mbps`보다 낮지만 충분, NET-014) / `short`(`link_mbps` < `needed_mbps` × 1.1, NET-013) / `down`(끊김, NET-015) / `unknown`(속도를 못 읽음). `needed_mbps`는 모듈 `link_need()`, `usual_mbps` = 지난 시험들의 최고 속도(11-0 `best_link_mbps`)와 이번 시작 속도 중 큰 값. `since_*` = 지금 `level`이 시작된 때. 무선 NIC는 없음(Wi-Fi는 `wifi_changes`).
- `wifi_changes`: 이번 시험에서 Wi-Fi 채널·대역폭·대역·BSSID가 바뀐 기록(Controller가 heartbeat를 비교, 오래된 것부터). 줄 표시·툴팁 이력(11-ui 3장).
- `run.plan`(2026-10-03, 20장 GAP-105 ④; `settings` 2026-10-09 GAP-126): 시험 중(IDLE 아님)에 **돌고 있는 계획**의 `test_type`·`quality`·`duration_s`·`settings`(바꾼 것만 — 시험 중에 연 창이 Source video 등을 이것으로 보임), IDLE이면 `null`. 화면 Test 칸은 시험 중엔 저장된 선택이 아니라 이것을 보여 준다(`--plan`으로 시작한 시험도).
- `test_ip`(2026-10-03, 20장 GAP-105 ①): 그 장비가 시험에 쓰는 카드 주소(계획 `nics`, Controller가 채운 값·시험 Wi-Fi로 붙은 주소 포함), 모르면 `null`. 화면 장비 줄은 Wi-Fi 값(RSSI·채널)을 **이 카드 것만** 보여 준다(PC의 인터넷 Wi-Fi는 시험이 아님).
- `row`: 모듈이 만든 장비 줄(12 2장 `live_row`). `color`는 초록/노랑/빨강/보라/회색(05 5.3), `why`는 그 색의 이유(툴팁), `link_label`은 링크 이름(`WAN`/`LAN`), `items`는 줄에 차례로 보일 값(`key`=가장 굵게, `value`가 `null`이면 `label`만). `channel`(2026-10-01, F19) = 그 장비의 수신기가 하나이고 측정 중일 때 지금 받는 채널, 아니면 `null`(화면 채널 상자가 씀).
- `planned_s`·`cell_s`: 계획 시간·칸 길이(IDLE이면 `null`).
- `checklist`: 마지막 Start 또는 `zeroloss precheck`의 단계별 결과(03 F17 구현 규칙). `kind` = `start` / `precheck`, `ok` = 실패 단계 없음. 단계 `state` = `waiting`·`running`·`ok`·`warn`·`failed`·`skipped`, `failed`·`warn`이면 `device`·`code`(09)·`what`·`why`·`fix`, 아니면 `null`. 아직 한 번도 없으면 `null`.
- `precheck`: 마지막 **대기 중 가벼운 점검**(1~4단계, 같은 모양, `kind` = `idle`). 화면은 이것으로 Start 활성/회색 + 이유(11-ui 1.10).
- `fault`: 장애로 멈춘 시험(FAULT)의 원인 — `{"device": "rx-pc" | null, "reason": "rx-pc: no answer for 10 s", "at_utc", "last_ok_utc", "run_id"}`, 그 밖 `null`. S6 장애 안내가 쓴다(03 F11). Reset All이 끝나면 `null`.
- `reset`: 마지막 Reset All 결과(2-8), 아직 없으면 `null`.
- `preparing`: 시작 절차에서 영상을 만드는 동안(iptv 1-1-2)만 `{"percent": 42.0, "left_s": 2280, "text": "CH1 UHD 42% · about 38 min left"}`, 그 밖 `null`. 화면은 이 값이 있을 때 버튼을 Cancel로(11-ui 1.10).
- `cells`: 장비별 5분 칸(색은 Controller가 계산, 05 5.3). **칸마다 `values`(모듈 `cell_values`: 손실·지터 최대·평균·비트레이트·패킷, 2026-10-09 GAP-146 — 5분마다 최대 지터·손실 표)와 `start_elapsed_s`·`duration_s`.** `result`: 시험이 끝났을 때 판정 요약, 그 전에는 `null`.

## 2-2. 문제 목록 (10994 `GET /v1/controller/problems`, F21)
> Problems 창·배지·CLI가 읽는 값. 2026-09-25 추가.
```json
{ "counts": { "error": 1, "warn": 2 },
  "flood": { "new_in_10_min": 12 },
  "problems": [
    { "key": "ZL-NET-013|sender", "code": "ZL-NET-013", "level": "error", "device": "sender",
      "what": "sender ↔ router WAN link changed: 2.5 Gbps → 100 Mbps (12:34:56)",
      "why": "The link renegotiated. This test needs about 116 Mbps, so packets will be lost.",
      "fix": "Check or replace the cable and port.",
      "active": true, "count": 1, "first_utc": "…", "last_utc": "…", "cleared_utc": null,
      "stale": false, "run_id": "R20260925-101500" } ] }
```
- `problems`: 진행 중(`active`)이 위, 해결된 것은 아래(최근 해결부터). 같은 `code` + `device`는 한 줄(`count` = 몇 번 났는지, 10 s 안에 다시 켜지면 새 알림으로 세지 않음). 해결된 줄은 최근 500개까지만(100시간 메모리, 11-ui 1.4).
- `what`: 사건 글(값이 들어간 문장), 없으면 문구 목록. `why`·`fix`: 사건이 실은 값(`data.why`·`data.fix`)이 먼저, 없으면 문구 목록(`controller/problems/catalog.json`, 09장과 자동 비교).
- `flood`: 10분 안에 새 문제가 10개를 넘으면 `{ "new_in_10_min": n }`, 아니면 `null`(F21). `stale`: 24시간 넘게 진행 중.
- `level`은 `error`·`warn`만(정보 사건은 문제 아님). 해결은 02 8 `data.clears`, 시험 끝(IDLE)·Reset All.

## 2-3. 사람 할 일 목록 (10994 `GET /v1/controller/actions`, F22)
> Action Required 창·배지·CLI `zeroloss actions --json`이 읽는 값. 2026-09-25 추가.
```json
{ "waiting": 1,
  "actions": [
    { "key": "human.cable.check|sender", "action_id": "human.cable.check", "device": "sender",
      "what": "Check the cable and port", "how": "Look at the cable between sender and the router WAN port; reseat or replace it.",
      "why": "sender ↔ router WAN link changed: 2.5 Gbps → 100 Mbps (12:34:56)", "opened_by": "ZL-NET-013",
      "state": "waiting", "done_by": null, "auto_done": true, "repeat": "every",
      "requested_utc": "…", "done_utc": null, "run_id": "R20260925-101500",
      "choose": null, "choices": null, "ask": null } ] }
```
- `state`: `waiting`(⏳) / `done`(✔). `done_by`: `auto`(여는 문제가 풀려 감지됨, 02 8 `clears`) / `human`([Done]) / `null`. `auto_done`: 완료를 자동으로 알 수 있는 일인지(아니면 [Done]이 보임). `repeat`: `once`(처음 한 번) / `every`(매번 — 자동화 대상 기록, 00-vision).
- 할 일 종류·문구·어느 문제 코드로 열리는지는 `controller/actions/catalog.json` 한 곳(`action_id`는 14장 형식). 같은 `action_id` + `device`는 한 줄. 끝난 줄은 최근 200개까지.
- `choose`·`choices`(20장 W-1): 사람이 후보 중 하나를 고르는 할 일이면 고르기 종류(`wan_nic` = 공유기 WAN 카드, `test_route` = Controller 시험망 카드의 기본 경로 고치기 NET-019)와 후보 `[{"id", "label"}]`(연 사건의 `data`에서, 예: 노드 DSC-006 `{"id": "enp1s0", "label": "enp1s0 · 1000 Mbps · aa:bb:…"}`), 아니면 `null`. 고르기: `POST /v1/controller/actions/choose` params `key`, `choice`(후보의 `id`만) → `{"key", "choice", "wan": <02 2-9 답>}` — `wan_nic`은 `devices wan <장비> --on --nic <카드>`와 같고, 받아들여지면(`ready`·`waiting_nic`) 그 할 일은 `done_by: human`. 후보가 아니거나 모르는 종류는 DSC-012.
- `ask`(2026-10-22): 고른 답을 보낼 노드 명령, 아니면 `null`. `throughput_fix`(NET-024, 카드 설정을 되돌리지 못함 → `human.card.put_back`)는 `{"path": "/v1/modules/throughput/client/resume", "target", "seq"}` — `actions/choose`가 그 PC에 `{"target", "seq", "answer": {"choice"}}`로 보낸다(Fixed, check again / Continue anyway / Stop the test).
- [Done]: `POST /v1/controller/actions/done` params `key`(02 3 명령 형식). 끝난 일은 경험 DB에 `actor` human/auto, `outcome` resolved로 남는다(14장 1).

## 2-4. 미리 준비 (10994 `POST /v1/controller/prepare`, `GET /v1/controller/prepare`)
> 시험 전에 모듈이 필요한 준비물을 만들어 둔다(IPTV: 자동 생성 영상, iptv 1-1-2). CLI `zeroloss prepare`(13장). 2026-09-25 추가(20장 GAP-50).
- `POST /v1/controller/devices/address` params `name`, `host`, `port`(선택) (2026-10-15 GAP-155): 장비 줄 IP 칸에서 고르거나 넣은 주소. 그 주소에서 **그 장비(device_id)가 답할 때만** 저장, 다른 PC가 답하면 DSC-016(누가 답했는지), 답이 없으면 DSC-014. `GET …/setup`의 `sides[<장비>]`에는 `host`(쓰는 주소), `addresses`(고를 주소: 쓰는 것·찾은 것·카드·예전), 응답 없을 때 `why`, 다른 PC가 답하면 `other`가 붙는다.
- `POST` params: `module`("iptv"), `sender`(장비 이름), `quality`, `bitrate_mbps`(선택, 없으면 화질 기본값), `channels`(선택, 기본 `[1, 2, 3]`), `sources`(선택, 채널별 S-48 파일 이름 `{"1": "movie.mp4"}`, 2026-10-04 GAP-116), `only`(선택, 참이면 이 요청에 없는 만들기는 멈춤 — 화면이 영화를 고를 때), `length`(선택, `"whole"` = 영화 전체, S-55, GAP-118). 방송 중이면 RUN-012(송출하는 PC에서 인코딩하지 않음). Controller는 모듈 `run_entry`의 `prepare_ahead()`로 노드 명령을 만들어 보낸다(Controller는 IPTV를 모름). 시험 중(IDLE이 아님)이면 RUN-012.
- `GET` params: `module`, `sender` → 그 장비 heartbeat의 모듈 준비 상태(IPTV는 5장 `prepare`) 그대로.

## 2-5. 배포 (10994 `POST /v1/controller/deploy`, `GET /v1/controller/modules`, `POST /v1/controller/modules/remove`)
> CLI `zeroloss deploy`·`zeroloss modules`(13장)가 쓰는 창구. Controller가 그 장비의 Deployer(10991, 15장, 장비 목록의 `deployer_port`)에 전달한다. 2026-09-25 추가(20장 GAP-52).
- `POST /v1/controller/deploy` params `node`, `package`(Controller PC의 파일 경로) → 파일을 보내고(15장 `PUT incoming`) 설치 → 15장 설치 결과 그대로.
- `GET /v1/controller/modules` params `node` → `{"node", "modules": [15장 목록], "deployer": 15장 health}`.
- `POST /v1/controller/modules/remove` params `node`, `id`, `purge`, `dry_run` → 15장 제거 결과 그대로.
- `POST /v1/controller/install/linux` params `host`, `user`, `password`, `name`, `ssh_port`(22), `node_port`(10992), `deployer_port`(10991), `accept_new_host_key`, `auto_login`(기본 `false`, 06 11.3 4-2), `package`·`modules`(선택, 없으면 `packages/`의 가장 새 것; 도구 묶음은 `packages/tools/*.deb` 전부, 06 11.3 4-3) → `{"host", "name", "host_key": {"sha256", "status": "new"|"same"|"replaced"}, "tools_installed": ["vlc"], "tools_missing": ["tsp"], "auto_login": "on for <계정>" | "not set (…)"(요청했을 때만), "steps": [{"name", "ok", "detail"}]}`(단계 이름: `host_key` → `login` → `checks` → `sudo` → `upload` → `install` → `rollback_copy` → (`auto_login`) → `tools` → `answer` → `register` → `module`). 비밀번호는 이 창구(127.0.0.1)로 한 번 건너가 SSH·sudo에만 쓰이고 결과·기록·파일에 남지 않는다(06 11.3).
- 시험 중이면 거부(INS-022). 설치·제거가 끝날 때까지 새 시험 시작은 기다린다(명령 대기열 하나, 11-ui 1.12). Deployer가 답하지 않으면 INS-014(CLI 종료 코드 5).

## 2-6. 사전 점검 (10994 `POST /v1/controller/precheck`, F17)
- params `plan`(11-1 형식), `full`(`false` = 대기 중 가벼운 점검 1~4 / `true` = 1~5, 시험 송출 포함) → 2-1 `checklist` 한 개. 결과는 `run.precheck`(가벼운 점검)·`run.checklist`(전체)에도 남는다. 시험 중이면 RUN-012. CLI `zeroloss precheck --plan <file>`은 `full=true`, 실패 단계가 있으면 종료 코드 3(13장).

## 2-7. 시험 Wi-Fi (10994 `/v1/controller/wifi`, S-51)
- `GET` → `{"ssid": "ZL-TEST" | null, "security": "WPA2" | null, "has_password": true}` — **비밀번호는 절대 돌려주지 않는다**.
- `POST` params `ssid`, `security`(`Open`/`WPA2`/`WPA3`), `password`(Open이면 없음) → 16 S-51 허용 값 검사(RUN-014) 뒤 저장, 응답은 `GET`과 같은 모양. `POST /v1/controller/wifi/clear` → 지움.
- 저장(11장 `test-wifi.json`): Windows는 DPAPI(서비스 계정 범위)로 암호화한 비밀번호, Linux Controller(개발용)는 주인만 읽는 파일(0600, 2026-09-25 사용자 결정). 로그·경험 DB·보고서·이벤트에 비밀번호를 적지 않는다(시험이 검사).
- CLI `zeroloss wifi show` / `wifi set --ssid <SSID> --security WPA2 --password-stdin` / `wifi clear`(13장).

## 2-8. Reset All·노드 조작 (10994, F11 장애 안내 S6, 2026-09-25)
- `POST /v1/controller/reset` → **등록된 모든 장비**에 `POST /v1/reset`(4장)을 새 세대 번호로 보낸다. `{"ok", "updated_utc", "devices": [{"device", "answered", "clean", "checks", "why", "fix"}]}`. 모두 깨끗하면 IDLE(문제 목록 해결), 하나라도 답이 없거나 깨끗하지 않으면 **FAULT 그대로**(`fix` = 그 PC에서 할 일, 답 없는 장비는 DSC-014로 Action Required 전원 껐다 켜기). IDLE(사전 점검 실패)·FAULT에서만, 시험 중이면 RUN-012.
- `POST /v1/controller/nodes` params `action`: `restart` + `node`(F11 [Restart Agent], 그 PC의 Deployer 10991 `/v1/deployer/node`) / `stop`(F11 [Shut Down All] = **모든 PC의 노드 서비스 정지, Deployer는 켜둠**, 2026-09-25 사용자 결정) / `start`(다시 켬). `{"action", "ok", "devices": [{"device", "done", "error"}]}` — 한 장비가 실패해도 나머지는 한다. IDLE·FAULT에서만.
- CLI `zeroloss reset --yes`(종료 코드 0 깨끗 / 3 덜 됨), `zeroloss nodes restart --node <이름>` / `nodes stop --yes` / `nodes start`(13장).

## 2-9. 송신 PC 통신사 역할 (10994 `POST /v1/controller/devices/wan`, 07 3.2 B)
- params `name`(장비), `on`(없으면 읽기만), `auto`(`true` = 다시 자동, 07 3.2 B), `nic`(선택) → 그 노드의 `GET`/`POST /v1/wan`(4장) 답 그대로. 켜고 끄기는 IDLE·FAULT에서만(RUN-012). CLI `zeroloss devices wan`(13장).

## 2-5-1. PC 업데이트 (10994 `POST /v1/controller/update_pcs`, 2026-10-17)

- 화면의 **Update PCs**(점검이 INS-015 "다른 빌드"라고 할 때 보임): 이 프로그램의 빌드와 다른 PC마다 이 프로그램 옆 `packages` 폴더(키트에 들어 있음) 또는 Controller 데이터의 `packages`에서 `zeroloss-node-agent-<빌드>.zlp`와 그 빌드의 `.zlm`들을 2-5처럼 설치. 시험 중이면 RUN-012, 파일이 없으면 INS-012. → `{"build", "devices": [{"device", "updated": true \| false, "from", "to" \| "why"}]}`

## 2-10-1. Wi-Fi 스캔 (10994 `GET /v1/controller/wifi_scan?node=<PC>&nic=<카드>`)

- 그 PC 노드의 `GET /v1/wifi/scan`을 그대로 전달(S-65 화면 줄의 밴드 목록). 시험 중이면 RUN-012.
- `node=controller`면 이 PC 자신의 카드(Windows wlanapi). 실패하면 화면이 그 카드 줄 밑에 이유를 적는다(옛 빌드 PC = `no such path` → "Update PCs").

## 2-10-2. 밴드 Wi-Fi 암호 (10994 `POST /v1/controller/wifi_password`, 2026-10-17)

- params `ssid`, `password` — 사람이 Devices의 밴드 SSID 옆 칸에 친 것. `""`면 지움. Controller 데이터 `band-wifi.json`에 Windows DPAPI로 암호화해 둠; 계획·보고서·로그·화면에 다시 나오지 않음. `GET setup`의 `wifi_passwords`는 암호가 있는 SSID 이름 목록뿐.
- 시험 때 그 망에 붙는 단계에만 넣어 보냄. **암호가 없으면 암호 없이(open) 붙는다 — 빈칸은 "암호 없음"이며 Start를 막지 않음**(2026-10-17 사용자 "빈칸이라고 또 테스트 못하게 하지 말고 … 빈칸이면 암호 없음을 의미").

## 2-10. 원본 영상 목록 (10994 `GET /v1/controller/sources?node=<송신>&module=iptv`, S-48)
- 모듈 계약 `sources_step(device)`의 노드 경로를 대신 불러 답을 그대로 돌려준다(iptv 1-1). CLI `zeroloss sources --node <송신>`.

## 2-11. 화면 선택 (10994 `GET`/`POST /v1/controller/setup`, F14)
- `GET` → `{"setup", "sides": {"<장비>": {"reachable", "side": "wan"|"lan"|"unknown", "isp_role", "links"}}, "roles": {"sender", "receivers", "why", "lan_sender"}, "plan": 11-1 계획 | null}`. 역할은 06 10.4(WAN 쪽 PC = 송신, 애매하면 `why`). LAN 송신이면 계획에 `"sender_side": "lan"`(비교 시험).
- `sides.<장비>.links`(2026-10-09 GAP-121): 그 PC의 카드(5장 `links` 모양) — 노드 `/v1/info`의 `links`(0328 다음 노드부터), 없으면 Controller가 마지막 시험에서 본 것, 아무것도 모르면 `null`. 화면의 Card 목록이 이것으로 그려짐(시험 사이에는 상태에 장비 카드가 없음). 하트비트로 읽지 않는다(노드의 최근 사건을 가져가 버림).
- `setup.nics`(2026-10-04 GAP-115): 사람이 장비 줄에서 고른 시험 카드 `{"<장비>": "<그 PC 카드의 IPv4>"}` — Auto는 적지 않음. 계획을 만들 때 시험에 쓰는 장비 것만 계획 `nics`로(무선 수신 PC는 `"wifi"`가 우선). CLI `setup set nic=<장비>:<주소>`·`nic=<장비>:auto`.
- `POST` params `changes`(`setup`의 일부) → 저장(`controller.json`), 시험 중이면 RUN-012. `settings`는 기본값과 다른 것만 남긴다. **메인 화면 설정 넷은 `settings`가 아니라 제 칸에 간다(2026-10-02 첫 실기, 20장 GAP-99):** `S-01` → `test_type`, `S-03` → `quality`, `S-05` → `duration_s`(초), `S-06` → `send_quality_reports`(`On`/`Off` → 참/거짓) — CLI `setup set S-03=HD`가 "Saved"라 하고 시험은 옛 값으로 돌던 결함.
- Start(`POST /v1/controller/run`)·사전 점검(2-6)은 `plan`을 안 주면 이 선택으로 만든 계획을 쓴다(애매하면 RUN-014 + `why`).
- `GET /v1/controller/settings` → `{"settings": {"S-15": {"value": 16, "label": "TTL", "where": "adv", "kind": "int", "min": 8, "max": 64, "help", "lock": true, "available": true}, …}}` — 화면이 Advanced 칸을 그리는 자료(16장 표와 자동 비교, `defaults.json`의 `ui`). 모듈의 `defaults.json`에서 온 설정에는 `"module": "<id>"`가 붙고, 화면은 고른 시험 종류의 모듈 설정과 Core 설정만 그린다(2026-10-10).
- CLI `zeroloss setup show` / `setup set KEY=VALUE…`(`receiver=<이름>:<채널>:<Wired|Wireless>`, `S-nn=값`), `zeroloss run`·`precheck`는 `--plan`이 없으면 이 선택(13장).

## 2-12. Agent Desktop 창구 (노드 10994, 127.0.0.1만, 03 F20 구현 규칙, 2026-09-26)
```json
{ "node": { "build": "20260926.0900", "protocol_version": 1, "device_id": "…", "boot_id": "…", "role": "agent", "state": "IDLE", "generation": 3 },
  "controller": { "connected": true, "last_seen_s": 0.4 },
  "my_ips": [ { "nic": "enp0s31f6", "ip": "10.0.0.2", "wireless": false } ],
  "wan": { "mode": "auto", "state": "off", "nic": null, "why": null },
  "module_data": { "iptv": {…}, "throughput": {…} },
  "problems": [ { "ts_utc": "…", "code": "ZL-RCV-003", "level": "warn", "device": "", "text": "…", "data": {…} } ] }
```
```json
{ "jobs": [ { "id": "R20260926-120000/rx-pc/ch1/player", "module": "iptv", "kind": "player",
              "config": { "url": "rtp://@239.1.1.1:5004", "nic": "enp0s31f6", "buffer_ms": 1000, "window": "Medium (1280×720)", "headless": false, "lowered": false, "flowing": true } } ],
  "node_boot_id": "…" }
```
```json
{ "desktop_id": "…", "reports": [ { "id": "R20260926-120000/rx-pc/ch1/player", "state": "RUNNING", "seq": 12,
  "events": [ { "code": "ZL-RCV-020", "level": "warn", "text": "…" } ],
  "second": { "playback": "ok", "dropped": 0, "audio_gaps": 0, "stall_started": false, "restarted": false, "decode_errors": 0, "decoder_dropped": 0 },
  "first_frame": true, "lowered": false } ] }
```
- 위 셋: `GET /v1/desktop/status`의 답, `GET /v1/desktop/jobs`의 답, `POST /v1/desktop/report`의 `params`(Desktop이 보내는 것). 실제 출력과 자동 비교(`tests/test_data_formats.py`).
- `GET /v1/desktop/status` → `{"node": <health>, "controller": {"connected", "last_seen_s"}, "my_ips": [{"nic", "ip", "wireless"}], "wan": {"mode", "state", "nic", "why"}(4장 `GET /v1/wan`의 일부, 20장 W-4), "module_data": {<모듈 id>: 2장 heartbeat와 같은 값}, "problems": [최근 warn·error 사건 10개]}`. 상태 창은 이것만 그린다.
- `GET /v1/desktop/jobs` params `desktop_id` → `{"jobs": [{"id", "module", "kind": "player", "config": {…}}], "node_boot_id"}` — 노드가 지금 원하는 화면 작업 전부. 부르면 그 Desktop이 연결된 것으로 본다(3초).
- `POST /v1/desktop/report` params `desktop_id`, `reports`: `[{"id", "state": "IDLE"|"STARTING"|"RUNNING"|"STOPPING"|"FAULT", "seq", "events": [{"code", "level", "text"}], …작업 종류의 값}]` → `{"accepted": [id…]}`. **명령이 아니라 보고**: `command_id`·세대를 쓰지 않고(명령 기억을 밀어내지 않게), 같은 보고를 두 번 보내도 결과가 같다(같은 `seq`는 한 번만). 목록에 없는 id는 버린다(G-13). 사건은 노드 사건 흐름으로 다시 낸다(12 4-1, `data.from` = `desktop`).
  - 재생기(`kind: player`) 값: `"second": {"playback", "dropped", "audio_gaps", "stall_started", "restarted", "decode_errors", "decoder_dropped"}`(`decode_errors`: 그 초 디코더 오류 수, 2026-10-10 GAP-152; `decoder_dropped`: `dropped` 중 디코더 단계에서 버린 수, 재지 못하면 `null`, 2026-10-23 GAP-180)(iptv 3.1 값, 재생기 자체 보고), `"first_frame"`, `"lowered"`(RCV-016). 작업 `config`: `{"url", "nic", "buffer_ms", "window", "headless", "lowered"}`.
  - 재생기 작업 `config.flowing`(2026-10-24 GAP-181): 노드가 이 채널에 패킷이 들어오는지(`true`) 안 들어오는지(`false`, 그 초 수신 0) 알려 준다. 처음은 `null`(모름 — 예전처럼). 바뀔 때만 고친다. 재생기는 `false`인 동안 프레임이 안 늘어도 재시작하지 않고, 그동안의 재시작은 한도(RCV-003, 3회/시간)에 세지 않으며, 한도로 멈춘 재생기는 `false`→`true`(패킷이 돌아옴)에서 다시 켠다(09 RCV-020).
- 모듈 상태 줄(`desktop_entry.status_rows`, 12 2): `[{"title", "color": "green"|"yellow"|"red"|"grey", "big": [[이름, 값]…], "lines": [...], "role": "sender"|"receiver", "detail": {"key", "line"} | null}]` — `detail`은 [Details] 목록의 그 초 한 줄(`key`로 한 번만).
- 한 PC에 Desktop 하나: 다른 `desktop_id`가 3초 안에 부르면 DSC-012로 거절.

## 2-13. 장비 목록·정지·보고서 (10994, 2026-09-26 재검토에서 빠진 것 보충, 20장 GAP-76)
- `GET /v1/controller/devices` → 11-0 장비 목록. `POST /v1/controller/devices/add` params `name`, `host`, `port`(10992), `deployer_port`(10991) → 노드 `/v1/health`로 확인 후 저장 `{"name", "device_id", "role"}`(CLI `devices add`). `POST /v1/controller/devices/remove` params `name` → `{"removed"}`.
- `POST /v1/controller/quit`(127.0.0.1, 2026-10-04 GAP-116) → 시험·시작·상시 방송이 없을 때만 서비스를 끝냄 `{"quitting": true}`, 아니면 RUN-012(창을 닫아도 시험은 계속, 12 1-0). 휴대 빌드 창이 닫힐 때 부른다(06 4.1-1).
- `POST /v1/controller/stop` → 정상 정지(F10: 수신 → 송신 → 마지막 요약 → 보고서), 답 = 10장 `summary.json`과 같은 내용. 시작 중 영상 만들기 단계면 취소 `{"cancelled": true}`(USR-006). CLI `stop`.
- `GET /v1/controller/report?run=<run_id>` → 그 회차의 `summary.json`(10장), 없으면 RPT-008. CLI `report`.
- `GET /v1/controller/reports` → 지난 보고서 목록(F15 Reports 목록 S8, 2026-09-27 사용자 결정 20장 GAP-83) `{"reports_dir", "reports": [{"run", "started", "test_type", "quality", "planned_s", "actual_s", "verdict", "verdict_reason", "with_warnings", "report", "folder", "why"}]}` — 최신순. `started` = 회차 번호의 시작 시각(`R%Y%m%d-%H%M%S`, Controller PC 시각 `2026-09-24 12:00`), 번호가 그 형식이 아니면 폴더 시각. 보고서 폴더 안에서 `summary.json` 또는 `plan.json`이 있는 폴더만(진단 파일 폴더 등은 뺌). `summary.json`이 없거나 읽지 못하면 `verdict: null` + `why`(`Running`: 지금 도는 회차 / `ZL-RPT-008 …`), 판정 값은 `null`. CLI `reports`.
- `POST /v1/controller/diagnostics` params `run`(선택, 없으면 마지막 회차) → 진단 파일 하나(2026-09-26 사용자 결정, GAP-78): 등록된 장비마다 `GET /v1/logs`로 받은 묶음 `<장비>.zip` + Controller 자신의 노드 묶음 `controller.zip` + `controller/`(장비 목록·화면 선택·문제·할 일·상태, 그 회차 `summary.json`; **`wifi.json`은 빼고**, 경험 DB는 크기 때문에 뺌) + `manifest.json`(`nodes`: 장비 → `ok` 또는 못 받은 이유). 닿지 않는 장비가 있어도 만든다. 저장: `Reports/<회차>/diagnostics-<UTC 시각>.zip`(회차가 없으면 `Reports/diagnostics/`) → `{"path", "bytes", "nodes": {…}}`. 장비마다 받기 제한 120초. CLI `diagnostics`.
- `POST /v1/controller/verify` params `suites`(목록, 없으면 `["accuracy", "judgement"]`), `seconds`(`loopback` 시험 길이, 기본 60, 10~3600) → 설치된 제품 자체 검사(2026-09-27 사용자 결정 20장 GAP-81, pytest 없이) `{"suites", "passed", "failed", "checks": [{"suite", "name", "ok", "detail", "ms"}], "run_id"}`. `accuracy` = 10장 1.1 합성 패킷 표(손실·버스트·정렬 창 안/밖·중복·RFC 3550 지터·SSRC 변경·되돌아감·중간 가입·안정화 뒤 첫 손실·TS CC/PAT)를 실제 계측 블록·모듈 분석기에 넣고 기대값과 비교, `judgement` = 05 5.3·5.4 색·판정 표의 모든 줄(Core `final_verdict` + 모듈 칸 규칙), 둘 다 장비·네트워크 없이 몇 초. `loopback` = 지금 화면 선택(2-11)의 계획을 `seconds` 길이로 실제 시험 한 번(보통 회차처럼 보고서가 남음, `run_id`) — 수신 장비마다 한 줄, 회차 판정 PASS이고 그 장비 칸에 빨강·보라·회색이 없으면 통과(손실이 있으면 빨강, Core는 모듈 필드를 읽지 않음). `loopback`은 IDLE에서만(RUN-012), 모르는 이름은 DSC-012. 검사 하나가 예외로 죽어도 나머지는 돈다(그 줄 `ok: false`). 모듈 검사는 `run_entry.self_checks()`(12 2장). CLI `verify`(실패가 하나라도 있으면 종료 코드 1).
- `POST /v1/controller/channel` params `device`, `ch` → 수신기 채널 전환(F19 구현 규칙, 20장 GAP-87): `{"device", "from", "to", "join_method": "A"|"B", "zap": <02 7-2 한 줄>}` — RUNNING에서만(RUN-012), 자동 전환(S-21) 켬이면 RUN-012 "Automatic channel change is on", 계획에 없는 채널·장비는 RUN-014, 같은 채널이면 `zap: null`. CLI `channel`.
- `POST /v1/controller/broadcast` params `on`(`false`만 — 켜는 것은 S-13 Always-on으로 Start가 함) → `{"on": false, "stopped": [채널]}`. 시험 중이면 RUN-012. 상태 `broadcast`(2-1). CLI `broadcast stop`.
- `GET /v1/controller/baselines` → `{"baselines": {"<장비>|<재생기>": 02 10 `receiver_baseline` 한 칸}}`(05 4.6.3, 2026-10-02 GAP-97). Controller 데이터 폴더 `baselines.json`(쓰는 쪽 하나, 원자적 쓰기). CLI `baselines`.
- `POST /v1/controller/records` params `run` → 그 회차의 1초 기록을 모든 장비에서 `Reports/<회차>/raw/<장비>/`로 가져옴(2026-09-27 사용자 결정 20장 GAP-80) `{"run", "files": [{"device", "hour", "bytes", "state": "got"|"had"}], "missing": {"<장비>": "<코드> <이유>"}, "reanalyzed"}`(`reanalyzed` = 1초 기록으로 원인 분석을 다시 해 보고서를 고쳤는지, iptv 2.4) — 이미 같은 크기로 있으면 다시 받지 않음(`had`), 받는 중인 파일은 `.part`, 받기 제한 파일마다 600초. **보고서를 만든 뒤 Controller가 스스로 한 번**(보고서를 늦추지 않음, 별도 작업), 못 가져온 장비는 RPT-005 경고 사건. 같은 회수가 돌고 있으면 끝날 때까지 기다렸다 한다(한 번에 하나). 노드 원본은 지우지 않는다(F15). 진행 중인 회차는 RUN-012. CLI `records --run`.
- `GET /v1/controller/storage` → 결과 사용량(F15, 20장 GAP-79): `{"reports_dir", "free_mb", "runs": [{"run", "bytes", "raw_bytes", "in_use", "where": {"Reports": {"bytes", "raw_bytes"}, "controller": {…}, "<장비>": {…}}}], "nodes": {"<장비>": "ok" | "<코드> <이유>"}, "free_mb_of": {"<장비>": n}}` — 회차는 전체 크기가 큰 것부터. `Reports` = 보고서 폴더(`Reports/<회차>`), `controller` = Controller PC 노드 자신의 데이터 폴더, 나머지 = 등록된 장비마다 `GET /v1/storage`. 닿지 않는 장비는 `nodes`에 이유만. 장비마다 제한 10초. CLI `storage list`.
- `POST /v1/controller/storage/delete` params `run`, `raw_only` → 보고서 폴더(`raw_only`면 `raw/`·`screens/`·`clips/`만, 아니면 회차 폴더 전체 — 진단 파일 포함)와 모든 장비의 `POST /v1/storage/delete` → `{"run", "raw_only", "deleted_bytes", "nodes": {"Reports"|"controller"|"<장비>": "ok" | "<코드> <이유>"}, "errors"}`. **진행 중인 회차**(시험 상태가 IDLE·FAULT가 아니고 그 회차)는 RUN-012, 다른 회차는 시험 중에도 지울 수 있다. 닿지 않는 장비는 사유만 적고 나머지는 지운다(그 장비 것은 목록에 계속 남음). 경험 DB 기록 `action_id` = `cli.storage.delete`. CLI `storage delete <run> [--raw-only] --yes`.

## 3. API 공통 형식 (10991, 10992, 10994, 10995)
- HTTP/1.1, keep-alive 유지(NAT 연결 추적 유지). 경로는 `/v1/...`.
- 요청(POST 본문):
```json
{ "run_id": "R20260923-143201", "command_id": "uuid", "generation": 3, "params": { } }
```
  - `generation`: Reset·재시작마다 1씩 오르는 번호. 예전 번호의 명령은 거부(F11).
  - Controller가 보내는 모든 요청 머리글에 `X-ZL-Controller-Boot: <controller boot_id>`를 싣는다. Agent는 시험 중 **처음 보는 Controller boot_id**를 받으면(= Controller가 재시작됨) 옛 시험을 즉시 안전 정지하고 자료를 마감한 뒤 그 요청을 처리한다(F11, CTL-008).
  - 같은 `command_id`가 다시 오면 **이전 결과를 그대로 돌려준다**(중복 실행 금지).
- 응답:
```json
{ "ok": true, "data": { } }
{ "ok": false, "error": { "code": "ZL-SND-001", "what": "...", "why": "...", "fix": "...", "detail": "..." } }
```
  - 오류 코드와 문구는 `09-exceptions.md`와 같다. `detail`은 [Show details]에만 표시.

## 4. 운영 API (Agent Service, TCP 10992)
> 레이어 구분(`12-architecture.md`): `/v1/...`는 **Core(L1) 공통**, `/v1/modules/<id>/...`는 **모듈(L2) 전용**. 아래 IPTV 경로는 IPTV 모듈 것이고, 2차 throughput 모듈은 `/v1/modules/throughput/...`로 붙는다.
> **코드에 아직 없는 줄(2026-09-26 재검토, 20장 GAP-76 ⑦, 단계는 W-6 결정):** `POST /v1/pair`(페어링·방화벽 범위 단계로 미룸, DSC-003·005와 같이), `POST /v1/snapshot`(F23과 함께, 2026-09-27 사용자 결정 20장 GAP-80). `GET /v1/logs`·`POST /v1/stop`·`/v1/storage`·`/v1/records`는 2026-09-26~27 만듦. `GET /v1/tablets`·`POST /v1/tablets/<동작>`은 2026-10-23 만듦.

| 메서드·경로 | 용도 | 주요 응답 |
|-------------|------|-----------|
| `GET /v1/health` | 살아 있는지, 버전 | `build`, `protocol_version`, `device_id`, `boot_id`, `role`, `state`, `generation` (명령 세대 — 새로 설치한·데이터를 옮긴 Controller가 F17 1단계에서 IDLE 장비의 세대보다 위로 올라감, 2026-10-03 GAP-111) |
| `GET /v1/heartbeat?seq=N[&hold_s=S][&awake_s=A]` | 1초 heartbeat (F11). `hold_s`(2026-10-09 GAP-130, S-56): 이 시험이 Controller 없이 계속돼도 되는 초 — 노드는 그때까지 Controller가 조용해도 안전 정지하지 않음(0~201시간, 시험이 끝나면 지워짐). `awake_s`(2026-10-22, SYS-005): 시험 중 Controller가 매초 보냄(60) — 그 PC는 자기 차례를 기다리는 동안도 그 초만큼 절전·화면 꺼짐을 막음(throughput은 PC가 차례로 재서 기다리는 PC가 IDLE이었음, 사용자 "iperf일땐 sleep들어가는거같다") | 5장 heartbeat 형식 |
| `GET /v1/info` | 자동 발견 정보 (F14 표) | 호스트, OS, NIC 목록, 도구 버전, 디스크·메모리, 태블릿 목록, `seen_from_ip`(NAT 판별용). `reached_ip`·`reached_prefix`: Controller가 닿은 이 노드의 주소와 그 NIC 대역 길이(07 3.3 같은 대역 점검, 모르면 `null`). `tools`: 켜진 모듈들이 필요하다고 적은 도구마다 `{"path", "version"}` 또는 `null`(없음), 예 `{"tsp": {"path": "/usr/bin/tsp", "version": "3.45-4798"}, "vlc": null}`(2026-09-25, F17 2단계) |
| `POST /v1/pair` | Controller 등록, 방화벽 범위 설정, (옵션) 키 | - |
| (없음) `POST /v1/precheck` | 만들지 않는다(2026-09-25): Controller가 `health`·`info`·`heartbeat`와 모듈 시험 송출(`…/probe`)로 모든 단계를 본다(03 F17) | - |
| `POST /v1/modules/iptv/sender/start` / `stop` | 송출 시작·정지. params: `ch`, `quality`(S-03), `file`(선택: 없거나 파일이 없으면 자동 생성 영상, iptv 1-1-2 — 준비돼 있지 않으면 SND-001), `dest_ip`, `port`, `muxrate_bps`, `ttl`(16), `local_address`. 시작은 안정 확인(±1% 3초)까지 기다리므로 **제한 시간 15 s** | 채널 상태, `ssrc` 프라임 방식(iptv 4.5)이면 `dest_ip`·`port` 대신 `prime: {"flow": 0\|1, "p", "q"}` — Q에서 프라임을 기다리며 **바로 돌아오고**(STARTING), 프라임이 오면 그 출발 주소로 P에서 보냄. 정지(`stop`)는 그 채널의 흐름 모두. 상태(02 5 `sender.channels[]`)에 `prime: {"flow", "dest", "last_prime_s", "primes"}` |
| `POST /v1/modules/iptv/sender/prepare` | 자동 생성 영상 만들기(iptv 1-1-2). params: `quality`, `muxrate_bps`, `channels`: `[{"ch": 1, "file": null, "source": null}]`, `only`(선택, 참 = 최신 선택: 이 요청에 없는 만들기는 멈춤, 나머지는 하던 곳에서 계속, GAP-116), `length`(선택, `"whole"` = S-48 파일 전체, S-55; `sender/start`에도 같은 값)(`file`이 있고 파일이 있으면 만들지 않음, `source` = S-48 미디어 폴더 파일 이름이면 그것으로 만듦 — 없으면 자동 생성 + SND-001, iptv 1-1). 답의 채널 `source`: `file` / `media` / `generated`. 바로 돌아오고 진행률은 heartbeat `prepare` | 채널별 `state`: `ready`(있음) / `queued` |
| `POST /v1/modules/iptv/sender/prepare/cancel` | 만들기 멈춤(만들던 파일 삭제, 다 만든 파일은 보관). 시험의 Stop·Cancel은 이것을 보내지 않는다(GAP-116) — 만들기 요청의 `only`(필요 없는 것만 멈춤)와 노드 안전 정지가 쓴다 | 멈춘 채널 |
| `GET /v1/modules/iptv/sender/sources` | S-48 미디어 폴더 목록(iptv 1-1): `{"folder", "files": [{"name", "bytes", "codec", "width", "height", "fps", "duration_s", "audio", "fits": {"UHD": {"ok", "upscaled", "why"}, "HD": {…}}, "error": null, "made": [{"quality", "ch", "mbps", "whole"}]}], "generated_made": [{…}]}` — ffprobe가 못 읽으면 `error`에 이유. `made`: 그 원본으로 이미 만들어 둔(이 빌드의 만드는 법과 같은) 파일, `generated_made`: 만든 영상 쪽(2026-10-17 사용자 "바로 사용가능한 파일은 별도로 알려주던가 직접 선택하게": Source video 목록이 `✔ ready, starts at once` / `made first`로 표시) | - |
| `POST /v1/modules/iptv/receiver/perfcheck` / `…/perfcheck/cancel` | RCV-015 수신 PC 성능(05 4.2-1). `cancel`: 만들던 시험 영상·해독 시험을 멈춤(정지 순서·되돌린 시작, 송신 `prepare/cancel`과 같음) → params `quality`, `muxrate_bps`, `measure`(`false` = 저장된 결과만) → `{"state": "ready"\|"making_sample"\|"unavailable", "hw_decode": true\|false\|null, "decode_speed": 2.4 \| null, "measured_utc", "warn": null \| {"code": "ZL-RCV-015", "what", "why", "fix"}}` | - |
| `POST /v1/modules/iptv/receiver/desktop` | F17 4단계(03 F20, RCV-010): 재생 켬 수신 PC에 Agent Desktop이 붙어 있는지. params `wait_s`(0~10, 대기 점검 0·Start 5) → 설치본(`--player-host desktop`)이 아니면 `{"needed": false, "connected": null}`, 붙어 있으면 `{"needed": true, "connected": true, "clears": ["ZL-RCV-010"]}`, 없으면 RCV-010 오류 | - |
| `POST /v1/modules/iptv/receiver/probe` / `…/probe/result` | F17 5단계 멀티캐스트 시험: params `ch`, `group`, `port`, `interface_ip`, `seconds`(4), `m2u`(`true`면 노드가 받는 NIC가 **Wi-Fi일 때만** 목적지 MAC을 봄, 유선은 스누핑만이라 `null`), `prime`(프라임 시험: `{"sender_ip", "p", "q"}` — `port` = R, 듣는 동안 0.5초마다 프라임) → 듣기 시작하고 `probe_id`만 바로 돌려줌 / `probe_id` → 끝날 때까지 기다려 `{"packets", "first_ms", "error", "m2u", "arrived_on", "arrived_cards"}`(`m2u` = `null` 또는 `{"state", "why", "frames"}`; `error` = 듣기 소켓 오류 글 또는 `null`). **카드를 모를 때(2026-10-08, 20장 GAP-115 ②):** `interface_ip`가 없거나 `0.0.0.0`이면 경로가 고른 카드 하나가 아니라 **켜진 실제 카드(IPv4 있는 것, loopback·가상 제외) 모두에서** 그룹에 가입한다. `arrived_on` = 패킷이 들어온 카드 `{"nic", "ip"}`(IP_PKTINFO의 카드 번호·그 카드 주소; 여러 카드로 오면 가장 많이 받은 카드), 아무것도 안 왔거나 OS가 알려 주지 않으면(Linux 밖) `null`. `arrived_cards` = 받은 카드마다 `{"nic", "ip", "packets"}`(많은 순, 없으면 `[]`) — 한 PC의 두 카드가 같은 공유기 LAN에 있으면(유선 + 시험 Wi-Fi) 둘 다 받으므로, Controller는 시험 종류에 맞는 카드(Wi-Fi 수신 시험이면 무선 카드)를 이 목록에서 고른다 | - |
| `GET /v1/wifi` | 이 PC의 Wi-Fi NIC들: `{"nics": [{"nic": "wlp4s0", "mac": "…", "ssid": "ZL-TEST" \| null, "ip": "192.168.1.23" \| null, "operstate": "up" \| "down" \| null, "nm_state": "connected" \| "unavailable" \| … \| null}], "supported": true, "rfkill": [{"name": "phy0", "type": "wlan", "soft": false, "hard": true}], "radio": "enabled" \| "disabled" \| null}`(03 F17 "무선 수신 PC"; 2026-10-15 GAP-157: 무엇이 카드를 막는지 — `hard`는 키·BIOS, `soft`·`radio`는 소프트웨어, 읽기만) | - |
| `GET /v1/wifi/scan?nic=<카드>` | 그 Wi-Fi 카드가 지금 보는 네트워크(다시 찾음, 10~25초): `{"nic", "networks": [{"ssid", "bssid", "freq_mhz", "band": "2.4GHz" \| "5GHz" \| "6GHz", "signal": 0~100, "security": "Open" \| "WPA2" …}]}`(이름 없는 숨은 네트워크는 빼고 밴드·신호순). 읽기만. 없는 카드면 DSC-012 (2026-10-17 S-65 "Cards to test": 사용자가 밴드마다 고름) | - |
| `POST /v1/wifi/join` | 시험 Wi-Fi 연결: params `ssid`, `security`, `password`, `nic`(선택), `timeout_s`(30) → IP를 받을 때까지 기다려 `{"nic", "ssid", "ip", "joined": true}`. 시험 중이면 RUN-012. 실패 RCV-023. 비밀번호는 기록·응답·오류에 넣지 않음 | - |
| `POST /v1/arp_probe` | SND-013 주소 충돌 점검(F17 3단계): params `ip`(이 노드가 가진 주소) → `{"checked", "conflict_macs": ["02:…"], "nic", "why"}` — `checked: false`면 `why`(패킷 소켓 권한 없음 등), 충돌이 없다는 뜻이 아님 | - |
| `GET /v1/wan` / `POST /v1/wan` | 07 3.2 B 통신사 역할: `GET` → `{"on", "mode": "auto"\|"on"\|"off", "state": "off"\|"ready"\|"waiting_nic"\|"conflict"\|"error", "nic", "server_ip", "lease", "why"}` — `on`: 지금 역할을 하고 있음(손으로 켬, 또는 자동으로 켜짐), `mode`: 정한 방식(Controller PC는 자동이 없어 `off`). `POST` params `on`(bool), `nic`(선택) → 켜고/끄고 저장(`agent.json`), 또는 `auto: true`(다른 값 없이) → 다시 자동, 끄면 더한 주소를 빼고 확인. 시험 중이면 RUN-012 | - |
| `POST /v1/modules/iptv/sender/probe` | 시험 흐름 송출(널 패킷 RTP, 1 Mbps) params `ch`, `dest_ip`, `port`, `seconds`(2), `ttl`, `local_address` → 끝나면 돌아옴 | `{"sent_s"}` 프라임 시험이면 `dest_ip`·`port` 대신 `prime: {"flow": 0, "p", "q"}`, `prime_wait_s`(3): 프라임을 기다렸다 그 출발 주소로 P에서 보냄(없으면 NET-003) |
| `POST /v1/modules/iptv/sender/broadcast` | 상시 방송 켬·끔 (F18) | - |
| `POST /v1/modules/iptv/receiver/start` / `stop` | 수신·계측·재생 시작·정지. params: `ch`, `group`, `port`, `interface_ip`, `device`, `cell_s`(300), `warmup_max_s`(15), `playback`(재생 켬이면 `{"player": "VLC", "buffer_ms": 1000, "window": "Medium (1280×720)"}` = S-26·S-25·S-50, 끔이면 `null`). 시작은 가입까지만 기다리고 안정화는 heartbeat로 확인 | 정지 때 Start-up(가입→첫 패킷·측정 시작 ms) 프라임 방식이면 `prime: {"sender_ip", "flows": [{"f", "p", "q", "r"}]}` — 분석기는 R₀에 유니캐스트로 받고 자기 소켓에서 프라임, 재생기는 `rtp://@:R₁` + raw 소켓 프라임(권한 없으면 RCV-024) |
| `POST /v1/modules/iptv/tablet/start` / `stop` | 이 PC USB의 태블릿 하나가 채널을 받음(2026-10-23). params: `device`(`tablet-1`~`tablet-3`), `serial`, `ch`, `group`, `port`, `play`(S-49, 기본 켬), `buffer_ms`(S-25 또는 그 태블릿 값), `cell_s`. 정지는 `device`만. 태블릿 1~3대, 채널은 같아도 달라도 됨 — 태블릿마다 따로 시작 | 앱이 1초 기록(PC와 같은 키)을 만들고 이 PC가 매초 가져와 PC 수신기와 같은 기록 파일·5분 요약으로. 측정 시작 = 패킷이 있고 PAT를 본 첫 초. USB가 빠지면 TAB-001, 다시 꽂히면 앱이 갖고 있던 기록(최근 1시간)을 이어서 가져옴 |
| `POST /v1/modules/iptv/receiver/channel` | 채널 전환(F19 구현 규칙): params `device`(그 장비의 받는 중인 수신기가 하나가 아니면 `from_ch`로 현재 채널도), `ch`, `group`, `port`, `prime`(받는 쪽이 primed이면 새 채널 것, `receiver/start`와 같은 모양), `join_method`(A/B), `success_timeout_s`(S-47), `mode`(manual/auto) → 분석 소켓·재생기를 함께 옮기고 첫 패킷(최대 5초)까지 기다려 답함, 정상 재생·Leave 확인은 뒤이어 `<회차>_zaps.jsonl`에 | `{"ch", "changed", "zap"}` — `zap` = 02 7-2 한 줄(아직 모르는 값은 `null`, 이미 끝났으면 `end: "done"`). 같은 채널이면 `{"ch", "changed": false}`. 받는 중 수신기가 그 장비에 딱 하나가 아니면 RUN-012, `join_method`·`mode` 값이 틀리면 DSC-012 |
| `GET /v1/summaries?after=<seq>` | 아직 안 가져간 5분 요약들. 노드가 받은 순서대로 `seq`(1부터)를 붙이고 노드에 계속 보관 → 가져가기가 실패해도 다음에 다시 받음. **노드 디스크(`<데이터>/summaries.jsonl`)에도 바로 쓰고 재시작 때 다시 읽어 같은 `seq`로 준다**(F12, 2026-09-25) | `{"summaries":[6장 + "seq"], "last_seq": n}` |
| `GET /v1/records/index?run=<회차>` | 1초 기록 파일 목록(시험 후 회수, 2026-09-27 사용자 결정 20장 GAP-80) | `{"run", "files": [{"device", "hour", "name", "bytes"}]}` — 회차 폴더 `raw/<device>/<run>_h<시간>[_r<n>].jsonl`(7장)과 채널 전환 기록 `<run>_zaps.jsonl`(7-2, `hour: null`, 2026-10-01), 문제 초의 증거 `<run>_ev_e<초 6자리>_ch<채널>.json|pcap|jpg`(03 F23, 2026-10-10 GAP-154, `hour: null`). 회차가 없으면 빈 목록 |
| `GET /v1/records?run=<회차>&device=<장비>&name=<파일 이름>` | 1초 기록 파일 한 시간치를 파일로(7장 형식 그대로, 원본은 노드에 남음). `name`은 목록의 이름(쓰기 실패 뒤 새 파일 `_r1`도 따로 있음, RCV-009) | JSONL 파일. 없으면 RPT-005, 이 노드가 아직 쓰는 회차면 RUN-012(시험이 끝난 뒤 가져감), 형식이 틀리면 DSC-012 |
| `POST /v1/snapshot` | 재생 화면 저해상도 캡처 | PNG(base64) |
| `POST /v1/stop` | 이 노드만 정상 종료 절차(F10: 모듈 역순 안전 정지·파일 마감, 2026-09-26 W-6). 같은 경로를 로컬 창구 10994에서는 세대 없이 받음(`zeroloss agent stop`, USR-007 — 두 번 불러도 결과 같음) | `{"stopped", "state", "run", "files": [{"name", "bytes"}]}` — 그 회차 폴더의 기록 파일(멈춘 뒤 남은 것) |
| `GET /v1/disk` | 이 노드 데이터 폴더 디스크(가벼움, 1분마다 Controller가 봄 — 09 SYS-010, 2026-09-27 20장 GAP-82) | `{"free_mb", "total_mb"}`(읽지 못하면 `null`) |
| `GET /v1/storage` | 이 노드의 회차 폴더 사용량(F15 Manage storage, 2026-09-26 사용자 결정 20장 GAP-79) | `{"free_mb", "runs": [{"run", "bytes", "raw_bytes", "in_use"}]}` — 큰 것부터. `raw_bytes` = 원자료(회차 폴더 안 `raw/`·`screens/`·`clips/` 아래 파일, 1초 원본·캡처·클립), `in_use` = 이 노드가 지금 쓰는 회차(노드가 시험 중이고 마지막 명령의 `run_id`) |
| `POST /v1/storage/delete` | 회차 하나 지우기(F15): params `run`, `raw_only`(`true` = 원자료만, 나머지 로그·요약은 남김) | `{"run", "raw_only", "deleted_bytes", "left_bytes", "errors": ["<파일>: <이유>"]}` — 없는 회차는 `deleted_bytes` 0(두 번 불러도 결과 같음), `in_use`면 RUN-012, 회차 번호 형식이 틀리면 DSC-012. 못 지운 파일은 `errors`에 적고 나머지는 지움 |
| `POST /v1/reset` | 원복 + 깨끗한 상태 점검 (F11 Reset All): 모든 모듈 안전 정지 → 크래시가 남긴 프로그램 정지(SYS-007, 노드 자신의 도우미 `iw event`는 제외) → 점검 | `{"clean", "boot_id", "checks": [{"name": "state"\|"processes"\|"modules"\|"network settings", "ok", "detail"}]}`. 모듈은 선택 계약 `clean_check()`(아직 돌고 있는 것 목록)로 답한다(2026-09-25). **시험이 바꾼 설정은 되돌린다(2026-10-22 사용자 "reset all누르면 모든 장비들 노트북 설정 원래대로"):** 선택 계약 `restore_settings()` → `[{"what", "ok", "detail"}]`(throughput: 유선 카드 속도·이중, Wi-Fi 망) — `network settings`의 ok·detail. Controller는 등록 장비와 **이 PC 자신의 노드**도 리셋한다 |
| `GET /v1/tablets` | USB 태블릿 목록·상태(2026-10-23 만듦, `core/blocks/tablets.py`) | `{"tablets": [{"serial", "state"(adb: `device`·`unauthorized`·`no permissions`·`offline`…), "model", "usb", "why"(사람이 할 일, 없으면 ""), "battery": {"level", "charging", "powered_by"}, "wifi_ip", "app": {"installed", "version"}, "api"(앱 `/v1/status` 답, 안 되면 `null`)}], "adb": null 또는 adb가 없는 이유}` — 태블릿 1~3대, 채널은 같아도 달라도 됨. 태블릿마다 따로 `adb forward`(PC 쪽 포트는 adb가 고름) |
| `POST /v1/tablets/<동작>` + `params.serial` | 태블릿 명령. 만듦(2026-10-23): `install_app`(이 PC의 `data/tablet/zeroloss-receiver.apk`, `adb install -r -g`), `start_app`, `stop_app`(설치·정지는 시험 사이에만, RUN-012). 나중: start, stop, channel, snapshot(앱 API 중계). 실패는 TAB-017(adb의 말 + 할 일) | 태블릿 응답 그대로 |
| `GET /v1/logs?run=<id>` | 진단 묶음(2026-09-26 사용자 결정, 20장 W-6·GAP-78): `logs/` 아래 모든 로그(사건·프로그램·`iw`), `procs/` 기록, 그 회차 폴더(`runs/<id>/`, 없으면 가장 새 회차)의 `.log`, 노드 설정 JSON(최상위·`modules/*/`), `state.json`(health·wan·최근 사건). **빼는 것:** 1초 기록·요약(`.jsonl`), 영상(`.ts` 등 캐시), 비밀번호·키 값(이름에 password·secret·token·psk·hmac·private가 든 JSON 값과 `key`는 `***`). **최대 50 MB:** 넘으면 오래된 파일부터 빼고 `manifest.json`의 `left_out`에 적음. 응답 = `application/zip` 파일 그대로(3장 봉투 아님, 오류일 때만 JSON 봉투), 보낸 뒤 노드의 임시 파일은 지움 | zip(`manifest.json`: `device_id`·`build`·`made_utc`·`files`[이름·바이트]·`left_out`·`redacted`) |

## 4-1. 자동 발견 (UDP, 노드 포트와 같은 번호, 06 10.2)
- 요청(Controller → 방송/주소): `ZLDISCOVER 1 <nonce>` ASCII. 노드는 IDLE일 때만 같은 출발지로 답한다.
- 답(한 줄 JSON):
```json
{ "zl": "discover", "v": 1, "nonce": "…", "device_id": "…", "host": "rx-pc", "role": "agent",
  "build": "20260926.0900", "protocol_version": 1, "port": 10992, "state": "IDLE" }
```
- 10994 `POST /v1/controller/discover` params `scan`(선택, 예 `192.168.1.0/24`, /22보다 넓으면 RUN-014), `try_sender`(기본 `true`) → `{"found": [{"device_id", "address", "port", "host", "role", "build", "how", "tcp_ok", "known", "name"}], "sender_try": "found"|"not found"|"skipped", "codes": ["ZL-DSC-001"…], "moved": [{"name", "from", "to"}]}` — `moved`: 등록 주소가 답하지 않고 다른 주소에서만 찾아 저장한 장비(06 10.2, GAP-73 ④). `tcp_ok`: 방송으로 찾은 노드에 TCP `/v1/health`가 닿는지(`false` = 방화벽이 제어 포트를 막음, DSC-008), 주소로 찾은 것은 `true`.

## 5. heartbeat 응답 (1초마다)
```json
{
  "seq": 12345, "device_id": "…", "boot_id": "…", "generation": 3,
  "state": "RUNNING",
  "mono_ns": 123456789, "ts_utc": "…",
  "role": "receiver",
  "resources": { "cpu_pct": 12.5, "mem_free_pct": 61.0, "disk_free_mb": 50210, "proc_mem_mb": 180 },
  "power": { "sleep_guard": "ok", "why": null, "held": true, "time_sync": { "state": "held", "why": null, "ntp": false } },
  "links": [ { "nic": "eth0", "ip": "10.0.0.2", "up": true, "link_mbps": 2500, "duplex": "full", "carrier_changes": 1 },
             { "nic": "wlan0", "ip": "192.168.1.10", "up": true, "carrier_changes": 3,
               "wifi": { "bssid": "…", "band": "5GHz", "channel": 36, "width_mhz": 80, "center_mhz": 5210, "freq_mhz": 5180, "rssi_dbm": -58, "rx_mbps": 130, "tx_mbps": 390, "dtim": 1 } } ],
  "module_data": { "iptv": { "sender": { "channels": [ { "ch": 1, "group": "239.1.1.1:5004", "state": "RUNNING", "tx_bitrate_bps": 35000000, "restarts": 0, "tx_packets": null, "interval_dev_ms": null, "source": "generated" } ] },
  "prepare": { "busy": true, "channels": [ { "ch": 1, "quality": "UHD", "mbps": 35, "state": "making", "percent": 42.0, "left_s": 2280, "error": null } ] },
  "receivers": [
    { "id": "pc", "ch": 2, "slot": 2, "state": "RUNNING", "measuring": true,
      "lost_total": 0,
      "last_1s": { "type": "s", "e": 5101, "ch": 2, "rx": 3333, "lost": 0, "jit": 2.1, "br": 35000000, "hd": 0 } }
  ],
  "tablets": [ { "serial": "…", "link": "usb_ok", "state": "RUNNING", "last_1s": { } } ] },
  "throughput": { "server": { "state": "RUNNING", "pid": 4321 },
    "clients": { "192.168.45.20": { "state": "running", "case": 0, "cases": 1, "doing": "Link as it is: send", "error": "", "done": false,
                "results": [ { "label": "Link as it is", "link": 1000, "send": 941.6, "receive": 940.1, "not_run": null } ],
                "need": null, "target": "192.168.45.20" } } } },
  "events": [ { "code": "ZL-TAB-007", "ts_utc": "…", "text": "…" } ],
  "summaries_ready": 4
}
```
- `module_data.throughput`(2026-10-15, docs/modules/throughput.md 0): `server` = WAN 쪽 PC의 iperf3 서버(`state`·`pid`, 없으면 `null`), `clients` = LAN 쪽 PC의 카드마다(열쇠 = 그 카드 주소 `target`, GAP-171) 도는 조합 목록(`state` starting/queued/running/done/failed/stopped, `case` 지금 조합 번호, `cases` 개수, `doing`, `error`, `done`, `results` 끝난 조합마다 `label`·`link`(협상 Mbps)·`send`·`receive`(Mbps)·`not_run`, `need` = WAN 쪽 PC에 부탁할 단계 또는 `null`, `target`), 카드가 없으면 `{}`.
- `prepare`(송신 PC, iptv 1-1-2): 자동 생성 영상 작업. `busy` = 만드는 중. 채널 `state`: `queued` / `making` / `checking` / `ready` / `failed`(SND-020, `error`에 이유) / `cancelled`. `percent`·`left_s`는 `making` 때만(그 밖 `null`). 작업마다 `source`: `generated` 또는 S-48 원본 파일 이름(2026-09-26). 한 번도 요청이 없으면 `{"busy": false, "channels": []}`. 송신 채널 `source`: `generated` / `file` / `media`(S-48 파일로 만든 것) / `generated_fallback`(SND-001).
- 송신 채널: `state`(구성 요소 상태 흐름), `restarts`(송출 재시작 수, SND-005), `tx_packets`·`interval_dev_ms`는 P3(`null`). 수신기: `id`(장비 이름), `ch`(**지금 받는 채널** — 전환(F19)하면 바뀜, 화면·timeline 열쇠 `장비|채널`), `slot`(Start 때의 채널 — 노드가 수신기를 구별하는 열쇠, `receiver/stop`이 씀), `state`, `measuring`(안정화 끝나 측정 중), `lost_total`(측정 시작부터 잃은 패킷 수, 안정화 구간 제외. 측정 전 `null`. 장비 줄 `Loss`, 11-ui 3장). 태블릿: `tablets`에 `{"id", "serial", "ch", "state", "measuring", "away"(USB로 닿지 않음, TAB-001), "lost_total", "last_1s"}`(2026-10-23, 그 전 빌드는 `"tablets": []`). `last_1s`·`events` 안의 모양은 7장·8장이 정의. **색(status)은 heartbeat에 없다** — 색·판정은 Controller가 계산(절대 원칙, 2026-09-24 20장 GAP-02·GAP-03).
- `power`(09 SYS-005, 2026-09-26): `sleep_guard` = `ok`(시험 동안 절전을 막을 수 있음) / `off`(막을 필요 없음: 이 PC는 절전 대상이 모두 mask — 06 4.3 설치 때 끔, 2026-10-03 GAP-107, ⚠ 없음, 시험 중 막지 않음) / `unavailable`(`why`에 이유, F17 4단계 ⚠) / `unknown`(아직 시험 안 함), `held` = 지금 막고 있음. `time_sync`(09 SYS-012, 2026-10-23): 시험 동안 자동 시계 맞춤 상태 `state` = `held`(이 노드가 끔, 시험 끝·Reset All·비정상 종료 뒤 시작 때 다시 켬) / `off`(원래 꺼져 있음, 되돌릴 것 없음) / `not held`(못 끔, `why`에 이유 — Windows는 관리자 권한이 필요해 바꾸지 않음) / `unknown`(아직 시험 안 함). `ntp` = 이 PC의 자동 시계 맞춤이 지금 켜짐 `true` / 꺼짐 `false` / 모름 `null`(`timedatectl show -p NTP`, 1분에 한 번만 읽음; Windows는 `null`). 시계가 어긋났는데(DSC-009) `false`면 Action Required에 "Turn on time sync on <PC>" 버튼(2026-10-31, `POST /v1/time/sync`, 시험 사이에만).
- `boot_id`가 바뀌면 = 재시작(사망 처리, F11). `seq`는 Controller가 보낸 번호를 되돌려 왕복 시간·시계 차이 계산(D39).
- `last_1s`는 **1초 기록(7장)의 마지막 줄 그대로**, 같은 짧은 키(정의는 `record_keys` 한 곳, 2026-09-24 결정 Q-1).
- `Send quality reports`가 꺼져 있으면 `last_1s`는 비운다(F12).
- `events`: 지난 heartbeat 이후 새 사건만. **Controller는 그중 `warn`·`error`를 자기 사건 흐름에 다시 낸다**(`device` = 보낸 장비, `data.from_node` = true) — 노드에서 난 이유(예: RCV-019 재생기 안 켜짐)가 Controller 기록·Problems·CLI에 보이게(2026-09-25, 20장 GAP-40). 시작 실패(RUN-009)의 `detail`에도 그 장비의 마지막 오류를 붙인다.
- `links`: 시험에 쓰는 NIC(송신 WAN NIC, 수신 NIC)의 링크 상태(Core가 매초 읽음, 05 1.8). 무선 NIC는 `link_mbps` 대신 `wifi` 묶음: `{ "bssid", "band": "5GHz", "channel": 36, "width_mhz": 80, "center_mhz": 5210, "freq_mhz": 5180, "rssi_dbm": -58, "rx_mbps": 130, "tx_mbps": 390, "dtim": 1 }` (05 1.9). 태블릿은 앱이 같은 묶음을 보낸다. NET-013/014 구분은 **Controller가** 계획의 필요 대역과 기준 속도로 판단한다.

## 6. 5분 요약 (수신기별, 채널 가입 구간별)
```json
{
  "run_id": "…", "device": "tablet-1", "index": 17,
  "start_elapsed_s": 5100, "duration_s": 300, "partial": false,
  "ch": 2, "join_segment": 1,
  "loss": { "expected": 999900, "received": 999900, "lost": 0, "loss_rate": 0.0, "loss_events": 0,
            "max_burst": 0, "duplicates": 0, "reordered_recovered": 0, "late_discarded": 0, "host_drop_loss": 0 },
  "jitter": { "avg_ms": 1.8, "max_ms": 4.2, "delay_low_ms": -3.1, "delay_high_ms": 5.2, "pdv_window_ms": 6.4, "skew_ppm": 12.5, "ipdv_max_ms": 3.9, "ipdv_p99_ms": 2.7 },
  "mdi": { "df_max_ms": 3.1, "mlr_max_pps": 0 },
  "ts": { "p1_errors": 0, "sync_loss": 0, "sync_byte_err": 0, "pat_err": 0, "pmt_err": 0, "cc_err": 0, "pid_err": 0,
          "tei_err": 0, "crc_err": 0, "pcr_repetition_err": 0, "pcr_accuracy_err": 0, "pts_err": 0 },
  "bitrate_bps": { "avg": 35000000, "min": 34900000, "max": 35100000 },
  "receiver": { "host_drops": 0, "scan_lost": 0, "rcvbuf_bytes": 16777216, "locks_ok": true, "non_video_traffic_bps": 1200 },
  "wifi": { "rssi_dbm_min": -48, "link_mbps_min": 1200, "band": "5GHz", "channel": 36, "width_mhz": 80, "channel_changes": 0, "bssid_changes": 0 },
  "playback": { "freezes": 0, "frame_drops": 0, "decoder_frame_drops": 0, "audio_underruns": 0, "decoder_errors": 0, "player_restarts": 0 },
  "channel": { "changes": 0, "success": 0, "success_timeout_s": 5.0, "join_failures": 0, "leave_not_processed": 0, "zap_ms": { "total_avg": null, "total_max": null, "join_to_first_packet_max": null, "first_packet_to_play_max": null } },
  "measure_method": { "loss": "rtp_seq", "jitter": "rfc3550", "timestamps": "kernel" },
  "device_color": "green"
}
```
- **모든 키는 항상 있다.** 이번 단계에서 재지 않는 값은 `null`(키를 빼지 않음, 20장 G-02). 묶음 전체를 재지 않으면 묶음이 `null`(예: 유선 수신기의 `wifi`).
- `wifi`(2026-10-03, 20장 GAP-104 ③): 그 칸의 1초 줄 Wi-Fi 키에서 — `rssi_dbm_min`=`rssi` 최소, `link_mbps_min`=`wlm` 최소, `band`·`channel`·`width_mhz`=마지막 `wbd`·`wch`·`wbw`, `channel_changes`=`wcc` 합, `bssid_changes`=`wbc` 합. 그 칸에 Wi-Fi 키가 하나도 없으면(유선) `null`.
- `delay_low_ms`·`delay_high_ms`: 1초 기록 `dlo`·`dhi`의 5분 최소·최대(보고서용). `pdv_window_ms`: 1초 기록 `dpw`(그 초까지 10초 창의 `dhi` 최대 − `dlo` 최소)의 5분 최대 — **DVB 40 ms p-p 경고는 이 값으로 판정**(05 1.10, 2026-10-22: 5분 최대 − 최소는 PC 시계의 느린 보정(NTP slew, ≤ 0.5 ms/s)까지 지연 변동으로 세어 원인 없는 노랑을 냈다. 10초 창이면 보정은 ≤ 5 ms, 실제 지연 몰림은 그대로). 없으면(예전 요약) 최대 − 최소. `host_drop_loss` = min(`lost`, `host_drops`) — 수신 장비 자체 드롭으로 설명되는 손실(보라, 05 5.3). `receiver.scan_lost`: 1초 기록 `sl`의 합 — 수신기(태블릿)가 스스로 Wi‑Fi 스캔한 초의 손실, 수신기 쪽(iptv R-25). 태블릿의 `host_drops`는 노드가 adb로 매초 읽은 앱 소켓 drops 증가분(`/proc/net/udp`·`udp6`, 앱 uid·채널 포트), `nd`/`ne`는 wlan0 수신 drop/err, `ue`/`ub`는 태블릿 전체 UDP InErrors/RcvbufErrors. `expected` = `received` + `lost`, `loss_rate` = `lost` ÷ `expected`.
- `ts.p1_errors` = TR 101 290 우선순위 1 합계(`sync_loss`+`sync_byte_err`+`pat_err`+`cc_err`+`pmt_err`+`pid_err`). `pcr_*`·`pts_err`는 우선순위 2(2차).
- `measure_method.timestamps`: `kernel`(커널 수신 시각) 또는 `user`(수신 직후 단조 시계).
- `device_color`는 수신기 참고값. **최종 색은 Controller가 이 수치로 다시 계산**한다(`05-standards.md` 5.3).
- 채널이 바뀌면 그 5분 안에서도 `join_segment`별로 나눠 보낸다.
- `channel`(2026-10-01, F19, 20장 GAP-90): **끝난 전환을 받은 칸**(= 새 채널의 첫 가입 구간)에 들어간다 — 그 전환의 마지막 값이 정해진 초의 1초 줄이 들어간 칸. `changes` = 끝난 전환 수, `success` = 그중 S-47 안에 성공한 수, `join_failures` = 첫 패킷이 5초 안에 없던 수(**그 칸은 빨강**, 05 5.3), `leave_not_processed` = NET-005 수, `zap_ms` = 전환 시간 평균·최대(= `t_normal_play − t_cmd`, 재생을 껐으면 `t_first_packet − t_cmd`)와 가입→첫 패킷 최대·첫 패킷→정상 재생 최대(재생 끔이면 `null`). 전환이 없던 칸은 0과 `null`(객체는 항상 있음). 1초 줄 키 `zc` 등의 합·최대로 만든다(7장).
- 노드 저장소에서 가져갈 때 `seq`가 붙고(4장), Controller의 `summaries.jsonl`에는 `node`(보낸 노드 이름)와 `trouble`(Controller가 나눈 문제 구간 값, 05 5.3 "문제 구간으로 나누기")이 붙는다: `{"sender_s", "sender_lost", "sender_p1", "link_s", "link_lost", "link_p1", "slow_s", "busy_s", "screen_s", "jitter_outside_ms", "unknown_lost", "unknown_in": null|"sender"|"link"}` — 구간이 없던 칸은 `null`.

**단계별로 채우는 값 (G-02)**
| 값 | P2 (PC 유선 수신) | 채우는 단계 |
|----|------------------|-------------|
| `loss.*`, `jitter.avg_ms`·`max_ms`·`delay_*`, `ts.p1_errors`·P1 여섯 항목, `bitrate_bps.*`, `receiver.host_drops`·`rcvbuf_bytes`, `measure_method` | 채움 | P2 |
| `jitter.ipdv_*`(RFC 3393/5481), `mdi.*`(RFC 4445) | `null` | P3 |
| `ts.tei_err`·`crc_err`(TR 101 290 우선순위 2의 2.1·2.2, **세기만 하고 칸 색·판정엔 안 넣음**) | 채움 | 2026-09-25(P2, 20 GAP-42 ②) |
| `jitter.skew_ppm`(송신·수신 시계 기울기 추정, 05 2.2) | 채움(추정에 쓸 자료가 120 s 미만이면 `null`) | 2026-09-25(P2, GAP-42 ③) |
| `ts.pcr_*`·`pts_err`(TR 101 290 우선순위 2) | `null` | 2차 |
| `receiver.locks_ok` | `null`(PC엔 없음) | P3 태블릿 |
| `receiver.non_video_traffic_bps` | `null` | P3 |
| `wifi` | `null`(유선) | P3 무선 PC·태블릿 |
| `playback` | 재생 켬(S-49 On)이면 `freezes`(멈춤 시작 수 = `pfs` 합)·`frame_drops`(`pdr` 합)·`decoder_frame_drops`(`pdd` 합 — `frame_drops` 중 디코더 단계, 나머지는 화면 출력 단계, 재지 못하면(VLC는 디코더 버림을 따로 세지 않음) `null`, 2026-10-23 GAP-180)·`audio_underruns`(`pau` 합)·`player_restarts`(`prs` 합), `frame_drops`·`audio_underruns`는 재생기 통계가 살아 있을 때만(VLC RTP 입력은 `null`, iptv 3.2), `decoder_errors`(`pde` 합, 재생기 자기 기록, 2026-10-10 GAP-152). 재생 끔이면 묶음 `null` | 2026-09-25(P2), `decoder_errors` 2026-10-10 |
| `channel` | 객체(전환이 없던 칸은 0·`null`) | 2026-10-01 (F19 수신기 쪽, PC) |
| `device_color` | `null` | P3 태블릿 앱 표시용 |

## 7. 1초 기록 (수신기 파일, JSON Lines)
- 파일: `raw/<device>/<run_id>_h<시간 3자리>.jsonl` (1시간마다 새 파일)
- 첫 줄 = 헤더, 끝 줄 = 종료 표시(F12):
```json
{"type":"header","run_id":"…","device":"tablet-1","build":"20260923.1432","settings":{…}}
{"type":"s","e":5101,"ch":2,"rx":3333,"lost":0,"dup":0,"reo":0,"jit":1.9,"br":35000000,"hd":0,"cc":0,"pat":1,"rssi":-47,"pb":"ok"}
{"type":"footer","end_reason":"stop","lines":3600,"ts_utc":"…"}
```
  - 1초 줄은 크기를 줄이려고 짧은 키를 쓴다: `e`=경과 초, `ch`=채널, `rx`=수신, `lost`=손실, `dup`=중복, `reo`=순서 뒤바뀜, `jit`=Jitter ms, `br`=비트레이트, `hd`=자체 드롭, `cc`=CC 오류, `p1`=TR 101 290 우선순위 1 오류 합계(CC 포함), `dlo`=그 초의 상대 전달 지연 최소 ms, `dhi`=최대 ms(도착 시각 − RTP 타임스탬프, 가입 구간 첫 패킷 기준, RFC 5481 PDV, **시계 기울기를 뺀 값**(05 2.2), 패킷이 없는 초는 생략), `dpw`=그 초까지 10초(그 초 포함)의 `dhi` 최대 − `dlo` 최소 ms(2026-10-22, 시계 보정에 안 흔들리는 지연 변동), `sk`=그 초까지 추정한 시계 기울기 ppm(추정 전이면 키 없음), `tei`=TS 헤더 전송 오류 표시(TEI) 수, `crc`=PAT·PMT CRC 오류 수(TR 101 290 2.1·2.2, 세기만), `pat`=PAT 확인, `lev`=그 초에 확정된 손실 사건(연속 손실 묶음) 수, `mb`=그 초에 확정된 가장 긴 연속 손실, `late`=늦게 와서 버린 패킷 수, `sync`=싱크 잃음, `sbe`=싱크 바이트 오류, `pate`=PAT 오류, `pmte`=PMT 오류, `pide`=PID 오류(TR 101 290 P1 항목별, 합은 `p1`에서 `cc` 포함), `rssi`=받는 Wi-Fi 카드의 RSSI dBm, `wlm`=그 카드의 링크 속도 Mbps(받는 쪽), `wch`=Wi-Fi 채널, `wbw`=채널 폭 MHz, `wbd`=대역(`2.4GHz`/`5GHz`/`6GHz`), `wst`=붙은 Wi-Fi 규격(`11n`/`11ac`/`11ax`/`11be`, 태블릿: 노드가 adb로 읽음, 2026-10-24), `wcc`=1(채널·폭·대역이 바로 앞 초와 달라짐), `wbc`=1(BSSID가 바뀜 — 다른 AP로 옮김), `wsc`=그 초에 이 수신기가 한 Wi-Fi 스캔 수(태블릿 앱, 0이면 키 없음, TAB-018 근거, 2026-10-24), `sl`=그중 `wsc`가 있는 초의 `lost`(노드가 붙임, 수신기 쪽 손실, iptv R-25)(무선으로 받는 수신기만, 2026-10-03 GAP-104 ③; 유선이면 키 없음), `pb`=그 초의 재생 상태(값과 뜻은 **iptv 3.1 표 한 곳**, 20장 GAP-37), `pdr`=그 초에 재생기가 버린 프레임 수, `pdd`=그중 디코더 단계에서 버린 수(mpv `decoder-frame-drop-count`; 나머지 `pdr`−`pdd`는 화면 출력 단계 `frame-drop-count`; VLC·태블릿(Media3 `onDroppedVideoFrames`는 출력 단계라 나눌 수 없음)은 재지 않음 = 키 없음, 2026-10-23 GAP-180), `pau`=잃은 오디오 버퍼 수, `pfs`=멈춤이 이 초에 시작됨(1), `prs`=재생기를 이 초에 다시 띄움(1), `pde`=그 초에 재생기 디코더가 풀지 못한 프레임 수(재생기 자기 기록의 오류 줄, `pb`=`glitch`, 2026-10-10 GAP-152). 재생 끔(S-49 Off)이면 `pb`·`p*` 키 없음. **`pb`·`p*`는 재생기 자체 보고로만 채운다**
  - `lev`·`mb`·`late`·`sync`·`sbe`·`pate`·`pmte`·`pide`·`pfs`·`prs`·`pde`·`tei`·`crc`는 **0이면 쓰지 않는다**. `pdr`·`pdd`·`pau`는 반대로 **잰 초에는 0이어도 쓰고, 재지 못한 초(재생기 통계가 멈춤, iptv 3.2)에는 쓰지 않는다** — 없음 = 재지 않음(5분 요약에서 `null`)(대부분 0이라 파일 크기 절약). 5분 요약은 없는 초를 0으로 센다.
  - **받는 카드·호스트 카운터(2026-10-09, 20장 GAP-148, 참고 도구의 "재지 않은 값은 0이 아니다"):** 그 초 동안 늘어난 양(델타). 계획의 수신 NIC 하나(루프백 `lo`는 없음), Linux에서 `/sys/class/net/<nic>/statistics`·`/proc/net/snmp`를 파일로 읽음(0.2 ms, 프로세스 없음, Core 블록 `host_counters`). **읽지 못한 값은 키 없음, 잰 0은 0**. 카운터가 줄어든 초(드라이버 재시작·카드 다시 꽂음)도 키 없음. `np`=그 카드가 받은 패킷, `nm`=그 카드가 받은 multicast 프레임(**유선 카드만** — Wi-Fi 드라이버(mac80211, rtl8814au)는 이 값을 세지 않아 늘 0이므로 쓰지 않음, T460 실측), `nd`=그 카드의 rx_dropped, `ne`=rx_errors, `ue`=호스트 UDP InErrors, `ub`=RcvbufErrors, `unp`=NoPorts(호스트 전체라 **판정에 쓰지 않고 보여 주기만**). Wi-Fi 카드의 M2U(10 4 방법 A를 매초): 그 채널 group:port 프레임을 목적지 MAC으로 나눠 `fm`=multicast MAC(01:00:5e:…)으로 온 수, `fu`=이 PC 자기 MAC으로 온 수(공유기가 unicast로 바꿈) — 커널 필터(BPF)가 그 흐름의 앞 64바이트만 넘기는 별도 패킷 소켓, CAP_NET_RAW가 없거나 그 초에 커널이 이 소켓 프레임을 버렸으면 키 없음. Wi-Fi 카드의 AP 쪽 통계는 05 1.9의 10초 전체 읽기 때만(`iw dev <nic> station dump`, 매초 `iw`를 띄우지 않는다): 읽은 초에만 앞 읽기부터의 델타로 `wr`=tx retries, `wf`=tx failed, `wbl`=beacon loss, `wrp`=rx packets, `wsd`=그 델타가 덮는 초. 드라이버가 주지 않으면(rtl8814au는 빈 답) 키 없음.
  - 짧은 키의 뜻은 정의 한 곳에서 관리한다(IPTV: `modules/iptv/record_keys.py`).
  - **채널 전환 키(2026-10-01, F19, 20장 GAP-90):** 끝난 전환(`end: "done"`, 7-2)의 값이 그 뒤 **처음 쓰는 1초 줄**에 실린다(전환이 끝나는 때에는 새 구간이 아직 안정화 중이라 줄이 없을 수 있음). 0이면 쓰지 않는다: `zc`=1(끝난 전환 1건), `zok`=1(S-47 안에 성공), `zjf`=1(가입 실패, NET-003), `zlp`=1(Leave 미처리, NET-005), `zt`=전환 시간 ms(`기준 시각 − t_cmd`), `zj`=가입→첫 패킷 ms, `zp`=첫 패킷→정상 재생 ms(재생 켬일 때만). 5분 요약 `channel`이 이 키의 합·최대·평균으로 만들어진다(6장). 끝나지 못한 전환(`superseded`·`stopped`)은 싣지 않는다.
  - 안정화(warm-up) 구간의 초는 이 파일에 쓰지 않는다. `e`=0은 안정화가 끝난 첫 초. 안정화 기록은 Start-up 항목으로 따로 남긴다(05 2.1-2).

## 7-1. 재생 이상 증거 (`evidence/<경과시각>_<장비>_<종류>/event.json`, F23)
```json
{ "run_id":"…", "device":"tablet-2", "ch":2, "kind":"glitch", "ts_utc":"…", "elapsed_s":31245.2,
  "detector": { "blockiness":0.62, "frozen_s":null, "silence_ms":null, "black":false, "decoder_errors":3 },
  "packets": { "lost_in_window":184, "cc_err":12, "jitter_max_ms":21.3, "host_drops":0 },
  "classification":"router_side",
  "files": { "screens":["screen_-1s.png","screen_0s.png","screen_+1s.png","screen_+3s.png"], "clip":"stream_clip.pcap", "clip_ts":"stream_clip.ts", "clip_range_s":[-10,5], "audio":null },
  "capture": { "method":"player_snapshot", "delay_ms":35 } }
```
- `kind`: `stall` `glitch` `black` `audio` `packet` `manual`(Snapshot·CLI로 사람이 남김, 감지기 없음). `files.screens`는 화면 순환 버퍼에서(F23).

## 7-2. 채널 전환 기록 (`raw/<장비>/<회차>_zaps.jsonl`, 수신기별, F19)
```json
{"device":"tablet-2","from":1,"to":2,"join_method":"A","mode":"auto","t_cmd":5101.0,"t_leave":5101.05,"t_join":5101.07,"t_first_packet":5101.23,"t_first_frame":5101.0,"t_normal_play":5102.0,"basis":"normal_play","timeout_s":5.0,"success":true,"join_failed":false,"leave_check":"ok","t_last_old_packet":5101.2,"end":"done"}
```
- **시계(2026-10-01, 20장 GAP-90):** 수신기가 쓰는 줄이라 시각은 **수신기의 `e` 시계**(1초 줄의 `e`와 같은 축: 첫 측정 초가 끝난 때 = 0, 실제 시간으로 흐름 — 전환 뒤 안정화 구간의 초는 줄을 안 써도 `e`는 그만큼 건너뜀)의 초(소수 셋째 자리). Controller가 timeline 차이(`timeline.json`, 02 10)를 더해 자기 시계(`run_s`)로 바꾼다. 아직·못 알면 `null`.
- 시각의 뜻: `t_cmd` = 수신기가 명령을 받은 때, `t_leave` = 재생기·분석 소켓을 모두 닫아 Leave가 나간 때(PC는 재생기를 새로 켜므로 전환 시간에 재생기 시작이 들어감, 비교용), `t_join` = 새 가입 직후, `t_first_packet` = 새 채널 첫 RTP 패킷이 도착한 때(커널 도착 시각), `t_first_frame` = 재생기가 첫 프레임을 알린 첫 초, `t_normal_play` = 재생기가 `ok`(정상 FPS + 소리, 05 1.6-2, TTAK.KO-01.0204/R1)를 처음 알린 초(재생기 보고는 초 단위라 초 경계로 올림).
- **기준(`basis`):** 재생을 켜면 `normal_play`, 재생을 끄면(S-49 Off) `first_packet`(`t_first_frame`·`t_normal_play`는 `null`). **전환 시간 = 기준 시각 − `t_cmd`.** 공유기 몫 = `t_first_packet − t_join`, 수신기 몫 = `t_normal_play − t_first_packet`.
- `success` = 전환 시간 ≤ `timeout_s`(S-47). 가입 실패면 `false`. 끝나기 전에 막힌 줄(`end`가 `superseded`·`stopped`)은 기준 시각이 있으면 같은 식으로, 없으면 `null`.
- `join_failed` = 첫 패킷이 가입 뒤 5초(고정, 05 2.1-2 4번) 안에 없음 = **NET-003**. 그때 `t_first_packet`은 `null`.
- `leave_check`: `ok`(Leave 뒤 3초 넘게 이전 그룹이 오지 않음) / `not_processed`(3초 넘게 옴 = **NET-005**) / `not_checked`(원시 소켓 권한 없음, 같은 노드의 다른 수신기가 그 그룹을 아직 받음, 확인 끝나기 전에 막힘, 그 밖 이유 — **노랑 아님**). `t_last_old_packet` = Leave 뒤 본 이전 그룹의 마지막 패킷 시각(없으면 `null`) — Leave → 마지막 패킷 지연 = `t_last_old_packet − t_leave`(500 ms 초과 횟수는 분석에서, 03 F19).
- `end`: `done`(끝남) / `superseded`(끝나기 전에 다음 전환이 옴) / `stopped`(끝나기 전에 시험이 끝남).
- **줄은 전환이 끝난 뒤 한 번 쓴다:** 가입이 가려지고(첫 패킷 또는 가입 실패) **그리고** Leave 확인이 끝나고(최대 4초) **그리고** 기준 시각이 정해졌을 때(`t_cmd` + 30초까지 안 정해지면 `null`로 끝냄, 재생기가 멈춘 경우). 명령에 대한 답(`receiver/channel`)은 첫 패킷 또는 가입 실패까지만 기다린 줄(아직 모르는 값 `null`, `end: null`)을 돌려준다.
- 5분 요약 `channel`은 이 파일이 아니라 1초 줄의 전환 키(7장)로 만든다 — 같은 값이 두 곳에 있으므로 시험이 둘을 맞춰 본다.

## 8. 사건 기록 (`events.log`, JSON Lines)
```json
{"code":"ZL-TAB-001","level":"error","text":"USB disconnected","device":"tablet-2","elapsed_s":5102.3,"ts_utc":"…","data":{}}
```
- 회차 폴더의 `events.log`(2026-09-27, 03 F15 표, 20장 GAP-85): 그 회차 동안(Start를 받은 때부터 보고서까지) 사건 흐름의 모든 사건 + `run_s`(회차 시각, iptv 2.4). Controller가 시작 단계를 하나 마칠 때마다 `ZL-INFO` `data.step`(경로)·`data.ch`. 노드 자신의 `logs/events.log`에는 `run_s`가 없다.
- `level`: `info` `warn` `error`
- `data`: 사건별 추가 값(예: 링크 변화 전후). 없으면 키를 뺀다(유일한 예외 — 사건마다 모양이 달라 비교 시험은 공통 키만 본다).
- `code`: 예외는 `09-exceptions.md`의 코드. 예외가 아닌 일반 사건(상태 변화, 시작·종료 등)은 `ZL-INFO`.
- 모든 사건은 Core의 사건 흐름(event bus) 하나를 지나 `events.log`, heartbeat `events`, Problems 창으로 간다(12-architecture 4-1).
- **해결 신호(2026-09-25, F21):** 문제가 풀렸다는 사건은 `data.clears`에 풀린 코드 목록을 싣는다(예: 링크가 정상으로 돌아옴 → `{"clears": ["ZL-NET-013", "ZL-NET-014"]}`, 같은 `device`). Problems 창은 이것으로 그 줄을 "해결됨"으로 옮긴다. 시험이 끝나거나(IDLE) Reset All이면 그 시험의 진행 중 문제는 모두 "해결됨(시험 끝)"으로 옮긴다.

## 9. 시험 상태 파일 (`run-state.json`, Controller)
```json
{ "run_id":"…", "status":"running", "planned_s":360000, "started_utc":"…", "last_saved_utc":"…",
  "devices":[…], "settings":{…}, "generation":3, "sender_mode":"test_linked", "controller_boot_id":"…" }
```
- `settings`: 시작 때 고정한 계획 값(계획 파일의 `settings`, 1.10 snapshot). `sender_mode`: S-13 값 — `test_linked` / `always_on`(F18, 2026-10-02).
- `status`: `running` `stopping` `finished` `recovered`. 다음 실행 때 `running`이면 복구(F10, CTL-003).
- 같은 폴더에 **시작한 계획의 사본 `plan.json`**(11-1 형식 그대로, 2026-10-02부터 **실제로 쓴 계획** — Controller가 채운 `hosts`·시험 카드 `nics` 포함, 20장 GAP-99 ②)을 둔다: 복구할 때 판정 규칙(장비·화질·시간)을 다시 세우는 데 쓴다(2026-09-25, 20장 GAP-47).

## 10. 결과 요약 (`summary.json`, 보고서 폴더)
```json
{
  "run_id":"…", "build":"20260923.1432", "verdict":"INCOMPLETE", "verdict_reason":"stopped_by_user", "recovered": false,
  "verdict_details":[ "…" ], "with_warnings": false, "not_run":[ "playback" ],
  "planned_s":360000, "actual_s":45240, "test_type":"tablet", "quality":"UHD", "method": [],
  "dut": { "model":"…", "firmware_build":"…", "mode_user":"NAT", "mode_detected":"NAT", "wan_ip":"10.0.0.100", "wan_subnet":"10.0.0.0/24", "wan_ip_way":"dhcp", "igmp_note":"…", "m2u_note":"…", "multicast_path":"igmp", "through_router":true, "not_through_router_why":null },
  "source": { "1":"generated", "2":"generated", "3":"movie_uhd.mp4" }, "source_fallback": [],
  "devices":[ { "device":"tablet-1", "ch":1, "colors": { "green":150, "yellow":0, "red":0, "purple":0, "grey":0 }, "loss_total":0, "jitter_max_ms":4.2,
                "igmp_version":"v3", "playback":"on", "wifi_auto_connected": false, "m2u": null,
                "cells":[ { "index":0, "start_elapsed_s":0, "duration_s":300, "partial":false, "color":"green", "reasons":[], "values":[ { "key":"lost", "label":"Lost", "unit":"", "value":0, "bad":true }, { "key":"jitter_max", "label":"Jitter max", "unit":"ms", "value":2.4, "bad":false } ] } ] } ],
  "devices_rtt_ms": { "sender-pc": 0.4 },
  "channel_change": { "changes":0, "success":0, "success_timeout_s":5.0, "total_ms_avg":null, "total_ms_max":null, "join_failures":0, "leave_not_processed":0 },
  "sender_mode":"test_linked", "broadcast_since":null, "test_network":"router",
  "receiver_baseline": { "tablet-1": { "verified":true, "run_id":"R20261001-090000", "date_utc":"…", "player":"VLC", "loss":0, "playback_issues":0 } },
  "settings_changed": { "S-11":"UHD 38 Mbps" },
  "analysis":[ { "rule":"R-03", "category":"Router", "confidence":"high", "devices":["tablet-2"], "at_s":5102.0, "text":"Only wireless receivers lost packets; wired PC was fine → Router wireless multicast (M2U) suspected" } ],
  "analysis_missing":[ { "rule":"R-02", "why":"1-second records not fetched yet" } ], "analysis_pass":"report",
  "results_web": { "requests": 0, "bytes": 0 },
  "receiver_side_issues": 0,
  "problems": [ { "code": "ZL-NET-013", "level": "error", "device": "sender", "what": "…", "count": 1,
                  "first_utc": "…", "last_utc": "…", "cleared_utc": "…", "open_at_end": false } ],
  "human_actions": [ { "action_id": "human.cable.check", "device": "sender", "what": "…", "requested_utc": "…",
                       "done_utc": "…", "done_by": "auto" } ],
  "alarm_stats": { "total": 3, "per_10_min_avg": 0.3, "per_10_min_max": 2, "flood_time_ratio": 0.0,
                   "top": [ { "code": "ZL-NET-013", "device": "sender", "count": 1 } ] },
  "measurement_trust": { "level": "ok", "checks": [ { "id": "host_drops", "ok": true, "text": "No receiver drops" } ] },
  "changes_since_previous": { "run_id": "…", "items": [ { "group": "link", "name": "Sender WAN link", "before": "2500 Mbps", "after": "1000 Mbps", "worse": true } ] },
  "versions": { "controller":"…", "agents":{…}, "app":{…}, "tools":{…} }
}
```
- `analysis`·`analysis_missing`·`analysis_pass`(2026-09-27, iptv 2.4, 20장 GAP-85): 원인 후보(판정과 무관), 자료가 없어 계산하지 못한 규칙, `report`(보고서 때 5분 요약·사건만) / `records`(1초 기록 회수 뒤 다시 계산).
- `verdict_details`: 판정 이유 문장들. `with_warnings`: PASS인데 노랑 칸이 있음. `not_run`: 시험하지 않은 항목(절대 원칙 `NOT RUN`). `devices[].cells`: 5분 칸별 색과 이유(보고서·결과 웹이 그림). `devices_rtt_ms`: 장비별 heartbeat 왕복 시간 최대값(최근 1시간, 가장 나쁜 경우).
- `method`(2026-10-17): 모듈이 어떻게 쟀는지 문장 목록(throughput: 도구·방향·iperf3 명령·시간·대기). 모듈 없이도 보고서가 보여 주게 결과에 남김(12 1-3). 그런 규칙이 없는 모듈(iptv)은 `[]`.
- 이 파일도 **모든 키가 항상 있다**(값이 없으면 `null`, G-02).

**단계별로 채우는 값 (G-02)**
| 값 | P2 | 채우는 단계 |
|----|----|-------------|
| 판정·시간·`devices[]`의 색·손실·지터·`cells`, `not_run`, `devices_rtt_ms`, `source`(채널별 파일 이름), `settings_changed`(계획의 `settings`), `receiver_side_issues`(보라 칸 수), `versions.controller`·`agents` | 채움 | P2 |
| `dut` `through_router`(2026-10-02, GAP-99 ②: 시험 카드가 계획에 있거나 Controller가 채웠으면 `true`, 못 채웠으면 `false` — 이때 판정 PASS는 INCOMPLETE, 09 NET-022)·`not_through_router_why`(아니면 `null`). | 2026-09-25부터 객체: `mode_detected`(07 4 판별 `NAT`/`Bridge`)·`wan_ip`(NAT면 장비가 본 Controller 주소 = 공유기 WAN IP)·`wan_subnet`(그 장비 NIC의 대역, 07 3.3)·`wan_ip_way`(`dhcp` = 송신 PC의 통신사 역할이 준 주소 / `static` = 공유기에서 정한 주소 / 모르면 `null`, 07 3.3)·`multicast_path` 채움, `model`·`firmware_build`·`mode_user`·`igmp_note`·`m2u_note`는 `null`(F14 입력 화면 때) | P3 |
| `dut.multicast_path` | `"lan"`(LAN 쪽 PC가 보낸 비교 시험, 06 10.4 — 이때 `mode_detected`는 `null`) / `"igmp"`(멀티캐스트로 받음) / `"primed"(프라임 유니캐스트, S-18 On 또는 Auto가 정함 — IGMP 프록시 없는 개발용·SDK 공유기, iptv 4.5) / `"direct"`(기준선 시험 S-54, 공유기 없이 바로 — 05 4.6.3, 2026-10-04 GAP-113) | 프라임 방식 구현 때(GAP-57) |
| `source_fallback` | `[]` | P3 |
| `devices[].igmp_version` | `null` | P3 (V15) |
| `devices[].playback` | S-49 값: `"on"`(재생 켬) / `"off"`(분석만, 재생 항목 `NOT RUN`) | 2026-09-25(P2) |
| `devices[].wifi_auto_connected` | 유선이면 `null`, 무선 수신 PC는 이번 Start에서 툴이 연결했으면 `true`, 이미 붙어 있었으면 `false`(2026-09-25). 태블릿은 P4 | P3 |
| `devices[].m2u` | 유선이면 `null`, 무선 수신 PC는 `{"state": "active"\|"not_active"\|"unknown", "why": "…"}`(5단계 시험 송출에서 본 목적지 MAC, 03 F17 "무선 수신 PC") | 2026-09-25(P3) |
| `channel_change` | 전환이 한 번도 없으면 `null`, 있으면 5분 요약 `channel`의 합: `changes`·`success`·`join_failures`·`leave_not_processed`, `total_ms_avg`(전환 수로 가중)·`total_ms_max`, `success_timeout_s` | 2026-10-01 (F19) |
| `test_network` · `receiver_baseline` | `test_network` = S-54: `"router"`(보통) / `"baseline"`(공유기 없는 기준선 시험, 05 4.6.3). `receiver_baseline` = 수신 장비마다 그 장비·재생기 조합의 **마지막 기준선 시험** `{"verified", "run_id", "date_utc", "player", "loss", "playback_issues"}` 또는 `null`(한 번도 안 함). 기준선 회차에서는 이 회차의 결과. `verified` = 회차 PASS(계획 시간 끝까지) + 손실 0 + 재생 이상 0(재생 끔이면 `player: "none"`, 재생 이상은 `null`) | 2026-10-02 (GAP-97) |
| `analysis` | `[]` | P3 (원인 분석 규칙) |
| `verdict_reason` | `planned_time_reached` / `all_cases_done`(경우로 도는 시험 — throughput — 이 모든 경우를 끝냄. 이런 시험은 계획 시간에 끊지 않고, 계획의 4배가 지나야 `took_too_long`으로 멈춤, 2026-10-22) / `took_too_long` / `stopped_by_user` / `device_lost: <장비>: <이유>` / `controller_shutdown`(서비스 정상 종료) / `controller_restarted`(Controller가 비정상으로 꺼졌다 다시 켜져 만든 복구 보고서, CTL-003) / `disk_low: <장비>`(RCV-008 자동 중단) / `file_write_failed: <장비>`(RCV-009 새 파일로도 못 씀, 2026-09-26) | 2026-09-25 목록으로 정리 |
| `recovered` | 복구 보고서면 `true`(F10: 비정상 종료 뒤 다음 실행이 만든 것) | 2026-09-25(P3) |
| `problems` | 이 시험 동안 난 문제(F21) 전부: 코드·장비 한 줄, `open_at_end` = 시험이 끝날 때 아직 진행 중이었음 | 2026-09-25(P3) |
| `human_actions` | 이 시험 동안 생긴 사람 할 일(F22): 무엇을, 언제 요청, 언제·누가 끝냄(`auto`/`human`, 안 끝났으면 `null`) | 2026-09-25(P3) |
| `alarm_stats` | 알림 수(F21, ISA-18.2): 새 알림 수, 10분당 평균·최대, 폭주(10분 10개 초과) 시간 비율, 가장 잦은 10개. **판정·색과 무관** | 2026-09-25(P3) |
| `measurement_trust` | `null` | P3 (F25) |
| `changes_since_previous` | `null` | P3 (F24) |
| `versions.app`·`tools` | `null` | P3 |

## 11. 설정 파일
| 파일 | 위치 | 내용 |
|------|------|------|
| `controller.json` | `C:\ProgramData\ZeroLoss\Controller\` | 마지막 선택(F14, 2026-09-26): `{"schema_version":1, "setup": {"module", "test_type": "PC receiver test", "quality", "duration_s", "send_quality_reports", "sender": null\|"tx", "receivers": [{"device", "ch", "network": "Wired"\|"Wireless", "buffer_ms"?}], "settings": {바꾼 것만}}}`. 보고서 위치·장애 정책은 그 설정이 생길 때 |
| `agent.json` | Agent 데이터 폴더 | `device_id`, 페어링된 Controller, 허용 IP, (옵션) 키, 역할, `wan_isp`(07 3.2 B: `{"on": true\|false\|null, "nic": null\|"enp3s0"}` — `on` 없음·`null` = 자동(기본), `nic` 없으면 자동 고르기) |
| `wan-lease.json` | Agent 데이터 폴더 | 07 3.2 B 임대 기록: `{"schema_version":1, "nic", "added_ip": "10.0.0.1/24", "lease": {"mac", "ip": "10.0.0.100", "since_utc", "expires_utc"} \| null}` — 우리가 더한 주소와 준 주소만 |
| `test-wifi.json` | Controller 데이터 폴더 | 시험 Wi-Fi(S-51): `{"schema_version":1, "ssid":"ZL-TEST", "security":"WPA2", "password": {"kind": "dpapi"\|"plain-0600", "data": "<base64>"}}`. Linux(개발용) 파일은 0600. 02 2-7 |
| `ssh-known-hosts.json` | Controller 데이터 폴더 | SSH로 설치한 PC의 호스트 키: `{"schema_version":1, "hosts": {"10.0.0.2": {"key_type":"ssh-ed25519", "sha256":"SHA256:…", "added_utc":"…"}}}`. 처음 접속 때 저장, 다르면 INS-009(06 11.3, 2026-09-25) |
| `defaults.json` | 프로그램 폴더(읽기 전용). **Core 하나(`core/settings/defaults.json`) + 모듈마다 하나(`modules/<id>/defaults.json`)** — 모듈을 지우면 그 설정도 함께 사라짐(12 1-3, 2026-09-24 결정) | 각 설정의 기본값의 **유일한 원본**. 코드에 기본값을 흩어 적지 않는다. 형식 `{"schema_version":1, "settings": {"S-15": {"value": 16, "doc": "16"}}}` — `doc`은 16장 표의 기본값 칸 글자 그대로(자동 시험이 비교). 같은 S-ID가 두 파일에 있으면 안 됨 |
- 모든 설정 파일과 **SQLite DB(`experience.db` 등, `PRAGMA user_version`)**에 `schema_version`(정수). 형식이 바뀌면 올리고 변환 코드를 둔다(06-install 6장). DB 변환 전에는 사본(`*.bak-<빌드>`)을 남기고, 변환이 실패하면 사본으로 되돌린 뒤 시작을 막는다(INS 오류로 표시).
- **프로그램 진단 로그**(측정 자료·`events.log`·보고서와 별개인 개발용 로그): 프로세스별 `logs/<프로세스>.log`, 20 MB마다 새 파일, 최대 10개까지 두고 가장 오래된 것부터 지운다(프로세스당 최대 200 MB). 100시간 시험에서도 무한히 커지지 않게 한다. 측정 자료와 사건 기록은 이 규칙으로 지우지 않는다(F15 보관 규칙).
- 저장은 **임시 파일에 쓰고 바꿔치기**(쓰다가 꺼져도 파일이 깨지지 않게).

## 11-0. 장비 목록 (`devices.json`, Controller) — P2 수동 등록 (2026-09-24 결정)
```json
{ "schema_version": 1, "devices": [ { "name": "sender-pc", "host": "10.0.0.2", "port": 10992, "deployer_port": 10991, "device_id": "…", "role": "sender", "added_utc": "…", "best_link_mbps": { "10.0.0.2": 2500 } } ] }
```
- `zeroloss devices add <이름> <IP> [--port 10992] [--deployer-port 10991]`(`deployer_port`: 그 PC Deployer 포트, 15장, 2026-09-25): Controller가 `GET /v1/health`로 확인한 뒤 `device_id`·역할을 저장. 응답이 없으면 등록 거부(종료 코드 5). `devices list` / `devices remove <이름>`.
- 계획 파일의 장비 이름은 이 목록에서 찾는다. P3 자동 발견은 같은 목록을 채운다. `device_id`로 같은 장비를 알아보므로 IP가 바뀌어도 이름은 유지.

## 11-1. 시험 계획 파일 (`plan.json`, CLI·[Save plan])
```json
{ "schema_version": 1, "module": "iptv", "test_type": "tablet",
  "devices": { "sender": "sender-pc", "receivers": [ {"device":"tablet-1","ch":1}, {"device":"tablet-2","ch":2} ] },
  "quality": "UHD", "duration_s": 360000, "send_quality_reports": true,
  "settings": { "S-21": "every 10 min", "S-47": 1.0 },
  "nics": { "sender-pc": "10.0.0.2", "rx-pc": "192.168.1.10" },
  "files": { "1": "C:\\ZeroLoss\\Prepared\\ch1_HD_10M.ts" },
  "note": "FW 1.2.3-rc4 overnight" }
```
- `devices`는 장비 이름(자동 발견 목록의 이름) 또는 `device_id`. 없는 장비면 종료 코드 3. 수신기 항목의 `buffer_ms`(선택, 300~5,000): 그 수신기 재생기의 버퍼(S-25를 그 PC만 바꿈, 2026-10-16).
- `settings`에는 **기본값과 다른 것만** 적는다(나머지는 `defaults.json`). 16-settings 허용 값·조합 규칙(K) 검사를 GUI와 똑같이 거친다.
- `nics`(선택): 장비별로 쓸 NIC의 IP, 또는 수신 PC에 `"wifi"`(그 PC의 Wi-Fi NIC로 받음 — S-51에 연결해 받은 IP를 씀, 03 F17 "무선 수신 PC", 2026-09-25). 송신은 WAN 쪽 송출 NIC(05 3.1, `--local-address`), 수신은 그룹 가입 NIC. 없으면 OS 기본 경로. GUI는 후보를 추천하고 고르게 한다(DSC-006). `files`(선택): 채널별 송신 PC의 준비 파일 경로. 없는 채널은 자동 생성 영상(iptv 1-1-2, P3부터. P2는 필수였음).
- `settings.S-48`(선택): 채널별 원본 영상 `{"1": "Generated", "2": "movie.mp4"}` — 송신 PC 미디어 폴더 안 파일 이름만(iptv 1-1 구현 규칙).
- `hosts`: **Controller가 채운다**(파일에 적지 않음, 적어도 덮어씀): 계획에 나온 장비마다 등록 주소(11-0 `host`). 모듈이 장비 주소가 필요할 때 쓴다(예: 프라임 목적지, iptv 4.5).
- 비밀 값(S-51 비밀번호)은 계획 파일에 넣지 않는다. Controller에 저장된 값을 쓴다.

## 12. 결과 조회 웹 (Controller, TCP 10993)
| 경로 | 내용 |
|------|------|
| `/` | 보고서 목록 (F15) |
| `/live` | **실시간 모니터(2026-10-08 GAP-119, 사용자 "모니터링이 지금은 너무 빈약해", 이전 RealTest 도구의 운영 보드를 본뜸)**: 1초마다 `/live.json`을 읽어 수신기마다 ① 지금(마지막 1초) 카드 ② 열린 5분 칸 카드(모듈이 정한 합·최대·최소) ③ 최근 10분 합 카드 ④ 10분 그래프(지터·비트레이트·손실, 아래 빨간 눈금 = 문제 초, 지터는 S-23 경고선) ⑤ 5분 칸과 이유 ⑥ 최근 10분 중 문제 초 표 ⑦ 최근 60초 표. 값이 없는 항목(유선의 Wi-Fi RSSI 등)은 숨김. 무엇을 보일지는 모듈 `live_view`의 열 목록(키·이름·단위·합 방식)이 정하고 Core는 키를 모름. 화면 위쪽 [Live view]가 이 페이지를 연다 (읽기 전용). **넓은 화면은 왼쪽 값 표·오른쪽 큰 그래프(축 값·실제 시각·마우스 값, 2026-10-09 GAP-131)**. **시험 전체의 문제 구간 표(시각·길이·손실·무엇·그 무렵 사건, 2026-10-09 GAP-138)** — 10분보다 오래된 손실도 언제였는지 바로 보임. **수신기가 2대 이상이면(태블릿 1~3대 등, 2026-10-23 사용자 "태블릿 3대면 live view에는 어떻게 다 보여줄거야")** 맨 위 "All receivers": 수신기마다 한 줄(색·지금 값·선 보이기), 그래프마다 수신기별 선을 한 시계에 겹침(색 + 선 모양), 수신기마다 문제 초 띠, "함께 본 문제" 표 — 모두 동시면 "공유기·송신 쪽", 한 대만이면 "그 수신기 쪽"(시각만 본 힌트, 색·판정 안 바꿈). 그 아래 수신기별 자세히는 그대로 |
| `/live.json` | 위 페이지의 값(10994 `GET /v1/controller/live`와 같음): `{"state", "run_id", "elapsed_s", "planned_s", "quality", "view": {"columns", "cell_s", "lines"}, "receivers": [{"key", "device", "ch", "records": [최근 600초, 열에 있는 키만], "row", "age_s", "e_shift"(이 수신기의 `e`에 더하면 페이지 시계 — 수신기마다 시작이 달라 나란히 그릴 때 씀, 2026-10-23)}], "senders", "cells", "result"}` |
| `/reports/<폴더>/report.html` | 보고서 (F16 구간 보기 포함) |
- 읽기 전용. 시험 조작 기능은 웹에 두지 않는다.
- 구현(2026-09-27, 20장 GAP-86): Controller 서비스 안의 표준 `http.server`(외부 의존 없음), `GET`만. `/reports/<폴더>/…`는 보고서 폴더 안의 파일만(폴더 밖·숨김 파일은 404), 진단 zip은 주지 않는다. `/`는 02 2-13 `reports`와 같은 목록, `/live`는 02 2-1 상태의 5분 칸·장비 줄(5초마다 새로 읽음, 영상 없음).
- **누가 볼 수 있나(S-44):** `Off` = 열지 않음, `This PC only` = 127.0.0.1에서만, `Same subnet`(기본) = Controller PC의 어느 NIC 대역 안 주소만(그 밖은 403). 포트를 못 열면 CTL-006(시험은 계속).
- **조회 트래픽 기록(07 7장):** 시험 중 보낸 바이트를 세어 `summary.json` `results_web` = `{"requests", "bytes"}`(시험 중에 온 것만)로 남긴다. 시험 중에는 `/live`·목록 위에 "Viewing during a test: use a wired PC (Wi-Fi viewing mixes into the test, K-22)"를 늘 보인다 — 판정 영향 없음.

## 13. 패키지 (`.zlm` 모듈 패키지, `.zlp` 노드 패키지 — 10991로 보냄)
> 06장 11.5·12장, 12장 1-3의 "ZeroLoss 패키지 형식". 2026-09-25 정함(20장 GAP-52). zip 파일 하나이고, 맨 위에 설명 두 개와 `payload/` 폴더가 있다.
```json
{ "schema_version": 1, "kind": "module", "id": "iptv", "build": "20260925.1200",
  "os": "any", "core_api_min": 1, "core_api_max": 1 }
```
- `package.json`(위): `kind` = `module` / `node` / `tools`(2026-10-16: 모듈이 쓰는 도구, 예 iperf3 — `payload/`에 Ubuntu `.deb` 파일들, Deployer(root)가 그 파일만으로 `apt-get install`(인터넷·암호 없음, 시험 중이면 INS-022 거부), 이름 `zeroloss-tools-<id>-<build>.zlt`, 도구는 시스템 패키지로 남음, 20장 GAP-172). `id` = 모듈 id(노드 패키지는 `agent`·`controller`). `os` = `any` / `linux` / `windows`. `core_api_*`는 모듈만(노드 패키지는 `null`).
- `payload-hashes.json`: `{"schema_version": 1, "files": {"payload/module.json": "<sha256 16진수 64자>", …}}` — `payload/` 아래 **모든 파일**의 SHA-256. 빌드 때 자동으로 만든다.
- **설치 전 검사(INS-006):** 목록에 없는 파일이 zip에 있거나, 목록의 파일이 없거나, 해시가 다르거나, 이름이 위험하면(절대 경로·`..`·링크·`\`) 설치 거부. 검사는 **아무것도 풀기 전에** 한다.
- 모듈 패키지의 `payload/`는 모듈 폴더 내용 그대로(`module.json` 포함, `__pycache__` 제외). 노드 패키지의 `payload/`에는 OS 설치 파일 하나(`.deb` 또는 `Setup.exe`).

## 14. 설치 목록 (`install-manifest.json`)
> 06장 1장 원칙 1·2의 설치 목록. 모듈은 Deployer가 `<데이터>/deployer/manifests/module-<id>.json`에 쓴다(12장 1-3). 2026-09-25 정함(20장 GAP-52).
```json
{ "schema_version": 1, "kind": "module", "id": "iptv", "build": "20260925.1200",
  "installed_utc": "…",
  "items": [ { "type": "dir", "path": "/opt/zeroloss/modules/iptv", "tree": false, "sha256": null },
             { "type": "file", "path": "/opt/zeroloss/modules/iptv/module.json", "tree": false, "sha256": "…" } ],
  "keep": [ { "type": "dir", "path": "/var/lib/zeroloss/modules/iptv/settings", "tree": true, "sha256": null } ],
  "tools": [] }
```
- `items`: 설치가 만든 것, 만든 차례대로. 제거는 **거꾸로**, 이름이 정확히 같은 것만 지운다(06 원칙 2). `type`: 1차는 `file`·`dir`. 노드 설치 목록은 `service`·`firewall_rule`·`registry_key`·`account`·`sysctl_file`·`polkit_rule`·`udev_rule`(태블릿 adb, 2026-10-23)·`sleep_mask`(06 4.3, path = systemd 대상 이름)·`autostart`·`auto_login`(06 11.3 4-2, 원래 GDM 파일은 `deployer/auto-login-orig.json`)·`group_member`(path = 그룹 이름, 설치가 `zeroloss`를 넣은 것만)을 더 쓴다(06 4장).
- `tree`: `true`인 `dir`만 안의 것까지 통째로 지운다 — **그 모듈만 쓰는 캐시 폴더**(`<데이터>/modules/<id>/cache`, 예: IPTV 자동 생성 영상)와, 모듈 코드 폴더마다 파이썬이 불러올 때 만드는 `__pycache__`(설치 때 정확한 이름을 미리 적어 둠, 2026-09-25 실제 노드 시험에서 발견)에만 쓴다. `false`인 `dir`은 비었을 때만 지우고, 모르는 파일이 남아 있으면 지우지 않고 INS-021로 보고한다.
- `keep`: 제거 때 기본으로 **남기는** 것(모듈 설정 `<데이터>/modules/<id>/settings`). `--purge`일 때만 지운다.
- `tools`: 이 모듈이 설치한 외부 도구(여러 모듈이 쓰는 도구는 쓰는 모듈 수를 `<데이터>/deployer/tool-users.json`에 세고, 0이 될 때만 지운다, 12 1-3). 1차 모듈은 도구를 따로 설치하지 않아 빈 목록.
- **진행 기록(INS-013):** 설치·제거·업데이트 중에는 `<데이터>/deployer/journal.json`(`{"operation", "kind", "id", "step", "backup"}`)을 두고, 끝나면 지운다. Deployer가 시작할 때 이 파일이 있으면 `backup`으로 이전 상태를 되돌린다.

## 15. 배포 API (Deployer, TCP 10991)
> L0(12장 1-1): Core·모듈 코드를 쓰지 않는 작은 서버(Python 표준 기능만). 봉투는 3장과 같다(`ok`·`data`·`error`, `command_id` 중복 방지). 설치는 시험과 무관한 통로라 `generation`은 검사하지 않고, 대신 **그 PC의 노드가 시험 중이면 거부**(INS-022). 2026-09-25 정함(20장 GAP-52).

| 메서드·경로 | 용도 | 주요 응답 |
|-------------|------|-----------|
| `GET /v1/deployer/health` | 살아 있는지, 같은 PC 노드 상태 | 아래 예 |
| `PUT /v1/deployer/incoming/<파일 이름>` | 패키지 받기(본문 = 파일 그대로, 머리글 `X-ZL-SHA256` 필수, 다르면 INS-006) | `name`, `bytes`, `sha256` |
| `POST /v1/deployer/install` | params `package`(받은 파일 이름). 모듈: 06 12장, 노드: 06 11.5 | 아래 예 |
| `POST /v1/deployer/remove` | params `kind`, `id`, `purge`(기본 `false`), `dry_run`(기본 `false`) | 지운·남긴·남은 것 |
| `GET /v1/deployer/modules` | 설치된 모듈 목록 | `modules`: `[{"id", "build", "installed_utc"}]` |
| `POST /v1/deployer/node` | params `action`: `restart`(F11 [Restart Agent], 새 boot_id로 답할 때까지) / `stop`(F11 [Shut Down All], 노드 서비스를 멈추고 **감시가 다시 켜지 않게** 표시 `deployer/node-stopped.json`) / `start`(표시 지우고 켬, 답할 때까지). 노드가 시험 중이면 INS-022, 확인 못 하면 INS-014(시작·재시작)·SYS-006(정지). 2026-09-25 | `{"action", "done": true, "node": <노드 health 또는 null>}` |
```json
{ "build": "20260925.1200", "deployer_api": 1, "os": "linux", "boot_id": "…", "busy": false, "node_stopped": false, "update": null, "updates_held": false,
  "agent": { "responding": true, "build": "20260925.1200", "state": "IDLE", "restarts": 0 } }
```
- `updates_held`: 시험 중이라 자동 업데이트·자동 재부팅을 멈춰 둠(09 SYS-005, Linux).
- `update`: 마지막 노드 업데이트 작업(06 11.5 Linux 구현 규칙) `{"id", "job", "from_build", "to_build", "state": "running"|"done"|"rolling_back"|"rolled_back"|"failed", "why", "started_utc", "ended_utc", "no_rollback"}`, 없으면 `null`. `job` = 지금 도는 설치 작업(되돌릴 때 `<id>b`). 노드 패키지(.zlp, 안에 .deb)의 `POST /v1/deployer/install` 답은 바로 `{"operation": "install", "kind": "node", "result": "started", "update_id", "from_build", "to_build"}`.
- `agent.responding`: 같은 PC 노드의 `/v1/health`가 답하는지(1초마다 확인). 10초 동안 답이 없으면 노드 서비스를 강제로 다시 시작하고 `restarts`를 올린다(SND-018, 12 4-4). 노드가 답하지 않으면 `build`·`state`는 `null`.

설치 결과(`POST /v1/deployer/install`):
```json
{ "operation": "install", "kind": "module", "id": "iptv", "build": "20260925.1200", "from_build": null,
  "result": "installed", "agent_restarted": true,
  "steps": [ { "name": "verify", "ok": true, "detail": "" } ] }
```
- `result`: `installed`(새로) / `repaired`(같은 빌드, 빠진 것 복원) / `upgraded`(이전 빌드 → 새 빌드). 더 새 빌드가 있으면 거부(INS-026). `steps` 이름: `verify` → `check_idle` → `backup` → `remove_old` → `copy` → `restart_agent` → `cleanup`(06 12장).

제거 결과(`POST /v1/deployer/remove`):
```json
{ "operation": "remove", "kind": "module", "id": "iptv", "dry_run": false,
  "removed": [ "/opt/zeroloss/modules/iptv/module.json" ], "kept": [ "/var/lib/zeroloss/modules/iptv/settings" ],
  "left": [ ], "agent_restarted": true }
```
- `left`가 비어 있지 않으면 실패(INS-021): 남은 것과 이유.

