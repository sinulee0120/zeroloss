# 04. 개발 단계

> 원칙: **꿈은 크게, 1차는 작게.** 단계마다 "돌아가는 것"을 만들고 완료 기준을 통과한 뒤 다음으로 간다.
> 한 단계 = Claude에게 맡기는 작업 묶음. 사용량 한도를 고려해 **한 단계를 여러 세션으로 나눠도 되지만, 단계 안의 순서는 지킨다.**
> 단계를 시작하기 전에 "먼저 정할 것"이 문서에 채워져 있어야 한다(없으면 코딩하지 말고 먼저 정한다).

## 단계 한눈에 보기
| 단계 | 목표 | 결과 |
|------|------|------|
| **P0** | 개발 환경 준비 | 새 컴퓨터에서 빌드·시험이 돌아감 |
| **P1** | 위험 확인(시험 코드) | 설계 전제가 실제로 맞는지 확인(V1~V19, 아래 표 한 곳) |
| **P2 (1차-A)** | 최소 동작 | 송신 1채널 → 공유기 → PC 수신 1대, 손실·지터·5분 칸·보고서 |
| **P3 (1차-B)** | 실제 시험 가능 | 태블릿 앱, 3채널, 장애 처리, 설치·제거·배포 |
| **P4 (1차-C)** | 완성도 | 원인 분석, 채널 전환, 화면 다듬기, 검증 시험 전체, 설명서 |
| P5~ (2차) | 확장 | 원격 지시, 메신저, 추천, throughput 모듈 … |

---

## P0. 개발 환경 준비
**할 일**
- 새 컴퓨터(Controller PC, Windows): Python 3.12, Git, Android Studio(SDK·JDK), Inno Setup, VS Code 또는 Claude Code
- Linux PC(송신·수신): Python 3.12, 빌드 도구
- GitHub 비공개 저장소 `zeroloss` 생성 → 현재 `CLAUDE.md`, `docs/` 올리기
- 저장소 뼈대: `CLAUDE.md` 5장 폴더 구조(core, controller, agent, modules/iptv, android, installer, tools, tests), `VERSION` 생성 스크립트, ruff·mypy·pytest 설정
- GitHub Actions `.github/workflows/ci.yml`: push마다 ruff·mypy·pytest(Linux), 실기·화면 필요 시험은 제외 표시(10 6장)
- **도구 보관소 구성**(`docs/15-tools.md`): `tools/tools-manifest.json`의 도구를 받아 `C:\ZeroLossDepot`에 보관, pinned 버전 확정, verify 통과, Windows·Ubuntu 오프라인 묶음 생성

**완료 기준**
- [x] 빈 Python 패키지들이 import되고 `pytest`가 0개 시험으로 통과 (2026-09-24: 지금은 시험 200개 이상)
- [x] `tools/build`가 `VERSION`(날짜·시각)을 만든다
- [ ] 빈 Android 앱이 Controller PC에서 빌드되어 태블릿에 설치됨 — **Windows PC 도착 후**
- [x] 저장소가 GitHub 비공개에 올라감, 첫 push에서 GitHub Actions 초록 (+ 커밋 전 검사 훅 `tools/githooks`)
- [ ] 도구 보관소의 모든 도구가 verify 통과, 오프라인 묶음 생성 — **Windows PC 도착 후** (고정 버전: TSDuck 3.45-4798, VLC 3.0.23 결정됨)

---

## P1. 위험 확인 (버리는 시험 코드)
> 설계가 기대는 전제 중 **틀리면 구조를 바꿔야 하는 것**을 먼저 실제로 확인한다. 여기 코드는 제품 코드가 아니다(`tests/spikes/`).

