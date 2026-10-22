"""감시 계정 돌려쓰기 / 접속 제한 회귀 테스트.

네트워크 없이 돈다.

배경 — 2026-10-22 betty. 예약창 확인(브라우저)이 늘 묶음 첫 계정만 써서, 닫힌 항목을
30초마다 다시 여는 동안 그 계정만 "과다한 요청 시도로 접속을 제한" 당했다. 제한
페이지는 상품 페이지가 아니라 '닫힘'으로 잡혔고, 닫힘은 30초마다 다시 보니 제한이
풀릴 틈도 없었다.

확인 내용:
  - 묶음 계정을 돌아가며 쓴다
  - MONITOR_ACCOUNTS로 감시 계정을 고를 수 있다 (안 맞으면 전체)
  - MONITOR_ACCOUNTS_EXCLUDE로 뺀 계정은 쓰지 않는다
  - 제한 페이지는 닫힘이 아니라 모름(None)이고, 그 계정은 쉬게 한다
  - 쉬는 계정은 건너뛰고, 전부 쉬면 브라우저를 열지 않는다
  - 비로그인 팝업은 비로그인만 쓴다 — 막혀도 계정으로 넘기지 않는다
  - 로그인 화면으로 가는 팝업은 기억해 그 팝업만 계정으로 본다 (상태 파일에 남는다)

사용법: python check_booking_monitoracct_test.py
"""

import json
import os
import sys

BUNDLE = {
    "betty": {"cookies": [{"name": "NID_AUT", "value": "b", "domain": ".naver.com", "path": "/"}]},
    "hye": {"cookies": [{"name": "NID_AUT", "value": "h", "domain": ".naver.com", "path": "/"}]},
    "gogo": {"cookies": [{"name": "NID_AUT", "value": "g", "domain": ".naver.com", "path": "/"}]},
}
os.environ["COOKIES_BUNDLE_JSON"] = json.dumps(BUNDLE)
os.environ.pop("COOKIES_BUNDLE_ORDER", None)
os.environ.pop("MONITOR_ACCOUNTS", None)
os.environ["MONITOR_ANON"] = "0"   # 1~5는 계정 돌려쓰기만 본다

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


def reset():
    cb._monitor_rr = 0
    cb._monitor_bench.clear()
    cb._login_items.clear()
    cb._login_forced.clear()


class FakePage:
    def __init__(self, url, text):
        self.url, self._text = url, text

    def goto(self, *a, **k):
        pass

    def wait_for_timeout(self, *a):
        pass

    def inner_text(self, _sel):
        return self._text


class FakeBrowser:
    def __init__(self, url, text):
        self.url, self.text, self.used = url, text, []

    def is_connected(self):
        return True

    def new_context(self, **k):
        outer = self

        class Ctx:
            def add_cookies(self, cookies):
                outer.used.append(cookies[0]["value"])

            def new_page(self):
                return FakePage(outer.url, outer.text)

            def close(self):
                pass
        return Ctx()


