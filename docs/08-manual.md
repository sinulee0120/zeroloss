# 08. 사용설명서 (작성 규칙과 구성)

> 목표: **비전공자, 경험 없는 사람도 혼자서** 설치 → 시험 → 보고서 확인 → 제거까지 할 수 있어야 한다.
> 동시에 **짧아야 한다.** 길면 안 읽는다. 읽는 설명서가 아니라 **보고 따라 하는 설명서**로 만든다.

## 1. 작성 규칙
1. **한 단계 = 한 동작 + 그림 한 장.** "IP를 입력하고 저장한 뒤 재시작"처럼 여러 동작을 한 단계에 넣지 않는다.
2. **단계마다 형식 고정:**
   ```
   ③ Click [Start].
      [화면 그림: Start 버튼에 빨간 테두리와 번호 표시]
      ✔ You should see: The button changes to "Stop" (red).
      ✖ If not: See "Start is grey" on page 12.
   ```
   - 무엇을 하는지(한 줄) → 그림 → **✔ 이렇게 되면 성공** → **✖ 안 되면 어디를 볼지**
3. **그림에 번호와 빨간 테두리:** 눌러야 할 곳만 강조한다. 그림 속 번호와 본문 단계 번호를 일치시킨다.
4. **한 장(챕터)은 한 페이지, 단계는 7개 이하.** 넘치면 장을 나눈다.
5. **전문 용어 금지, 쓸 수밖에 없으면 한 번만 풀어 쓴다.** 예: "Router WAN port (the port where the internet cable usually goes)"
6. **선택 설명은 접는다:** "Advanced" 설명은 기본 절차에서 빼고 부록으로 보낸다.
7. **그림은 실제 프로그램 화면으로 자동 생성:** 빌드 때 프로그램을 자동 실행해 화면을 캡처하고 설명서 그림을 갱신한다. 프로그램이 바뀌어도 그림이 옛날 화면으로 남지 않게 한다. 도구: `python -m tools.manual_shots <출력 폴더>`(화면을 오프스크린으로 띄워 main-idle·main-running·main-fault·storage 그림을 만듦, 화면이 늘면 여기에 더함), 시험 `tests/test_manual_shots.py`.
8. **연결 그림(배선도)은 사진이 아니라 단순한 그림:** 어떤 선을 어느 포트에 꽂는지 색으로 구분한다(WAN 파랑, LAN 초록, USB 회색).

## 2. 형태
| 형태 | 내용 | 분량 |
|------|------|------|
| **Quick Start (1장)** | 선 연결 그림 + 설치 → Start → 결과 보기까지 한 장 요약. 인쇄해서 장비 옆에 붙여 둘 수 있게 | **A4 1장** |
| **Step-by-step Guide** | 아래 3장 목차. 장마다 1페이지 | 약 12~15페이지 |
| **프로그램 안 도움말** | 각 화면의 `?` 버튼 → 설명서의 **해당 장을 바로** 연다 | - |
| **문제 해결 (Troubleshooting)** | 자주 생기는 문제 Top 10: 증상 → 원인 → 해결 순서 | 2페이지 이내 |

- 형식: 프로그램에 포함된 **HTML**(오프라인에서 열림) + 인쇄용 **PDF**.
- 언어: 영어(화면 문구와 일치). 한국어판은 선택(D33).

