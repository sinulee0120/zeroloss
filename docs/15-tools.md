# 15. 외부 도구 보관소 (Tool Depot)

> 목적: 외부 도구를 쓸 때마다 받지 않도록 **Controller PC의 별도 공간에 최신·검증 버전을 미리 받아 두고**, 여기서 설치·배포·릴리즈한다.
> 인터넷은 Controller PC만 된다(13-ai-loop). 시험망 노드는 이 보관소에서 받는다(Deployer 10991).

## 1. 구조
```
C:\ZeroLossDepot\                 ← 보관소 (저장소 밖, 큰 파일이라 git에 넣지 않음)
├── depot.json                    ← 받아 둔 파일 목록: 도구, 버전, OS, 크기, SHA-256, 받은 날, 검증 결과
├── adb\36.0.0\windows\  linux\
├── ffmpeg\<버전>\windows\  linux\
├── tsduck\<버전>\ …
├── …
└── bundles\
    ├── ZeroLoss-Tools-20260923.1432-windows.zip   ← 릴리즈용 오프라인 묶음
    └── ZeroLoss-Tools-20260923.1432-ubuntu22.04.tar
```
- **목록 원본은 저장소 안** `tools/tools-manifest.json`(무엇을, 어느 버전 정책으로, 어디서 받는지). 실제 파일은 보관소(저장소 밖).
- 버전마다 폴더를 따로 두고 **이전 버전을 1개 이상 보관**(문제 시 되돌리기).

## 2. 명령 (CLI, GUI의 장비 관리 화면에서도 같은 기능)
| 명령 | 하는 일 |
|------|---------|
| `zeroloss tools check` | 목록의 각 도구에 새 버전이 있는지 공식 배포처에서 확인만(받지 않음) |
| `zeroloss tools fetch [--tool X]` | 정책대로 받기: **latest** 도구는 최신, **pinned** 도구는 지정 버전. 공식 배포처에서만 받음 |
| `zeroloss tools verify` | 받은 파일 검증: SHA-256(공식 값이 있으면 대조), 실행 확인(예: `ffmpeg -version`에 libx264 포함, `tsp --version`), 라이선스 파일 존재 |
| `zeroloss tools bundle` | OS별 오프라인 묶음 생성(라이선스 원문 포함) → 릴리즈에 첨부 |
| `zeroloss tools diff` | 현재 배포된 버전과 보관소 버전 비교(어느 노드가 오래됐는지) |

- 받기는 **자동으로 하지 않는다.** `check`는 주기적으로(기본 주 1회) 돌려 새 버전을 알려 주고, 받기·배포는 사람이 확인 후(또는 Claude가 사용자 확인 후) 한다.
- **pinned 도구의 버전 올리기:** 새 버전을 받아 `verify` → 10-verification의 해당 시험(송출 정확도, 반복 지점, 재생 V6 등) 통과 → 목록의 고정 버전 갱신. 통과 전에는 배포하지 않는다.
- 노드 배포는 Deployer(10991)로. 노드는 **보관소에서만** 받는다(인터넷 직접 접속 없음).

## 3. 도구 목록 (1차)
> 버전 정책: **latest** = 항상 최신(호환성 문제로 최신이 필요한 것), **pinned** = 검증한 버전 고정(바뀌면 측정 결과가 흔들릴 수 있는 것). 정확한 버전 번호는 P0에서 받아 확정하고 `tools-manifest.json`에 적는다.

