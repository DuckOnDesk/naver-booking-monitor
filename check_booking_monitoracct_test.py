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
  - 접속 제한 알림은 한 번만, 비로그인 제한은 새 실행(IP 변경)으로 넘기되 계속 막히면 쉰다
  - 예매 기한이 끝난 팝업은 닫힘 사유에 표시가 붙고, 10분마다만 다시 본다

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
    cb._block_notified.clear()
    cb._block_notices.clear()
    cb._anon_block_pending = False


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

    print("9) 계정 접속 제한 알림은 풀릴 때까지 한 번만")
    reset()
    sent = []
    cb.send_ntfy = lambda topic, title, body, url: sent.append(title)
    U7 = "https://m.booking.naver.com/booking/12/bizes/1/items/7"
    cb._login_forced.add("/items/7")
    os.environ["MONITOR_ACCOUNTS"] = "hye"
    cb._browser_get = lambda: blocked
    cb._playwright_check(U7)
    cb.flush_block_notices("t")
    check(len(sent) == 1 and "계정 hye" in sent[0], f"처음 막히면 알림: {sent}")
    cb._monitor_bench.clear()               # 60분 지나 다시 봤는데 또 막힘
    cb._playwright_check(U7)
    cb.flush_block_notices("t")
    check(len(sent) == 1, f"풀리기 전엔 다시 안 보냄: {sent}")
    cb._monitor_bench.clear()
    cb._browser_get = lambda: ok
    cb._playwright_check(U7)                # 정상으로 열림 → 해제
    cb._browser_get = lambda: blocked
    cb._playwright_check(U7)
    cb.flush_block_notices("t")
    check(len(sent) == 2, f"풀렸다 다시 막히면 또 알림: {sent}")
    os.environ.pop("MONITOR_ACCOUNTS")

    print("10) 비로그인 제한 → 새 실행(새 IP), 계속 막히면 쉼")
    reset()
    sent.clear()
    cb._runner_ip = "1.1.1.1"
    U = "https://m.booking.naver.com/booking/12/bizes/1/items/2"
    alerted = {}
    check(cb.decide_anon_restart(alerted) is False, "안 막혔으면 그대로")
    cb._playwright_check(U)
    check(cb.decide_anon_restart(alerted) is True, "막히면 새 실행으로")
    cb.flush_block_notices("t")
    check(len(sent) == 1 and "IP를 바꿔" not in sent[0] and "비로그인" in sent[0], f"첫 알림: {sent}")
    # 새 실행 (메모리는 비고 상태 파일만 넘어간다)
    reset(); cb._runner_ip = "2.2.2.2"
    cb._playwright_check(U)
    check(cb.decide_anon_restart(alerted) is True, "두 번째도 새 실행으로")
    cb.flush_block_notices("t")
    check(len(sent) == 1, f"중간 재시작은 알림 없음: {sent}")
    reset(); cb._runner_ip = "3.3.3.3"
    cb._playwright_check(U)
    check(cb.decide_anon_restart(alerted) is False, "1시간 안에 2번 넘겼으면 그만 — 쉰다")
    cb.flush_block_notices("t")
    check(len(sent) == 2 and "IP 바꿔도" in sent[1], f"포기할 때 한 번 더: {sent}")
    check("" in cb._monitor_bench, "비로그인 쉼")
    old = {"at": "2026-01-01T00:00:00+09:00", "ip": "9.9.9.9"}
    alerted = {cb.ANON_RESTART_KEY: [old, old]}
    reset()
    cb._playwright_check(U)
    check(cb.decide_anon_restart(alerted) is True, "1시간 지난 기록은 안 센다")

    print("11) 예매 기한 종료 표시")
    reset()
    ended = FakeBrowser("https://m.booking.naver.com/booking/5/bizes/1733276",
                        "넷플잔치 ** <넷플잔치>의 티켓 예매 / 최종 취소 기한은 종료되었습니다.")
    cb._browser_get = lambda: ended
    closed, reason = cb._playwright_check("https://m.booking.naver.com/booking/5/bizes/1733276/items/8035197")
    check(closed is True and reason.startswith(cb.ENDED_MARK), f"사유에 표시: {reason}")
    al = {}
    g = cb.UrlGate({"name": "넷플"}, "x", "u", "넷플", al, "", "00:00:00")
    g._apply(True, reason)
    check(al["x:url_closed"] == "ended" and g.ended, f"상태에 남김: {al}")
    g._apply(True, "URL 리다이렉트: /error/")
    check(al["x:url_closed"] == 1 and not g.ended, "그냥 닫힘이면 표시 안 함")

    print("12) 기한 종료 항목은 10분마다만 다시 본다")
    import time as _t
    w = cb.GateWorker()
    now = _t.monotonic()
    w._watches = {
        "plain": {"last": now - 60, "closed": True, "ended": False, "seen": now, "url": "a"},
        "ended": {"last": now - 60, "closed": True, "ended": True, "seen": now, "url": "b"},
    }
    task, _ = w._next_task()
    check(task and task[0] == "plain", f"그냥 닫힘은 30초 지나면 다시 본다: {task}")
    del w._watches["plain"]
    task, wait = w._next_task()
    check(task is None, f"기한 종료는 1분 만엔 안 본다: {task}")
    w._watches["ended"]["last"] = now - 601
    task, _ = w._next_task()
    check(task and task[0] == "ended", "10분 지나면 본다")
    cb._gate_worker = None
    calls = []
    cb.browser_check = lambda u: (calls.append(u), (True, cb.ENDED_MARK + " x"))[1]
    al = {"e:url_closed": "ended"}
    g = cb.UrlGate({"name": "e"}, "e", "u", "e", al, "", "00:00:00")
    cb._url_checked_at["e"] = _t.monotonic() - 60
    check(g.closed is True and not calls, f"워커 없을 때도 주기 대기: {calls}")
    cb._url_checked_at["e"] = _t.monotonic() - 601
    g2 = cb.UrlGate({"name": "e"}, "e", "u", "e", al, "", "00:00:00")
    check(g2.closed is True and len(calls) == 1, f"10분 지나면 확인: {calls}")
    al2 = {"p:url_closed": 1}
    cb._url_checked_at["p"] = _t.monotonic() - 1
    g3 = cb.UrlGate({"name": "p"}, "p", "u", "p", al2, "", "00:00:00")
    g3.closed
    check(len(calls) == 2, "그냥 닫힘은 매 회차 확인 (종전대로)")

    print("13) URL에 끼어 든 공백 (2026-10-23 디웨더)")
    spaced = "https://m.booking.naver.com/booking/5/bizes/1756472/items /8131769"
    check(cb.parse_naver_url(spaced) == {"service_id": 5, "biz_id": "1756472", "item_id": "8131769"},
          "파싱")
    saved_fetch = cb.fetch_remote_monitors
    cb.fetch_remote_monitors = lambda: {"monitors": [{"url": spaced}, {"name": "url 없음"}]}
    got = cb.load_monitors(from_github=True)["monitors"][0]["url"]
    cb.fetch_remote_monitors = saved_fetch
    check(" " not in got and got.endswith("/items/8131769"), f"불러올 때 공백 제거: {got}")

    print()
    if fails:
        print(f"실패 {len(fails)}건")
        sys.exit(1)
    print("모두 통과")


if __name__ == "__main__":
    main()
