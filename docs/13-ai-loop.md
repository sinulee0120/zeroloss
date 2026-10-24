# 13. 외부 연결과 Claude AI 연동 (폐쇄 루프)

> 시험망은 **닫힌 망**이다. 외부(인터넷)와 연결되는 장비는 **Controller PC 하나뿐**이고, 이 PC는 **Claude AI(Claude Code)와 항상 연동**되어 있다.
> 그래서 Claude가 Controller PC에서 **빌드 → 배포 → 시험 → 결과 분석 → 수정**을 한 바퀴 돌리는 **폐쇄 루프**를 만들 수 있어야 한다.

## 1. 네트워크 구성
```
 인터넷 ── [Controller PC] ── DUT LAN ── (닫힌 시험망: 송신·수신 PC, 태블릿)
           NIC-A: 외부       NIC-B: 시험망
```
- Controller PC는 **NIC 두 개**: 외부용(NIC-A), 시험망용(NIC-B, DUT LAN 유선).
- **두 망을 절대 잇지 않는다:** 인터넷 연결 공유, 브리지, 라우팅 전달을 끈다. 시험망 장비는 인터넷에 나가지 않는다(시험 오염·보안 방지). 사전 점검에서 확인.
- 경로: 기본 경로(인터넷)는 NIC-A, 시험망 대역은 NIC-B. ZeroLoss의 모든 제어·발견은 **NIC-B로만** 나간다(방송 발견이 외부망으로 새지 않게).
- **시험망 NIC는 기본 경로·DNS를 가져가지 않는다(2026-09-26 사용자 결정, 20장 GAP-73 ⑤):** 시험망 공유기가 DHCP로 기본 경로를 주면 인터넷이 그쪽으로 가서 끊긴다(첫 실제 설치에서 발생). Controller 설치가 시험망 NIC 연결을 `never-default`·DNS 무시로 **자동 설정**(Windows: 인터페이스 메트릭·게이트웨이 없음, V18 / Linux: NetworkManager `ipv4.never-default`·`ignore-auto-dns`)하고, F17 3단계가 Controller의 기본 경로가 시험망 NIC로 나가면(다른 NIC가 있을 때) ⚠ NET-019 + Action Required [Fix](누르면 `nmcli`로 그 연결을 다시 설정, 20장 GAP-73 ⑤). Linux Controller는 설치본이 없어(개발용) 설치 때 자동 설정은 Windows 설치 파일(V18)에서.
- **시험 PC도 시험망 밖으로 연결돼 있으면 안 된다(2026-09-26 첫 실제 설치, 20장 W-3):** 사전 점검 3단계가 Controller가 시험 PC에 Wi-Fi로 닿거나(무선 수신 PC의 시험 Wi-Fi 제외), 통신사 역할 송신 PC에 NAT 없이 닿거나, 시험에 안 쓰는 Wi-Fi에 주소가 있으면 ⚠ NET-018(시작은 허용). Controller PC 자신의 인터넷용 NIC는 제외.
- 외부 연결은 **유선 권장.** Controller가 Wi-Fi로 인터넷에 붙으면 DUT 무선과 간섭할 수 있으므로, 쓸 경우 DUT와 다른 대역·채널을 쓰고 사전 점검에서 경고.
- 외부에서 받은 것(도구 최신판, adb 등)은 Controller가 받아 **오프라인 묶음으로 만든 뒤** 시험망 노드에 Deployer(10991)로 나눠 준다(06-install 2.1).

## 2. Claude가 도구를 다룰 수 있게 (자동화 인터페이스)
사람은 화면(GUI)으로, Claude는 **명령줄(CLI)**로 같은 기능을 쓴다. 두 경로는 **같은 Core 명령**을 부른다(기능 중복 구현 금지).

> **2026-09-26 재검토(20장 GAP-76 ⑦)에서 코드에 없던 명령:** `diagnostics`·`agent stop`·`storage`·`records`·`verify` 모두 2026-09-26~27 만듦(20장 W-6).

