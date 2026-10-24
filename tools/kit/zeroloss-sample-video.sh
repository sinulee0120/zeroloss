#!/bin/bash
# ZeroLoss: 시험용 샘플 영상(원본 + 미리 만든 UHD 영상)을 이 송신 PC에 넣습니다.
# Windows에서 이 PC를 등록(5단계)한 다음, 이 USB에서 실행:  sudo bash zeroloss-sample-video.sh
# 미리 만든 UHD 영상이 있어서 시험이 바로 시작됩니다 (새로 만들면 몇 시간 걸림).
set -e
if [ "$(id -u)" != "0" ]; then
  echo "관리자 권한이 필요합니다. 이렇게 실행하세요:  sudo bash $0"
  exit 1
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/sample_1"
DATA=/var/lib/zeroloss
if ! id zeroloss >/dev/null 2>&1 || [ ! -d "$DATA" ]; then
  echo "ZeroLoss가 아직 이 PC에 없습니다. 먼저 Windows PC에서 이 PC를 등록하세요 (안내 5단계)."
  exit 1
fi
if [ ! -f "$SRC/sample_1_original.mp4" ] || ! ls "$SRC"/sample_1_cache/*.ts >/dev/null 2>&1; then
  echo "USB에 sample_1 폴더가 없습니다. 키트를 USB에 다시 복사하세요."
  exit 1
fi
MEDIA="$DATA/media"
MADE="$DATA/modules/iptv/cache/generated"
mkdir -p "$MEDIA" "$MADE"
chown zeroloss:zeroloss "$MEDIA" "$DATA/modules" "$DATA/modules/iptv" "$DATA/modules/iptv/cache" "$MADE"
echo "[1/2] 원본 영상을 복사합니다 (약 1.2 GB, 1~2분)..."
install -o zeroloss -g zeroloss -m 0644 "$SRC/sample_1_original.mp4" "$MEDIA/"
echo "[2/2] 미리 만든 UHD 영상을 복사합니다 (약 2.6 GB, 2~4분)..."
for f in "$SRC"/sample_1_cache/*; do
  install -o zeroloss -g zeroloss -m 0644 "$f" "$MADE/"
done
sync
echo
echo "완료! Windows PC의 ZeroLoss에서 Test type = PC receiver test, Video quality = UHD로 두고"
echo "Source video의 CH1에서  sample_1_original.mp4 · ✔ ready, starts at once  를 고르세요."
