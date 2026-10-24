# 16. 설정 목록과 조합 규칙 (Settings catalog)

> 목적: 기본값뿐 아니라 **모든 옵션**을 한곳에 모으고, **옵션끼리 섞였을 때 생기는 문제**를 미리 막는다.
> 코드의 `defaults.json`(02-data 11장)은 이 표에서 만든다. 표에 없는 설정은 코드에 만들지 않는다. 화면 이름·위치·잠금·허용 범위·선택지는 `defaults.json`의 `ui`에도 있고(설정 화면이 그림, 2026-09-26) **이 표와 자동 비교**한다(`tests/test_settings_schema.py`). 이번 빌드에 아직 없는 설정은 `available`에 이유(화면은 회색 + 툴팁).
> 화면 문구는 영어(11-ui 1.7-1 용어 규칙).

## 1. 설정 목록
**위치:** Main = 메인 화면 / Adv = Advanced settings / Setup = Set up devices / Desktop = 송신·수신 PC의 Agent Desktop 상태 창(03 F20, 그 PC에만 저장) / Fixed = 바꿀 수 없음(원칙)
**잠금:** 시험 중 잠김(🔒) 여부는 11-ui 1.10 조작 상태표와 같다.

### 1.1 시험
| ID | 화면 이름 | 위치 | 기본값 | 허용 값 | 🔒 | 문서 |
|----|-----------|------|--------|---------|----|------|
| S-01 | Test type | Main | IPTV test | IPTV test(수신기는 장비 줄에서 PC 1대 + 태블릿 0~3대 중 고름, 2026-10-24 사용자 "메뉴가 두개 있을 필요없는거잖아 … 4대중에 골라서") | 🔒 | 01 |
| S-02 | Devices | Main | 발견된 전부 | 태블릿 1~3 / 수신 PC 1 | 🔒 | 06 10장 |
| S-03 | Video quality | Main | UHD · 3840×2160 · 35 Mbps | UHD / HD | 🔒 | 01 D4 |
| S-04 | Channel per device | Main | 서로 다른 채널 자동 배정 | CH1~CH3, 같아도 됨 | 시험 중 변경 가능 | F19 |
| S-05 | Duration | Main | 1 h | 10 s, 1 min, 10 min, 1 h, 24 h, 100 h, 직접 입력(10 s~200 h) | 🔒 | 01 |
| S-06 | Send quality reports | Main | On | On / Off | 🔒 | F12 |

### 1.2 송신
| ID | 화면 이름 | 위치 | 기본값 | 허용 값 | 🔒 | 문서 |
|----|-----------|------|--------|---------|----|------|
| S-10 | Transport | Adv | RTP / MPEG-TS | RTP / MPEG-TS, MPEG-TS / UDP | 🔒 | 05 2.1 |
| S-11 | Bitrate | Adv | UHD 35 / HD 10 Mbps | UHD 30~40, HD 8~20 (1 Mbps 단위) | 🔒 | iptv 1장 |
| S-12 | Sender mode | Adv | Prepared file | Prepared file / Live encode | 🔒 | 01 |
| S-13 | Broadcast | Adv | Test-linked | Test-linked / Always-on | 🔒 | F18 |
| S-14 | Multicast groups · port | Adv | 239.1.1.1~3 : 5004 | 239.0.0.0/8 중 MAC 비충돌, 포트 1024~65535 | 🔒 | 05 3.3 |
| S-15 | TTL | Adv | 16 | 8~64 | 🔒 | 05 3.2 |
| S-16 | DSCP | Adv | 0 | 0~63 | 🔒 | 05 3.2 |
| S-17 | Router WAN IP | Adv | Built-in DHCP | Built-in DHCP / Static IP | 🔒 | 07 3장. Built-in DHCP = 송신 PC의 **통신사 역할**(공유기 WAN 포트에 꽂으면 스스로 켜짐, 07 3.2 B, 사람 할 일 없음), Static IP = 공유기에서 정한 주소(A). 주소 값은 입력하지 않는다 — 툴이 알아봄, 실제 쓰인 방식은 `dut.wan_ip_way`(07 3.3, 2026-09-26) |
| S-18 | Hole-punch mode (no-IGMP firmware) | Adv | Auto | Auto / Off / On | 🔒 | 01 매트릭스. 상용 펌웨어는 보통 IGMP·M2U가 켜져 있고 개발용·SDK는 꺼져 있다(2026-09-25 사용자) → **Auto: 사전 점검 5단계가 멀티캐스트를 먼저 보고, 안 오면(NAT) 프라임을 시험해 되면 프라임으로 시험**. Off = 멀티캐스트만, On = 항상 프라임. 동작 iptv 4.5 |

