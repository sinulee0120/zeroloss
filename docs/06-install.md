# 06. 설치와 제거 (Installer / Uninstaller)

> 목표: **설치한 것은 정확히 기록하고, 제거할 때는 기록한 것만 남김없이 지운다.** 그래야 다음 버전을 설치할 때 이전 버전의 찌꺼기 때문에 생기는 문제가 없다.

## 0. 설치 방식 요약
| 대상 | 최초 설치 | 이후 업데이트 |
|------|-----------|---------------|
| Controller (Windows) | 직접 설치 (원클릭) | 새 설치 파일로 직접 |
| **Linux Agent** | **Controller가 SSH로 원격 설치** (필요한 도구까지 전달·설치) | 배포 포트 **10991** |
| **Windows Agent** | **직접 1회 설치** (원클릭) | 배포 포트 **10991** |
| **태블릿 앱 (APK)** | 수신 Agent가 **adb**로 설치 | Controller → 10991 → 수신 Agent → adb |

- **원클릭:** 직접 설치하는 경우 질문 없이 기본값으로 끝까지 설치한다.
- **설정은 설치 후 Controller 화면에서:** 장비 정보를 자동으로 긁어와 목록으로 보여주고, 사용자는 고르기만 한다(10장).
- **포트 역할:** 10991 = 설치·업데이트 파일 전달(배포 서비스), 10992 = 실제 동작(시험 제어·heartbeat·데이터). 상세는 11장.

## 1. 원칙
1. **설치 목록(Install Manifest):** 설치 프로그램은 자기가 만든 모든 것(파일, 폴더, 서비스, 방화벽 규칙, 레지스트리, 바로가기)을 `install-manifest.json`에 기록한다.
2. **목록 기반 제거:** 제거 프로그램은 설치 목록에 있는 것만, **이름이 정확히 일치할 때만** 지운다. 추측으로 지우지 않는다(접두사 일치, 와일드카드 삭제 금지).
3. **제거 후 검증:** 제거가 끝나면 목록의 모든 항목이 실제로 사라졌는지 다시 확인한다. 남은 항목이 있으면 목록과 함께 **실패로 보고**한다. 조용히 넘어가지 않는다.
4. **시스템을 건드리지 않는다:** 시스템 PATH, 전역 환경 변수, 다른 프로그램 설정을 바꾸지 않는다. 필요한 경로는 ZeroLoss 설정 파일 안에서만 쓴다.
5. **사용자 데이터 분리:** 프로그램 파일과 시험 결과·설정(사용자 데이터)을 **다른 폴더**에 둔다. 제거 시 결과를 지울지 사용자에게 묻는다(기본: 보존).
6. **외부 도구는 소유권 기준:** ffmpeg, TSDuck, mpv, VLC, adb 중 ZeroLoss가 설치한 것만 목록에 적고 제거한다. 원래 있던 것은 건드리지 않는다.
7. **시험 중에는 설치·제거·업그레이드 거부:** 실행 중인 시험이 있으면 멈추라고 안내하고 진행하지 않는다.
8. **무결성 확인:** 설치 패키지 안의 파일 목록과 SHA-256 해시(`payload-hashes.json`)를 설치 전에 확인한다. 하나라도 다르거나 빠지면 설치를 거부한다.
9. **미리 보기와 확인:** 제거는 `--dry-run`(무엇을 지울지 목록만 표시)을 지원하고, 실제 제거 전에 목록을 보여주고 확인을 받는다.

## 2. 설치 구성 요소