| # | 확인 | 문서 | 틀리면 |
|---|------|------|--------|
| V1 | Windows에서 재생기와 분석기가 같은 멀티캐스트 포트 동시 수신 | 05 4.2.3 | 재생기 입력 방식 변경 |
| V2 | Linux 소켓 2개 수신 수 일치 | 05 4.2.3 | 소켓 옵션 |
| V4 | 분석기 켬·끔에 따라 재생 품질(프레임 드롭) 차이가 없는가 | 05 4.2 | 분석 연산 경량화 |
| V5 | 채널 전환 때 IGMP Leave/Join이 실제로 나가는가(패킷 캡처) | 05 4.2.2 | 전환 순서 조정 |
| V7 | TSDuck RTP 타임스탬프 기준 | 05 4.2.3 | 송신 모듈이 직접 RTP 붙임 |
| V8 | 서비스 + Agent Desktop 구조로 재생 창 표시 | 05 4.2.3 | 사용자 세션 실행 방식 |
| V10 | 파일 반복 지점 CC·PCR 이음새(수신 CC·PCR 오류·재생 끊김 0건) | 09 SND-004 | 송출 파이프라인 변경 |
| V11 | 100시간 시험에서 RTP 타임스탬프·PCR 되돌아감 뒤에도 Jitter·PCR 지표 정상 | 09 RUN-001·003 | 되돌아감 처리 수정 |
| M2U-A | Linux 무선 수신에서 목적지 MAC 확인 | 10 4장 | 간접 판단만 |
| NAT | 내장 DHCP로 DUT WAN IP 할당 + NAT 판별 | 07 3~4장 | 고정 IP 기본 |
| V3·V9 | Android 16·17에서 멀티캐스트 UHD 수신 + 포그라운드 서비스 장시간(우선 24시간) | 05 4.5 | 앱 구조 조정 |
| V6 | **VLC(기본)** PCR 시계 복원·장시간(24시간) 재생 이상 0, mpv(옵션)도 같은 시험 | 05 4.6 | 사용자에게 알리고 기본값 재결정 |
| V12 | 자동 생성 영상(`testsrc2`+`sine`+채널 이름) + 프레임 번호 표식 새기기·읽기(사람 숫자 + 막대 코드) — ✅ 2026-09-25 개발 PC 자동 시험(HD·UHD, 작은 캡처 포함, iptv 3.4). 재생 창 캡처에서 읽기는 F23 때 | iptv 1-1, 3.4 | 표식 방식 변경 |
| V13 | 저사양 PC(T460급)에서 VLC(기본) UHD 하드웨어 재생 + 분석기 동시 실행 시 CPU·자체 드롭, 10초 해독 점검 값이 실제 재생 가능 여부와 맞는지 | 05 4.2-1 | 점검 기준(1.5배) 조정 |
| V14 | Android 16·17 `adb shell cmd wifi connect-network`, Windows `netsh wlan`, Linux `nmcli`로 WPA2/WPA3 자동 연결 + Windows 업데이트 일시 중지·원복 방법 | 03 F14, S-51, SYS-005 | Action Required로 사람 처리 |
| V15 | 수신기 IGMP 버전 읽기(Linux `/proc/net/igmp`, Windows `netsh`), 채널 전환 시각 표식(Join·첫 패킷·정상 재생) 측정 | 05 1.6-1, 1.6-2, F19 | 기록 항목 축소 |
| V16 | 링크·Wi-Fi 상태 1초 읽기: Windows `Get-NetAdapter`(링크 속도·끊김), `netsh wlan show interfaces`(채널·BSSID·속도, 대역폭 가능 여부), Android `WifiInfo` + `ScanResult.channelWidth`(대역폭), 읽기 부하가 계측에 영향 없는지 | 05 1.8·1.9, 09 NET-013~017 | 대역폭 못 읽는 OS는 "unknown" 표시·보고서 명시 |
| V17 | Windows에서 한글·공백·기호 파일 이름으로 ffprobe·ffmpeg·tsp·VLC·mpv 실행, cp949 콘솔에서 출력 읽기, 한글 사용자 이름 경로 | iptv 1-1-1, 09 RPT-002 | 영어 준비 파일 이름으로 우회(원본 이름은 기록만) |
| V18 | Windows Controller PC에서 Controller Desktop(`zeroloss-desktop`): `uv run pytest tests/controller_ui`를 offscreen과 실제 화면(`QT_QPA_PLATFORM` 없이) 둘 다 통과, 배율 100/125/150/175/200%·어두운 모드·고대비에서 캡처(11-ui 7), Segoe UI 숫자 폭 고정(tnum), 창 닫아도 서비스·시험 계속(12 1-0) | 11-ui 1.8·1.11·7 | 캡처에서 잘림·겹침을 찾으면 20장에 적고 배치 수정 |
| V19 | 송신·수신 PC 두 대의 시계 기울기(ppm) 실측과 보정 확인: 24시간 시험에서 `sk` 값이 안정한지, 보정 전후 5분 칸 p-p 차이, 대기열 변화가 지워지지 않는지(netem 지연 계단 주입) | 05 2.2, RFC 5481 7 | 기울기가 시간에 따라 크게 변하면(온도 등) 추정 창을 줄이거나 보정을 끄고 보고만 |