### 1.3 수신·측정
| ID | 화면 이름 | 위치 | 기본값 | 허용 값 | 🔒 | 문서 |
|----|-----------|------|--------|---------|----|------|
| S-20 | Receiver PC network | Setup/Main | Wired | Wired / Wireless | 🔒 | D14 |
| S-21 | Channel change | Adv | Manual | Manual / Every 5 min / Every 10 min / Every 30 min / Every 60 min(화면 후보). CLI·API는 `Every <N> min` N 1~120(20장 GAP-91). 켜면 CH1~CH3 모두 송출 | 🔒 | F19 |
| S-22 | Stagger channel changes | Adv | On (10 s) | On (10 s)(화면) / Off(동시). CLI·API는 `On (<N> s)` N 5~60 | 🔒 | F19 |
| S-23 | Jitter warning level | Adv | 50 ms | 10~149 ms | 🔒 | 05 5.3 |
| S-24 | Reorder window | Adv | 1,000 ms | 100~3,000 ms | 🔒 | 05 2.1 |
| S-25 | Player buffer (수신 PC마다 장비 줄 Buffer로 따로 바꿀 수 있음, 2026-10-16 사용자 "수신기마다 다를거아니냐") | Adv | 1,000 ms | 300~5,000 ms | 🔒 | 05 4.6.1 |
| S-26 | PC player | Adv | mpv | mpv / VLC | 🔒 | 05 4.6 (기본 mpv 2026-10-10 사용자 결정, GAP-152: 하드웨어 디코딩. 그 전 VLC 2026-09-24) |
| S-27 | Receive buffer size | Adv | 16 MB | 4~64 MB | 🔒 | 05 4.3 |
| S-28 | Start-up wait (warm-up max) | Adv | 15 s | 5~60 s | 🔒 | 05 2.1-2 |
| S-29 | Packet capture | Adv | Off | Off / Headers only / Full | 🔒 | 05 4.7 |
| S-30 | Evidence capture (체크박스 `Capture screen on glitches and stalls`, 2026-09-24 사용자: 항상은 아니고 원할 때만 → **2026-10-10 사용자 "바꿔": 기본 켬**, 깨진 화면을 봐야 엔지니어가 믿음, GAP-154) | Adv | **On** | On / Off, 시간당 최대 수(기본 20), 용량 한도(기본 5 GB) | 🔒 | F23 |

