#!/bin/bash
# ZeroLoss: 시험용 샘플 영상(원본 + 미리 만든 UHD 영상)을 이 송신 PC에 넣습니다.
# Windows에서 이 PC를 등록(5단계)한 다음, 이 USB에서 실행:  sudo bash zeroloss-sample-video.sh
# 미리 만든 UHD 영상이 있어서 시험이 바로 시작됩니다 (새로 만들면 한 편에 몇 시간 걸림).
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
SAMPLES=()
for d in "$HERE"/sample_*/; do
  n="$(basename "$d")"
  if [ -f "$d/${n}_original.mp4" ] && ls "$d/${n}_cache"/*.ts >/dev/null 2>&1; then
    SAMPLES+=("$n")
  fi
done
if [ ${#SAMPLES[@]} -eq 0 ]; then
  echo "USB에 sample_ 폴더가 없습니다. 키트를 USB에 다시 복사하세요."
  exit 1
fi
MEDIA="$DATA/media"
MADE="$DATA/modules/iptv/cache/generated"
mkdir -p "$MEDIA" "$MADE"
chown zeroloss:zeroloss "$MEDIA" "$DATA/modules" "$DATA/modules/iptv" "$DATA/modules/iptv/cache" "$MADE"
i=0
for n in "${SAMPLES[@]}"; do
  i=$((i + 1))
  echo "[$i/${#SAMPLES[@]}] $n: 원본과 미리 만든 UHD 영상을 복사합니다 (약 4 GB, 3~6분)..."
  install -o zeroloss -g zeroloss -m 0644 "$HERE/$n/${n}_original.mp4" "$MEDIA/"
  for f in "$HERE/$n/${n}_cache"/*; do
    install -o zeroloss -g zeroloss -m 0644 "$f" "$MADE/"
  done
done
sync
echo
echo "완료! Windows PC의 ZeroLoss에서 Test type = IPTV test, Video quality = UHD로 두고"
echo "Source video에서 고르세요 (미리 만든 채널에서  · ✔ ready, starts at once  표시):"
for n in "${SAMPLES[@]}"; do
  ts="$(ls "$HERE/$n/${n}_cache"/*.ts | head -1)"
  ch="$(basename "$ts" | cut -d_ -f1 | tr -d c-h)"  # ch2_UHD_35M_<tag>.ts -> 2
  echo "  CH$ch: ${n}_original.mp4"
done
