# 10. 사전 검증 방법 (도구를 믿을 수 있게)

> 목적: 공유기를 시험하기 전에 **ZeroLoss 자체가 맞게 재고, 맞게 판정하고, 예외를 맞게 처리하는지** 먼저 증명한다.
> 원칙: **답을 미리 아는 상황**을 만들어 넣고, 도구가 **정확히 그 답**을 내는지 확인한다.

## 1. 측정 정확도 (품질 결과에 오류가 없는지)

### 1.1 합성 패킷 시험 (자동, 개발 PC에서 매번)
- 시험용 패킷 생성기가 **정해진 문제를 정확한 개수만큼** 넣은 패킷 열을 만들고, 분석기(`core/blocks/` 계측 블록, `modules/iptv/receiver/analyzer.py`)에 넣는다. 네트워크 없이 몇 초 만에 끝난다(pytest).
| 넣는 문제 | 기대 결과 |
|-----------|-----------|
| 손실 7개(흩어짐), 버스트 손실 50개 1회 | lost=57, loss_events=8, max_burst=50 |
| 순서 뒤바뀜, 정렬 창 안에서 도착 | reordered_recovered=N, lost=0, **초록** |
| 정렬 창(1,000 ms)을 넘겨 도착 | late_discarded=N, lost에 포함, **빨강** |
| 중복 | duplicates=N, lost=0 |
| 도착 간격을 정해진 양만큼 흔듦 | RFC 3550 Jitter 값이 계산식과 일치(오차 ±0.1 ms) |
| SSRC 변경·순번 점프(송신 재시작) | 새 구간 처리, 수천 개 손실로 세지 않음 |
| 채널 전환(Leave·Join) | 전환 공백은 손실 아님, 구간별 계산 |
| **RTP 순번·타임스탬프·PCR 되돌아감** | 오류 없음. 시작값을 되돌아가기 직전으로 설정해 **몇 초 안에** 확인(13시간·26시간 기다리지 않음) |
| RTP 없는 모드: CC로 손실, 16개 배수 손실 | CC 기반 손실 수, "한계 있음" 표시 |
| TS 오류(PAT 없음, sync 깨짐, PCR 간격 초과) | 해당 TR 101 290 카운터 증가 |
| 누적 카운터 43억 초과 | 64비트로 정상 |
| RET·FEC 복구(2차) | 원래 손실 N, 복구 후 손실 0 |
| 스트림 중간 가입(첫 순번 50,000), 첫 CC 불연속, 가입 후 PAT 2초 뒤 도착 | 손실·CC 오류·PAT 오류 **0**, Start-up 기록만 |
| 안정화 뒤 첫 손실 1개 | lost=1, 빨강 (안정화 규칙이 문제를 숨기지 않음) |
| 채널 전환 직후 새 채널 첫 몇 패킷 | 오류 0 |
| 종료 절차 | 송출 정지로 인한 손실 0 |

### 1.1-1 실제 문제 캡처 재생 회귀 (자동, 매 커밋, 2026-09-24 채택)
- 실제로 만난 문제(공유기의 순서 뒤바뀜, Wi-Fi 채널 이동 중 덜 온 패킷, 늦은 패킷, 링크 하락, 반복 이음새)의 **받은 패킷을 도착 시각과 함께** 저장해 두고, 매 커밋마다 분석기에 그대로 다시 넣어 결과가 기대값과 같은지 본다. 하드웨어 없이 계측이 몰래 바뀌는 것을 막는다.
- 형식: `tests/corpus/<이름>/packets.pcap`(도착 시각 + UDP 내용) + `expected.json`(1초 기록 요약·5분 요약·색) + `about.md`(무엇을, 어떤 환경에서, 왜 이 값이 맞는지). 증거 캡처(F23)의 패킷 클립도 도착 시각을 가진 pcap으로 저장하므로 그대로 옮겨 쓸 수 있다.
- 크기: 한 건 20 MB 이하만 저장소에(앞뒤를 잘라 문제 구간만), 더 큰 것은 도구 보관소(15장)에 해시와 함께.
- 개인 정보: 우리가 만든 시험 영상만 담긴다. 파일 이름·설명에 공유기 실제 모델·펌웨어·SSID·MAC을 넣지 않는다(MAC은 바꿔 저장).
- 기대값을 바꾸는 커밋은 이유를 `about.md`와 20장에 적는다(측정 정의가 바뀐 경우만).
- 완료 기준(도구는 만들 예정, P3): 재생 도구(`tools/replay_pcap.py`)로 넣은 결과 = 실시간 수신 결과(같은 패킷), 코퍼스 전부 통과, 새 문제 사례는 코퍼스에 추가.

### 1.2 판정 로직 시험 (자동)
- `05-standards.md` 5.3(색)·5.4(판정) 표의 **모든 줄**을 입력 → 기대 색·판정으로 만든 표 기반 시험.
- 예: 자체 드롭 10 + 손실 10 → 보라 / 자체 드롭 5 + 손실 10 → 빨강 / 중간 중단 + 빨강 없음 → INCOMPLETE.

### 1.3 실제 네트워크 문제 주입 (Linux, netem)
- 송신 PC와 수신 장비 사이에 Linux 브리지를 두거나 송신 PC 출구에 `tc netem`으로 **정해진 양의 문제**를 넣는다.
| 주입 | 확인 |
|------|------|
| 손실 0.1%, 버스트 손실 | ZeroLoss 손실 수 = netem이 버린 수(`tc -s` 통계) **정확히 일치** |
| 지연 흔들림 20/80/200 ms | Jitter 값이 기대 범위, 150 ms 넘는 경우만 빨강 |
| 순서 뒤바뀜 | 창 안 정리·늦음 구분 |
| 중복 | 중복 수 일치 |
- 이 시험은 개발 단계별 완료 기준에 넣는다.
- 구현(2026-09-25): `tools/netem_check.py` — netem은 lo의 UDP 5004에만(prio + u32 필터), 측정이 시작된 뒤 켜고 정지 전에 끈 다음 1초 정리 창이 지나도록 기다려 **경계에서 생기는 차이 없이** 비교한다. root 필요(tc), 재생은 끔(VLC는 root 실행 거부).

### 1.4 오탐 없음 (0 손실 기준선)
- 공유기 없이 송신 ↔ 수신 직접 연결로 **24시간**: 손실 0, 보라 0, 재생 이상 0이어야 한다(4.6.3 기준선 시험). 도구가 없는 문제를 만들어 내지 않는다는 증명.

### 1.5 서로 다른 방법끼리 대조
- **송신 수 = 수신 수 + 손실 수**(송신 Agent 기록과 대조)
- Linux 수신 PC: 소켓 분석기 결과 = 패킷 캡처(pcap) 결과
- ZeroLoss 지터 = TSDuck `iat` 플러그인(`-I ip --timestamp-priority kernel-tsp`)의 도착 간격 결과와 비교(허용 오차 안)
- 같은 조건에서 PC 수신과 태블릿 수신 결과 비교