| 명령 (예) | 하는 일 | 출력 |
|-----------|---------|------|
| `zeroloss status --json` | 노드·역할·연결·버전·현재 시험 상태 | JSON |
| `zeroloss serve --role controller\|agent [--data 폴더] [--modules 폴더] [--node-port 10992] [--player-host self\|desktop]` | 서비스를 앞에서 실행(개발·시험용. 설치본은 OS 서비스로 등록, P3). `--modules`: 모듈 폴더(설치본은 Deployer의 모듈 폴더, 06 12장). `--player-host desktop`: PC 재생기는 Agent Desktop이 띄움(설치본, 03 F20) | 실행 중 표시 |
| `zeroloss devices add <이름> <IP>` / `list` / `remove <이름>` | 장비 목록 관리(P2 수동, P3부터 자동 발견이 채움, 02 11-0) | JSON |
| `python -m tools.build deb\|module\|version` | 빌드(버전 = 날짜·시각): Linux 노드 .deb + .zlp, 모듈 .zlm, VERSION 찍기(06 8·11). Windows 설치 파일은 V18, APK는 태블릿 앱 때. (예전 이름 `zeroloss build` — 제품 CLI가 아니라 개발 도구라 `tools.build`) | 결과 파일 목록 |
| `zeroloss install linux --host <IP> --user <계정> [--name <장비 이름>] [--password-stdin] [--accept-new-host-key] [--package <deb>] [--module <zlm>]… [--ssh-port 22] [--node-port 10992] [--deployer-port 10991] [--auto-login] --yes` | Linux Agent 최초 설치(SSH, 06 11.3): 점검 → .deb 설치 → 되돌림 사본 → (`--auto-login`이면 그 계정 GDM 자동 로그인, 06 11.3 4-2) → 서비스 확인 → 장비 등록 → 모듈 전달. 비밀번호는 묻거나 표준 입력으로만(저장 안 함). `--yes` 필요 | 단계별 결과(`steps`), 호스트 키 지문 |
| `zeroloss deploy --node <name> --package <file>` | 노드에 패키지·모듈 배포(10991, 06 11.5·12장). `.zlm` = 모듈, `.zlp` = 노드 업데이트(Linux: 따로 도는 설치 작업, 끝날 때까지 최대 15분 기다림, 실패하면 이전 빌드로 되돌아감) | 단계별 결과(02 15장) |
| `zeroloss modules list --node <name>` / `zeroloss modules remove <id> --node <name> [--purge] [--dry-run] --yes` | 노드의 모듈 목록 / 모듈 제거(06 12장, 제거는 `--yes` 필요) | 목록 / 지운·남긴·남은 것 |
| `zeroloss deploy --all --yes` | 전체 업데이트(Agent → 태블릿 앱 → Controller 순서, 06 11.9) | 노드별 결과 |
| `zeroloss wifi show` / `wifi set --ssid <SSID> --security Open\|WPA2\|WPA3 [--password-stdin]` / `wifi clear` | 시험 Wi-Fi(S-51) 보기·저장·지우기(02 2-7). 비밀번호는 표준 입력으로만, 화면·JSON에 나오지 않음 | `{ssid, security, has_password}` |
| `zeroloss reset --yes` | Reset All(F11, 02 2-8): 모든 장비 정지·깨끗한 상태 점검. 0 = 모두 깨끗, 3 = 덜 됨(장비별 할 일) | 장비별 결과 |
| `zeroloss nodes restart --node <이름>` / `nodes stop --yes` / `nodes start` | 장애 안내의 [Restart Agent] / [Shut Down All](노드 서비스만, Deployer는 켜둠) / 다시 켜기(02 2-8) | 장비별 결과 |
| `zeroloss devices wan <이름> [--on\|--off\|--auto] [--nic <NIC>]` | 송신 PC의 통신사 역할(07 3.2 B): 보기·켜기·끄기(끄면 더한 주소를 뺌)·`--auto` 다시 자동(기본, 공유기 WAN 포트에서 스스로 켜짐). 평소에는 쓸 일 없음. 시험 중이면 거부 | `{on, mode, state, nic, server_ip, lease, why}` |
| `zeroloss discover [--scan <CIDR>] [--no-try-sender]` | 자동 발견(06 10.2): 방송·송신 권장 주소·(선택) 범위 조회. 시험 중이면 거부 | 찾은 장비 목록(등록 여부·이름 제안) |
| `zeroloss setup show` / `setup set KEY=VALUE…` | 화면과 같은 시험 선택 보기·바꾸기(F14, 02 2-11): 화질·시간·송신·수신(`receiver=rx:1:Wireless`)·`S-nn=값`. `zeroloss run`·`precheck`는 `--plan`이 없으면 이것 | 선택·역할·계획 |
| `zeroloss precheck --plan <file>` | 사전 점검(F17 1~5단계, 시험 송출 1~2초 포함·시험은 시작 안 함, 02 2-6). 실패 단계가 있으면 종료 코드 3 | 단계별 JSON |
| `zeroloss prepare --sender <name> --quality UHD\|HD [--bitrate <Mbps>] [--ch 1,2,3] [--wait]` | 자동 생성 영상 미리 만들기(iptv 1-1-2). `--wait` 없으면 바로 돌아오고 `zeroloss prepare --sender <name> --status`로 진행률 | 채널별 상태 |
| `zeroloss run --plan <file> [--wait]` | 시험 실행(계획 파일: 모듈, 장비, 설정, 시간) | 시험 ID |
| `zeroloss stop` / `zeroloss reset` | 정지 / 전체 리셋 | 결과 |
| `zeroloss stop --all --yes` | Controller 기록과 상관없이 발견되는 모든 노드의 시험·송출 정지(Deployer 10991 경유, CTL-008) | 노드별 결과 |
| `zeroloss agent stop` | 이 PC의 Agent 시험·송출만 정지(Controller 없이도 동작, 로컬 10994 `POST /v1/stop`, USR-007) | `{stopped, state, run, files}` |
| `zeroloss storage list` / `delete <run> [--raw-only] --yes` | 결과 사용량 보기(회차별·장비별, 큰 것부터) / 회차 지우기(F15, 02 2-13; `--yes` 없으면 종료 코드 4, 진행 중 회차는 RUN-012) | 목록 / 결과 |
| `zeroloss records --run <회차>` | 1초 기록을 모든 장비에서 보고서 폴더로 다시 가져오기(보고서 뒤 자동 회수가 못 한 것, 02 2-13) | 가져온 파일·못 가져온 장비(있으면 종료 코드 3) |
| `zeroloss sources --node <sender>` | 송신 PC 미디어 폴더 영상 목록(S-48) | JSON |
| `zeroloss report --run <id> --json` | 판정·요약·문제·원인 분석 | `summary.json` 형식 |
| `zeroloss reports` | 지난 보고서 목록(최신순, 02 2-13 `reports`) | 회차·시작 시각·종류·화질·시간·판정 |
| `zeroloss channel <장비> <채널>` | 시험 중 수신기 채널 전환(F19, 02 2-13). 첫 패킷까지 기다려 답함(최대 7초), 가입 실패(NET-003)면 종료 코드 3 | 전환 기록(02 7-2) |
| `zeroloss broadcast stop` | 상시 방송 멈춤(F18, 02 2-13) | 멈춘 채널 |
| `zeroloss baselines` | 수신 장비·재생기 조합마다 마지막 기준선 시험(05 4.6.3, 02 2-13): 검증됨 여부·날짜·회차 | 조합별 줄 |
| `zeroloss problems [--json]` | 지금 문제 목록(F21, 진행 중이 위) | 02 2-2 |
| `zeroloss actions [--json]` / `actions done <key>` / `actions choose <key> <후보>` | 사람 할 일 목록(F22) / 감지 못 하는 할 일을 끝냄 표시 / 후보가 있는 할 일에 답함(예: 공유기 WAN 카드, 20장 W-1) | 02 2-3 / 결과 |
| `zeroloss verify [--suite accuracy\|judgement\|loopback …] [--seconds N]` | 설치된 제품 자체 검사(02 2-13, 20장 GAP-81): 기본 `accuracy`·`judgement`(장비 없이 몇 초), `loopback`은 지금 선택으로 짧은 실제 시험 | 통과·실패 목록(실패가 있으면 종료 코드 1) |
| `zeroloss diagnostics [--run <id>]` | 모든 노드 로그·설정·사건 모아 zip(02 2-13, 비밀번호 빠짐, 노드별 최대 50 MB) | 파일 경로 |

