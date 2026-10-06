"""쿠키 묶음(COOKIES_BUNDLE_JSON) 회귀 테스트 (네트워크 불필요).

2026-10-06: 자동예약이 오래전 NAVER_COOKIES_1~5를 쓰다 5개 계정 모두 로그인으로
튕겼다. 같은 시각 naver_sync는 자기 쿠키 묶음으로 멀쩡히 로그인하고 있었다.
그래서 자동예약도 naver_sync와 같은 묶음을 읽는다.

  1) 묶음이 있으면 묶음 계정을 쓰고, 아이디를 계정번호에 붙여 기억한다
  2) COOKIES_BUNDLE_ORDER로 번호 순서를 정할 수 있다
  3) 묶음이 없으면 종전대로 NAVER_COOKIES_1~5
  4) storage_state 쿠키가 브라우저에 그대로 들어간다 (sameSite 등 형식 정리)
  5) 묶음이 깨졌으면 NAVER_COOKIES_1~5로 대체
  6) check_booking.naver_cookies()도 묶음 첫 계정을 쓴다

사용법: python auto_book_cookie_test.py
"""

import json
import os
import sys

import auto_book

FAILS = []


def check(name, cond, actual=None):
    print(f"    {'PASS' if cond else 'FAIL'} — {name}" + (f" (실제: {actual!r})" if actual is not None else ""))
    if not cond:
        FAILS.append(name)


def ck(name, value, **kw):
    return {"name": name, "value": value, "domain": ".naver.com", "path": "/",
            "expires": 1830000000, "httpOnly": True, "secure": True, "sameSite": "Lax", **kw}


BUNDLE = {
    "betty": {"cookies": [ck("NID_AUT", "a1"), ck("NID_SES", "s1")], "origins": []},
    "gogo": {"cookies": [ck("NID_AUT", "a2"), ck("NID_SES", "s2", sameSite="")], "origins": []},
    "hye": {"cookies": [ck("NID_AUT", "a3")], "origins": []},
}


def setenv(**kw):
    for k in ("COOKIES_BUNDLE_JSON", "COOKIES_BUNDLE_ORDER", "NAVER_COOKIES",
              *(f"NAVER_COOKIES_{i}" for i in range(1, 6))):
        os.environ.pop(k, None)
    os.environ.update(kw)


def main() -> int:
    print("1) 묶음이 있으면 묶음 계정을 쓴다")
    setenv(COOKIES_BUNDLE_JSON=json.dumps(BUNDLE), NAVER_COOKIES_1="NID_AUT=old")
    acc = auto_book.get_accounts()
    check("3계정", [a[0] for a in acc] == [1, 2, 3], [a[0] for a in acc])
    check("번호→아이디", auto_book.ACCOUNT_NAMES == {1: "betty", 2: "gogo", 3: "hye"}, auto_book.ACCOUNT_NAMES)
    check("쿠키는 목록", isinstance(acc[0][1], list) and auto_book._has_login_token(acc[0][1]))
    check("우선순위 지정", [a[0] for a in auto_book.get_accounts([3, 1])] == [3, 1])

    print("2) COOKIES_BUNDLE_ORDER")
    setenv(COOKIES_BUNDLE_JSON=json.dumps(BUNDLE), COOKIES_BUNDLE_ORDER="hye, betty")
    auto_book.get_accounts()
    check("순서", auto_book.ACCOUNT_NAMES == {1: "hye", 2: "betty", 3: "gogo"}, auto_book.ACCOUNT_NAMES)

    print("3) 묶음이 없으면 NAVER_COOKIES_1~5")
    setenv(NAVER_COOKIES_2="NID_AUT=x; NID_SES=y")
    acc = auto_book.get_accounts()
    check("계정2 문자열", acc == [(2, "NID_AUT=x; NID_SES=y")], acc)

    print("5) 깨진 묶음은 무시")
    setenv(COOKIES_BUNDLE_JSON="{broken", NAVER_COOKIES_1="NID_AUT=x")
    check("NAVER_COOKIES_1로 대체", auto_book.get_accounts() == [(1, "NID_AUT=x")])

    print("6) check_booking.naver_cookies()")
    setenv(COOKIES_BUNDLE_JSON=json.dumps(BUNDLE), NAVER_COOKIES="NID_AUT=old")
    import check_booking
    got = check_booking.naver_cookies()
    check("묶음 첫 계정", [c["value"] for c in got] == ["a1", "s1"], [c["value"] for c in got])

    print("4) storage_state 쿠키를 브라우저에 넣는다")
    parsed = auto_book._parse_cookies(BUNDLE["gogo"]["cookies"])
    check("빈 sameSite는 뺀다", "sameSite" not in parsed[1], parsed[1])
    from playwright.sync_api import sync_playwright
    kw = {"headless": True}
    exe = os.environ.get("AUTO_BOOK_CHROMIUM", "").strip()
    if exe:
        kw["executable_path"] = exe
    with sync_playwright() as p:
        b = p.chromium.launch(**kw)
        ctx = b.new_context()
        ctx.add_cookies(parsed)
        names = sorted(c["name"] for c in ctx.cookies("https://m.booking.naver.com/"))
        check("booking 도메인에 로그인 쿠키", names == ["NID_AUT", "NID_SES"], names)
        b.close()

    print(f"\n=== 실패 {len(FAILS)}건 ===")
    for f in FAILS:
        print(f"  - {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
