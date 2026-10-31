#!/bin/bash
# ZeroLoss: 시험용 영상(미리 만든 UHD 10분 영상, 채널마다 하나)을 이 송신 PC에 넣습니다.
# Windows에서 이 PC를 등록(5단계)한 다음, 이 USB에서 실행:  sudo bash zeroloss-sample-video.sh
# 이미 만든 영상이라 시험이 바로 시작됩니다 (새로 만들면 한 편에 몇 시간 걸림).
set -e
if [ "$(id -u)" != "0" ]; then
  echo "관리자 권한이 필요합니다. 이렇게 실행하세요:  sudo bash $0"
  exit 1
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA=/var/lib/zeroloss
if ! id zeroloss >/dev/null 2>&1 || [ ! -d "$DATA" ]; then
  echo "ZeroLoss가 아직 이 PC에 없습니다. 먼저 Windows PC에서 이 PC를 등록하세요 (안내 5단계)."
  exit 1
fi
FILES=()
for f in "$HERE"/sample_*/*.ts; do
  if [ -f "$f" ] && [ -f "${f%.ts}.json" ]; then
    FILES+=("$f")
  fi
done
if [ ${#FILES[@]} -eq 0 ]; then
  echo "USB에 sample_ 폴더가 없습니다. 키트를 USB에 다시 복사하세요."
  exit 1
fi
MEDIA="$DATA/media"
mkdir -p "$MEDIA"
chown zeroloss:zeroloss "$MEDIA"
i=0
for f in "${FILES[@]}"; do
  i=$((i + 1))
  echo "[$i/${#FILES[@]}] $(basename "$f") 를 복사합니다 (약 2.6 GB, 2~4분)..."
  install -o zeroloss -g zeroloss -m 0644 "$f" "${f%.ts}.json" "$MEDIA/"
done
sync
echo
echo "완료! Windows PC의 ZeroLoss에서 Test type = IPTV test, Video quality = UHD로 두고"
echo "Source video의 채널마다 고르세요 ( · ✔ ready, starts at once 표시):"
for f in "${FILES[@]}"; do
  n="$(basename "$f")"
  ch="$(echo "$n" | sed -E 's/.*_CH([0-9]+)_.*/\1/')"
  echo "  CH$ch: $n"
done