- 출력은 **사람용 문장 + `--json` 기계용** 둘 다. 오류는 09-exceptions 코드 그대로.
- GUI와 CLI는 둘 다 Controller 노드 서비스(127.0.0.1:10994)에 붙는다. 같은 상태 저장소·같은 명령 대기열을 쓰므로 충돌하지 않는다(11-ui 1.12). GUI가 없어도, Windows에 로그인하지 않아도 CLI가 동작한다. 시험 중 CLI로 설정 변경은 GUI와 똑같이 거부.
- 계획 파일(`plan.json`)은 GUI에서 [Save plan]으로도 만들 수 있다. 사람이 GUI로 만든 시험을 Claude가 그대로 반복 실행 가능. 형식은 02-data 11-1장.
- **종료 코드(Claude가 결과를 바로 판단하도록 고정):**
  | 코드 | 뜻 |
  |------|-----|
  | 0 | 성공. `run --wait`·`report`는 판정 PASS |
  | 1 | 판정 FAIL |
  | 2 | 판정 INCOMPLETE(중단·회색 포함) |
  | 3 | 시작 거부: 사전 점검 실패·조합 규칙 ⛔·버전 불일치·시험 중이라 거부 |
  | 4 | 확인 필요: 위험 동작에 `--yes` 없음 |
  | 5 | Controller 또는 대상 노드에 연결 못 함 |
  | 6 | 잘못된 명령·계획 파일 형식 오류 |
  | 10 | 내부 오류(진단 묶음 경로를 함께 출력) |
  - `--json` 출력에는 항상 `{"ok":bool,"exit_code":n,"codes":["ZL-…"],"data":{…}}`가 들어 있다.