### 3.1 제품 실행에 필요한 도구 (노드에 배포)
| 도구 | 용도 | 정책 | OS | 공식 배포처 | 라이선스 | 비고 |
|------|------|------|----|-------------|----------|------|
| **adb (platform-tools)** | 태블릿 설치·제어·중계 | **latest** | Win, Linux | Google platform-tools 배포 | Apache 2.0 등 | 오래되면 최신 태블릿과 불안정(06-install 2.1) |
| **FFmpeg / ffprobe** | 영상 분석, 사전 준비 변환, 이상 감지 필터(freezedetect·blackdetect·blockdetect·silencedetect) | pinned | Win, Linux | FFmpeg 공식 안내의 빌드 배포처 | **GPL**(libx264·libx265 포함 빌드) | libx264·libx265 포함 필수, 필요한 필터 포함 여부를 verify에서 확인. 제품과 분리 배포 |
| **TSDuck** | 정속 송출(RTP/TS), 연속성·PCR 보정, IAT 교차 확인 | pinned **3.45-4798** | Win, Linux(.deb) | TSDuck GitHub 릴리즈 | BSD-2 | 옵션 조합은 `docs/modules/iptv.md`. 공식 .deb는 Ubuntu 24.04 이상만 있음(개발 PC는 2026-09-24부터 24.04라 공식 .deb) → 22.04 노드는 같은 버전을 소스 빌드 |
| **mpv** | PC 재생기 **기본**(S-26) | pinned **0.37.0**(Ubuntu 24.04) | Win, Linux | mpv.io 안내 빌드 / apt | GPL/LGPL | 기본 재생기(2026-10-10 결정, GAP-152) |
| **VLC** | PC 재생기 **선택**(S-26), 재생 확인 | pinned **3.0.23** | Win, Linux | videolan.org | GPL/LGPL | 2026-09-24~10-10 기본이었음 |
| Linux 기본 도구 | `iproute2`(tc·netem), `ethtool`, `iw` | pinned(배포판 버전) | Linux | Ubuntu 저장소 → 오프라인 .deb 묶음 | GPL | 대상 Ubuntu 버전별 묶음 |

### 3.1-1 제품에 넣는 파이썬 라이브러리 (실행 파일 안에 함께 배포)
> 개발·빌드 도구(3.3)와 달리 **제품과 함께 나간다** → 라이선스가 배포에 문제없어야 한다(CLAUDE.md). 2026-09-25 전체 검토에서 3.3에 잘못 들어 있던 것을 옮김(20장 GAP-53).

| 라이브러리 | 쓰는 곳 | 정책 | 라이선스와 배포 조건 |
|------------|---------|------|----------------------|
| PySide6 (Qt for Python) | Controller Desktop 화면 | pinned | LGPL-3.0 — 동적 연결(PyInstaller onedir로 Qt 라이브러리를 따로 파일로 둠)이면 제품 배포 가능, 라이선스 원문 동봉 |
| asyncssh (+ cryptography) | Controller의 Linux Agent 최초 설치 SSH(06 11.3, 2026-09-25 결정) | pinned | EPL-2.0(asyncssh, GPL-2.0+ 이중) / Apache-2.0·BSD(cryptography) — 제품에 넣어 배포해도 됨 |

> 아직 쓰지 않는 **후보 도구**(TRex, Flent, Scapy, PyVISA, uiautomator2 등)와 판단은 19장 5절. 도입이 정해지면 이 표로 옮기고 버전·검증을 적는다.

### 3.2 진단·검증용 도구 (필요할 때만 배포)
| 도구 | 용도 | 정책 | 비고 |
|------|------|------|------|
| tcpdump / tshark(Wireshark) | IGMP·M2U 확인, 패킷 캡처 | pinned | Linux 노드. Windows 캡처용 Npcap은 **재배포 조건 때문에 묶음에 넣지 않고** 사용자가 설치(Action Required 안내) |
| iperf3 | 처리량 시험 | pinned | **2차**(throughput 모듈) |

### 3.3 개발·빌드용 도구 (Controller PC 빌드 환경, 노드에 배포 안 함)
| 도구 | 용도 | 정책 |
|------|------|------|
| Python 3.12 | 전체 PC 쪽 | pinned(3.12.x 최신 패치) |
| PyInstaller | 실행파일 빌드 | pinned |
| pytest, pytest-qt, Hypothesis, mypy, ruff, mutmut | 시험·검사 | pinned (Python 휠 오프라인 보관) |
| Playwright | 보고서 HTML 몽키·캡처 시험 | pinned |
| Inno Setup | Windows 설치 파일 | pinned |
| JDK, Android SDK, Gradle | 태블릿 앱 빌드 | SDK는 최신 안정, 나머지 pinned |
| Git, GitHub CLI | 저장소 | latest |