| 구성 요소 | 대상 OS | 패키지 | 설치 위치 |
|-----------|---------|--------|-----------|
| Controller | Windows | `ZeroLoss-Controller-Setup-<버전>.exe` | 프로그램: `C:\Program Files\ZeroLoss\Controller\` / 데이터: `C:\ProgramData\ZeroLoss\Controller\` |
| Agent (송신·수신) | Windows | `ZeroLoss-Agent-Setup-<버전>.exe` | 프로그램: `C:\Program Files\ZeroLoss\Agent\` / 데이터: `C:\ProgramData\ZeroLoss\Agent\` |
| Agent (송신·수신) | Linux (Ubuntu/Debian) | `zeroloss-agent_<버전>_amd64.deb` (Controller가 SSH로 전달·설치) | 프로그램: `/opt/zeroloss/agent/` / 설정: `/etc/zeroloss/` / 데이터: `/var/lib/zeroloss/` / 로그: `/var/log/zeroloss/` |
| 태블릿 앱 | Android | `ZeroLoss-<버전>.apk` | 앱 패키지 `com.zeroloss.receiver` (가칭) |

- Agent는 역할 구분 없이 **같은 패키지 하나**를 설치한다. 역할(송신 / 수신 / 둘 다)은 설치 후 Controller 화면에서 고른다.
- 최소 구성(Controller + 송신 한 대): Controller와 Agent를 **각각** 설치한다. 서로 독립적으로 제거·업그레이드된다.
- 외부 도구는 제품 패키지에 넣지 않고 **별도 도구 패키지**(`ZeroLoss-Tools-<버전>`)로 배포한다. 라이선스 원문을 함께 넣는다.

### 2.1 외부 도구 버전 정책
| 도구 | 버전 정책 | 받는 곳 |
|------|-----------|---------|
| **adb (Android platform-tools)** | **항상 최신 버전.** 오래된 adb는 최신 Android 태블릿과 연결이 불안정하다 | Google 공식 platform-tools 배포본 (Windows/Linux) |
| ffmpeg / ffprobe | 검증된 버전 고정 (libx264·libx265 포함 빌드) | 공식 배포처 |
| TSDuck | 검증된 버전 고정 | TSDuck 공식 배포본 |
| mpv, VLC | 검증된 버전 고정 | 공식 배포처 |
| iperf3 | (2차: 속도 시험 추가 시) | ESnet 공식 |

- **adb 최신 유지 방법:**
  - Controller가 인터넷에 연결되면 Google 공식 배포처에서 **최신 platform-tools를 받아 오프라인 묶음을 갱신**한다(Windows용, Linux용 모두).
  - 수신 Agent 설치·업데이트 때 최신 adb를 함께 전달한다(10991). 수신 Agent는 자동 발견 화면에 **adb 버전**을 보고하고, Controller가 가진 최신보다 오래됐으면 노랑으로 표시하고 [adb 업데이트] 버튼을 보여준다.
  - 받은 파일의 SHA-256과 버전을 기록한다.
- **adb 충돌 방지:** 한 PC에 서로 다른 버전의 adb 서버가 돌면 연결이 계속 끊긴다(버전 불일치로 서버가 재시작됨). ZeroLoss는 **자기 폴더의 adb만** 쓰고, 시작할 때 다른 버전의 adb 서버가 떠 있으면 경고하고 정리 방법을 안내한다.

## 3. 설치 도구 선택
| OS | 도구 | 이유 |
|----|------|------|
| Windows | **Inno Setup** | 무료, 상용·배포 제약 없음. 제거 프로그램 자동 생성, "앱 및 기능" 등록, 서비스·방화벽 처리를 스크립트로 제어 가능 |
| Linux | **.deb 패키지** (dpkg/apt) | 패키지 관리자가 설치 파일을 추적. `remove`/`purge`로 깔끔한 제거, 버전 업그레이드 표준 지원 |
| Android | APK (adb 설치) | 수신 Agent가 USB(adb)로 설치·제거·버전 확인 가능 |

## 4. 설치가 만드는 항목 (전체 목록)

### 4.1 Windows Controller
| 종류 | 이름 | 비고 |
|------|------|------|
| 폴더 | `C:\Program Files\ZeroLoss\Controller\` | 프로그램 |
| 폴더 | `C:\ProgramData\ZeroLoss\Controller\` | 설정 (사용자 데이터) |
| 폴더 | `C:\ZeroLoss\Reports\` | **결과 보고서** (사용자 데이터, 서비스 쓰기·사용자 읽기 권한, 제거 시 기본 보존) |
| 서비스 | `ZeroLossNode` (노드 Core + 지휘 역할, 자동 시작, 실패 시 자동 재시작) | 지휘는 여기서 돈다(12 1-0) |
| 서비스 | `ZeroLossDeployer` (10991, 자동 시작) | 모든 노드와 같음. Agent·Controller 서로 감시 |
| 로그인 시 자동 실행 | Controller Desktop 트레이 모드(11-ui 2-1) | 사용자별 시작 항목, 제거 시 삭제 |
| 방화벽 규칙 | 10991·10992 | 같은 PC에 송신·수신 역할을 켤 때만 Agent와 같은 규칙 추가 |
| 방화벽 규칙 | `ZeroLoss-Controller-Web-TCP10993` | 결과 조회 웹 포트 **TCP 10993**. 기본: 같은 서브넷만 허용 |
| 방화벽 규칙 | `ZeroLoss-Controller-Discovery-UDP` | 자동 발견 응답 수신 |
| 바로가기 | 시작 메뉴 `ZeroLoss Controller` | |
| 레지스트리 | `HKLM\Software\ZeroLoss\Controller` | 버전, 설치 경로만 |
| 앱 등록 | "앱 및 기능"의 ZeroLoss Controller | Inno Setup이 관리 |

#### 4.1-1 Windows Controller 휴대 빌드 (설치 파일 전 단계, 2026-10-03 사용자 "내가 실행해볼 exe도 없어", 20장 GAP-108)
- `python -m tools.build win`(Windows에서) → `dist/ZeroLoss-Controller-<빌드>.zip`. 풀면 `ZeroLoss.exe`(창) + `cli\zeroloss.exe`(서비스·CLI) + `modules\`(서비스가 실행 중에 불러옴, 설치된 노드와 같은 방식).
- **빌드 번호는 새로 찍지 않는다:** 짝이 되는 노드 빌드(`.deb`)가 찍은 VERSION을 그대로 쓴다(Controller와 노드의 빌드가 같아야 F17 1단계를 지남). 8장 "빌드마다 새 번호"의 예외 — 한 번호로 두 OS 결과물을 만드는 것.
- `ZeroLoss.exe` 더블클릭: 127.0.0.1:10994가 응답하지 않으면 Controller 서비스(`cli\zeroloss.exe serve --role controller`)를 창 없이 따로 띄우고(창을 닫아도 서비스·시험은 계속, 12 1-0), Controller Desktop을 연다. 이미 도는 서비스가 있으면 그것에 붙는다.
- **창을 닫으면(2026-10-04 GAP-116, 사용자가 작업 관리자로 강제 종료해야 했음):** 화면은 일하는 스레드를 기다리지 않고 바로 닫힌다(응답 없는 PC에 묻는 중이어도). 그다음 시험·시작·상시 방송이 없으면 서비스에 `quit`(02 2-13)을 보내 서비스도 끝난다 — 남는 프로세스가 없다. 시험 중이면 서비스는 계속(12 1-0).
- 데이터 `C:\ProgramData\ZeroLoss\Controller\`, 보고서 `C:\ZeroLoss\Reports\`(위 표와 같은 곳 — 나중에 설치 파일로 바꿔도 이어짐), 서비스 출력 `…\Controller\logs\controller-service.log`.
- 아직 없음(설치 파일, V18): Windows 서비스 등록·자동 시작·Deployer, 방화벽 규칙, 트레이·로그인 때 자동 실행, 시작 메뉴, 제거.

### 4.2 Windows Agent
| 종류 | 이름 | 비고 |
|------|------|------|
| 폴더 | `C:\Program Files\ZeroLoss\Agent\` | 프로그램 |
| 폴더 | `C:\ProgramData\ZeroLoss\Agent\` | 설정·기록 (사용자 데이터). 권한: SYSTEM, Administrators |
| 서비스 | `ZeroLossAgent` | 자동 시작. 운영 포트 TCP 10992 |
| 자동 시작 항목 | 로그인 시 실행 `ZeroLoss Agent Desktop` | 재생기·1초 모니터 창 표시(Windows 서비스는 세션 0에서 돌아 화면에 창을 못 띄움, D38) |
| 서비스 | `ZeroLossDeployer` | 자동 시작. **배포 포트 TCP 10991.** 항상 살아 있음. Agent 설치·업데이트·복구 담당 |
| 방화벽 규칙 | `ZeroLoss-Deployer-TCP10991` | 페어링 전: 같은 서브넷만. 페어링 후: Controller(또는 DUT WAN IP)만. 키 서명은 옵션(기본 꺼짐) |
| 방화벽 규칙 | `ZeroLoss-Agent-Control-TCP10992` | 설치 직후: 같은 서브넷만 허용. 페어링 후 LAN 쪽은 Controller IP, WAN 쪽 송신 PC는 DUT WAN IP로 자동 축소. 모든 요청은 페어링 키 서명(HMAC) 확인 (`07-network.md` 6장) |
| 방화벽 규칙 | `ZeroLoss-Agent-Discovery-UDP10992` | 자동 발견 응답용. 페어링 전과 설정 화면에서만 응답, **시험 중에는 응답 안 함** |
| 방화벽 규칙 | `ZeroLoss-Agent-Media-UDP` | 수신 역할일 때만. 영상 수신 포트 |
| 방화벽 규칙 | `ZeroLoss-Agent-DHCP-UDP67` | WAN IP 방식 B(송신 PC 통신사 역할, 07 3.2)용. **설치 시 비활성**, 통신사 역할을 켤 때 그 WAN NIC에 한해 활성화(Windows 통신사 역할은 V18 때) |
| 레지스트리 | `HKLM\Software\ZeroLoss\Agent` | 버전, 역할, 설치 경로만 |
| 앱 등록 | "앱 및 기능"의 ZeroLoss Agent | |

### 4.3 Linux Agent (.deb) — `zeroloss-agent_<빌드>_amd64.deb` (2026-09-25 구체화, 20장 GAP-54)
| 종류 | 이름 | 누가 만들고 지우나 | 비고 |
|------|------|--------------------|------|
| 폴더 | `/opt/zeroloss/agent/` | dpkg | 노드 실행 파일 `zeroloss`(PyInstaller onedir, 런타임 동봉). PySide6는 넣지 않음(노드 서비스는 화면이 없음, D38) |
| 폴더 | `/opt/zeroloss/desktop/` | dpkg | Agent Desktop 실행 파일 `zeroloss-agent-desktop`(따로 onedir, PySide6 위젯만, 03 F20 — 로그인한 사용자 세션에서 상태 창·재생기). 화면에 `libxcb-cursor0`이 필요 — .deb `Recommends`(인터넷 없는 `dpkg -i`도 노드는 설치됨), SSH 설치가 apt로 넣음(11.3 4-3, GAP-73 ②) |
| 폴더 | `/opt/zeroloss/deployer/` | dpkg | Deployer 실행 파일 `zeroloss-deployer`(따로 onedir — 노드가 망가져도 살아 있게, 11.2). Core 코드 없음(12 1-1) |
| 폴더 | `/opt/zeroloss/modules/` | 설치 스크립트가 만들고, 안은 **Deployer 소유**(모듈별 설치 목록, 12장) | 제거 때 모듈 코드를 목록대로 지운 뒤 비었을 때만 지움 |
| 파일 | `/etc/zeroloss/zeroloss.conf` | dpkg conffile | 포트(10991·10992)·데이터 폴더. 업그레이드해도 사용자가 바꾼 값 유지 |
| 패키지 의존(Depends) | `adduser, systemd` | dpkg | 노드가 도는 데 꼭 필요한 것만. 인터넷 없는 `dpkg -i`도 노드는 설치되어야 한다. 코드 `tools/build.py` `DEPENDS`와 자동 비교 |
| 패키지 권장(Recommends) | `libxcb-cursor0` | dpkg(없어도 설치됨) / SSH 설치가 apt로 넣음 / **손으로 설치할 때는 `sudo apt install ./zeroloss-agent_….deb`**(권장까지 설치. `dpkg -i`만 하면 Agent Desktop이 "Could not load the Qt platform plugin xcb"로 안 뜸, 2026-10-10 T460, 20장 GAP-151) | Qt 6.5+ 화면(xcb)이 요구 — 없으면 깨끗한 Ubuntu에서 Agent Desktop 창이 안 뜸(2026-09-26 첫 실제 설치 빌드 경고, 20장 GAP-73 ②). 코드 `RECOMMENDS`와 자동 비교 |
| 폴더 | `/var/lib/zeroloss/` | 설치 스크립트 | 데이터 한 곳: 노드 기록·시험 자료·`logs/`·`modules/<id>/`·`deployer/`(Deployer 작업 폴더). 주인 `zeroloss`, `deployer/`는 root. Deployer(root)가 만드는 `modules/<id>/`는 주인을 `zeroloss`로 바꾼다(노드가 캐시를 쓰게). Deployer가 노드에 남기는 재시작 쪽지(SYS-009)는 노드가 지울 수 있게 `/var/lib/zeroloss/restart-note.json`. **사용자 데이터 — `apt remove`는 보존, `purge`는 삭제**(7장). 로그를 따로 `/var/log`에 두지 않는다(한 폴더만 지키면 되게) |
| 서비스 | `zeroloss-agent.service` (systemd) | dpkg + 설치 스크립트가 켬 | `User=zeroloss`, 필요한 권한만: `CAP_NET_ADMIN`(통신사 역할이 WAN NIC에 10.0.0.1/24를 더하고 빼기, 07 3.2 B), `CAP_NET_BIND_SERVICE`(내장 DHCP 67번), `CAP_NET_RAW`(DHCP를 한 NIC에만 묶기 `SO_BINDTODEVICE`, M2U 확인 캡처, 프라임 방식 재생기 프라임), `CAP_SYS_NICE`(송신 경로 — tsp와 고른 간격 송신기 — 를 그 PC에서 맨 먼저: SCHED_RR 20, 안 되면 nice −15, 2026-10-23 사용자 "Tx가 우선순위가 가장 높아야겠지", core/blocks/priority). `KillMode=control-group`(서비스를 멈추면 띄운 송출·재생도 같이, 12 4-3), `Restart=on-failure` |
| 서비스 | `zeroloss-deployer.service` (systemd) | 〃 | root(패키지·모듈 설치용), `Restart=always`, 배포 포트 TCP 10991, 노드 감시(SYS-009) |
| polkit 규칙 | `/etc/polkit-1/rules.d/90-zeroloss.rules` | 설치 스크립트가 쓰고 제거 때 지움(sysctl 파일과 같이 conffile 아님) | 계정 `zeroloss`에게 NetworkManager 동작 세 가지만 허용(`settings.modify.system`, `network-control`, `wifi.scan`) — 무선 수신 PC의 시험 Wi-Fi 연결(03 F17 "무선 수신 PC", 2026-09-25) + login1 `inhibit-block-sleep`·`inhibit-block-idle` — 시험 동안 절전 막기(09 SYS-005, 2026-09-26). 노드 설치 목록 `polkit_rule` |
| 태블릿 USB 규칙 | `/etc/udev/rules.d/70-zeroloss-adb.rules` | 설치 스크립트가 쓰고 `udevadm control --reload` + `trigger`(이미 꽂힌 태블릿도), 제거 때 지움 | adb 인터페이스(USB class ff/42/01)가 있는 장치만 `MODE=0660 GROUP=zeroloss` — 노드 계정이 태블릿 1~3대를 adb로 다룸(01 D3). 그 전에는 화면 앞 사람(uaccess)만 열 수 있었음(2026-10-23 T460 실측). 노드 설치 목록 `udev_rule` |
| 자동 로그인(원할 때만) | `/etc/gdm3/custom.conf` `[daemon]` 두 줄 | SSH 설치 `--auto-login` 때만(11.3 4-2), 제거 때 원래 파일로 | 노드 설치 목록 `auto_login` |
| 절전 끄기 | systemd `sleep.target`·`suspend.target`·`hibernate.target`·`hybrid-sleep.target` mask(`/etc/systemd/system/<target>` → `/dev/null`) | 설치 스크립트가 걸고 제거 때 풂 — **설치 때 이미 mask였던 것은 기록하지 않고 풀지도 않음**(관리자 것) | 시험 PC는 절전하지 않는다(2026-10-03 사용자 결정 "니가 해" — 사람 손 0, 20장 GAP-107). 시험 중 inhibitor(09 SYS-005)가 막히거나 노드가 재시작돼도 PC가 잠들지 않게. 노드 설치 목록 `sleep_mask`(대상마다 한 줄) |
| 시험 중 자동 업데이트 멈춤 | `/etc/apt/apt.conf.d/99zeroloss-test-hold` | 시험 동안만 Deployer가 두고 끝나면 지움(09 SYS-005). 설치 목록에 넣지 않음(늘 있는 파일이 아님), 제거 때 남았으면 지움 | `Unattended-Upgrade::Automatic-Reboot "false"`, `APT::Periodic::Unattended-Upgrade "0"` |
| 계정 | `zeroloss` (시스템 계정, 로그인 불가) | 설치 스크립트 | `purge` 때 지움 |
| 방화벽 규칙 | TCP 10991·10992, UDP 10992(발견), UDP 67(통신사 역할의 DHCP — 서버는 WAN NIC 하나에만 묶여 다른 NIC에서는 답하지 않음, 07 3.2 B) 허용 | 설치 스크립트 | **ufw가 켜져 있을 때만** 추가하고 추가한 규칙만 노드 설치 목록에 기록(1장 원칙 6). 페어링 뒤 범위 좁히기는 07 6장 |
| 커널 설정 | `/etc/sysctl.d/90-zeroloss.conf` (`net.core.rmem_max` = 16 MB) | 설치 스크립트가 쓰고 바로 적용, 제거 때 지움 — **dpkg conffile로 두지 않는다**(conffile은 `remove`에 안 지워져 재부팅 때 되살아남) | **설치 때 항상**(2026-09-25 사용자 결정: 역할은 설치 뒤에 정해지고 올려도 해가 없음). 원래 값을 기록해 두고 제거 때 되돌림 |
| 자동 시작 항목 | `/etc/xdg/autostart/zeroloss-agent-desktop.desktop` (사용자 로그인 시 `/opt/zeroloss/desktop/zeroloss-agent-desktop`) | P3 Agent Desktop(F20, 2026-09-26) | .deb 파일로 설치·제거. 노드 서비스는 `--player-host desktop`: 재생기는 로그인한 사용자의 Desktop이 띄운다(03 F20 구현 규칙). 로그인 안 됨 = 재생 켬 수신 시작 실패 RCV-010 |
| 사용자별 Desktop 폴더 | `~/.local/share/zeroloss/desktop/`(`desktop.json` = 16 S-53, `jobs/` = 재생기 작업 파일) | Agent Desktop이 만듦. 작업 폴더는 작업이 끝나면·Desktop이 켜질 때 지움 | 패키지 제거 때 모든 사용자 것을 지움(비면 `zeroloss/`도) |
| 되돌림 사본·업데이트 기록 | `/var/lib/zeroloss/deployer/packages/current.deb`, `deployer/update.json` | SSH 설치·10991 업데이트(11.5) | 패키지 제거 때 지움(사용자 데이터 아님) |
| 노드 설치 목록 | `/var/lib/zeroloss/deployer/manifests/node-agent.json` | 설치 스크립트 | dpkg가 모르는 것만(계정·방화벽 규칙·sysctl 원래 값·만든 폴더, 02 14 `account`·`firewall_rule`·`sysctl_file`·`dir`). 제거 후 검증(7장 6번)에 쓴다 |

#### 4.3-1 설치 스크립트(maintainer script)가 하는 일
> 셸 스크립트는 한 줄씩만 두고 실제 일은 Deployer 실행 파일의 하위 명령이 한다(`zeroloss-deployer node-setup|node-remove`) — 파이썬으로 짜서 임시 폴더를 뿌리로 삼아 자동 시험할 수 있게(12 1-1 L0, 표준 기능만). **dpkg는 파일을 지운 뒤 postrm을 부르므로** 실행 파일이 필요한 정리는 모두 prerm에서 하고, postrm은 셸 몇 줄(빈 폴더 지우기, purge)만 한다.

| 때 | 하위 명령 | 하는 일(차례대로) |
|----|-----------|-------------------|
| `postinst configure` | `node-setup` | ① 계정 `zeroloss` 없으면 만듦 ② `/var/lib/zeroloss`(주인 zeroloss)·`deployer/`(root)·`/opt/zeroloss/modules` 만듦 ③ `net.core.rmem_max` 원래 값 기록(처음 한 번) → sysctl 파일 적용 ③-1 polkit 규칙 파일 쓰기 ③-2 절전 대상 네 개 mask(이미 mask면 건너뜀, 건 것만 설치 목록 `sleep_mask`, GAP-107) ③-3 GPU 그룹 `render`·`video`가 있으면 `zeroloss`를 넣음(이미 회원이면 건너뜀, 넣은 것만 `group_member` — 노드의 해독 시험이 VA-API로 GPU를 엶, 2026-10-16 T460 GAP-165) ④ ufw가 켜져 있으면 TCP 10991·10992와 **UDP 10992**(자동 발견, 10.2)·**UDP 67**(통신사 역할, 07 3.2 B) 허용 규칙 추가 ⑤ 노드 설치 목록 쓰기 ⑥ 두 서비스 켜기(enable --now) |
| `prerm upgrade` | `node-remove --stop-only` | 서비스만 멈춤(postinst가 다시 켬) |
| `prerm remove` | `node-remove` | 서비스 멈춤·끄기 → **모듈 코드를 모듈 설치 목록대로** 지움(설정은 남김, 캐시는 지움 — 12장 제거 규칙) → 열린 Agent Desktop 끝냄 → `deployer/packages/`·`update.json`·남은 apt 멈춤 파일 지움 → 추가한 방화벽 규칙 지움 → sysctl 파일 지우고 원래 값으로 되돌림 → polkit 규칙 파일 지움 → 설치가 건 절전 mask만 풂(`sleep_mask`) → 설치가 넣은 GPU 그룹에서만 뺌(`group_member`) → 자동 로그인을 켰으면 GDM 파일 원래대로(11.3 4-2) → 송신 역할이 더한 WAN 주소 뺌 → 노드 설치 목록 갱신·남은 것 검사(INS-021) |
| `postrm remove` | (셸) | 비어 있으면 `/opt/zeroloss/modules`·`/opt/zeroloss` 지움 |
| `postrm purge` | (셸) | `/var/lib/zeroloss`(사용자 데이터) 지움, 계정은 **설치 스크립트가 만든 경우에만**(표시 파일 `/var/lib/zeroloss/.account-created`) 지움 |
- 업그레이드(dpkg가 새 버전으로 바꿈)에서는 설정·데이터·모듈을 건드리지 않는다(6장). 모듈의 `core_api` 범위가 새 Core와 안 맞으면 그 모듈만 비활성(INS-024).
- 실행 파일은 대상과 같은 OS 버전·CPU 구조의 Linux PC에서 PyInstaller로 만든다(11.3 빌드 위치). 모듈은 실행 파일에 넣지 않으므로, **모듈 코드가 쓰는 표준 라이브러리는 빌드 때 모듈 폴더를 읽어 전부 함께 넣는다**(모듈이 새로 쓰는 표준 모듈 때문에 설치본에서만 실패하지 않게).

### 4.4 Android 앱
- 설치: 수신 Agent가 `adb install`로 설치하거나, 사용자가 APK를 직접 설치.
- 제거: `adb uninstall <패키지>` 또는 태블릿 설정에서 제거. 앱 데이터도 함께 삭제된다.
- **업그레이드 조건:** 같은 서명 키로 서명해야 덮어쓰기 설치가 된다. 서명 키를 잃어버리면 기존 앱을 지우고 새로 설치해야 하므로, **서명 키는 별도로 안전하게 보관**한다.

## 5. 설치 절차
1. **사전 점검**(하나라도 실패하면 중단하고 이유 표시)
   - 관리자/root 권한
   - OS 버전
   - 디스크 여유 공간(100시간 기록 용량 포함)
   - TCP 10992 포트 사용 가능 여부 (Agent)
   - 패키지 무결성(SHA-256)
   - 실행 중인 시험 없음
2. **기존 설치 확인** → 6장(업그레이드) 규칙 적용
3. 파일 복사, 서비스·방화벽·설정 생성, **설치 목록 기록**
4. 서비스 시작 → `/v1/health` 응답 확인
5. 외부 도구 확인: 필요한 도구가 있는지, 버전이 맞는지 검사한다. 결과는 Controller 화면에서도 보인다. 없으면 도구 패키지 설치를 안내한다.
6. 설치 결과 요약 표시(만든 항목 목록, 버전). **사용자 입력 없이 여기까지 끝난다.**
7. 이후 설정은 Controller 화면의 자동 발견(10장)에서 한다.

## 6. 업그레이드 (다음 버전 설치)
| 상황 | 동작 |
|------|------|
| 설치 안 됨 | 새로 설치 |
| **같은 버전** | 복구(Repair): 목록 기준으로 빠진 파일·설정 복원 |
| **이전 버전** | 업그레이드: 아래 순서 |
| 더 새 버전이 이미 있음 | **다운그레이드 거부** (옵션으로만 허용) |

업그레이드 순서:
1. 시험 실행 중이면 거부
2. 서비스 중지
3. **설정 백업** (`설정 파일 + 설치 목록`을 백업 폴더로)
4. **이전 버전의 설치 목록으로 이전 버전 제거** (사용자 데이터는 보존)
5. 새 버전 설치
6. 설정 이전: 설정 파일에 `schema_version`을 두고, 새 버전이 이전 형식을 변환한다
7. 서비스 시작 → `/v1/health` 확인
8. **실패하면 되돌림:** 새 버전 제거 → 백업으로 이전 버전 복원 → 실패 원인 표시

## 7. 제거 절차
1. 실행 중인 시험이 있으면 거부
2. `--dry-run`: 지울 항목 목록만 표시
3. 목록 표시 후 확인 받기
4. 서비스 중지·삭제 → 방화벽 규칙 삭제 → 커널 설정 복원 → 파일·폴더 삭제 → 레지스트리 삭제 → 계정 삭제(Linux)
5. **사용자 데이터(결과·설정)**: 삭제 여부를 묻는다. 기본은 보존. (Linux: `apt remove`는 보존, `apt purge`는 삭제)
6. **제거 후 검증:** 목록의 모든 항목이 사라졌는지 확인하고, 남은 항목이 있으면 목록과 함께 실패 보고
7. 제거 기록(`uninstall-report.txt`)을 남긴다. 사용자 데이터를 보존했다면 그 폴더에, 아니면 임시 폴더에 남긴다.

## 8. 버전과 호환성
- **버전 = 빌드 날짜·시각** 하나로 통일한다. 이름을 따로 짓지 않는다. **빌드 명령(`tools.build deb`·`module`)이 빌드할 때마다 VERSION을 새로 찍는다**(2026-09-26, 20장 GAP-73 ③ — 옛 값으로 빌드되던 결함). `deb` 명령은 .deb·.zlp와 **모든 모듈 .zlm을 한 번 찍은 같은 값으로** 만든다 — 따로 빌드하면 값이 둘로 갈려 Controller·노드 빌드가 달라짐(INS-015, 첫 실제 시험에서 발생, GAP-88).
  - 기본 형식: `YYYYMMDD.HHMM` (예: `20260923.1432`). Controller, Agent, Deployer, APK, 설치 파일, 보고서 모두 같은 값.
  - 형식 제약 때문에 자동 변환만 한다(사람이 따로 정하지 않음):
    - Windows 파일 버전(숫자 4칸): `2026.923.1432.0`
    - Android versionCode(정수, 계속 증가): `YY×10,000,000 + 연중일×10,000 + HHMM` (예: `262661432`)
    - Linux .deb: `20260923.1432`
  - 설치 파일 이름: `ZeroLoss-Controller-Setup-20260923.1432.exe` 등.
- **버전은 한 곳에만:** 저장소 맨 위 `VERSION` 파일 하나. 빌드 스크립트가 빌드할 때 날짜·시각으로 만들어 각 구성 요소에 **자동으로 넣는다.** 코드 안에 버전을 직접 적지 않는다.
- 통신 호환성은 빌드 버전과 따로 `protocol_version`(정수, 통신 형식이 바뀔 때만 올림)으로 판단한다.
- 제어 프로토콜에 `protocol_version`을 둔다. Controller는 시험 시작 전 모든 Agent와 태블릿 앱의 버전을 확인하고, **호환되지 않으면 시험을 시작하지 않고** 어떤 장비를 업그레이드해야 하는지 알려준다.
- 보고서에 모든 구성 요소의 버전을 기록한다.

## 9. 설치·제거 자체의 시험 (완료 기준)
> **실제 PC 설치·제거 시험은 실제 Linux PC(와 새 Windows PC)가 올 때**(2026-09-25 사용자 결정). 그 전에는 패키지·설치 목록·Deployer 논리를 임시 폴더에서 자동 시험한다(12장).
- [ ] 깨끗한 PC에 설치 → 제거 → **설치 전 상태와 비교해 남은 것이 없음** (파일, 서비스, 방화벽, 레지스트리, 계정, sysctl)
- [ ] 이전 버전 설치 → 새 버전 업그레이드 → 설정 유지, 정상 동작
- [ ] 업그레이드 중 강제 실패 → 이전 버전으로 되돌림 확인
- [ ] 같은 버전 재설치(복구) → 정상 동작
- [ ] 시험 실행 중 설치·제거 시도 → 거부
- [ ] 원래 있던 외부 도구(예: 사용자가 설치한 VLC)가 제거 후에도 그대로 있음
- [ ] 패키지 파일 하나를 바꾼 뒤 설치 → 무결성 오류로 거부
- [ ] Android: 새 버전 APK 덮어쓰기 설치 → 설정 유지 / 제거 후 흔적 없음

## 10. 설치 후 자동 발견과 선택 (Controller 화면)

### 10.1 흐름
1. Controller를 실행하면 **자동으로 장비 찾기**를 시작한다.
2. 찾은 Agent와 각 Agent에 연결된 장비 정보를 목록으로 보여준다.
3. 사용자는 목록에서 **고르기만** 한다: Agent 역할(송신/수신), 사용할 네트워크 카드, 태블릿, 채널 배정.
4. 고르면 Controller가 Agent와 **연결(페어링)**한다. 이후 Agent는 이 Controller의 명령만 받는다.
5. 선택 결과는 저장되어 다음 실행 때 그대로 불러온다. 장비 구성이 바뀌었으면 달라진 점을 표시한다.

### 10.2 Agent 찾는 방법 (위에서부터 차례로 시도)
| 방법 | 동작 | 언제 |
|------|------|------|
| ① 방송 발견 | Controller가 UDP 10992으로 "ZeroLoss Agent 있나요?" 방송 → Agent가 자기 정보로 응답 | 같은 서브넷 (기본) |
| ② 서브넷 스캔 | Controller가 선택한 IP 범위(예: /24)에 TCP 10992 `/v1/health`를 차례로 조회 | 방송이 막힌 환경 |
| ③ 직접 입력 | 사용자가 IP를 입력 | 서브넷이 다른 경우 |

- 모든 PC가 DUT 한 대에 연결된다. NAT 모드에서는 방송이 WAN 쪽에 닿지 않으므로 송신 PC는 **권장 주소(10.0.0.1·10.0.0.2)를 자동으로 시도**하고, 등록된 송신 PC가 옮겨지면 스스로 다시 찾는다(아래 구현 규칙). 그래도 못 찾을 때만 IP를 한 번 입력한다(송신 PC 화면의 "My IP"). 모드별 규칙은 `07-network.md` 5장.
- 발견은 **설정 단계에서만** 한다. 시험이 시작되면 방송·스캔을 멈추고 Agent도 발견 요청에 응답하지 않는다.
- **구현 규칙(2026-09-26, 20장 GAP-59):**
  - 노드는 TCP와 같은 번호의 **UDP 포트**(기본 10992)에서 발견 요청을 듣고, **노드가 IDLE일 때만** 답한다(시험 중·정지 중에는 조용히). 요청 `ZLDISCOVER 1 <nonce>`(ASCII), 답은 JSON 한 줄(02 4-1). 요청에 답할 뿐 먼저 보내지 않는다(07 6 연결 규칙).
  - Controller(`POST /v1/controller/discover`, CLI `zeroloss discover`)는 차례로: ① 방송 — 자기 NIC마다 그 대역 방송 주소와 255.255.255.255로 요청 3번(0.3초 간격), 1.5초 동안 답 모음 ② 송신 PC 권장 주소 **자동 시도**(10.0.0.1·10.0.0.2, 07 5 — NAT 모드에서는 방송이 WAN에 안 닿으므로) ③ `scan`을 준 경우만 그 범위(/22까지)에 TCP `/v1/health`(0.3초 제한, 동시 64개). 같은 `device_id`는 한 줄로 합친다.
  - 결과 줄: `device_id`·주소·포트·호스트 이름·역할·빌드·찾은 방법(`broadcast`/`try`/`scan`)·**이미 등록됨**(11-0 목록에 같은 `device_id`)·이름 제안(호스트 이름, 겹치면 뒤에 주소 끝자리). 등록은 사용자가 고른다(`devices add`, 화면은 F14 때).
  - 시험 중이면 RUN-012(07 7 발견 금지). 하나도 없으면 DSC-001, 자동 시도가 다 안 되면 결과에 `sender_try: "not found"`(DSC-010 안내), 호스트 이름이 같은 다른 장비는 DSC-002 표시.
  - 방화벽: Linux 설치 스크립트가 ufw가 켜져 있으면 **UDP 10992**도 허용(06 4.3 표 ④), Windows는 설치 파일 때.
  - **주소가 바뀐 등록 장비(2026-09-26 사용자 결정, 20장 GAP-73 ④):** 발견이 등록된 장비와 같은 `device_id`를 **다른 주소**에서 찾으면 등록 주소를 자동으로 고친다(사람이 다시 등록하지 않음) + 사건 `ZL-INFO` "<장비> moved from <옛 주소> to <새 주소>"(경험 DB). 예: B 방식에서 송신 PC를 LAN에서 설치한 뒤 WAN으로 옮기면 192.168.x → 10.0.0.1. 시험 중에는 발견을 하지 않으므로 고치지 않는다. **스스로 찾기:** 사용자가 발견을 누르지 않아도, 설정·시작 화면이 장비 상태를 볼 때(02 2-11) 등록된 장비가 답하지 않으면 Controller가 발견(방송 + 송신 권장 주소)을 한 번 해서 고친다 — 시험 사이에만, 30초에 한 번까지(GAP-75). 옛 주소가 아직 답하거나 새 주소에 TCP가 닿지 않으면 고치지 않는다.

### 10.3 자동으로 긁어와 보여주는 정보
| 대상 | 정보 |
|------|------|
| Agent 공통 | 호스트 이름, OS, ZeroLoss 버전, 관리용 IP, 페어링 상태 |
| 네트워크 카드 | 이름, 유선/무선, IP, 서브넷, 기본 게이트웨이, 링크 속도, (무선) SSID·신호 세기·채널·주파수 대역 |
| 추정 표시 | 기본 게이트웨이가 DUT로 보이는 카드 → "DUT 연결 후보"로 표시. WAN 쪽/LAN 쪽 후보 추정 |
| 외부 도구 | ffmpeg, TSDuck, mpv, VLC, adb 설치 여부와 버전 (없거나 버전이 안 맞으면 빨간색) |
| 태블릿 (수신 Agent의 adb) | USB로 연결된 태블릿 목록: 일련번호, 모델, Android 버전, ZeroLoss 앱 설치 여부·버전, Wi-Fi IP, SSID, 신호 세기 |
| 포트 | TCP 10992 사용 가능 여부, 영상 포트(기본 5004) 사용 중 여부 |

### 10.4 사용자가 고르는 것
- 송신 Agent와 송신에 쓸 네트워크 카드(DUT WAN 쪽)
- 수신 PC Agent와 수신에 쓸 네트워크 카드(유선 또는 무선)
- 시험에 쓸 태블릿(1~3대)과 태블릿별 채널
- 앱이 없거나 버전이 다른 태블릿은 목록에서 **[앱 설치/업데이트]** 버튼으로 바로 처리(adb)

- **송신·수신 역할(2026-09-26 사용자 결정, 20장 GAP-63):** 송신은 **기본으로 공유기 WAN에 연결된 PC**다(통신사가 보내는 것이므로). 자동으로 정한다: 통신사 역할이 켜진 PC(07 3.2 B) 또는 NAT 판별에서 공유기 WAN 쪽으로 보이는 PC(07 4) = 송신, 공유기 LAN·Wi-Fi 쪽 PC = 수신. 한쪽 후보가 둘 이상이거나 알 수 없으면 장비 줄의 목록에서 고른다. 한 번 정하면 저장(F14 원칙 4).
  - **비교 시험:** LAN 쪽 PC를 송신으로 골라도 된다(LAN→LAN, 공유기 안에서 스누핑만 거침). 이때 보고서 `dut.multicast_path`에 `lan`, 맨 위에 "Sender on LAN (comparison test)", 프라임 방식(S-18)은 쓰지 않는다(뚫을 NAT가 없음).

### 10.5 기본값 자동 선택
- 후보가 하나뿐이면 자동으로 골라 두고, 사용자는 확인만 한다.
- 추천한 선택에는 이유를 한 줄로 표시한다(예: "게이트웨이 192.168.1.1이 DUT로 보임").

## 11. 원격 설치·업데이트 (배포 경로)

### 11.1 포트
| 포트 | 서비스 | 용도 | 항상 켜짐 |
|------|--------|------|-----------|
| **TCP 10991** | 배포 서비스 (ZeroLoss Deployer) | Agent 앱·도구·APK 파일 전달, 설치·업데이트·복구·버전 조회 | **예** (Agent가 죽어도 살아 있음) |
| **TCP 10992** | Agent | 시험 제어, heartbeat, 1초 핵심값·5분 요약, 태블릿 제어 중계 | Agent 실행 중 |
| UDP 10992 | Agent | 설정 단계 자동 발견 | 설정 단계만 |
| TCP 22 | OS의 SSH | **Linux Agent 최초 설치에만** | OS 설정에 따름 |

- 포트 번호 10991/10992는 IANA 등록 목록에서 **미할당 구간(10991~10999)**이라 다른 프로그램과 충돌 가능성이 낮다. (9990은 WildFly/JBoss 관리 콘솔 기본 포트라 제외)

### 11.2 배포 서비스(Deployer)를 따로 두는 이유
- 실행 중인 프로그램은 자기 자신을 교체할 수 없다. **Agent 업데이트는 Deployer가**, Deployer 업데이트는 **Agent가** 맡는다(서로 교체). **Linux(2026-09-26 사용자 결정):** .deb 하나에 둘이 함께 있고 .deb의 `prerm`이 두 서비스를 멈추므로(Deployer가 직접 `dpkg -i`를 돌리면 dpkg도 같이 죽는다), Deployer(root 서비스)가 설치를 **systemd의 일회용 작업**(`systemd-run`)으로 따로 돌린다 — 설치 중 Deployer 서비스가 멈춰도 설치는 끝까지 가고, 작업이 끝에 두 서비스를 다시 켠다. 새 Deployer가 결과를 이어받는다(11.5 Linux 구현 규칙).
- Agent가 죽거나 망가져도 Deployer는 살아 있으므로, Controller에서 **원격으로 복구 설치**할 수 있다.
- Deployer는 기능을 최소로 유지해 거의 바뀌지 않게 한다: 파일 받기, 무결성 확인, 설치·제거 실행, 서비스 시작·중지, 버전·상태 조회, **같은 PC의 Agent 감시(멈춤 감지 시 강제 재시작)**.

### 11.3 Linux Agent 최초 설치 (SSH)
- **송신 PC(통신사 역할, 07 3.2 B)는 처음 한 번만 공유기 LAN 포트에 꽂아** 설치한다 — WAN 쪽에서는 공유기 WAN에 주소가 생기기 전이라 Controller가 닿지 못한다. 설치 뒤 케이블을 WAN 포트로 옮기면 역할이 스스로 켜지고 Controller가 새 주소를 스스로 찾는다(07 3.2 B, 10.2, 20장 GAP-75) — 명령 없음. 이후 사람 할 일 없음(2026-09-26 사용자 결정).
1. Controller 화면에서 Linux PC의 IP와 **계정 정보**를 입력한다.
   - Ubuntu는 기본적으로 **root 비밀번호로 SSH 로그인을 막아 둔다.** 그래서 두 방식을 지원한다: ① root 계정 ② `sudo` 가능한 일반 계정 + sudo 비밀번호.
   - 비밀번호는 **설치에 한 번만 쓰고 저장하지 않는다.** 화면·로그·보고서에 남기지 않는다.
   - SSH는 **asyncssh**(Python 라이브러리, EPL-2.0)로 한다(2026-09-25 사용자 결정): Windows Controller에서도 비밀번호·sudo 처리가 확실하고, 시험용 SSH 서버를 코드 안에서 띄워 INS-007~010을 자동 시험할 수 있다. 라이선스는 15장 3.1-1.
   - **호스트 키(2026-09-25 사용자 결정):** 처음 접속한 PC의 키는 **믿고 저장**한다(Controller 데이터 `ssh-known-hosts.json`, 02 11, 지문을 결과·기록에 보임, 사람 일 0). 다음 접속에서 키가 다르면 INS-009로 멈추고, 사람이 확인해 `--accept-new-host-key`로만 바꾼다.
   - sudo는 `sudo -S`(비밀번호를 표준 입력으로, 화면·기록에 안 남김)로만 부른다. root로 들어왔으면 sudo를 쓰지 않는다.
2. Controller가 SSH로 접속해 사전 점검: OS·버전, CPU 구조, 디스크, 포트 10991/10992 사용 가능 여부.
3. Controller가 파일 전달(SFTP, 임시 폴더 `/tmp/zeroloss-install-<빌드>/`, 끝나면 지움):
   - ZeroLoss Agent 패키지(.deb)와 Deployer
   - 필요한 도구: ffmpeg, TSDuck, mpv, VLC, (수신 역할) adb, (수신 역할, Linux) 하드웨어 해독 드라이버 — Intel `intel-media-va-driver` + 확인용 `vainfo`(05 4.2-1). 원래 있던 드라이버는 제거하지 않는다
   - **인터넷 확인 후 결정:** 대상 PC가 인터넷에 연결되면 apt 등 공식 저장소로 설치, 안 되면 Controller가 가진 **오프라인 설치 묶음**(대상 OS 버전별)으로 설치한다. 어느 방식으로 설치했는지 기록한다.
   - **단계(2026-09-25):** 도구 보관소(15장)가 생기기 전에는 도구를 보내지 않고 **있는지만 확인**해 없으면 결과와 Action Required로 알린다(INS-012 흐름). 보관소가 생기면 위 규칙대로 보낸다.
4. 설치 실행(`dpkg -i`, 패키지 잠금이면 INS-011 대기, 실패면 INS-028) → 서비스 시작(설치 스크립트 4.3-1) → Controller가 네트워크로 `/v1/health`(10992)·Deployer 상태(10991)를 확인(60 s 안)
   - 이어서 Controller는 그 PC를 **장비 목록에 등록**하고(02 11-0) 필요한 모듈 패키지(1차: `iptv`)를 10991로 보낸다(12장).
4-1. **되돌림 사본:** 설치한 .deb를 `<데이터>/deployer/packages/current.deb`로 남긴다(11.5, 첫 업데이트가 되돌아갈 곳).
4-2. **자동 로그인(사용자가 원할 때만, 2026-09-26 사용자 결정, 20장 GAP-67):** `--auto-login`(화면은 체크박스)이 사용자 확인이다(00-vision "확인 1회"). SSH 계정(그 PC의 화면 사용자)으로 GDM 자동 로그인을 켠다: `sudo zeroloss-deployer auto-login --user <계정>` → `/etc/gdm3/custom.conf`의 `[daemon]`에 `AutomaticLoginEnable=true`·`AutomaticLogin=<계정>`(원래 파일은 `<데이터>/deployer/auto-login-orig.json`, 노드 설치 목록 `auto_login`). 제거 때 원래 파일로 되돌린다(그 사이 사람이 바꿨으면 우리 줄만 빼고 그 사람 줄을 살림). GDM이 아니거나 root 계정이면 설치는 성공, 결과에 "not set" + 사람 할 일(`human.os.check_auto_login`). 재부팅 뒤 Agent Desktop(03 F20)이 사람 없이 돌아오게 하려는 것.
4-3. **도구 설치(2026-09-26 사용자 결정, 20장 GAP-73 ①):** 같은 SSH·sudo(비밀번호 한 번)로 역할에 필요한 외부 도구(송신: ffmpeg·ffprobe·TSDuck `tsp`, 수신: VLC·ffmpeg)를 설치한다. 그 PC가 인터넷에 닿으면 `apt-get install`, 아니면 Controller의 `packages/tools/`에 있는 .deb 묶음(15장 보관소의 Ubuntu 묶음, 보관소 전에는 Controller가 받아 둔 공식 .deb)을 올려 `apt-get install ./…deb`로. 이미 있으면 건너뜀. 결과 `tools_installed`·`tools_missing`. 사람이 따로 도구를 설치하지 않는다(00 원칙 8). **구현(2026-09-26):** 빠진 도구 확인(`command -v`) → apt 패키지(`ffmpeg`=ffmpeg·ffprobe, `vlc`)는 `apt-get update` 성공 때만 `apt-get install`(인터넷 확인을 겸함) → 아직 빠졌으면(인터넷 없음, 또는 apt에 없는 TSDuck `tsp`) `packages/tools/*.deb` 전부 올려 `apt-get install ./…deb` → 다시 확인. apt 잠금은 dpkg와 같이 30초마다 최대 10분 기다림, 한 번의 apt는 최대 15분. 못 들어온 도구는 설치를 멈추지 않고 `tools_missing`(사전 점검 2단계 INS-029가 다시 알림). 비밀번호는 sudo 표준 입력으로만.
5. 페어링(Controller 등록, 키 옵션을 켰다면 키 등록) 후 SSH 연결 종료. 이후 업데이트는 10991로 한다.
6. 설치 결과(설치한 항목 목록, 버전)를 Controller에 표시

#### 빌드 위치 (OS에 따라 달라짐)
PyInstaller는 **다른 OS용 실행파일을 만들 수 없다.** 그래서 결과물마다 빌드하는 곳이 다르다.

| 결과물 | 빌드 위치 |
|--------|-----------|
| Controller, Windows Agent, Windows 설치 파일 | **Controller PC (Windows)** |
| Linux Agent (.deb) | Linux PC (대상과 같은 OS 버전·CPU 구조) |
| Android APK | **Controller PC (Windows)**: 성능이 가장 좋은 PC. Android SDK·Gradle 빌드 환경을 여기에 구성 |

Controller는 Linux Agent를 설치할 때 상황에 맞게 고른다(보낼 패키지는 Controller 데이터의 `packages/`에서 **가장 새 빌드**를 쓴다: `zeroloss-agent_<빌드>_amd64.deb`, 모듈마다 `zeroloss-module-<id>-<빌드>.zlm`. CLI `--package`·`--module`로 바꿀 수 있다, 2026-09-25):
1. 대상 PC와 **OS 버전·CPU 구조가 맞는 미리 빌드된 패키지**가 Controller 설치본에 있으면 → 그것을 전달 (기본, 빠름)
2. 맞는 패키지가 없으면 → 소스와 빌드 도구를 SSH로 보내 **대상 Linux PC에서 직접 빌드** 후 설치
- 빌드 결과(어디서, 어떤 버전으로 빌드했는지)는 설치 기록과 보고서에 남긴다.

### 11.4 Windows Agent 최초 설치
- 설치 파일(`ZeroLoss-Agent-Setup-<버전>.exe`)을 각 PC에서 **직접 1회** 실행한다(원클릭).
- Agent와 Deployer가 함께 설치된다. 이후는 Controller에서 10991으로 관리한다.

### 11.5 업데이트 (10991)
1. Controller가 각 장비의 현재 버전을 조회(10991)하고, 업데이트가 필요한 장비를 표시
2. 사용자가 [Update] 선택
3. **새 버전 보내기:** Controller → Deployer: 패키지를 받는 폴더(`<데이터>/deployer/incoming/`)에 전달 (패키지 SHA-256 목록 확인. 키 옵션을 켰다면 서명 확인)
4. **기존 것 제거:** Agent 중지 → 설정·설치 목록 백업 + **되돌림용 이전 프로그램 사본**(`<데이터>/deployer/rollback/`) → 이전 버전 설치 목록 기준으로 제거(사용자 데이터 보존)
5. **예전에 보낸 압축본 지우기:** `incoming/`에서 이번 패키지를 뺀 **이전 패키지 전부**를 지운다(되돌림은 4번의 사본으로 하므로 필요 없음)
6. **새 것 설치:** 새 패키지 설치 → 설정 이전(`schema_version`)
7. **재시작:** Agent 서비스·Agent Desktop 재시작 → `/v1/health`·버전 확인. PC 재부팅은 설치 프로그램이 요구할 때만 하고(사용자에게 먼저 알림), 재부팅 뒤 Deployer가 자동으로 health를 다시 보고한다
8. **정리:** 성공하면 이번 패키지와 `rollback/` 사본(Linux 노드 패키지는 `packages/previous.deb`, 아래 구현 규칙)을 지운다. **받은 폴더에 압축본이 남지 않아야 완료**
9. 실패 시 `rollback/` 사본(Linux 노드: `packages/previous.deb`)으로 이전 버전을 되돌리고 원인 보고. 이때도 새로 받은 패키지는 지운다(다음에 다시 보냄)
10. **시험 실행 중에는 업데이트 거부**
- **Linux 구현 규칙(2026-09-26, 20장 GAP-64):**
  - 노드 패키지 = `.zlp`(02 13, `kind: node`, `id: agent`, `os: linux`)이고 payload는 SSH 설치와 같은 .deb 하나(`zeroloss-agent_<빌드>_amd64.deb`). 11.7대로 해시 목록이 맞아야 연다. `python -m tools.build deb`가 .deb와 `zeroloss-node-agent-<빌드>.zlp`를 함께 만든다. Deployer가 .deb를 꺼내 `dpkg-deb -f`로 이름(`zeroloss-agent`)·버전을 읽어 `package.json` 빌드와 같은지 확인: 다르면 INS-006, 지금보다 옛 빌드면 INS-026, 시험 중이면 INS-022, 다른 작업 중이면 INS-027, Windows 노드면 RUN-013(V18). 같은 빌드 = 복구 설치.
  - 되돌림 사본: 지금 설치된 .deb를 늘 `<데이터>/deployer/packages/current.deb`에 둔다(SSH 첫 설치가 넣고, 업데이트가 성공할 때마다 바꿈). 업데이트 때 이것을 `packages/previous.deb`로 복사한다(모듈의 `rollback/<id>`와 섞이지 않게). 사본이 없으면(옛 설치) 업데이트는 하되 결과에 "no previous package to go back to".
  - 작업 기록 `<데이터>/deployer/update.json`(`id`, `from_build`, `to_build`, `state`: `running` → `done` / `rolling_back` → `rolled_back` / `failed`, `why`, 시각)을 쓴 뒤 `systemd-run --unit=zeroloss-update-<id> --collect`로 `dpkg -i`(패키지 잠금이면 30초 간격 최대 10분 다시, INS-011) → 종료 코드를 파일에 → **두 서비스 다시 켜기**를 돌린다(설치가 실패해도 Deployer는 되살아난다). 명령 줄에는 고정 경로만 들어간다. 설치 명령의 답은 바로 `{"result": "started", "update_id"}`.
  - 새로 켜진 Deployer가 `running` 기록을 보면: 종료 코드를 기다리고(최대 10분) → 0이고 노드가 **새 빌드로** 답하면(60초) `done`: 새 .deb를 `current.deb`로, `incoming/`·`previous.deb` 비움 → 아니면 `rolling_back`: 같은 방법으로 `previous.deb`를 설치 → 다음 Deployer가 `rolled_back`(이유 INS-028) 또는 되돌림도 실패하면 `failed`(INS-013 안내: SSH로 다시 설치). 전원이 꺼져 끊긴 경우(작업 단위가 없고 종료 코드도 없음)도 같은 기록으로 이어받아 되돌린다(INS-013). 이어받는 동안 Deployer는 다른 작업을 받지 않고(INS-027) 노드 감시로 재시작하지 않는다. 패키지를 제거하면 `packages/`와 작업 기록도 지운다(사용자 데이터 아님).
  - Deployer `GET /v1/deployer/health`에 `update`(마지막 작업 기록, 02 15). Controller `zeroloss deploy --node <이름> --package <.zlp>`는 `started` 뒤 health를 보며 `done`/`rolled_back`/`failed`까지 기다려(최대 15분) 단계별 결과를 돌려준다.
  - Windows 노드 업데이트는 Windows 설치 파일 때(V18).
- 같은 순서를 모듈 패키지(.zlm)와 Controller 자신의 업데이트에도 쓴다. CLI: `zeroloss deploy --node <name> --package <file>`가 1~9를 한 번에 하고 단계별 결과를 돌려준다.
- 확인 시험: 업데이트 뒤 `incoming/`·`rollback/`이 비어 있음, 실패 주입 시 이전 버전으로 복구되고 역시 압축본이 남지 않음(10 4-1, 설치 몽키).

### 11.6 태블릿 앱(APK) 업데이트
1. Controller → 수신 Agent의 Deployer(10991): APK 전달
2. 수신 Agent가 USB로 연결된 태블릿에 `adb install -r`(설정 유지 업데이트)
3. 설치 후 앱 버전 확인, 앱 다시 시작, 결과를 Controller에 표시. 수신 PC와 태블릿(`/data/local/tmp`에 올린 경우)의 APK 파일은 설치 뒤 지운다
4. 서명 키가 다르면 업데이트가 실패하므로, 이 경우 "기존 앱 제거 후 설치 필요(앱 데이터 삭제됨)"를 안내하고 사용자 확인 후 진행

### 11.7 배포 경로 보안
- 10991은 **파일을 받아 설치하는 포트**이므로 가장 엄격하게 막는다:
  - **기본(키 없음):** 방화벽으로 Controller(또는 NAT 모드의 DUT WAN IP)만 허용
  - 패키지는 **ZeroLoss 패키지 형식 + SHA-256 목록 일치**일 때만 설치(해시 목록은 빌드 때 자동 생성, 키 불필요). 임의 명령 실행 기능 없음
  - **선택 옵션:** 페어링 키 서명(HMAC). 켜면 서명 없는 요청을 전부 거부. 공개 배포 시 권장
- SSH 계정 정보는 저장하지 않는다.

### 11.8 완료 기준 추가
- [ ] 깨끗한 Linux PC에 Controller에서 SSH 설치 → Agent·Deployer·도구 설치, 10991/10992 응답, SSH 종료 후 비밀번호 흔적 없음
- [ ] root SSH가 막힌 Ubuntu에서 sudo 계정으로 설치 성공
- [ ] Windows Agent 수동 설치 후 10991으로 새 버전 업데이트 성공
- [ ] Agent 프로세스가 망가진 상태에서 Deployer로 복구 설치 성공
- [ ] 해시가 다른 패키지 → 10991이 거부 / 키 옵션을 켠 상태에서 서명 없는 요청 → 거부
- [ ] APK를 10991 → 수신 Agent → adb로 업데이트, 앱 설정 유지
- [ ] 시험 중 업데이트 요청 → 거부


### 11.9 전체 업데이트 순서 ([Update all], `zeroloss deploy --all --yes`)
1. 시험 실행 중이면 거부. 모든 노드의 현재 버전·Deployer API 버전을 조회한다.
2. **Agent들 먼저**(송신·수신, 서로 동시에) → **태블릿 앱**(수신 Agent 경유, 11.6) → **Controller 마지막**. 옛 Controller가 끝까지 지휘하고, 마지막에 자기 PC의 Deployer로 자신을 교체한 뒤 다시 켜져서 모든 버전을 다시 확인한다.
3. **배포 통로(10991)와 `/v1/health`는 이전 버전과 항상 호환**: 새 Controller는 옛 Deployer API를 계속 지원하고, Deployer API를 바꿀 때는 새 경로를 더하고 옛 경로를 지우지 않는다. 그래야 어떤 순서로 섞여도 다시 업데이트할 수 있다.
4. **일부 실패:** 실패한 노드만 이전 버전으로 되돌린다(11.5 9번). 성공한 노드는 그대로 둔다. 하나라도 실패하면 Controller 업데이트는 하지 않고 멈춘다. 결과 화면에 노드별 성공·실패·원인과 [Retry failed]를 보인다. 버전이 섞여 `protocol_version`이 안 맞으면 시험 시작은 막힌다(8장).
5. 끝나면 노드별 버전 표와 "압축본 남음 0"을 확인해 기록한다(11.5 8번).

## 12. 모듈 패키지 설치·제거 (12장 1-3, 2026-09-25)
> 모듈(`.zlm`, 02 13장)은 노드 설치 파일과 따로 Deployer가 넣고 뺀다. 원칙은 1장 그대로, 단위만 모듈이다.

**위치**
| 무엇 | Linux | Windows | 제거 때 |
|------|-------|---------|---------|
| 모듈 코드 | `/opt/zeroloss/modules/<id>/` | `C:\Program Files\ZeroLoss\Modules\<id>\` | 지움 |
| 모듈 설정 | `<데이터>/modules/<id>/settings/` | 같음 | **남김**(`--purge`면 지움), 다시 설치하면 그대로 씀 |
| 모듈 캐시(모듈이 만든 파일, 예: IPTV 자동 생성 영상) | `<데이터>/modules/<id>/cache/` | 같음 | 지움 |
| 설치 목록 | `<데이터>/deployer/manifests/module-<id>.json` | 같음 | 지움 |
- 모듈 코드 폴더는 노드 설치 파일(.deb·Setup.exe)이 소유하지 않는다(Deployer 소유). 노드는 이 폴더를 모듈 폴더로 읽는다(12장 1-1 `module.json` 찾기).
- 시험 기록·보고서는 모듈 것이 아니다 — 모듈을 지워도 남는다(12장 1-3 "보고서는 모듈 없이도 열린다").

**설치 순서**(02 15장 `steps`)
1. `verify`: 패키지 검사(02 13장, INS-006). `os`가 이 PC와 맞는지.
2. `check_idle`: 그 PC의 노드가 시험 중이면 거부(INS-022). Controller도 시험 중이면 보내지 않는다.
3. 같은 모듈이 있으면: 같은 빌드 = 복구, 이전 빌드 = 업그레이드, **더 새 빌드 = 거부**(INS-026, 6장).
4. `backup`: 이전 모듈 코드와 설치 목록을 `<데이터>/deployer/rollback/`에 사본. 진행 기록(`journal.json`) 시작.
5. `remove_old`: 이전 설치 목록으로 이전 코드 제거(설정·캐시는 남김 — 캐시는 새 빌드가 판별해 다시 씀).
6. `copy`: 새 파일을 쓰며 한 개씩 설치 목록에 기록, 쓴 뒤 해시 확인. 코드 폴더마다 파이썬이 나중에 만들 `__pycache__`도 이름을 정확히 적어 둔다(통째로 지움, 02 14 `tree`).
7. `restart_agent`: 대기 상태의 그 PC 노드 서비스를 다시 시작해 반영(몇 초, 사람 일 없음). 노드가 모듈을 다시 읽어 `core_api` 범위 밖이면 그 모듈만 비활성(INS-024). 제자리 다시 읽기(재시작 없이)는 2차 후보.
8. `cleanup`: `incoming/`의 패키지와 `rollback/` 사본을 지운다(11.5 8번과 같음). 진행 기록 삭제.
- 어느 단계든 실패하면 `rollback/`으로 이전 상태를 되돌리고 원인을 돌려준다(패키지는 지움).

**제거 순서**
1. 시험 중이면 거부(INS-022). `dry_run`이면 지울 목록만 돌려준다(원칙 9).
2. 설치 목록을 거꾸로 지운다: `file`(해시가 설치 때와 달라도 이름이 같으면 지움 — 이름 기준, 원칙 2), `dir`(비었을 때만), `tree` 폴더(캐시만). 설정(`keep`)은 `purge`일 때만.
3. 노드 서비스 다시 시작. 4. **제거 후 검증:** 목록의 것이 모두 사라졌는지, 남은 것이 있으면 `left` + INS-021.

**시험(자동, 임시 폴더):** 설치 → 제거 → 설치 전과 같음(폴더 목록 비교), 제거·재설치 10회(설정 복원, 다른 모듈 영향 없음, 10 4-1), 같은 빌드 복구, 업그레이드 뒤 옛 파일 없음, 다운그레이드 거부, 모르는 파일이 든 폴더는 안 지우고 보고, 패키지 변조 거부, 설치 중 멈춤 → 다음 시작에 되돌림(INS-013), 무작위 순서 몽키(불변: "설치 전과 같음" 또는 "완전히 설치됨"만).