## 3. 폐쇄 루프 흐름 (개발·검증)
```
① Claude가 코드 수정 → ② zeroloss build → ③ zeroloss deploy (10991)
→ ④ zeroloss verify / run → ⑤ zeroloss report --json · diagnostics
→ ⑥ Claude가 결과 분석 → ①로
```
- 문서(`docs/`)와 결과(보고서·진단 파일)가 모두 Controller PC에 있으므로 Claude가 바로 읽는다.
- **사람 확인이 필요한 동작:** 제거, Reset All, 모든 노드 정지(`nodes stop`, 2026-09-25), 장시간(예: 24시간 이상) 시험 시작, SSH 설치, 다른 지휘 노드 넘겨받기 → CLI는 `--yes` 없이는 실행하지 않고 확인을 요구한다. Claude는 이 동작 전에 사용자에게 묻는다.
- **DUT는 건드리지 않는다 — 읽기 전용 DUT 모듈만 예외:** 도구도 Claude도 DUT에 로그인하지 않는다(설계 원칙 5). 예외는 명시적으로 켠 읽기 전용 DUT 모듈(로그·상태 떠 오기)뿐이고, 공유기 설정은 어떤 모듈도 바꾸지 않는다(2026-10-02). 공유기 설정 변경이 필요하면 Action Required로 사람에게 요청한다. 규칙은 12 4-2 "DUT 접근 규칙".

## 4. 외부에서 원격 지시 (2차)
> 사용자가 밖에 있을 때도, **권한이 있는 사람**이 외부 PC나 휴대폰으로 Controller PC에 지시하면 동작해야 한다.

**방법 (둘 다 지원 목표)**
| 방법 | 내용 |
|------|------|
| A. Claude를 통한 지시 | 휴대폰·외부 PC에서 Controller PC의 Claude 세션에 지시(Claude의 원격 세션 기능) → Claude가 CLI(2장)로 실행하고 결과를 알려줌. **추가 개발이 거의 없고, 확인 규칙(3장)이 그대로 적용됨** |
| B. 원격 조회·조작 웹 | Controller의 결과 웹(10993)을 확장해 로그인한 사용자가 상태 보기·Start/Stop 등 제한된 조작 |

**보안 규칙 (B 방식 또는 외부 접속 전반)**
- 시험망 포트(10991, 10992)는 **절대 외부에 열지 않는다.** 외부에서는 Controller만 접근 가능.
- 외부 접속은 **VPN(예: 사내 VPN, Tailscale 등) 안에서만.** 인터넷에 포트를 직접 열지 않는다.
- 로그인 필수(개인 계정 + 2단계 인증), 암호화 연결(TLS).
- **권한 등급:** 보기만(View) / 시험 실행(Operate) / 관리(Admin: 설치·제거·Reset).
- 위험 동작(제거, Reset All, 장시간 시험, 지휘 넘겨받기)은 원격에서도 **추가 확인**.
- 모든 원격 명령은 누가·언제·무엇을 했는지 **감사 기록**을 남기고 보고서의 사건 기록에도 넣는다.
- 원격 조회 트래픽은 Controller의 외부 NIC로만 다니므로 시험망에 영향 없음(07-network 7장의 "시험 중 무선 조회 금지"와 다름).

## 5. 앞으로 (2차 이후 후보)
- 시험 결과를 Claude가 읽어 **원인 분석 문장 보강**(보고서에 "AI analysis" 항목, 판정에는 영향 없음 — 판정은 항상 Controller 규칙).
- 시험 계획 자동 생성(예: "펌웨어 빌드 A·B 비교 시험 짜 줘").