### 1.4 장애·운영
| ID | 화면 이름 | 위치 | 기본값 | 허용 값 | 🔒 | 문서 |
|----|-----------|------|--------|---------|----|------|
| S-40 | On device lost | Adv | Stop all + report | Stop all + report / Try to recover / Shut down all | 🔒 | F11 |
| S-41 | Recover wait | Adv | 60 s | 10~600 s (S-40이 Try to recover일 때만) | 🔒 | F11 |
| S-42 | Heartbeat suspect / lost | Adv | 3 s / 10 s | 의심 2~10 s, 사망 5~60 s (의심 < 사망) | 🔒 | F11 |
| S-56 | Without the Controller (Controller 없이, 2026-10-09 사용자 "이어받기", 20 GAP-130) | Adv | Keep running to the planned end | Keep running to the planned end / Stop after 10 s — Controller가 응답하지 않을 때(잠듦·네트워크 끊김) 시험 PC들이 예정 끝까지 계속 보내고 재고, Controller가 돌아오면 같은 시험을 이어받음; 또는 예전처럼 10초 뒤 정지 | 🔒 | F11, 09 CTL-008 |
| S-43 | Report location | Adv | C:\ZeroLoss\Reports (Linux: `~/ZeroLoss/Reports`) | 서비스가 쓸 수 있는 로컬 폴더(OneDrive·네트워크 드라이브 경고, 서비스 쓰기 권한 확인) | 🔒 | F15, RPT-001 |
| S-44 | Results web access | Adv | Same subnet | Off / This PC only / Same subnet | 가능 | 07 7장 |
| S-45 | Request signing (pairing key) | Adv | Off | Off / On | 🔒 | D30 |
| S-46 | Tool update check | Adv | Weekly | Off / Daily / Weekly | 가능 | 15 |
| S-47 | Channel change success timeout | Adv | 5 s | 0.5~30 s (0.1 s 단위) | 🔒 | F19, 05 1.6-2 |
| S-48 | Source video (채널마다 한 줄, 2026-10-04 사용자 "advanced에서도 파일 선택이 안 보인다" → Advanced에서 기본 화면 Video quality 아래로, GAP-115) | Main | Generated | Generated / 송신 PC 미디어 폴더의 파일(목록에서 고르기, 계획에는 채널별 파일 이름만) | 🔒 | iptv 1-1 구현 규칙(앞 10분 변환), SND-001 |
| S-49 | PC playback (PC receiver only) | Adv | On | On / Off (analyze only) | 🔒 | 05 4.2-1. On이면 설치본 수신 PC에 로그인한 사용자의 Agent Desktop이 있어야 함(03 F20, 없으면 RCV-010으로 시작 안 함) |
| S-50 | Player window size (PC receiver only) | Adv | Medium (1280×720) | Small (640×360) / Medium (1280×720) / Full screen | 가능(기록) | 05 4.2-1 |
| S-51 | Test Wi-Fi (SSID · security · password) | Adv | 비어 있음(자동 연결 안 함) | SSID 1~32바이트, Open / WPA2 / WPA3, 비밀번호 8~63자(Open이면 없음) | 🔒 | F14 태블릿 Wi-Fi 자동 연결, TAB-014, 무선 수신 PC(03 F17 "무선 수신 PC", RCV-023). 저장·입력 02 2-7(CLI `zeroloss wifi set`) |
| S-52 | Problem sound | Adv | Off | Off / On | 시험 중 변경 가능 | F21 (2026-09-25 사용자 결정: 무인 시험실이라 기본 꺼짐) |
| S-53 | Always on top (Agent Desktop status window) | Desktop | On | On / Off | 시험 중 변경 가능 | 03 F20 (항상 위, 끌 수 있음). 그 PC의 Desktop 폴더 `desktop.json`에만 저장(시험 계획·Controller와 무관). 2026-09-26 문서 재검토에서 표에 없던 것을 찾아 넣음(20장 GAP-69) |
| S-54 | Test network | Adv | Through the router | Through the router / Baseline (no router) | 🔒 | 05 4.6.3 기준선 시험(2026-10-02, 20장 GAP-97): 송신 PC와 수신 장비를 공유기 없이 스위치 하나(또는 직접)로 묶고 수신 장비·재생기 조합을 검증 |
| S-55 | Source length (영화 송출 길이, 2026-10-08 사용자 "추천대로 해", 20 GAP-118) | Adv | 10 min | 10 min / Whole movie — 고른 영화(S-48)를 송출용으로 한 번 만들어 계속 재사용: 앞 10분(빠름·작음, 10분마다 반복) 또는 영화 전체(ProBook HD 98분 ≈ 2시간 20분, 채널당 HD 7.4 GB·UHD 26 GB). 둘은 따로 보관(서로 덮어쓰지 않음). 자동 생성 영상은 늘 10분 | 🔒 | iptv 1-1·1-1-2 |

**Throughput 모듈 설정(2026-10-10, `modules/throughput/defaults.json`, docs/modules/throughput.md 0·1.3) — Test type이 Throughput일 때만 Advanced에 보인다:**