def main():
    print("1) 돌아가며 쓴다")
    reset()
    got = [cb.naver_cookies()[0]["value"] for _ in range(4)]
    check(got == ["b", "h", "g", "b"], got)

    print("2) MONITOR_ACCOUNTS")
    reset()
    os.environ["MONITOR_ACCOUNTS"] = "hye, gogo"
    got = [cb.naver_cookies()[0]["value"] for _ in range(3)]
    check(got == ["h", "g", "h"], got)
    os.environ["MONITOR_ACCOUNTS"] = "nobody"
    got = [cb.naver_cookies()[0]["value"] for _ in range(3)]
    check(sorted(got) == ["b", "g", "h"], f"안 맞으면 전체: {got}")
    os.environ.pop("MONITOR_ACCOUNTS")
    os.environ["MONITOR_ACCOUNTS_EXCLUDE"] = "betty"
    got = [cb.naver_cookies()[0]["value"] for _ in range(4)]
    check("b" not in got and set(got) == {"h", "g"}, f"제외 계정은 안 쓴다: {got}")
    os.environ["MONITOR_ACCOUNTS"] = "betty,hye"
    got = [cb.naver_cookies()[0]["value"] for _ in range(3)]
    check(got and set(got) == {"h"}, f"지정해도 제외가 이긴다: {got}")
    os.environ.pop("MONITOR_ACCOUNTS")
    os.environ["MONITOR_ACCOUNTS_EXCLUDE"] = "betty,hye,gogo"
    check(cb.naver_cookies() == [], "전부 제외면 제외 계정으로 되돌아가지 않는다")
    os.environ.pop("MONITOR_ACCOUNTS_EXCLUDE")

    print("3) 제한 페이지 → 모름 + 그 계정 쉼")
    reset()
    blocked = FakeBrowser("https://m.booking.naver.com/error",
                          "지금은 서비스에 접속할 수 없습니다. 과다한 요청 시도로 접속을 제한하였습니다.")
    cb._browser_get = lambda: blocked
    closed, reason = cb._playwright_check("https://m.booking.naver.com/booking/12/bizes/1/items/2")
    check(closed is None, f"닫힘으로 보지 않는다: {closed} {reason}")
    check("betty" in reason, reason)
    check("betty" in cb._monitor_bench, "betty 쉼")

    ok = FakeBrowser("https://m.booking.naver.com/booking/12/bizes/1/items/2", "예약하기")
    cb._browser_get = lambda: ok
    for _ in range(4):
        cb._playwright_check("https://m.booking.naver.com/booking/12/bizes/1/items/2")
    check("b" not in ok.used and set(ok.used) == {"h", "g"}, f"쉬는 계정 건너뜀: {ok.used}")

    print("4) 전부 쉬면 브라우저를 열지 않는다")
    for n in BUNDLE:
        cb._bench_monitor_account(n)
    ok.used.clear()
    closed, reason = cb._playwright_check("https://m.booking.naver.com/booking/12/bizes/1/items/2")
    check(closed is None and not ok.used, f"{closed} {reason} {ok.used}")

    print("5) 정상 페이지는 그대로 열림")
    reset()
    closed, _ = cb._playwright_check("https://m.booking.naver.com/booking/12/bizes/1/items/2")
    check(closed is False, f"{closed}")

    print("6) 비로그인 우선 (MONITOR_ANON=1)")
    reset()
    os.environ["MONITOR_ANON"] = "1"
    ok.used.clear()
    closed, _ = cb._playwright_check("https://m.booking.naver.com/booking/12/bizes/1/items/2")
    check(closed is False and not ok.used, f"쿠키 없이 본다: {closed} {ok.used}")
    check([c[0]["value"] for c in [cb.naver_cookies()]] == ["b"], "진단(naver_cookies)은 계정을 준다")
    cb._browser_get = lambda: blocked
    closed, _ = cb._playwright_check("https://m.booking.naver.com/booking/12/bizes/1/items/2")
    check(closed is None and "" in cb._monitor_bench, "비로그인 제한 → 비로그인 쉼")
    cb._browser_get = lambda: ok
    ok.used.clear()
    closed, reason = cb._playwright_check("https://m.booking.naver.com/booking/12/bizes/1/items/2")
    check(closed is None and not ok.used, f"비로그인이 쉬어도 계정으로 넘기지 않는다: {reason} {ok.used}")
    cb._login_forced.add("/items/5")
    cb._playwright_check("https://m.booking.naver.com/booking/12/bizes/1/items/5")
    check(len(ok.used) == 1, f"로그인 필요 팝업은 그대로 계정으로: {ok.used}")

    print("7) 로그인 필요 팝업만 계정으로")
    reset()
    U = "https://m.booking.naver.com/booking/12/bizes/1/items/2"
    U2 = "https://m.booking.naver.com/booking/12/bizes/1/items/3"
    login = FakeBrowser("https://nid.naver.com/nidlogin.login?url=x", "로그인")
    cb._browser_get = lambda: login
    closed, reason = cb._playwright_check(U)
    check(closed is None, f"로그인 화면은 닫힘 아님: {closed} {reason}")
    check("" not in cb._monitor_bench, "비로그인 전체를 쉬게 하지는 않는다")
    check("/items/2" in cb._login_items, f"팝업 기록: {cb._login_items}")
    ok.used.clear()
    cb._browser_get = lambda: ok
    cb._playwright_check(U)
    check(len(ok.used) == 1, f"기록된 팝업은 계정으로: {ok.used}")
    ok.used.clear()
    cb._playwright_check(U2)
    check(not ok.used, f"다른 팝업은 그대로 비로그인: {ok.used}")
    textlogin = FakeBrowser(U2, "이 상품은 로그인이 필요합니다")
    cb._browser_get = lambda: textlogin
    cb._playwright_check(U2)
    check("/items/3" in cb._login_items, "본문 '로그인이 필요'도 로그인 팝업")

    print("8) 상태 파일 저장/복원, monitors.json login: true")
    alerted = {}
    cb.sync_login_items([], alerted)
    check(set(alerted.get(cb.LOGIN_ITEMS_KEY, {})) == {"/items/2", "/items/3"}, f"저장: {alerted}")
    cb._login_items.clear()
    cb.sync_login_items([], alerted)
    check(set(cb._login_items) == {"/items/2", "/items/3"}, "재시작 후 복원")
    cb._login_items.clear()
    cb.sync_login_items([{"url": "https://m.booking.naver.com/booking/12/bizes/1/items/9", "login": True}], {})
    check(cb._needs_login("https://booking.naver.com/booking/12/bizes/1/items/9?x=1"), "login: true")
    ok.used.clear()
    cb._browser_get = lambda: ok
    cb._playwright_check("https://booking.naver.com/booking/12/bizes/1/items/9")
    check(len(ok.used) == 1, f"login: true 팝업은 계정으로: {ok.used}")

    print()
    if fails:
        print(f"실패 {len(fails)}건")
        sys.exit(1)
    print("모두 통과")


if __name__ == "__main__":
    main()