**먼저 정할 것:** 송신 명령 구성(ffmpeg·TSDuck 옵션 초안) — ✅ `docs/modules/iptv.md` 1장 작성 완료(2026-09-23). P1에서 S1~S7 확인 후 ? 항목 확정
**완료 기준**
- [ ] 위 항목 결과를 `docs/spikes.md`에 기록(통과/실패/바꾼 결정) — **진행 중**: 송신 S1~S6·반복 이음새(V10 일부) 기록됨, 나머지는 장비 필요
- [ ] 실패한 항목은 관련 문서를 먼저 고친 뒤 P2로

---

## P2. 1차-A: 최소 동작 (MVP)
**범위**
- L0: 없음(수동 실행). L1 Core: 노드 Core 최소(10992 API: health·heartbeat·info), 상태 흐름(IDLE→STARTING→RUNNING→STOPPING), 1초 기록 파일, 5분 요약 수집, 색·판정 틀, 경험 DB 기록 틀
- 공용 블록: `process_runner`, `udp_receiver`, `seq_tracker`, `jitter_calc`, `rate_meter`, `ts_analyzer`(P1만), `recorder`, `summarizer`, `color_judge`, `warmup_gate`
- IPTV 모듈: **사전 준비 파일 필수**(계획 파일 `files`, 02 11-1. 자동 생성은 P3 — 2026-09-24 결정 Q-2, UHD 실시간 생성 불가), 송신 1채널(사전 준비 파일 모드, RTP/MPEG-TS), PC 수신 1대(유선), 분석기 + 재생기(별도 소켓. P2는 노드가 재생기를 직접 띄우고 P3에 Agent Desktop으로 옮김, 20장 GAP-39), 시작·종료 순서와 안정화
- Controller: **지휘는 노드 서비스 안, 화면 앱·CLI는 10994 창구**(12 1-0) — 처음부터 이 구조로 만든다. 트레이 아이콘·알림(11-ui 2-1)은 P3. 메인 화면 최소판(Start/Stop 토글, 장비 2대 상태, Jitter·Loss, 5분 칸), 조작 상태표 함수, 상태 저장소, 결과 팝업, report.html 최소판(판정·5분 칸·구간 보기 없음)
- 진단 로그 회전·설정/DB `schema_version`(02 9장)
- CLI: `status`, `run`, `stop`, `report --json`, `devices add/list/remove`
- **만드는 순서(2026-09-24 결정):** 시험 진행·판정·`summary.json`·report.html 최소판·CLI를 먼저 → 그다음 PySide6 메인 화면