- **개발 PC 예외(2026-09-24):** 임시 개발 PC(T460, 2026-09-24부터 Ubuntu 24.04)는 VLC를 OS 패키지(3.0.20)로 쓴다. 검증(V6)과 배포는 고정 버전 3.0.23으로 한다.

### 3.4 도구별 동작 방식·특징 (실측·확인 기록)
> 도구가 **실제로 어떻게 동작하는지**(옵션, 출력, 함정)를 도구마다 한 곳에 모은다(2026-09-25 사용자 요구). 결정과 그 이유는 원래 절에 있고 여기서는 가리키기만 한다(20장 G-03). 확인 수준은 18장 표기(✓ 실측·원문 / ◐ 일부 / ? 미확인). 새로 알게 되면 여기에 먼저 적는다.

#### TSDuck `tsp` (3.45-4798, Linux 확인)
- **동작:** 한 프로세스 = 한 채널. 입력(`-I file … --infinite`) → 플러그인(`-P …`) → 출력(`-O ip`) 파이프라인을 계속 돈다. 옵션·순서 확정은 iptv 4.4 ✓.
- `regulate --bitrate`는 **지정 속도로** 내보낸다(PCR 기준 아님, spikes S3) ✓. `--infinite` 반복 이음새의 CC·PCR 끊김은 `continuity --fix` + `pcradjust`로 잇는다(iptv 4.2, `tsfixcc` 기각) ✓.
- `bitrate_monitor --json-line=<태그>`가 1초마다 실제 송출 속도를 표준 출력에 쓴다 → 송출 안정 확인(±1% 3초)에 쓴다 ✓.
- `-O ip --rtp --payload-type 33 --ssrc-identifier N`: RTP 헤더(90 kHz 타임스탬프). `--local-address`로 송출 NIC를 고른다 ✓. TTL 16·출발 IP는 시험망 캡처로 확인 남음(spikes S2).
- 같은 PC 안 멀티캐스트 되돌림(loopback)이 켜져 있어 같은 PC의 소켓이 받을 수 있다 → 루프백 자동 시험의 근거 ✓.

#### FFmpeg / ffprobe (6.1.1, Linux 확인)
- 사전 준비 변환(iptv 3장·4.1): CBR 정확도 spikes S1 ✓, PAT 간격 `-pat_period 0.08` S5 ✓, T460급은 UHD 실시간 인코딩 불가 S6.
- **RTP 입력:** `rtp://<그룹>:<포트>?localaddr=<NIC IP>`로 **가입 NIC를 지정**해 받는다. 우리 tsp RTP 송출을 초당 30프레임 정상 해독 ✓(2026-09-25, HD, 루프백).