## 3. 목차 (Step-by-step Guide)
| 장 | 제목 | 핵심 그림 |
|----|------|-----------|
| 0 | What you need (준비물) | 장비 사진 대신 아이콘 목록: PC 3대, 공유기, 태블릿, 케이블 |
| 1 | Connect the cables (선 연결) | 배선도: 송신 PC→WAN, Controller·수신 PC→LAN, 태블릿→Wi-Fi + USB |
| 2 | Install the Controller (Windows) | 설치 파일 더블클릭 → 완료 화면 |
| 3 | Install Agents | 3-A Windows: 설치 파일 실행 / 3-B Linux: Controller 화면에서 IP·계정 입력 → (무인 시험이면 [Log in automatically] 체크, 06 11.3 4-2) → [Install] / 3-C 송신 PC는 설치 뒤 케이블을 공유기 WAN으로 옮김(SND-022) |
| 4 | Connect tablets | USB 연결 → 태블릿의 "USB 디버깅 허용" 팝업 → Controller에서 [Install app] |
| 5 | First start: find devices | 자동 발견 목록, 추천 표시 확인 → [OK] |
| 6 | Run a test | 메인 화면: 시험 종류, 기기, 화질, 시간 → [Start] → [Stop] |
| 7 | Read the results | 5분 칸 색 의미(초록/노랑/빨강/회색), Jitter·Packet Loss 읽는 법, PASS/FAIL/INCOMPLETE 의미 |
| 8 | Open and share the report | 보고서 열기, 웹에서 보기(휴대폰 포함), 결과 보관·정리(Manage storage). 두 회차 비교는 2차 |
| 9 | When something goes wrong | 장애 안내 화면 → [Reset All]. Controller가 꺼졌을 때 끄는 법(Agent 상태 창 [Stop], `zeroloss stop --all`) |
| 10 | Update | Controller에서 [Update] (Agent, 태블릿 앱, adb) |
| 11 | Uninstall | 제거 순서: 태블릿 앱 → Agent → Controller, 결과 보존 여부 선택 |
| 부록 | Advanced settings | 고급 항목별 한 줄 설명 (화면 툴팁과 같은 문구). 특히: 시험 Wi-Fi 입력(S-51), 내 영상 파일 쓰기(송신 PC `C:\ZeroLoss\Media\`에 넣기 → Source video에서 고르기, S-48), 저사양 수신 PC(재생 끄기·창 크기, S-49·S-50) |

## 4. 결과 읽는 법 (7장) 필수 내용
- 5분 칸 한 장 그림에 색 설명을 바로 붙인다:
  - 🟢 **Green** = No problem in these 5 minutes
  - 🟡 **Yellow** = No loss, but a warning (e.g. jitter higher than usual)
  - 🔴 **Red** = Packets were lost or jitter was too high → **FAIL**
  - 🟣 **Purple** = Packets were lost, but the tool or the receiving device caused it (not the router) — check the receiver (05 5.3)
  - ⚪ **Grey** = No data (device disconnected or reports off)
- 최종 판정 3가지를 한 줄씩:
  - **PASS** = Ran the full time with no red box
  - **FAIL** = At least one red box
  - **INCOMPLETE** = No red box, but the result is not complete: stopped early, or some boxes are grey (no data) or purple (tool/receiver side) (05 5.4)
- "원인 분석" 문구 예시와 읽는 법 (예: "Only tablets lost packets, PC receiver was fine → Likely a tablet issue")

## 5. 문제 해결 Top 10 (초안)
| 증상 | 확인할 것 |
|------|-----------|
| Start가 회색이고 안 눌림 | 버튼 옆 이유 문구 → 해당 장으로 이동 |
| 장치 목록에 송신 PC가 없음 | 송신 PC 화면의 "My IP"를 입력 |
| 태블릿이 목록에 없음 | USB 케이블, 태블릿의 USB 디버깅 허용 팝업 |
| 태블릿 연결이 자주 끊김 | adb 업데이트 버튼, 다른 adb 프로그램 종료 |
| 시작하자마자 전부 빨강(수신 0) | 공유기 IGMP 설정, 사전 점검의 "IGMP forwarding" 결과 |
| 무선만 빨강 | 공유기 M2U 설정, 태블릿 신호 세기 |
| 몇 분마다 규칙적으로 끊김 | 공유기 IGMP 유지(Query) 설정 |
| 수신 장비 자체 드롭 경고 | 수신 PC·태블릿 성능, 다른 앱 종료. PC 수신이면 하드웨어 해독 확인, 재생 끄기(S-49) |
| 태블릿이 시험 Wi-Fi에 안 붙음 | Advanced의 Wi-Fi 이름·비밀번호(S-51), Android 10 태블릿은 직접 연결(Action Required 창 안내) |
| 장애 안내 화면이 뜸 | [Reset All], 응답 없는 장비는 안내대로 재시작 |
| 제거 후 "남은 항목" 경고 | 표시된 항목과 안내대로 정리 |

## 5-1. 자동으로 안 될 때 손으로 하기 (2026-10-22)
사용자 "AI없이 할때도 문제가 나오지않도록. 복구나 정상 동작가능하게 매뉴얼로 사용자가 설정을 할수있도록": ZeroLoss가 자동으로 하는 일마다 **화면의 버튼**과 **손으로 하는 길**이 있어야 하고, 둘 다 설명서(키트 안내 9-1)와 그 문제의 Fix 문구(09)에 같은 말로 적는다. 새 자동 동작을 넣으면 이 표에 줄을 더한다.

| 보이는 것 | 먼저 (버튼) | 손으로 |
|---|---|---|
| 송신 PC가 ○ (Cannot reach) — 이 PC에서 10.0.0.x로 가는 길이 없음 | Action Required의 **[Add the route]** → Windows가 물으면 [예] | 관리자 명령 프롬프트에서 `route add 10.0.0.0 mask 255.255.255.0 192.168.45.1` (끝의 주소 = 이 PC 랜카드의 공유기 주소). 재부팅하면 다시 해야 합니다 |
| "… has build …" (PC마다 버전이 다름) | **[Update PCs]** | 그 PC로 키트의 `packages` 폴더 .deb 파일을 옮겨, 그 폴더의 터미널에서 `sudo apt install ./zeroloss-agent_*.deb` |
| 랜카드 속도가 원래대로 안 돌아옴 (Action Required "Put a card setting back by hand") | 손으로 고친 뒤 **[Fixed, check again]** (그냥 계속하려면 [Continue anyway]) | Windows: 장치 관리자 → 네트워크 어댑터 → 그 카드 → 고급 → 속도 및 이중 = **자동 협상**. 리눅스: `sudo ethtool -s 카드이름 autoneg on`. 또는 그 랜선을 뽑았다 다시 꽂기 |
| Wi-Fi가 원래 망으로 안 돌아옴 (같은 창) | 손으로 고친 뒤 **[Fixed, check again]** | 그 PC 화면 오른쪽 위 Wi-Fi 메뉴에서 원래 망을 고르기 (또는 `nmcli connection up id 원래망이름`) |
| 이 PC 랜카드 속도 케이스가 회색 "administrator approval was declined" | 다음 시험 때 Windows가 "변경을 허용할까요?"라고 물으면 **[예]** | 회색 케이스만 빠지고 나머지는 정상입니다 |
| "Could not hold this PC's time sync" (SYS-012, 경고) | 그대로 진행해도 됩니다 (판정은 시계 보정을 빼고 함) | 그 리눅스 PC에서 시험 전 `sudo timedatectl set-ntp false`, 끝나고 `sudo timedatectl set-ntp true` |
| "The sender PC has Wi-Fi on …" 팝업 (NET-025) | 원하면 송신 PC의 Wi-Fi를 끄고 [Done] — ZeroLoss는 끄지 않습니다 | 송신 PC 화면 오른쪽 위 메뉴 → Wi-Fi 끄기. 시험이 끝나면 다시 켜기 |
| 무선 밴드 목록이 비거나 "Scan failed" | 줄 밑에 적힌 이유대로 (예: older ZeroLoss → [Update PCs]) 한 뒤 **[Scan]** | SSID를 몰라도 됨: 그 PC를 시험할 Wi-Fi에 손으로 붙이면 그 망으로 잽니다 |
| 송신 PC가 늦게 보냄 (보라 "sender trouble", SND-025) | 시험 중에는 송신 PC에서 다른 무거운 일(영상 변환, 업데이트, 큰 복사)을 하지 않기 | 송신 PC가 뜨거우면(90 °C 넘음) 운영체제가 CPU를 잠깐씩 쉬게 해 송신이 멈춤(senderPC 2026-10-23: intel_powerclamp) — 통풍, 덮개 열기. 쓰지 않는 Wi-Fi는 끄기 권장(팝업 안내) |
| 모든 PC 설정을 원래대로 돌리고 싶음 | 시험이 끝난 뒤 **[Reset All]** (시험 중엔 회색: 먼저 Stop) | 응답 없는 PC는 그 PC를 다시 켠 뒤 [Reset All] |
| ZeroLoss 창이 멈춘 것 같음 | 창을 닫고 다시 열기 — **시험은 계속 돕니다** | 그래도 이상하면 [Reset All] |

## 6. 설명서 자체의 완료 기준
- [ ] **처음 보는 사람**(네트워크 경험 없음)이 설명서만 보고 설치 → 10분 시험 → 보고서 확인 → 제거까지 **도움 없이** 완료
- [ ] 그 과정에서 막힌 곳을 기록하고 설명서·화면을 고친다
- [ ] Quick Start는 A4 1장, 장마다 1페이지·단계 7개 이하
- [ ] 모든 그림이 현재 버전의 실제 화면 (빌드 시 자동 캡처)
- [ ] 프로그램의 `?` 버튼이 해당 장을 연다