**먼저 정할 것:** 화면 시안 검토(메인 화면) — ✅ 시안(`docs/mockups/`) 검토 후 `11-ui.md` 3장에 반영 완료(2026-09-23)
**완료 기준**
- [x] 합성 패킷 시험(10 1.1) 전부 통과: 손실·중복·순서·늦음·Jitter·되돌아감·중간 가입 (추적표 17장)
- [x] 판정 로직 시험(10 1.2) 통과
- [ ] 실제 공유기로 10분 시험 → 보고서 생성, 결과 팝업 — **새 PC(장비) 올 때로 미룸**(2026-09-25 사용자). 그동안 P3 준비
- [x] netem 손실 0.1% 주입 → 손실 수 정확히 일치(10 1.3) — 2026-09-25 T460 루프백 HD 60초: netem 46개 버림 = ZeroLoss 46개 손실(`tools/netem_check.py`, root로 실행). 버스트·지연·순서·중복 주입은 P3
- [x] Start/Stop 20회 반복 → 시작·종료 오판 0 (2026-09-25, 무작위 대기·연타·정지 중 시작 포함: `tests/iptv/test_run_controller.py::test_start_stop_20_times_without_a_wrong_start_or_stop`)
- [x] 조작 상태표 자동 시험 통과 (`tests/controller_ui/test_control_state.py`, 화면 연동 몽키 `test_main_window.py`)
- [x] Core가 `modules/`를 직접 import하지 않음(자동 검사, Controller·Agent까지 확장)

---

## P3. 1차-B: 실제 시험 가능
> **시작 예외(2026-09-25 사용자 결정):** P2의 "실제 공유기 10분 시험" 하나만 장비 대기로 남기고, 장비 없이 할 수 있는 P3 항목부터 시작한다. 공유기 시험은 장비가 오면 P2 항목으로 따로 끝낸다.
**범위**
- 태블릿 앱(Android): 포그라운드 서비스, 분석기 + 재생기, 1초 기록, 작은 상태 표시, adb 통신(10995)
- 수신 Agent: adb 중계(02 4 `GET /v1/tablets`·`POST /v1/tablets/…`, 2026-09-26 W-6), 태블릿 자동 준비(Wi-Fi 자동 연결 S-51·권한·화면 켜짐)
- 송신 3채널, UHD, 태블릿 1~3대, 무선 PC 수신, M2U 확인(방법 A)
- 장애 처리(F11 heartbeat·정책 ①·Reset All, Controller 재시작 감지 boot_id·남은 시험 정리 CTL-003/008, Deployer의 Agent 감시, 절전·자동 업데이트 막기 SYS-005), 중간 중단·복구 보고서(F10), 파일 마감·복구(F12)
- Agent Desktop 상태 창(F20), Problems 창(F21), Action Required 창(F22)
- 설치·배포: Deployer(10991), Windows 설치 파일, Linux SSH 설치, APK 배포, 제거·업그레이드(06), 모듈 패키지(12 1-3)
- 자동 발견·NAT 판별·내장 DHCP(07), 사전 점검 상태창(F17), 수신 PC 성능 점검·분석기 보호(RCV-015/016, S-49/50), 사용자 영상 파일 목록(S-48)
- 09-exceptions의 이 단계 해당 코드 전부