#### VLC (이 PC 3.0.20 Ubuntu 24.04 패키지 — 보관소 pinned 3.0.23과 다름, Windows에서 다시 확인)
- **자체 보고 읽기 ✓(2026-09-25):** `-I dummy --extraintf http --http-host 127.0.0.1 --http-port <P> --http-password <임의 값> --stats`로 띄우고 `GET /requests/status.xml`(Basic 인증, 사용자 이름 빈칸)을 읽으면 누적값 `displayedpictures`·`lostpictures`·`decodedvideo`·`playedabuffers`·`lostabuffers`·`demuxcorrupted`·`demuxdiscontinuity`와 `<state>`가 나온다. 추가 라이브러리 필요 없음. 1초마다 읽는 비용: 읽는 쪽 CPU 약 3 ms/회, 가장 느린 응답 67 ms(T460, G-10).
- 파일 재생은 `--vout=dummy`(창 없음)에서도 표시 프레임이 초당 30씩 는다 ✓ → 자동 시험에서 창 없이 셀 수 있다.
- **가입 NIC:** `rtp://@그룹:포트`만 주면 **기본 경로 NIC로 가입**한다(lo로 보낸 것은 못 받음, wlan IP로 보낸 것은 받음). `--miface=<NIC 이름>`을 주면 그 NIC로 가입 ✓(도움말 설명은 "출력 인터페이스"지만 가입에도 적용됨). 수신 PC에 NIC가 둘이면 반드시 지정. Windows의 이름 형식은 V1에서 확인.
- **`--no-media-library` 필요:** 빼면 사용자 미디어 목록(`~/.local/share/vlc/ml.xspf`)을 읽는다 → 시험 재생기는 사용자 설정·목록과 분리한다.
- **RTP/MPEG-TS 재생은 된다 ✓, 통계는 안 된다 ✗(3.0.20, 2026-09-25 정정):** `rtp://`로 받으면 화면은 정상(scene 필터로 뽑은 그림: 5.267초에 158번째 프레임 = 초당 30, 영상 속 시계·번호 정상). 그런데 **RTP 입력에서는 status.xml의 모든 통계가 시작 직후 멈춘다**(`displayedpictures` 0, `decodedvideo` 2, `demuxreadbytes`·`<time>`도 고정). 같은 TS를 RTP 없이 `udp://`로 받으면 통계가 정상으로 는다(8초 160프레임). → **VLC 3.0.20은 RTP 입력에서 자체 보고(표시 프레임 수)를 HTTP 창구로 줄 수 없다.** 처음에는 통계만 보고 "재생 실패"로 잘못 판단했음(사용자 지적: Windows에서 VLC·mpv 모두 RTP 재생 확인됨, 이전 도구 ◐). Windows 3.0.23의 통계 동작은 V6에서 확인.
- **scene 필터로 프레임 진행 확인 ✓:** `--video-filter=scene --scene-format=png --scene-ratio=30 --scene-path=<폴더> --scene-prefix=f`는 해독된 30프레임마다 그림 한 장을 `f00001.png, f00031.png …`로 쓴다(파일 이름 번호 = 프레임 번호). RTP 입력에서도 동작 → 프레임이 늘고 있는지 알 수 있다(표시 직전이 아니라 **해독 후 필터 단계** 기준, 화면 출력 단계의 버림은 못 봄). 부하 ✓(2026-09-25, T460, HD RTP 20초): 필터 없음 VLC CPU 7.58 s, `--scene-ratio=10 --scene-width=64 --scene-height=36` 7.35 s(차이 없음, 잡음 범위), 파일 57개 110 KB(초당 약 2.9개). RTP 입력에서는 status.xml 전체(상태·시간·통계)가 6초와 9초에 한 글자도 다르지 않고, `-vv` 로그에도 매초 나오는 재생 값이 없다 → RTP에서 VLC 재생 진행을 아는 방법은 이 필터뿐(이 버전).
- **창을 닫으면 끝나는지 ◐(2026-09-26, RCV-004):** 재생기가 부르지 않았는데 종료 코드 0으로 끝나면 "창을 닫음"으로 본다. `-I dummy`에서 영상 창을 닫았을 때 VLC가 실제로 끝나는지·종료 코드가 0인지는 실제 화면(로그인한 세션)에서 확인해야 한다(창 없이 도는 자동 시험으로는 못 봄).
- 초기 판단에 쓴 시험 조건(참고): 유니캐스트·멀티캐스트, 우리 tsp·ffmpeg `rtp_mpegts` 송출, `--network-caching` 1000·3000, 창 없음·실제 창, `--no-ts-trust-pcr`·`--clock-jitter=0`·`--rtp-max-dropout`·`--avcodec-threads=1` 모두 통계는 같았음. 로그의 `ES_OUT_SET_PCR is called too late (pts_delay increased to 1000 ms)`는 시작 때 한 번 나오고 재생에는 영향 없음.