## 2. 송수신 문제 시 처리

| 시험 | 방법 | 기대 |
|------|------|------|
| 송출 프로세스 강제 종료 | 프로세스 kill | SND-005 처리, 끊긴 구간 보라 |
| 송신 NIC 끊김 | 케이블 뽑기 / NIC 끄기 | NET-015(송신 PC), 끊긴 구간(앞 1초~돌아온 뒤 2초)의 수신 손실은 보라, 그 밖은 규칙대로(05 5.3) — 장비 |
| 파일 반복 지점 | 30초짜리 짧은 파일로 1시간 송출 | CC·PCR 오류 0, 재생 끊김 0 (V10) |
| **시작 오판** | Start/Stop을 100회 반복(정상 조건) | 시작·종료 때문에 생긴 빨강·보라 **0회** |
| 채널 전환 오판 | 10초마다 전환 1시간 | 전환 때문에 생긴 빨강 0 |
| 송신 과부하 | CPU 부하 프로그램 동시 실행 | 송신 불안정 표시, 보라 |
| **송신 멈칫·멈춤 주입** | 송출 프로세스를 2초 일시 정지(SIGSTOP/SIGCONT), 송신 NIC에 netem으로 출구 손실 주입 | 수신 쪽 손실이 **보라(송신 원인)**로 분류되고 빨강이 아님. 송신 상태 창·Problems에 표시 |
| 수신 버퍼 넘침 | 수신 버퍼를 일부러 작게 + 부하 | 자체 드롭 → 보라(빨강 아님) |
| 재생기 강제 종료 | 프로세스 kill | 계측 계속, 재생기 재시작, 수신단 원인 분류 |
| **Wi-Fi 채널·대역폭·AP 변경 (태블릿·무선 PC 각각)** | 자동: 가짜 Wi-Fi 상태 공급으로 채널 36→149, 80→40 MHz, 5G→2.4G, BSSID 변경. 수동(사람이 공유기 화면에서 — 공유기 설정은 어떤 모듈도 바꾸지 않음, 12 4-2): 시험 중 채널·대역폭 변경 | 1초 안에 NET-016/017, 장비 줄·Problems·그래프 표식, 보고서 Wi-Fi 이력, 손실이 몰리면 R-20 후보. 유선 수신기에는 표시 없음 |
| **링크 속도 변화 (송신 WAN·유선 수신 각각)** | 시험 중 `ethtool -s <nic> speed 100 duplex full autoneg off`, 10 Mbps, 원래 속도보다 한 단계 낮게(2.5G→1G), 케이블 뽑았다 꽂기 | 1초 안에 감지. 필요 대역 미만 → NET-013 ✖ + Action Required, 충분하지만 낮음 → NET-014 ⚠, 끊김 → NET-015. 보고서 링크 이력·R-19 후보. 판정은 규칙대로 |
| **재생기가 안 켜짐 (PC·태블릿 각각)** | ① 재생기 실행 파일 없음/실행 실패 ② 창을 못 띄움(Desktop 없음) ③ 켜졌지만 첫 프레임 없음(잘못된 주소·코덱) ④ 태블릿: 재생 화면 비활성화 | **시작 실패 + 되돌림**(RCV-019/TAB-015). 분석기 값만 올라와서 초록으로 보이는 일 0 |
| **재생기는 있는데 재생 안 함 (PC·태블릿 각각)** | 시험 중 ① 재생기 일시 정지(SIGSTOP) ② 재생 창 최소화·가림 ③ 태블릿 화면 끄기·다른 앱 전경 | 멈춤 감지 → 장비 줄 "Not playing", Problems, 재시작, 보고서 "재생 없음 hh:mm"(RCV-020/TAB-016). **재생 상태는 재생기 자체 보고로만 정함**(프로세스 존재·분석기 값으로 정하지 않음) |
| **사전 점검 단계별 실패 (F17, 2026-09-25)** | 자동(루프백 노드): ① 도구 없음(`tsp` 없다고 보고) ② 디스크 부족(1.1 GB 남음, 100시간) ③ 다른 빌드·없는 모듈·계획 값 밖 ④ 시험 송출이 다른 그룹으로 감(아무도 못 받음) ⑤ 대기 중 가벼운 점검 ⑥ 시험 중 점검 요청. 실기: 공유기 IGMP 끄기, 수신 PC 시계 5초 틀리기 | ① 2단계 ✖ INS-029 ② 4단계 ✖ RCV-008 + 가능한 최대 시간(짧은 시험은 통과) ③ 1단계 ✖ INS-015 / INS-024 / RUN-014 ④ 5단계 ✖ NET-003 ⑤ 1~4만, 트래픽·시험 폴더·상태 변화 없음 ⑥ RUN-012. 모두 뒤 단계 ⏸, 아무 프로세스도 남지 않음, 화면 Start 회색 + 이유·상태창 Why/Fix. 실기: NET-003 / DSC-009 ⚠(시작은 됨). 시험: `tests/iptv/test_run_controller.py`(F17 절), `test_probe.py`, `tests/controller/test_precheck.py`, `tests/controller_ui/test_main_window.py` |
| **시험망 밖 연결 (NET-018, 20장 W-3, 2026-09-26)** | 자동(순수 함수): ① 송신 PC에 Wi-Fi로 닿음(첫 실제 설치 그대로) ② 시험 선으로 닿지만 다른 Wi-Fi에 주소 ③ 통신사 역할 준비됨 + NAT 없이 닿음 ④ 시험 선만·NAT 거침 = 없음 ⑤ 무선 수신 PC의 시험 Wi-Fi = 없음 ⑥ Controller PC 자신(같은 주소·루프백) = 없음. 실기: 송신 PC Wi-Fi 켠 채 사전 점검 → 3단계 ⚠ | `tests/controller/test_precheck.py::test_other_network_the_first_real_install` |
| **Controller 시험망 카드의 기본 경로 (NET-019, 20장 GAP-73 ⑤, 2026-09-26)** | 자동(가짜 경로 파일·가짜 nmcli): ① 기본 경로 카드 = 가장 낮은 metric의 UP 경로(첫 실제 설치: 시험 선 100이 Wi-Fi 600을 이김) ② 시험망 카드가 기본 경로 + 다른 카드 있음 → ⚠ 한 번(10초마다 점검해도), [Fix] 후보 하나 ③ 카드 하나뿐·장비가 모두 이 PC 안 → 없음 ④ [Fix] = `nmcli` 연결 찾기 → never-default·ignore-auto-dns → reapply, 실패 3가지는 이유와 함께 NET-019, 성공이면 ✔ 사람 ⑤ 정상으로 돌아오면 clears. 실기: 개발 PC에서 Fix 후 인터넷이 Wi-Fi로 돌아옴 | `tests/blocks/test_route_guard.py`, `tests/controller/test_setup.py::test_fix_route_choice_changes_this_pcs_card` |
| **진단 파일 (02 4·2-13, 13 `diagnostics`, 20장 GAP-78, 2026-09-26)** | 자동: ① 비밀번호·키 값은 어느 깊이든 `***` ② 1초 기록(`.jsonl`)·영상·`wifi.json`은 안 들어감 ③ 50 MB 넘으면 새것부터 넣고 나머지는 `left_out` ④ 실제 노드가 zip을 파일로 보냄(그 회차 로그·`state.json`·`manifest.json`), 잘못된 회차는 JSON 오류·빈 파일 없음, 노드 임시 파일 남지 않음 ⑤ CLI `diagnostics`: Controller 묶음 + 장비 묶음 + 상태, 닿지 않는 장비는 사유(DSC-014)만 적고 파일은 만듦, 어디에도 Wi-Fi 암호 없음 | `tests/blocks/test_diag_bundle.py` |
| **이 PC만 멈춤 (02 4 `/v1/stop`, 13 `agent stop`, 09 USR-007, 2026-09-26)** | 자동: ① Controller 없이 이 PC에서 `agent stop` → 모듈이 순서대로 멈추고 IDLE, 가장 최근 회차 폴더의 파일 목록(이름·크기) ② 두 번 눌러도 같은 답(중복 명령) ③ 시험 중 이 PC에서 멈추면 USR-007(warn) 사건이 Controller로 감 ④ Controller가 보낸 `/v1/stop`은 USR-007을 만들지 않음 | `tests/core/test_node_stop.py` |
| **결과 보관·정리 (F15 `storage`, 02 4·2-13, 20장 GAP-79, 2026-09-26)** | 자동: ① 노드 사용량 = 회차별 전체·원자료(`raw/`·`screens/`·`clips/`), 큰 것부터 ② 원자료만 지우면 로그·요약은 남음, 전체 지우면 폴더 없음, 두 번 지워도 같은 결과(0) ③ 노드가 시험 중 쓰는 회차·Controller의 진행 중 회차는 RUN-012, 다른 회차는 지워짐 ④ 잘못된 회차 번호(`../x`)는 DSC-012, 폴더 밖은 손대지 않음 ⑤ CLI `storage list`: 보고서 폴더·Controller 노드·장비를 회차별로 합침, 닿지 않는 장비는 사유(DSC-014) ⑥ `storage delete`는 `--yes` 없으면 종료 코드 4·아무것도 안 지움, 있으면 모든 곳에서 지움 | `tests/blocks/test_run_storage.py` |
| **1초 기록 회수 (02 4 `/v1/records`·2-13, 20장 GAP-80, 2026-09-27)** | 자동: ① 노드 목록 = 회차의 장비·시간별 파일 ② 파일 받기: 내용이 원본과 같고 원본은 남음, 없는 파일 RPT-005, 쓰는 중인 회차 RUN-012, `../` 같은 이름 DSC-012 ③ 실제 루프백 시험이 끝나면 보고서 뒤에 Controller가 스스로 `Reports/<회차>/raw/<장비>/`로 가져옴 ④ `records --run`을 다시 부르면 같은 크기는 `had`(다시 받지 않음) ⑤ 닿지 않는 장비는 `missing`·RPT-005, 나머지는 받음 ⑥ 진행 중 회차 RUN-012 | `tests/iptv/test_records.py`, ③ 실제 시험은 `tests/iptv/test_run_controller.py::test_records_are_fetched_after_the_report`(tsp 필요) |
| **제품 자체 검사 `zeroloss verify` (02 2-13, 13, 20장 GAP-81, 2026-09-27)** | 자동: ① 기본(`accuracy`·`judgement`)이 모두 통과하고 1.1 표·05 5.3·5.4 표의 줄이 모두 들어 있음(모듈 검사 포함) ② 일부러 틀린 검사를 넣으면 그 줄만 `ok: false`·이유, 나머지는 돌고 종료 코드 1 ③ 모르는 스위트 DSC-012, 시험 중 `loopback` RUN-012 ④ `loopback`: 지금 선택을 `seconds` 길이로 줄인 계획으로 시험 한 번 → 수신 장비마다 한 줄(PASS이고 빨강·보라·회색 없음이면 통과, FAIL·INCOMPLETE는 실패), `run_id` ⑤ 합성 생성기와 표(1.1·05 5.3·5.4)는 제품 코드 한 곳(`tests/`는 그것을 씀). 실제 장비로 `verify --suite loopback`은 실기 | `tests/core/test_verify.py`(④는 가짜 시험으로 시작·끝·결과 흐름), `tests/blocks/test_color_judge.py` |
| **보관·진단 화면 (11-ui S14·1.10, 09 SYS-010, 20장 GAP-82, 2026-09-27)** | 자동: ① `/v1/disk` 값 ② 여유 20% 또는 50 GB 미만 → SYS-010 ⚠ 장비마다 한 번, 돌아오면 `clears`, 닿지 않는 장비는 건너뜀, 상태 `storage_low` ③ 조작 상태표 3줄(모든 열) ④ [Storage] 글자가 부족하면 `⚠ Storage low` ⑤ Manage storage: 큰 것부터, 진행 중 회차는 지우기 회색, 확인 창에서 취소하면 아무것도 안 지움, 확인하면 지우고 목록 다시 ⑥ [Export diagnostics] 누르면 `Exporting…` 회색·두 번 눌러도 한 번, 끝나면 경로 ⑦ 몽키(무작위 클릭·연타·대기)에도 화면 스레드에서 네트워크 없음 | `tests/controller/test_storage_watch.py`, `tests/controller_ui/test_storage_window.py`, `tests/controller_ui/test_control_state.py` |
| **Reports 목록 (11-ui S8, 02 2-13 `reports`, 20장 GAP-83, 2026-09-27)** | 자동: ① 최신순, 시작 시각은 회차 번호에서(형식이 다르면 폴더 시각) ② 진행 중 회차는 `Running`, 보고서 없는·깨진 폴더는 RPT-008 이유, 진단 폴더는 빠짐 ③ 창: 판정 색·글자, [Show]는 `report.html`, [Folder]는 폴더, 더블클릭 = Show, 보고서 없는 줄은 [Show] 회색 ④ CLI `reports` ⑤ 몽키(무작위 선택·새로 고침·연타) | `tests/controller/test_reports_list.py`, `tests/controller_ui/test_reports_window.py` |
| **ⓘ 도움말 (11-ui 1.10, 20장 GAP-84, 2026-09-27)** | 자동: 메인 화면(장비 줄·Advanced 펼침 포함)의 **모든 입력 칸**이 대기·시험 중 모두 도움말 툴팁을 가짐, 회색이면 첫 줄이 이유, ⓘ 이름표의 도움말과 같음, 옛 문장("plan file") 없음 | `tests/controller_ui/test_help_tips.py` |
| **원인 분석 규칙 (iptv 2·2.4, 20장 GAP-85, 2026-09-27)** | 자동: ① 규칙마다 맞는 자료·안 맞는 자료(R-01·02·03·04·05·05a·06·07·08·09·10·12·13·14·15·16·18·19·20) ② 자료가 없으면 `analysis_missing`에 이유(무선 없음·수신기 하나·1초 기록 아직·2차) ③ 확신도 높은 것부터, 같으면 먼저 일어난 것부터 ④ 1초 기록의 손실 사건을 회차 시각(`run_s = e + offset`)으로, 차이를 모르면 넣지 않음 ⑤ 두 번째 계산은 분석 칸과 보고서 화면만 고치고 판정 그대로, 새것이 없으면 파일을 건드리지 않음 ⑥ 실제 시험: 회차 `events.log`(`run_s`, 수신 시작 단계), `timeline.json`(수신기 차이), 보고서 때 `report` → 회수 뒤 `records` | `tests/iptv/test_analysis.py`, `tests/controller/test_reanalyze.py`, ⑥ `tests/iptv/test_run_controller.py::test_run_clock_events_timeline_and_both_analysis_passes`(tsp 필요) |
| **보고서 완성: 구간 보기·1초 그래프·PDF·결과 웹 (03 F16, 02 12, 20장 GAP-86, 2026-09-27)** | 자동(브라우저, file://, 서버 없음): ① All·First 1h·Last 1h·Around first red(앞뒤 30분)·Red only ② From/To로 고른 구간만 칸·요약(손실 합·지터)·그래프·원인, 전체 판정은 늘 "(unchanged)" ③ 장비 끄기, 구간이 주소에 남고 그 주소로 열면 같은 구간 ④ 칸 클릭 → 그 5분의 1초 그래프(데이터 파일), 1초 기록이 없는 시간은 "not here yet" ⑤ CSV ⑥ 100시간·3대 보고서 5초 안에 열림. PDF: 구조(교차 참조)가 맞고 글자가 읽힘(pdftotext), 길면 여러 쪽, 모든 보고서에 있음. 결과 웹: 목록·`/live`·보고서 파일, 폴더 밖·숨김·zip 404, POST 405, S-44 끔·이 PC·같은 대역, 시험 중 바이트 기록·유선 안내, 포트 사용 중이면 CTL-006(시험은 계속) | `tests/controller/test_report_page.py`(Playwright·Chromium이 있을 때), `test_pdf.py`, `test_web.py` |
| **무선 수신 PC (03 F17 "무선 수신 PC", 2026-09-25)** | 자동(가짜 NetworkManager, Wi-Fi 주소 = 루프백): ① S-51 없음·Wi-Fi 없음 ② 대기 중 점검 ③ Start ④ 이미 다른 Wi-Fi(S-51 없음 / 있음) ⑤ 시험 중 join 요청 ⑥ nmcli 실패·주소 없음·카드 없음·다른 OS ⑦ 비밀번호: 명령줄·파일·이벤트·보고서·CLI 출력 어디에도 없음 ⑧ M2U 프레임 판정(자기 MAC / 01:00:5e / 섞임 / 권한 없음). 실기(V14): 실제 공유기 Wi-Fi에 연결·재연결·재부팅 뒤 자동 연결, 유선 제어 연결 유지(never-default), M2U 켠/끈 공유기에서 active / not active | ① 3단계 ✖ RCV-023 ② 연결 안 하고 ⚠ "connects at Start" ③ 연결 → 주소가 가입 NIC, 보고서 `wifi_auto_connected: true` ④ ⚠ 현재 Wi-Fi 사용 / S-51로 새로 연결 ⑤ RUN-012 ⑥ RCV-023(비밀번호 가림 ***) ⑦ 없음 ⑧ active / not_active / unknown + 이유, 유선은 `null`. 시험: `tests/iptv/test_run_controller.py`(Wi-Fi 절), `tests/core/test_wifi_join.py`, `tests/core/test_m2u_check.py`, `tests/controller/test_wifi_store.py` |
| **프라임 방식 (iptv 4.5, S-18, 2026-09-25)** | 자동(루프백, NAT 없음): ① 송신 흐름이 프라임 전에는 안 보냄·다른 run의 프라임 무시·프라임 출발 주소로 P에서 보냄·프라임 끊기면 멈춤 ② S-18 On 시험 전체(사전 점검·시작·정지·보고서) ③ Auto + 가짜 NAT + 멀티캐스트 막힘 → 프라임으로 바꿈 ④ Auto + NAT 없음 → 바꾸지 않음 ⑤ 재생 켬 + raw 소켓 권한 없음 ⑥ raw 프라임 패킷 체크섬·출발 포트(권한 있을 때). 실기(P1 ③): SDK 공유기 NAT에서 흐름 도착·NAT 종류(포트 유지·걸러내기)·UDP 만료, 상용 펌웨어에서 Auto가 멀티캐스트로 남음 | ① SND-021 ✖ 뒤 멈춤, 손실 0 ② `dut.multicast_path: primed`, 체크리스트 5단계 "primed unicast … not the real IPTV path" ③ "no multicast came through", `mode_detected: NAT` ④ NET-003 ⑤ RCV-024, 남는 프로세스 없음 ⑥ 체크섬 0, 출발 포트 = 재생기 포트(재생기 소켓은 그대로). 시험: `tests/iptv/test_prime.py`, `tests/iptv/test_run_controller.py`(프라임 절) |
| **장애 안내·Reset All (F11 S6, 2026-09-25)** | 자동: ① 시험 중 노드 사라짐 → FAULT, `fault`(장비·마지막 응답) ② 그 상태에서 Reset All → 답 없음·FAULT 그대로·할 일 ③ 장비를 빼고 Reset All → IDLE ④ 노드 `/v1/reset`: 대기 노드 깨끗, 크래시가 남긴 기록 정지·셈, 모듈이 아직 돌면 깨끗하지 않음, 노드 자신의 `iw event`는 건드리지 않음(회귀) ⑤ Deployer `node` restart/stop/start, 멈춘 노드를 감시가 다시 켜지 않음, 시험 중 거부 ⑥ 화면 S6: 원인·장비 줄·자료, 확인 뒤 한 번만 보냄, 답 기다리는 동안 셋 다 회색 ⑦ CLI `--yes`·종료 코드. 실기: 10 2장 장애 시험 뒤 Reset All로 남은 프로세스·포트 없음(03 F11 완료 기준) | ① ~ ⑦ 시험: `tests/iptv/test_run_controller.py::test_lost_agent_safe_stops_to_fault_then_reset`, `tests/core/test_node_reset.py`, `tests/agent/test_deployer_service.py::test_node_restart_stop_start`, `tests/controller_ui/test_main_window.py::test_fault_screen_says_what_stopped_and_offers_reset`, `test_control_state.py`, `tests/iptv/test_cli.py::test_reset_and_nodes_from_the_cli` |
| **자동 발견 (06 10.2, 2026-09-26)** | 자동(루프백): ① 요청 형식·잘못된 요청 무시 ② 노드는 IDLE에서만 답함(시험 중 조용) ③ 방송·송신 주소 시도·범위 스캔을 한 줄로 합침, 등록된 장비 표시·이름 제안 ④ 아무도 없음·송신 주소 없음 코드 ⑤ 너무 넓은/틀린 범위 거부 ⑥ CLI. 실기: DUT LAN 쪽 방송으로 수신 PC 찾기, NAT 모드에서 송신 PC는 방송으로 안 보이고 10.0.0.x 시도로 찾음, ufw UDP 10992 | ② 시험 중 0개 ④ DSC-001·DSC-010 ⑤ RUN-014. 시험: `tests/core/test_discovery.py`, `tests/iptv/test_cli.py::test_discover_from_the_cli`, `tests/agent/test_nodesetup.py` |
| **송신 PC 통신사 역할·내장 DHCP (07 3.2 B, 2026-09-26)** | 자동(루프백 높은 포트, 가짜 NIC·가짜 `ip`): ① DISCOVER→OFFER→REQUEST→ACK, 옵션(마스크·게이트웨이·임대, DNS 없음) ② 주소는 하나: 다른 MAC은 답 없음·NAK, 다른 서버 선택은 무시, 갱신은 같은 주소, RELEASE 뒤 비움, 재시작 뒤 같은 임대 ③ 자기 NIC MAC은 절대 안 줌 ④ 다른 DHCP 서버 찾기 ⑤ 역할: 비어 있는 유선 NIC 하나 고르기·주소 더하기·끄면 빼기·노드 재시작에도 유지·후보 둘이면 기다림(DSC-006 한 번)·다른 서버면 시작 안 함(SND-012)·케이블 옮기면 스스로 계속(SND-022)·요청 없음 경고(SND-011) ⑤-1 **자동(GAP-75):** 20초 주소 없음 + DHCP 답 없음이면 스스로 켬, 그 전·DHCP가 답하는 LAN·주소 있는 NIC·Controller PC는 그대로(조용히), 후보 둘이면 DSC-006 한 번, **10.0.0.1을 다른 PC가 쓰거나 같은 순간에 묻으면(ARP) 붙이지 않고 무작위 뒤 다시·ARP 확인 못 하면 켜지 않음·재시작 뒤 다른 PC가 맡았으면 우리 주소를 뺌(GAP-76 ⑤)**, 링크 끊기면 바로 응답 멈춤, 다시 올라와 DHCP가 답하면 더한 주소 빼고 끔, `--auto`로 되돌림 ⑤-1-0 **WAN 카드 고르기(W-1):** 빈 유선 카드 둘 → DSC-006에 카드마다 `{id, label}`(이름·속도·MAC, 속도 모름은 "speed ?") → Action Required 줄에 후보, 표에서 고르면 버튼, 누르면 `actions/choose`, 후보 밖·모르는 줄은 거부, `ready`면 ✔ 사람, 충돌이면 남음(`tests/core/test_dhcp_server.py::test_two_cards_ask_with_one_choice_per_card`, `tests/controller/test_actions.py::test_choices_come_from_the_event_and_are_kept_clean`, `tests/controller/test_setup.py::test_a_choice_runs_only_what_the_action_offered`, `tests/controller_ui/test_main_window.py::test_action_with_choices_shows_one_button_each`) ⑤-1-1 **케이블 옮기기 안내(W-2):** WAN 쪽 PC 없음 + 송신 안 고름 → SND-022 한 번(장비 이름 없음, 10초마다 봐도 한 번), WAN 쪽 PC가 생기면 문제·할 일 모두 닫힘, LAN 송신을 고른 비교 시험·장비 없음은 알리지 않음(`tests/controller/test_setup.py::test_no_wan_side_pc_asks_to_move_the_cable_once_and_closes_itself`) ⑤-2 **옮긴 장비 주소(GAP-73 ④):** 옛 주소가 답하지 않고 송신 권장 주소에서 같은 `device_id`가 답하면 저장·사건, 옛 주소도 답하면 그대로 ⑥ 패키지 제거 때 더한 주소만 뺌 ⑦ 같은 대역 점검(SND-010)·방식 기록. 실기: 공장 설정 공유기 WAN이 10.0.0.100을 받아 감, 재부팅 뒤 같은 주소, NetworkManager가 더한 주소를 지우지 않는지, 처음 LAN 설치 → WAN으로 옮김 | ① ~ ⑦ 시험: `tests/core/test_dhcp_server.py`(⑤-1 `test_auto_*`·`test_link_lost_*`), `tests/core/test_discovery.py`(⑤-2 `test_moved_*`·`test_no_move_*`), `tests/agent/test_nodesetup.py`, `tests/iptv/test_run_controller.py::test_router_wan_outside_the_sender_subnet_is_snd_010` |
| **수신 PC 성능·분석기 보호 (RCV-015/016, 05 4.2-1, 2026-09-26)** | 자동(루프백): ① 첫 점검 = 해독 시험 영상 만드는 중 ⚠(막지 않음) ② 만든 뒤 10초 해독 속도·대기 중 점검은 저장값만 ③ ffmpeg 속도·vainfo 해석, 느림·하드웨어 해독 없음·CPU 바쁨 문구 ④ 안전 정지 때 만들던 영상 멈춤·남는 것 없음 ⑤ CPU 97% 10초 넘음 → 재생기 nice ≥ 10, RCV-016. 실기: T460 UHD HEVC(05 4.2-1 실측 표)로 ⚠가 나오는지, Windows(V18) | ① ~ ⑤ 시험: `tests/iptv/test_run_controller.py::test_pc_speed_check_makes_its_sample_once_then_measures`·`test_player_gives_way_when_the_pc_is_saturated`, `tests/iptv/test_perf.py` |
| **사용자 영상 S-48 (iptv 1-1, 2026-09-26)** | 자동(루프백): ① 미디어 폴더의 한글 이름·소리 없는·HD보다 작은 파일 → 채널 영상으로 한 번 만듦(`_f<요약>` 이름, 설명에 원래 이름·`upscaled`, 채널 음 넣음) → 송출(`source: media`) → 보고서 `source`에 원래 이름 ② 없는 파일 → 자동 생성으로 대체·SND-001·`source_fallback` ③ 계획의 경로·`..`·없는 채널·모양 틀림 → RUN-014 ④ 목록: 영상 아닌 파일 빼기, 못 읽는 파일 `error`, 원본이 바뀌면 요약이 바뀜 ⑤ CLI `sources`. 실기: 실제 UHD·HD 영화 파일, Windows 미디어 폴더 | ① ~ ⑤ 시험: `tests/iptv/test_run_controller.py`(S-48 절), `tests/iptv/test_sources.py`, `tests/iptv/test_cli.py::test_sources_from_the_cli` |
| **F14 설정 화면 (2026-09-26)** | 자동(화면 오프스크린): ① 선택을 읽기 전 Start 회색 + 이유 ② 선택 보이기·바꾸면 바로 보이고 저장 명령·시험 중 바꾸기 거부 ③ 역할이 애매하면 Start 회색 + 이유, 장비 줄에서 송신 고르기·유선/무선 ④ Advanced: 설정 자료로 칸 만들기·범위·회색 + 이유(값과 함께)·바꾼 개수·Tablet 선택지 회색·시험 중 잠김 ⑤ 몽키(무작위 바꾸기·선택 답·시험 상태 섞기)에서 모든 요소가 1.10 표와 같음 ⑥ 실제 서비스에 붙어 "choose the sender" 표시 ⑦ 설정 자료 = 16장 표(이름·위치·잠금·범위·선택지), Advanced 묶음당 7개 이하 ⑧ 시험 중에는 화면 선택 읽기(장비마다 묻기)를 하지 않음. 캡처 검토(1.11, 1366×768·1920×1080·150%·4K·좁은 1024×600): 잘림·겹침 없음, 좁은 배치에서 장비 줄이 보임 | 시험: `tests/controller_ui/test_main_window.py`, `test_store.py`, `tests/test_settings_schema.py`, `tests/controller/test_setup.py`, `tests/iptv/test_cli.py::test_run_from_the_saved_choice` |
| **F19 채널 전환·F18 상시 방송 (GAP-87 기획 2026-09-27, 구현 2026-10-01~02 GAP-90~92)** | 자동(루프백·가짜 수신기): ① 수신기 전환: 분석 소켓과 재생기가 함께 옮겨지고 새 `join_segment`마다 분석이 새로 시작, 안정화 초는 1초 기록에 안 씀, 5분 칸이 그 순간 구간별로 닫힘 ② `<회차>_zaps.jsonl` 한 줄(경과 초·`t_normal_play`·`basis`·`leave_check`), 같은 채널·RUNNING 아님·자동 켜짐이면 거부 ③ 첫 패킷 5초 안에 없음 → NET-003(빨강), Leave 뒤 이전 그룹이 계속 옴 → NET-005(노랑), 원시 소켓 권한 없음 → `not_checked`(노랑 아님) ④ 가입 방식 A/B를 Controller가 정함 ⑤ 자동 S-21·S-22: 계획의 다음 채널 순환·수신기 사이 간격·멈추면 타이머도 멈춤·전환 공백이 손실로 안 잡힘 ⑥ 보고서 `channel_change`(`join_failures`·`leave_not_processed`)·5분 요약 `channel` ⑦ F18: Always-on이면 Start가 이미 도는 송출을 건너뜀(다른 채널·화질이면 RUN-014)·시험 Stop은 수신기만 멈춤·`broadcast stop`·시험 중 RUN-012·방송 중 idle heartbeat·Controller 끊기면 방송 멈춤·Reset All이 방송 끔·상태 `broadcast`·보고서 `sender_mode`·`broadcast_since` ⑧ CLI `channel`·`broadcast stop`. 실기: 태블릿(F19 태블릿 쪽), 공유기 IGMP 탈퇴 처리(NET-005), 24시간 자동 전환 | ✅ 자동 ①~⑧(실기 ⬜): `tests/iptv/test_receiver_switch.py`(수신기 쪽), `tests/blocks/test_leave_watch.py`, `tests/blocks/test_warmup_gate.py`(강제 시작), `tests/iptv/test_zap.py`(전환 줄·키·5분 요약 `channel`), `tests/iptv/test_module_api.py::test_channel_change_over_http`(경로·`slot`·기록 목록), 가입 실패·Leave 미처리 칸 색은 `modules/iptv/self_checks.py` 표(`tests/core/test_verify.py`가 줄마다 돌림), `tests/controller/test_channel.py`, `tests/controller/test_broadcast.py`(방송이 시험 뒤에도 돌고 idle heartbeat로 유지·같은 방송이면 송출·5단계 건너뜀·다르면 RUN-014이고 방송은 그대로·Stop broadcast·Reset All·Controller 침묵이면 꺼짐), `tests/controller_ui/test_main_window.py::test_broadcast_banner_and_stop_broadcast` |
| 시계 변경 | 시험 중 PC 시각 변경 | 간격 계산 영향 없음(TIM-001) |
| **Controller 화면 앱 장애** | 시험 중 화면 앱 강제 종료·창 닫기·로그아웃 | 시험 계속, 다시 열면 같은 상태(CTL-002/009). 서비스 계정으로 보고서 폴더 쓰기(RPT-001) |
| **Controller 서비스 장애** | 강제 종료·재부팅(10초 안/밖 복귀) | 모든 Agent 정지, 새 Controller가 남은 시험 정지·자료 회수(CTL-003, CTL-008) |

## 3. 예외 처리 (`09-exceptions.md` 전체)
- **모든 오류 코드에 시험이 최소 1개** 있어야 한다. 방법은 세 가지:
| 방법 | 대상 | 예 |
|------|------|-----|
| **가짜 장비(시뮬레이터)** | 대부분의 예외. 개발 PC 한 대에서 자동 | 가짜 Agent·태블릿·재생기를 명령으로 "죽기, 멈추기, 오류 내기, 늦게 답하기, IP 바꾸기" 시킴 |
| **오류 주입 스위치** | 실제 코드 경로 | 시험용 빌드에서만 켜지는 환경 변수로 특정 지점에 오류 발생(예: 파일 쓰기 실패, 디스크 부족, 메모리 부족 보고) |
| **실기 체크리스트** | 사람 손이 필요한 것 | 케이블 뽑기, USB 분리, 공유기 재부팅, 전원 차단 |
- 자동 검사: 문서의 오류 코드 목록과 코드 안의 오류 코드 목록이 **같은지** 스크립트로 확인(빠진 코드·안 쓰는 코드 찾기).
- 각 시험은 **감지 → 처리 → 안내 문구 → 기록 → 색·판정** 5가지를 모두 확인한다.

## 4. 무선 M2U 전달 확인
M2U가 켜진 공유기는 무선으로 멀티캐스트를 **수신기마다 유니캐스트로 바꿔** 보낸다. **유선 포트에는 M2U가 없고 IGMP 스누핑만 한다**(2026-09-25 사용자) — 그래서 이 확인은 무선 수신기만 대상이다. IP 주소(239.x.x.x)는 그대로라 IP만 봐서는 구분이 안 되고, **무선 구간의 목적지 MAC 주소**를 봐야 한다.

| 방법 | 어디서 | 판단 | 단계 |
|------|--------|------|------|
| **A. 수신 PC(Linux) 무선 수신 시 MAC 확인** | 무선 PC 수신 시험 | 받은 영상 패킷의 목적지 MAC이 **자기 MAC(유니캐스트) → M2U 동작** / **01:00:5e:… (멀티캐스트) → M2U 안 됨**. 패킷 캡처 권한 필요(CAP_NET_RAW) | **1차** |
| B. 간접 판단 | 태블릿(루트 권한 없어 MAC 확인 불가) | 신호가 좋은데(RSSI 양호) UHD 여러 대에서 손실이 크면 "M2U 미동작 의심". A 결과와 함께 원인 분석 | 1차 |
| C. 무선 모니터 모드 캡처 | 모니터 모드를 지원하는 무선 카드가 있는 Linux PC | 공중의 무선 프레임을 직접 봄: 목적지 MAC, 전송 속도, 재전송 수 | 2차(옵션) |

- 보고서에 "M2U: active / not active / unknown(tablet only)"와 판단 근거를 적는다.
- 사전 점검(F17)에 **무선 수신 시 M2U 확인 단계**를 둔다(방법 A가 가능한 경우).
- 공유기에 M2U를 켰는데 결과가 "not active"면 노랑 경고와 함께 원인 분석에 적는다.

## 4-1. 모듈 설치·제거 독립성
| 시험 | 기대 |
|------|------|
| 모듈 제거 → Core만으로 실행 | Core 정상, 모듈 목록에 "none", 지난 보고서 열림 |
| 모듈 A 제거·재설치 10회 반복 | 매번 흔적 없이 제거, 재설치 후 설정 복원, 모듈 B 영향 없음 |
| 공유 도구(adb)를 쓰는 모듈 둘 중 하나 제거 | adb 남아 있음 |
| Core 버전 범위 밖 모듈 설치 | 그 모듈만 비활성 + 이유 표시 |
| 시험 중 모듈 제거 요청 | 거부 |
| **자동 로그인(2026-09-26, 00-vision·06 11.3 4-2)** | 켜기 → GDM이 보는 값 = 그 계정, 두 번 켜도 한 번, 제거 → 원래 파일과 바이트까지 같음, 그 사이 사람이 바꿨으면 우리 줄만 빠지고 그 사람 설정이 살아남, [daemon] 없음·GDM 없음·root·나쁜 이름, 무작위 파일 200가지(몽키), SSH 설치에서 요청했을 때만·비밀번호는 sudo 입력으로만. `tests/agent/test_auto_login.py`, `test_ssh_install.py` |
| **첫 실제 설치에서 나온 것(2026-09-26, GAP-73)** | SSH 설치가 빠진 도구를 같은 sudo로 설치(apt + TSDuck .deb 전달), 인터넷 없으면 알리고 설치는 계속 / 발견이 같은 장비를 새 주소에서 찾으면 등록 주소 자동 수정 / Controller 인터넷 경로가 시험망 NIC면 ⚠ DSC-015 / .deb에 `libxcb-cursor0` 권장. 실제 송신 PC(HP ProBook 4330s)로 확인 | `tests/controller/test_ssh_install.py`, `tests/core/test_discovery.py`, `tests/controller/test_test_nic_route.py` |
| **09 코드 P3 마무리(2026-09-26, GAP-70)** | SYS-003 CPU 90% 10초 넘음 → 경고(상위 프로그램)·풀림, 겹친 칸 노랑 / SYS-004 빈 메모리 10% ⚠·5% 시작 차단 / RCV-009 쓰기 실패 → 새 파일, 또 실패 → 시험 정지 `file_write_failed` / SND-013 ARP 탐색(RFC 5227) 충돌 → 차단, 권한 없으면 "점검 못 함" / DSC-008 발견은 되는데 TCP 막힘 / SND-014 주소 빼기 실패 → 한 번 더 → `error` + 명령 / RCV-004 창 닫힘(종료 코드 0) ≠ 죽음 / RCV-006 화면 잠김 막기·알림·노랑(실제 세션 D-Bus로 확인). `tests/blocks/test_cpu_watch.py`, `test_arp_probe.py`, `test_rate_and_recorder.py`, `tests/agent/test_screen_guard.py`, `tests/iptv/test_trouble_windows.py`, `test_run_controller.py` |
| **절전·자동 업데이트 막기(2026-09-26, 09 SYS-005 Linux)** | 가짜 systemd-inhibit: 노드가 STARTING~STOPPING 동안만 잡고 IDLE이면 놓음(노드에 묶임, 시험 프로그램으로 안 셈), 거절되면 SYS-005 경고·4단계 ⚠(시험은 계속). Deployer: 노드가 시험 중이면 apt 멈춤 파일, IDLE 5초 뒤 지움, 시작·제거 때 남은 것 지움, 무작위 상태 500번(몽키). 실제 PC에서 절전·자동 재부팅이 정말 막히는지는 실기. `tests/core/test_sleep_guard.py` |
| **Agent Desktop·재생기 맡기기(2026-09-26, 03 F20)** | 실제 노드(`player_host=desktop`) + Desktop 연결부: 목록대로 켜고 끔, 노드 3초 무응답·노드 재시작이면 재생기 끔, 두 번째 Desktop 거절, 모르는 작업은 FAULT로 알림. 원격 재생기: 보고 없으면 절대 "ok" 아님(무작위 40가지). 실제 VLC로 시험 전체 PASS(재생기는 Desktop이 띄움). Desktop 없음 → 4단계에서 시작 실패 RCV-010, 로그인하면 대기 점검이 clears를 내서 Action Required 자동 완료. 상태 창 8가지 상태 캡처·9 pt·넘침·닫기 = 숨김·[Details] 한 초 한 줄. `tests/agent/test_desktop.py`, `test_desktop_window.py` |
| **노드 패키지 업데이트(2026-09-26, 06 11.5 Linux)** | 가짜 설치 작업으로: 성공 → `done`·`current.deb` = 새 것 / dpkg 실패·전원 차단·새 빌드 무응답 → 이전 .deb로 `rolled_back` / 되돌림도 실패·이전 사본 없음 → `failed`. 어느 경우든 `packages/`에 `current.deb`만, `incoming/` 비어 있음. 작업 도중 Deployer가 멈췄다 새로 켜져도 Controller가 끝까지 기다림. 무작위 결과 60가지(몽키). 설치 작업 셸 스크립트는 가짜 dpkg·systemctl로 실제 실행(잠금 재시도, 실패해도 두 서비스 다시 켬). `tests/agent/test_node_update.py` |

## 4-2. 몽키(섞어 누르기) 회귀 — 모든 화면·설치·노드
> 근거와 방법: `docs/playbook/verification-playbook.md`. 시안 단계에서 이미 버그 3건을 찾았다(버튼 상태 초기화, 시험 중 열린 설정 창, 연타로 중복 추가).

| 대상 | 흔드는 방법 | 불변 조건 |
|------|-------------|-----------|
| Controller 화면 | 무작위 클릭 + 같은 버튼 2~5번 연타 + 0.75~1.5초 대기 + 상태 강제 전환(장애 주입) | 조작 상태표 일치, 모달 ≤1, 같은 작업 1개, 타이머 수, 대기 아닐 때 설정 창 닫힘, 장비 줄 = 시험 종류 |
| 보고서(HTML) | 구간 버튼·체크박스·끌기·칸 클릭 무작위 | 요약 = 다시 계산 값, 선택 칸 보임, 격자가 화면 폭 안 |
| Agent 상태 창·태블릿 앱 | 화면 회전, 앱 전환, 연타, USB 분리 | 재생·계측 계속, 상태 표시 = 실제 |
| 설치·제거·업데이트 | 중간 취소, 잘못된 입력 반복, 두 번 실행, 전원 차단, 제거 중 설치 | "설치 전과 같음" 또는 "완전히 설치됨"만 허용 |
| 노드 간 | 무작위 kill·지연·끊김·재부팅 | 서로 살아 있음 확인, 상태 일치(N초 안), 안전 정지·원복 |
- 고정 시드 목록 + 매번 새 시드. **실패한 시드는 고정 회귀 시험으로 추가.**
- 시안용 스크립트: `docs/mockups/fuzz-main.js`, `docs/mockups/fuzz-report.js` (실제 앱에서는 pytest-qt·Playwright·adb monkey로 옮긴다).

## 5. 장시간 안정성
| 시험 | 기준 |
|------|------|
| 24시간 → 100시간 연속 실행 | 멈춤 없음, 파일 정상, 보고서 5초 안에 열림 |
| 메모리 증가 | ZeroLoss 프로세스 메모리가 시작 대비 계속 늘지 않음(SYS-002) |
| 태블릿 100시간 | 앱이 시스템에 의해 종료되지 않음(V9) |

## 5-1. 스트레스·과부하 시험 (2026-09-24 추가, 근거 18장 9절·19장 4절)
> **의도(오해 없도록, 00장):** 격리된 시험망 안에서 **내가 검증하려는 내 장비(DUT)에만** 부하를 건다. 남의 장비·서비스·네트워크를 대상으로 하지 않는다. 공개된 장비 검증 표준(ISA EDSA-401, RFC 3918)을 따른다.
> 목적: **일부러 감당하기 어려운 부하를 걸어** 공유기가 그 조건에서도 IPTV를 손실 없이 전달하는지, 아니면 손실·끊김·재부팅·행(hang)·오동작 같은 결함이 나오는지 재현한다. 이건 정상 장비 검증 방법이다(ISA EDSA-401 3단계 부하 스트레스, RFC 2544 과부하, RFC 3918 처리량·수용량). 결함을 만들어 **한계를 찾는 것**이 목적이므로 판정 방식이 일반 시험과 다르다.

**3단계 (EDSA-401 구조를 따름)**
| 단계 | 내용 | 판정 |
|------|------|------|
| ① 기준선 | 스트레스 없이 정상 IPTV. 손실 0 확인(일반 시험과 같음) | 손실 0 아니면 스트레스 이전에 이미 문제 |
| ② 점증 부하 | 배경 트래픽·동시 연결·채널 수·비트레이트를 단계적으로 올림 | 각 단계에서 손실·끊김이 처음 나타나는 지점을 기록(= 한계값) |
| ③ 과부하 | 링크 한계까지 밀어붙임 + 잘못된/폭주 트래픽 | 재부팅·행·글리치·오동작이 나오는지, 부하가 사라진 뒤 **스스로 회복**하는지 |

**부하 종류 (배경 트래픽은 시험 트래픽과 별개 경로·별개 카운터)**
| 종류 | 방법 | 근거 |
|------|------|------|
| 높은 비트레이트 영상 | 실제 UHD 영상(예: 45 Mbps 이상)으로 인코딩 부담·대역을 키움 | 19장 4절 |
| 배경 데이터 트래픽 | IPTV 재생 중 동시 TCP/UDP 연결(iperf3 등)로 CPU·NAT 압박 | soar.name 실측(19장) |
| 많은 동시 연결 | 10·100·1000 동시 연결로 연결 추적표 압박 | 〃 |
| 멀티캐스트 그룹 수·수용량 | 그룹을 늘려 가며 전달 한계 측정 | RFC 3918 7.1 그룹 수용량, 4.3 총 처리량 |
| 채널 전환 폭주 | 빠른 Join/Leave 반복으로 IGMP 처리 압박 | RFC 3918 6장 |
| 잘못된/기형 프레임(퍼징) | 규격을 어긴 프레임을 섞어 넣음 | ISA EDSA-401 2단계(2차) |
| 저속 링크 강제 | 100 Mbps 포트로 강제해 병목 | soar.name(대부분 100 Mbps 가입) |

**결과·회복 (핵심)**
- 스트레스로 유발한 손실·끊김은 "한계 자료"로 기록하고, **버틴 최대 부하**를 보고서에 적는다.
- 심각한 결함(재부팅 의심 NET-001, 응답 없음, 오동작)을 감지하면 사건으로 남기고 그 순간 증거(캡처·로그)를 모은다(F23).
- **회복 확인:** 부하를 제거한 뒤 정해진 시간 안에 손실 0으로 돌아오면 "회복함", 아니면 "회복 못 함(수동 재부팅 필요)"으로 기록.
- 판정 표시: 스트레스 시험은 PASS/FAIL 대신 **"버틴 한계값 + 회복 여부"**를 결과로 준다(고정 손실 0 기준은 ① 기준선 단계에만 적용).

**범위: 전부 2차(스트레스 모듈, 2026-09-24 결정).** 1차에는 스트레스 기능·설정·판정 형식을 넣지 않는다. 2차에 03·16·02·05장(결과 형식이 판정 원본 5.3~5.4와 맞는지 포함)을 먼저 정한 뒤 만든다. 무선은 4장(M2U)·TR-398과 함께 다룬다.

## 6. 언제 돌리나
- **실행 위치:** 전체 시험은 Controller PC에서(`zeroloss verify` = pytest 전체 + 몽키 회귀 + 화면 캡처 검사). GitHub Actions(`.github/workflows/ci.yml`, P0에서 만듦)는 push마다 Linux에서 ruff·mypy·단위 시험·시뮬레이터 시험·고정 시드 몽키를 돌린다(실기·화면 필요한 시험은 제외 표시). 비공개 저장소 무료 한도(월 2,000분) 안에서 쓴다.
| 시점 | 돌리는 시험 |
|------|-------------|
| 코드 수정할 때마다 | 1.1, 1.2, 3(시뮬레이터) — 자동, 몇 분 안 |
| 개발 단계 완료 시 | 1.3, 2, 3(실기 체크리스트 중 해당 부분) |
| 배포 전 | 1.4(24시간), 4, 5 |