**먼저 정할 것:** 재생 이상 감지 값 — ✅ `docs/modules/iptv.md` 3장 작성(2026-09-23). P1 V6에서 ? 항목 확정
**완료 기준**
- [ ] 태블릿 3대 × UHD 3채널 × 24시간 시험 완주, 보고서 정상
- [ ] 장애 시험(10 2장·3장 해당 항목) 통과: 프로세스 kill, USB 분리, Controller 강제 종료·재부팅(10초 안/밖), Agent 재부팅
- [ ] 깨끗한 PC에 설치 → 제거 → 흔적 없음, 업그레이드 → 설정 유지, 실패 시 되돌림 — **실제 PC는 실제 Linux PC·새 Windows PC 올 때**(2026-09-25 사용자). 그 전에 패키지·설치 목록·Deployer 논리를 임시 폴더에서 자동 시험(06 12장)
- [ ] 모듈 제거·재설치 반복해도 Core 정상 — 자동 시험 통과(임시 폴더: 10회 반복·무작위 설치 몽키, `tests/agent/test_modules_install.py`, 2026-09-26). 실제 PC 확인은 깨끗한 PC 시험과 함께
- [ ] 1차 사람 할 일이 `00-vision.md` 표 이하로만 남음 — Linux는 표와 같아짐(2026-09-26 대조, 20장 GAP-67: 자동 로그인 자동 설정 추가, 태블릿 설치 허용을 표에 추가). 남은 것: Windows 업데이트 일시 중지·Windows 자동 로그인(V18), 태블릿(P4)
- **첫 실제 장비(2026-09-26):** Linux 송신 PC(192.168.45.58)에 SSH 설치 성공(노드·Deployer·IPTV 모듈 IDLE). 도구 설치·WAN 옮기기·수신 시험은 진행 중. 설치에서 찾은 문제는 20장 GAP-73(사용자 결정 받음: ④는 GAP-75로 반영, ①②③⑤는 아직 코드 없음 — 장비 없이 할 수 있는 P3 일). 통신사 역할은 공유기 WAN 포트에 꽂으면 스스로 켜지고 Controller가 옮긴 주소를 스스로 찾음(GAP-75).
- **미룸(2026-09-26 사용자 결정):** DSC-003(다른 Controller의 Agent 가져오기)·DSC-005(Controller IP 바뀜 → 방화벽 범위 갱신)는 페어링(`POST /v1/pair`)·방화벽 IP 범위 제한을 만드는 단계에서(지금 ufw 규칙은 포트만 엶).
- **중간 점검(2026-09-26, 20장 3절):** 장비 없이 할 수 있는 범위는 끝. 장비 없이 남았던 09 코드(SYS-003·SYS-004·RCV-009·SND-013·DSC-008·SND-014·RCV-004·RCV-006)는 2026-09-26 반영(20장 GAP-70), DSC-003·005는 위 "미룸"

---

## P4. 1차-C: 완성도
> **시작 예외(2026-09-26 사용자 결정):** P3 완료 기준은 모두 장비(태블릿·실제 PC·공유기)가 있어야 하므로 장비 대기로 두고, **장비 없이 되는 P4 항목부터** 시작한다. 첫 순서는 20장 W-6 창구: `diagnostics`·`/v1/logs` → `agent stop`·`/v1/stop` → `storage` → `records` → `verify`(`snapshot`은 F23 증거 수집과 함께, 장비가 있을 때 — 2026-09-27 사용자 결정 20장 GAP-80). 장비가 오면 P3 항목으로 돌아가 끝낸다(P2 → P3 때와 같은 방식).
**범위**
- 원인 분석 규칙(IGMP 주기, 공유기 재부팅, M2U, 수신단 원인 등) ✅ GAP-85, 재생 이상·패킷 이상 대조(05 4.6.2) ✅ R-08, 기준선 시험 모드(4.6.3) ✅ GAP-97(24시간 실측 ⬜)
- 채널 전환 수동·자동(F19, 전환 시간 구간·반응 성공률 S-47) ✅ GAP-90·91, 상시 방송(F18) ✅ GAP-92 — 태블릿 쪽 전환·24시간 실측 ⬜
- 보고서 완성: 구간 보기(F16), 1초 그래프(1초 기록 회수 `GET /v1/records`), PDF, 결과 조회 웹(10993), 결과 보관·정리 화면(F15 Manage storage, CLI `zeroloss storage`) ✅ GAP-79·80·82·86·96. 비교 화면은 2차
- 원격·진단 창구(2026-09-26 사용자 결정 20장 W-6): 재생 화면 캡처 `POST /v1/snapshot`(F23 증거 수집과 함께 만든다, GAP-80), 이 PC만 멈춤 `POST /v1/stop`·`zeroloss agent stop`(Controller 없이), 진단 묶음 `GET /v1/logs`·`zeroloss diagnostics`, 10장 자동 시험 `zeroloss verify` — ✅(snapshot만 ⬜, F23과 함께)
- 화면 다듬기: 11-ui 전 규칙(디자인·대비·해상도·어두운 모드), 도움말 툴팁, Advanced 설정 — 해상도×배율·어두운 모드·도움말 ✅ GAP-84·94·95·96(모니터 이동·흑백·Windows 실기 ⬜)
- 검증 시험 전체(10), 100시간 시험, 사용설명서(08, 화면 자동 캡처 `tools/manual_shots.py` ✅) — 장비 ⬜