| ID | 항목 | 위치 | 기본값 | 범위 | 시험 중 | 참조 |
|----|------|------|--------|------|---------|------|
| S-57 | Wait before and after each PC (Throughput) | Adv | 10 s | 0~300 s | 🔒 | throughput.md 1.3 (2026-09-26 사용자: PC·포트·단말마다 시작 전·끝난 뒤 대기) |
| S-58 | Measure time per direction (Throughput) | Adv | 30 s | 5~600 s | 🔒 | throughput.md 1.3 (2026-09-26 사용자: 보내기·받기 각각) |
| S-59 | Pass threshold (wired, Throughput) | Adv | 90 % | 1~100 % | 🔒 | throughput.md 0 (2026-10-10 레퍼런스 기본: 협상된 링크 속도의 90 %, 무선은 값만) |
| S-60 | Protocol (Throughput) | Adv | TCP | TCP / UDP (UDP 목표 = 링크 속도의 95 %) | 🔒 | throughput.md 0 (T-3) |
| S-61 | Parallel connections (Throughput) | Adv | 1 | 1~16 | 🔒 | throughput.md 0 (T-3, 레퍼런스 기본 1) |
| S-62 | Link speeds (Throughput) | Adv | Every supported | Every supported(유선 카드가 지원하는 속도·이중 방식마다, 첫 유선은 WAN 쪽 카드도, 끝나면 원복) / As it is(지금 링크 그대로) | 🔒 | throughput.md 1.1·1.3 (2026-10-16 사용자 "참고자료 유선 순차 방식") |
| S-63 | Directions (Throughput) | Adv | LAN→WAN, then WAN→LAN | LAN→WAN, then WAN→LAN(LAN PC 기준 보내기=업로드, 받기=-R 다운로드, 따로따로) / LAN→WAN only / WAN→LAN only | 🔒 | throughput.md 1.3 (2026-10-17 사용자 "send와 receive를 각각 iperf, iperf -R로", 레퍼런스 B→A·A→B 선택) |
| S-64 | UDP rate (Throughput) | Adv | 95 % | 1~100 % (협상된 링크 속도 기준 -b) | 🔒 | throughput.md 0 (레퍼런스 udp_bandwidth_percent 95) |
| S-65 | Cards to test (Throughput) | Devices | 없음 | Devices에서 PC의 카드마다 줄이 있고 사용자가 체크: 공유기 LAN 쪽 유선 카드, 또는 Wi-Fi 카드(이 Controller PC의 것도) + 2.4/5/6 GHz마다 공유기 Wi-Fi(그 카드의 스캔에서 고름, 옆 칸에 암호 — 빈칸 = 암호 없음, open 망은 "open" 표시; 02 2-10-2). 아무것도 대신 고르지 않음 | 🔒 | throughput.md 1.3 (2026-10-17 사용자 "연결된 포트가 있으면 그만큼 row가 늘어나야지 그래야 사용자가 고르지", "와이파이도 카드 뭐로 할건지 사용자가 정할거야") |

### 1.5 바꿀 수 없음 (Fixed, 원칙)
| 항목 | 값 | 이유 |
|------|----|------|
| 판정 기준 | 손실 0%, 순번 오류 0, Jitter ≤150 ms, 최소 1,000 패킷·100 kbps | 설계 원칙 3 |
| 채널 수 | 최대 3 | D5 |
| 데이터그램당 TS 수 | 7 | 단편화 없음(05 3.2) |
| 화면 언어 | English | D10 (한국어는 나중에 문구 파일로) |
| Controller 연결 | 유선 LAN | 07 1장 |

## 2. 조합 규칙 (옵션끼리의 관계)
**처리 방법:** ⛔ 막음(Start 비활성 + 이유) / ⚠ 경고(시작은 가능, 보고서에 표시) / ↻ 자동 조정(알림)

