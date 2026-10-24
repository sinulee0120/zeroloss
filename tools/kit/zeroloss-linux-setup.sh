#!/bin/bash
# ZeroLoss: 이 Ubuntu PC를 시험용으로 준비합니다 (인터넷 없이, 이 USB의 파일만으로).
# 실행:  sudo bash zeroloss-linux-setup.sh
set -e
if [ "$(id -u)" != "0" ]; then
  echo "관리자 권한이 필요합니다. 이렇게 실행하세요:  sudo bash $0"
  exit 1
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
. /etc/os-release
if [ "$VERSION_ID" != "24.04" ]; then
  echo "주의: 이 키트는 Ubuntu 24.04용입니다. 이 PC: $PRETTY_NAME"
fi
TMP=/tmp/zeroloss-offline
rm -rf "$TMP"; mkdir -p "$TMP/lists/partial"
echo "[1/3] 설치 파일을 준비합니다..."
cp -r "$HERE/offline" "$TMP/repo"
cp "$HERE"/tsduck_*.deb "$TMP/repo/"
chmod -R a+rX "$TMP"
echo "deb [trusted=yes] file:$TMP/repo ./" > "$TMP/sources.list"
OPTS=(-o "Dir::Etc::SourceList=$TMP/sources.list" -o "Dir::Etc::SourceParts=/nonexistent"
      -o "Dir::State::Lists=$TMP/lists" -o "APT::Get::List-Cleanup=0" -o "DPkg::Lock::Timeout=600")
apt-get "${OPTS[@]}" update -qq
echo "[2/3] 프로그램을 설치합니다 (몇 분 걸립니다)..."
DEBIAN_FRONTEND=noninteractive apt-get "${OPTS[@]}" install -y -q --no-install-recommends \
  openssh-server ffmpeg mpv vlc libxcb-cursor0 "$TMP"/repo/tsduck_*.deb
systemctl enable --now ssh >/dev/null 2>&1 || systemctl enable --now sshd >/dev/null 2>&1 || true
rm -rf "$TMP"
echo "[3/3] 확인..."
MISSING=""
for t in ffmpeg ffprobe tsp mpv vlc sshd; do command -v $t >/dev/null || [ -x /usr/sbin/$t ] || MISSING="$MISSING $t"; done
echo
if [ -n "$MISSING" ]; then
  echo "설치되지 않은 것:$MISSING  (이 화면을 사진으로 찍어 보내 주세요)"
  exit 1
fi
echo "완료! Windows PC에서 아래 두 값을 입력하세요:"
echo "  이 PC의 IP 주소 : $(hostname -I | awk '{print $1}')   (여러 개면 공유기에 꽂힌 쪽: $(hostname -I))"
echo "  이 PC의 아이디  : ${SUDO_USER:-$(logname 2>/dev/null)}"