**먼저 정할 것:** 원인 분석 규칙표 — ✅ `docs/modules/iptv.md` 2장 작성 완료(2026-09-23)
**완료 기준**
- [ ] 10-verification 전 항목 통과(24시간 기준선, 100시간 안정성 포함)
- [ ] 11-ui 7장 UI 시험 전부 통과
- [ ] 처음 보는 사람이 설명서만으로 설치 → 시험 → 보고서 → 제거 완료
- [ ] **태블릿 3대 × 서로 다른 UHD 채널 × 100시간** 시험 완주(목표 시나리오)

---

## P5~ 2차 (순서는 그때 정함)
| 후보 | 문서 |
|------|------|
| 원격 지시(Claude 경유 → 로그인 웹) | 13 4장 |
| 알림 모듈(메신저·이메일, 승인 답장) | 14 4장 |
| 경험 DB 추천 → 그림자 모드 학습 모델 | 14 2~2-1장 |
| throughput 모듈(유선·무선 처리량, 측정 PC NIC 설정 자동 전환 — 공유기는 안 건드림, 배경 부하 생성에도 사용) | `docs/modules/throughput.md`, 12 2장, 20장 4절 T-1~T-7 |
| **스트레스·과부하 모듈**(점증 부하로 한계·회복 측정, 퍼징, 그룹 수용량) | 10 5-1 |
| **무선 스트레스·성능**(TR-398, M2U 부하) | 10 4·5-1, 05 1.9 |
| 시험 큐, **펌웨어 빌드 비교 화면**(보고서 2개 나란히, iptv 2.2 C-01~C-05) | 00, iptv 2.2 |
| 이벤트 내보내기(외부 로깅 장비·트리거 연동) | 12 4-1 |
| DUT 모듈(읽기 전용): 공유기 로그 동시 기록, 사건 때 공유기 상태 떠 오기(펌웨어 올리기는 하지 않음) | 12 4-2 |
| iPad 앱, YouTube/Netflix형 모듈, RET/FEC 모드 | 01, 05 2.1-1 |
| **전원 제어 모듈**(SCPI 전원 장치·스마트 PDU, PyVISA): PC·공유기 전원 재시작을 사람 할 일에서 자동 행동으로(F22 `human.pc.power_cycle`), 공유기 재부팅 시험 | 19 5절, 00-vision |
| **동시 부하**: TRex(NAT·PPS·세션), Flent(부하 중 지연)와 IPTV를 함께 — 스트레스 모듈의 부하원 | 19 5절, 10 5-1 |
| 검증 도구: Scapy로 IGMP Join/Leave·스누핑 확인과 변형 패킷(개발·검증용, 분리 설치) | 19 5절, 05 1.11 |
| 태블릿 팝업 자동 누르기(uiautomator2, 제조사별 설치 허용 등) | 19 5절, 09 INS-018 |
| **IPv6 멀티캐스트(MLDv1·v2) 시험**(옵션 D21): MAC 33:33 겹침 검사, 범위 ff0X, MLD 스누핑·프록시 원인 규칙, 내장 DHCP 제약(K-27) | 05 1.7, 18 1-1 A |

## 단계 진행 규칙
- 단계 시작 시 Claude에게: "`docs/04-phases.md`의 P?를 진행해. 먼저 정할 것이 채워졌는지 확인하고, 없으면 질문해."
- 단계 중 설계를 바꿔야 하면 **문서를 먼저 고치고** 코드를 고친다.
- 단계가 끝나면: 완료 기준 체크 결과를 이 문서에 표시하고 GitHub에 올린다.
