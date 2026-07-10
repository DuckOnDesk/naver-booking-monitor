"""네이버 로그인 쿠키 캡처 + GitHub Secrets 자동 업로드 — 로컬 PC에서 실행하는 스크립트.

크롬 창이 열리면 네이버에 로그인하세요. 로그인이 감지되면 쿠키를 추출해
GitHub Secret(NAVER_COOKIES_n)으로 자동 업로드합니다. 계정마다 새 창(독립 세션)이
열리므로 5개 계정을 차례로 로그인하면 됩니다.

사용법:
  python save_cookies.py            # 계정 1~5 차례로
  python save_cookies.py 2          # 계정 2만
  python save_cookies.py 1 3        # 계정 1, 3만

사전 준비 (최초 1회):
  pip install playwright pynacl requests
  (크롬이 설치돼 있으면 그대로 사용, 없으면: python -m playwright install chromium)

GitHub PAT: 환경변수 GH_PAT 또는 실행 시 입력.
  권한 — Fine-grained: 이 리포의 Secrets "Read and write" / Classic: repo 스코프
"""

import base64
import getpass
import os
import sys
import time

import requests

OWNER, REPO = "DuckOnDesk", "naver-booking-monitor"
LOGIN_URL = "https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fwww.naver.com"
LOGIN_WAIT_SEC = 600  # 계정당 최대 10분 대기


def get_pat() -> str:
    pat = os.environ.get("GH_PAT", "").strip()
    if not pat:
        pat = getpass.getpass("GitHub PAT (Secrets 쓰기 권한): ").strip()
    return pat


def upload_secret(pat: str, name: str, value: str) -> None:
    """리포 공개키로 암호화해 Actions Secret 저장."""
    from nacl import encoding, public

    headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"}
    r = requests.get(
        f"https://api.github.com/repos/{OWNER}/{REPO}/actions/secrets/public-key",
        headers=headers, timeout=15,
    )
    r.raise_for_status()
    key = r.json()
    pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(value.encode())
    r = requests.put(
        f"https://api.github.com/repos/{OWNER}/{REPO}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": base64.b64encode(sealed).decode(), "key_id": key["key_id"]},
        timeout=15,
    )
    r.raise_for_status()


def capture_account(n: int, pat: str) -> bool:
    from playwright.sync_api import sync_playwright

    print(f"\n=== 계정{n}: 열린 크롬 창에서 네이버에 로그인하세요 (최대 {LOGIN_WAIT_SEC // 60}분) ===")
    cookie_str = None
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = p.chromium.launch(headless=False)  # 크롬 미설치 시 내장 chromium
        ctx = browser.new_context(locale="ko-KR")
        page = ctx.new_page()
        page.goto(LOGIN_URL)
        try:
            for _ in range(LOGIN_WAIT_SEC):
                try:
                    cookies = ctx.cookies("https://www.naver.com")
                except Exception:
                    break  # 창이 닫힘
                names = {c["name"] for c in cookies}
                if "NID_AUT" in names and "NID_SES" in names:
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    print(f"[계정{n}] 로그인 감지 ✓")
                    break
                time.sleep(1)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if not cookie_str:
        print(f"[계정{n}] ❌ 로그인이 감지되지 않아 건너뜁니다")
        return False

    upload_secret(pat, f"NAVER_COOKIES_{n}", cookie_str)
    print(f"[계정{n}] ✅ GitHub Secret NAVER_COOKIES_{n} 저장 완료")
    return True


def main() -> None:
    try:
        nums = [int(a) for a in sys.argv[1:]] or [1, 2, 3, 4, 5]
    except ValueError:
        print("사용법: python save_cookies.py [계정번호...] (1~5)")
        sys.exit(2)
    if any(n < 1 or n > 5 for n in nums):
        print("계정 번호는 1~5만 가능합니다")
        sys.exit(2)

    pat = get_pat()
    done = []
    for i, n in enumerate(nums):
        if capture_account(n, pat):
            done.append(n)
        if i < len(nums) - 1:
            try:
                input(f"Enter를 누르면 계정{nums[i + 1]} 진행 (중단: Ctrl+C)... ")
            except KeyboardInterrupt:
                print("\n중단됨")
                break

    print(f"\n완료된 계정: {done or '없음'}")
    if done:
        print("모니터가 다음 실행부터 새 쿠키를 사용합니다 (Secret은 새 워크플로우 실행부터 반영).")


if __name__ == "__main__":
    main()
