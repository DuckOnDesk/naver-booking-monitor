#!/usr/bin/env bash
# 스크린샷의 한글이 네모(두부, □□□)로 깨지지 않도록 한글 폰트를 설치한다.
#
# GitHub Actions의 ubuntu 러너에는 한글 글리프를 가진 폰트가 없어서, Chromium이
# 페이지를 렌더할 때 한글이 전부 네모로 찍힌다. (DOM 텍스트는 멀쩡하므로 로그는
# 정상으로 보이고, 스크린샷만 깨져 원인을 찾기 어렵다.)
#
# 취소표 잡기 워크플로에서는 준비 시간도 성공률이라, 무거운 apt 대신
# 나눔고딕 TTF 2개(약 4MB)를 사용자 폰트 디렉터리에 내려받는 것을 우선한다.
# 실패하면 apt 패키지로 넘어가고, 그마저 안 되면 경고만 남기고 통과시킨다
# (폰트가 없다고 예약 자체를 막을 이유는 없다).
set -u

FONT_DIR="${HOME}/.local/share/fonts"
BASE_URL="https://raw.githubusercontent.com/google/fonts/main/ofl/nanumgothic"
FILES="NanumGothic-Regular.ttf NanumGothic-Bold.ttf"

has_korean_font() {
  command -v fc-list >/dev/null 2>&1 && [ -n "$(fc-list :lang=ko 2>/dev/null)" ]
}

if has_korean_font; then
  echo "한글 폰트 이미 있음: $(fc-list :lang=ko | head -1 | cut -d: -f2)"
  exit 0
fi

echo "한글 폰트 없음 → 나눔고딕 설치 시도"
mkdir -p "$FONT_DIR"
downloaded=0
for f in $FILES; do
  if curl -fsSL --retry 3 --retry-delay 1 --max-time 60 -o "${FONT_DIR}/${f}" "${BASE_URL}/${f}"; then
    downloaded=1
  else
    echo "  다운로드 실패: ${f}"
    rm -f "${FONT_DIR}/${f}"
  fi
done

if [ "$downloaded" = "1" ]; then
  fc-cache -f "$FONT_DIR" >/dev/null 2>&1 || fc-cache -f >/dev/null 2>&1 || true
fi

if ! has_korean_font; then
  echo "다운로드로 해결되지 않음 → apt 패키지 시도"
  sudo apt-get install -y -qq fonts-nanum >/dev/null 2>&1 \
    || { sudo apt-get update -qq >/dev/null 2>&1 && sudo apt-get install -y -qq fonts-nanum >/dev/null 2>&1; } \
    || sudo apt-get install -y -qq fonts-noto-cjk >/dev/null 2>&1 \
    || true
  fc-cache -f >/dev/null 2>&1 || true
fi

if has_korean_font; then
  echo "한글 폰트 준비 완료: $(fc-list :lang=ko | head -1 | cut -d: -f2)"
else
  echo "[경고] 한글 폰트를 설치하지 못했습니다 — 스크린샷의 한글이 깨질 수 있습니다 (예약 동작에는 영향 없음)"
fi
exit 0