| # | 조합 | 문제 | 처리 |
|---|------|------|------|
| K-01 | S-11 Bitrate가 S-03 화질 범위 밖 | 잘못된 화질 | ↻ 화질 바꿀 때 기본값으로 조정 + 알림 |
| K-02 | S-24 Reorder window > S-25 Player buffer | "늦음" 기준이 실제 재생과 달라짐(재생기는 이미 버린 패킷을 정상으로 셈) | ⛔ (기본: 두 값 연동, 창 ≤ 버퍼) |
| K-03 | S-23 경고값 ≥ 150 ms | 노랑이 의미 없음 | ⛔ (허용 범위로 막음) |
| K-04 | S-10 = MPEG-TS/UDP | 손실은 CC 기반(16배수 손실 놓침), Jitter는 IPDV로 대체 | ⚠ 보고서 "measurement limited" |
| K-05 | S-10 = MPEG-TS/UDP + (2차) RET/FEC | RTP 없이는 재전송 불가 | ⛔ |
| K-06 | S-12 Live encode + 최소 구성(Controller+Sender 같은 PC) | 송출 불안정 위험 | ⚠ 강한 경고, Prepared file 권장 |
| K-07 | S-12 Live encode + UHD 3채널 | 송신 PC 성능 부족 가능 | ⚠ 사전 점검에서 CPU 측정, 부족하면 ⛔ |
| K-08 | S-05 Duration < 5 min | 5분 칸 하나가 부분 칸 | ⚠ "partial box" 표시 |
| K-09 | S-05 Duration과 안정화 | 시험 시간은 **안정화가 끝난 뒤부터** 센다(짧은 시험도 측정 시간 보장) | 규칙 |
| K-10 | S-21 자동 전환 간격 N < (S-28 안정화 + 30 s) | 측정할 틈이 없음 | ⛔ |
| K-11 | S-21 자동 전환 + 시험 중 수동 채널 변경 | 두 방식 충돌 | 자동 전환 켜면 수동 채널 선택 잠금(11-ui 1.10) |
| K-12 | S-13 Always-on + S-17 Built-in DHCP | 방송 중에는 공유기 WAN 주소가 있어야 함 | 허용 — 통신사 역할은 시험·방송과 상관없이 늘 켜져 있음(07 3.2 B, 2026-09-26) |
| K-13 | S-17 Static IP + 공유기 WAN이 우리 DHCP 임대를 받음 | S-17은 기본값 기록용(07 3), 실제 방식은 툴이 알아봄 | 허용 — 실제 방식만 보고서 `dut.wan_ip_way`에 기록(경고 없음, DSC-007 쓰지 않음 2026-09-26 W-5) |
| K-14 | S-18 On, 또는 Auto에서 프라임으로 정해짐 | 수신기가 시험 경로로 트래픽을 보냄(원칙 예외), 유니캐스트라 M2U 확인 무의미 | ⚠ 보고서 "not the real IPTV path", M2U 결과 "n/a" |
| K-15 | S-18 On + Bridge 모드 | 의미 없음(NAT 뚫기용) | ⛔ (Auto는 Bridge면 멀티캐스트만) |
| K-16 | S-29 Full capture + UHD + 긴 시간 | 디스크 폭주(시간당 채널당 약 18 GB) | 사전 점검에서 **필요 용량 계산**, 부족하면 ⛔, 충분해도 ⚠ |
| K-17 | S-29 capture On + 태블릿 시험 | 태블릿은 캡처 불가 | 캡처는 수신 PC·외부 장비만, 태블릿은 "n/a" |
| K-18 | S-30 Evidence On + 태블릿 메모리 부족 | 순환 버퍼 축소 | ↻ 버퍼 줄이고 실제 값 기록 |
| K-19 | S-20 Wireless + 태블릿 시험 | 수신 PC를 Receiver로 둔 태블릿 시험(2026-10-23)에서는 PC 수신 시험과 같이 적용 | 수신 PC를 Not used로 두면 중계만, 적용 안 함 |
| K-20 | S-06 Reports Off + S-40 Try to recover | 복원 중 실시간 판단 자료가 적음 | 허용(heartbeat 상태로 판단) |
| K-21 | S-42 의심 ≥ 사망 | 잘못된 값 | ⛔ |
| K-22 | S-44 Results web = Same subnet + 시험 중 무선 기기로 조회 | 시험 무선 구간 오염 | ⚠ (07 7장) |
| K-23 | S-14 그룹 주소끼리 MAC 충돌 / 예약 주소 | 결과 왜곡 | ⛔ (05 3.3) |
| K-24 | S-15 TTL < 8 | NAT 모드에서 버려질 수 있음 | ⛔ |
| K-25 | S-11 × 채널 수 합계 > 120 Mbps | 통과 기준 시나리오 초과 | ⚠ (허용하되 표시) |
| K-26 | S-43 보고서 위치가 OneDrive·네트워크 드라이브 | 대용량 동기화·끊김 | ⚠ 로컬 권장 |
| K-27 | IPv6(MLD, 옵션) + S-17 Built-in DHCP | 내장 DHCP는 IPv4만 | ⛔ (2차에 정의) |
| K-28 | S-48 파일의 영상 해상도·프레임레이트 ≠ S-03 Video quality | 변환 시 크기·속도 조정 필요 | ⚠ 표시하고 화질에 맞춰 변환(원본보다 크게 늘리면 "Upscaled" 표시) |
| K-29 | S-49 Off | 재생 이상 감지·화면 증거 캡처 불가 | 해당 항목 `NOT RUN`, S-50 숨김. 손실·지터 판정은 그대로 |
| K-30 | S-01 Tablet test | S-49·S-50은 PC 수신 시험 전용 | 숨김(적용 안 함) |
| K-31 | S-51 설정 + Android 10 태블릿(minSdk 29) | `cmd wifi connect-network`는 Android 11 이상 | 그 태블릿만 자동 연결 안 함 → Action Required(F22)로 사람에게 연결 요청 |