#### mpv (기본, 0.37.0, T460 2026-10-10 ✓)
- RTP 입력 ✓(`rtp://@그룹:포트?localaddr=카드IP`로 그 카드에서 가입). JSON IPC(`--input-ipc-server`)로 `estimated-frame-number`·`decoder-frame-drop-count`·`frame-drop-count`·`hwdec-current` 읽기 ✓, 송신을 멈추면 프레임 번호도 멈춤 ✓.
- UHD HEVC 35 Mbps: `--hwdec=vaapi`(창에 바로) CPU 22 %·화면 원본과 같음 ✓. **복사 모드(`vaapi-copy`, `auto-safe`)는 라이브 가입에 못 씀:** 가입 직후(GOP 중간) 첫 복사 실패 한 번에 하드웨어 디코더를 버리고, 소프트웨어 대체를 끄면 영상이 끝남("filter input EOF") ✓. 기본값(`software-fallback=yes`)이면 첫 오류에 영원히 소프트웨어로 감 ✓ → `--vd-lavc-software-fallback=300`(연속 300프레임 실패 뒤에만).
- 디코딩 오류는 기록에 프레임마다 `Error while decoding frame` 한 줄 ✓. Windows `--hwdec=d3d11va`·이름 있는 파이프 IPC는 ◐(Windows 수신 실기 전).

#### Linux 기본 도구 (`iw`, sysfs, `/proc/net/wireless`)
- 링크·Wi-Fi 읽기 방식과 부하(유선 sysfs 0.57 ms, `iw` 전체 약 4 ms + 프로세스 2개 → `iw event` 하나를 계속 띄움)는 05 1.9 "감시 주기와 부하" ✓.
- 이 PC 기본 `rmem_max` 208 KB라 16 MB 수신 버퍼 요청이 잘린다(spikes, RCV-001) ✓.

## 4. 목록 파일 형식 (`tools/tools-manifest.json`)
```json
{
  "schema_version": 1,
  "tools": [
    { "id": "adb", "purpose": "tablet control", "policy": "latest",
      "platforms": ["windows", "linux"], "source": "official:google-platform-tools",
      "license": "Apache-2.0", "deploy_to": ["receiver"], "verify": ["adb version"],
      "pinned_version": null },
    { "id": "ffmpeg", "purpose": "analysis, prepare, detectors", "policy": "pinned",
      "platforms": ["windows", "linux"], "source": "official:ffmpeg-builds",
      "license": "GPL-3.0", "deploy_to": ["sender", "receiver", "controller"],
      "verify": ["ffmpeg -version | contains libx264", "ffmpeg -filters | contains blockdetect"],
      "pinned_version": "TBD-in-P0" }
  ]
}
```
- `source`는 **공식 배포처 이름만** 허용(임의 URL 금지). 실제 주소는 도구 받기 코드 한 곳에서 관리.
- `depot.json`(보관소)은 받은 결과: 버전, 파일, SHA-256, 받은 날, verify 결과, 배포된 노드 목록.

## 5. 규칙
1. 도구는 **공식 배포처에서만** 받는다. 받은 파일은 SHA-256을 기록하고, 공식 체크섬·서명이 있으면 대조한다.
2. **GPL 도구는 제품과 분리**한 도구 묶음으로만 배포하고 라이선스 원문을 함께 넣는다.
3. 노드는 인터넷에서 직접 받지 않는다. 항상 보관소 → Deployer 경로.
4. pinned 도구 버전 변경은 검증 시험 통과 후에만. 변경 내역은 릴리즈 노트에 적는다.
5. 보고서에는 그 시험에 쓰인 모든 도구의 버전·SHA-256을 적는다(재현성).
6. 보관소 용량 관리: 도구별 최신 + 직전 1개만 남기고 나머지는 정리(`zeroloss tools prune`, 삭제 전 목록 확인).

## 6. 완료 기준
- [ ] `check → fetch → verify → bundle`이 Windows·Ubuntu 묶음을 만들고, 모든 도구가 verify 통과
- [ ] 인터넷 없는 노드에 묶음으로 설치 → 모든 도구 실행 확인
- [ ] adb 새 버전이 나오면 `check`가 알려 주고, 받기·배포 후 노드 adb 버전이 갱신됨
- [ ] pinned 도구를 올린 뒤 검증 시험 실패 → 배포되지 않고 이전 버전 유지
- [ ] 보고서에 도구 버전·해시가 모두 기록됨
