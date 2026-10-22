"""감시 계정 돌려쓰기 / 접속 제한 회귀 테스트.

네트워크 없이 돈다.

배경 — 2026-10-22 betty. 예약창 확인(브라우저)이 늘 묶음 첫 계정만 써서, 닫힌 항목을
30초마다 다시 여는 동안 그 계정만 "과다한 요청 시도로 접속을 제한" 당했다. 제한
페이지는 상품 페이지가 아니라 '닫힘'으로 잡혔고, 닫힘은 30초마다 다시 보니 제한이
풀릴 틈도 없었다.

확인 내용:
  - 묶음 계정을 돌아가며 쓴다
  - MONITOR_ACCOUNTS로 감시 계정을 고를 수 있다 (안 맞으면 전체)
  - 제한 페이지는 닫힘이 아니라 모름(None)이고, 그 계정은 쉬게 한다
  - 쉬는 계정은 건너뛰고, 전부 쉬면 브라우저를 열지 않는다

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

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


def reset():
    cb._monitor_rr = 0
    cb._monitor_bench.clear()


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

    print()
    if fails:
        print(f"실패 {len(fails)}건")
        sys.exit(1)
    print("모두 통과")


if __name__ == "__main__":
    main()