## 3. 설정 저장·표시 규칙
- **이 빌드에 아직 없는 기능의 설정**(예: P2의 S-49 PC 재생)은 표의 기본값과 상관없이 꺼진 값으로 보고, 그 항목은 보고서에 `NOT RUN`으로 남긴다. 켜진 값을 계획에 직접 적으면 RUN-013(이 빌드에 없는 기능)으로 거부한다.
- 시험 시작 때 모든 설정을 **복사해 고정**하고(11-ui 1.10), 보고서 첫 부분 "Settings used"에 **기본값과 다른 것만 강조**해서 모두 적는다.
- Advanced에서 바꾼 개수 표시("2 changed"), [Reset to defaults].
- 조합 규칙 ⛔는 Start 버튼 옆 이유 한 줄 + 해당 설정칸 강조, ⚠는 Problems에 노랑.
- CLI(`zeroloss run --plan`)의 계획 파일도 **같은 검사**를 거친다(화면과 CLI가 다른 규칙을 쓰지 않음).

## 4. 검증 방법 (옵션 조합 시험)
- 옵션 조합을 전부 시험하면 수가 너무 많다. 그래서:
  1. **조합 규칙표(2장)의 모든 줄**을 각각 시험: ⛔는 막히는지, ⚠는 경고·보고서 표시, ↻는 자동 조정·알림.
  2. **쌍별 조합 시험(pairwise):** 모든 옵션의 모든 값이 **다른 옵션의 모든 값과 최소 한 번씩** 만나도록 시험 세트를 자동 생성(조합 시험 기법, 미국 NIST가 정리한 방법. 실제 결함 대부분이 2~3개 옵션의 만남에서 생긴다는 근거). 조합 규칙으로 불가능한 조합은 제외.
  3. 각 조합에서: 화면 상태표 일치(11-ui 1.10), 설정 복사 고정, 보고서 "Settings used" 정확, 짧은 시험(1분) 정상 완료.
  4. 몽키 회귀(10-verification 4-2)에 **Advanced 값 무작위 변경**을 포함.
- 자동 검사: 이 문서의 설정 ID 목록 = `defaults.json` 키 목록 = 화면 문구 목록(빠진 것·남는 것 없음).
