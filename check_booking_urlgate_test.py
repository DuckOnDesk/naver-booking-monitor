"""예약창 확인(브라우저) 정책 회귀 테스트 — UrlGate.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — 예약창 열림/닫힘 확인은 항목마다 chromium을 새로 띄워 4초 넘게 걸렸고,
항목이 8개면 한 회차의 절반 이상을 여기에 썼다. 그런데 이 값이 필요한 순간은
"알릴 자리가 실제로 있을 때"뿐이다. 자리가 없으면 🎉로도 🔒로도 알릴 게 없다.

확인 내용:
  - 자리가 없으면 브라우저를 아예 켜지 않는다
  - 자리가 생기면 그 회차에 확인하고, 열림/닫힘에 따라 알림이 갈린다
  - 자리가 있는데 닫혀 있으면 매 회차 확인한다 (열리는 순간을 놓치지 않도록)
  - 자리가 있고 열려 있으면 URL_RECHECK_SEC 안에는 다시 확인하지 않는다
  - 닫힘 감지가 진행 중(확정 전)이면 주기를 무시하고 매 회차 확인한다
  - 자리가 사라지면 다시 확인하지 않는다 (1번 상태로 복귀)
  - 자동예약 직전에는 주기와 무관하게 확인하고, 닫혀 있으면 시도하지 않는다
  - 닫힘 연속 카운트에 상한이 있어 alerted가 회차마다 바뀌지 않는다
    (바뀌면 회차마다 git 커밋·푸시가 돈다)
  - 브라우저가 죽으면 다음 확인에서 새로 띄운다

사용법: python check_booking_urlgate_test.py
"""

import sys
from datetime import date, timedelta

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


URL = "https://booking.naver.com/booking/13/bizes/111/items/222"
D = (date.today() + timedelta(days=5)).isoformat()

calls: list = []      # _playwright_check 호출 기록
closed_now = False    # 대체 _playwright_check가 돌려줄 상태


def fake_check(url):
    calls.append(url)
    return (True, "URL 리다이렉트: /error/") if closed_now else (False, "")


def advance():
    """재확인 주기(URL_RECHECK_SEC)가 지난 상황을 만든다."""
    cb._url_checked_at.clear()


def unit(hhmm, *, stock=10, booked=0):
    return {"unitStartTime": f"{D} {hhmm}:00", "unitStock": stock,
            "unitBookingCount": booked, "isUnitSaleDay": True}


def run_round(hourly, alerted, *, item=None):
    """check_all 한 회차. (로그, 보낸 알림, 이번 회차 브라우저 호출 수)를 돌려준다."""
    total_stock = sum(s["unitStock"] for s in hourly)
    total_booked = sum(s["unitBookingCount"] for s in hourly)
    day = {"dateKey": D, "stock": total_stock, "bookingCount": total_booked,
           "hasBookableSlots": total_stock > total_booked, "isSaleDay": True}

    logs: list = []
    sent: list = []
    before = len(calls)

    cb.check_availability = lambda b, i, s, t: {
        "days": [day], "sale_start_date": None, "sale_end_date": None, "_all_summary": [day],
    }
    cb.fetch_slots = lambda b, i, s, dk: {
        "times": [x["unitStartTime"][11:16] for x in hourly
                  if x.get("unitStock", 0) - x.get("unitBookingCount", 0) > 0],
        "total": len(hourly), "queried": True, "all_slots": list(hourly),
        "api_slot_count": len(hourly),
    }
    cb.fetch_calendar_day_status = lambda s, b, dk: None
    cb._playwright_check = fake_check
    cb.probe_schedule_period = lambda p: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda topic, title, body, u: sent.append(title)
    cb.prune_dead_dates = lambda pruned: None

    import builtins
    real_print = builtins.print
    builtins.print = lambda *a, **kw: logs.append(" ".join(str(x) for x in a))
    try:
        cb.check_all([item or {"id": "t1", "name": "테스트", "url": URL,
                               "enabled": True, "target_dates": [D]}], "topic", alerted)
    finally:
        builtins.print = real_print
    return logs, sent, len(calls) - before


def main() -> int:
    global closed_now
    cb.LOG_DEDUP = False        # 로그 생략과 무관하게 정책만 본다
    cb.URL_RECHECK_SEC = 300

    print("1) 자리가 없으면 브라우저를 켜지 않는다")
    cb.reset_log_state()
    alerted: dict = {}
    closed_now = False
    logs, sent, n = run_round([unit("11:00", stock=4, booked=4)], alerted)
    check(n == 0, f"브라우저 확인 0회 (실제 {n}회)")
    check(any("예약창 확인 생략" in l for l in logs), f"생략 사유를 로그로 남김 (실제: {logs})")
    check(not any("예약창 열림" in l for l in logs), "예약창 상태 줄 없음")
    check(sent == [], "알림 없음")

    print("2) 자리가 생기면 그 회차에 확인하고 알림이 나간다 (열림)")
    logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted)
    check(n == 1, f"브라우저 확인 1회 (실제 {n}회)")
    check(any("✅" in l and "예약창 열림" in l for l in logs), "예약창 열림 기록")
    check(any("예약 가능" in t for t in sent), f"예약 가능 알림 (실제: {sent})")

    print("3) 열려 있으면 재확인 주기 안에는 다시 켜지 않는다")
    logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted)
    check(n == 0, f"브라우저 확인 0회 (실제 {n}회)")
    check(any("주기 대기" in l for l in logs), f"주기 대기로 생략 표시 (실제: {logs})")

    print("3-1) 주기가 지나면 다시 확인한다")
    cb.URL_RECHECK_SEC = 0
    try:
        logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted)
        check(n == 1, f"브라우저 확인 1회 (실제 {n}회)")
    finally:
        cb.URL_RECHECK_SEC = 300

    print("4) 닫힘이 잡히면 확정 전까지 주기를 무시하고 매 회차 확인한다")
    closed_now = True
    advance()                      # 재확인 주기가 지나 다시 들여다본 회차
    logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted)
    check(n == 1, f"1회차 확인 (실제 {n}회)")
    check(any("닫힘 감지 (1/2" in l for l in logs), f"확정 전 대기 표시 (실제: {logs})")
    check(alerted.get("t1:url_close_streak") == 1, "연속 카운트 1")
    logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted)
    check(n == 1, f"주기(5분)를 무시하고 바로 재확인 (실제 {n}회)")
    check(alerted.get("t1:url_closed") == 1, "닫힘 확정")
    check(any("🔒" in t for t in sent), f"닫힘 상태 자리 알림 (실제: {sent})")

    print("5) 자리가 있고 닫혀 있으면 계속 매 회차 확인한다")
    logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted)
    check(n == 1, f"브라우저 확인 1회 (실제 {n}회)")

    print("6) 닫힘 연속 카운트에 상한이 있어 alerted가 더는 안 바뀐다")
    snapshot = dict(alerted)
    run_round([unit("11:00", stock=4, booked=1)], alerted)
    check(alerted == snapshot,
          f"회차를 더 돌아도 저장 내용 동일 (커밋 낭비 없음) — 차이: "
          f"{ {k: (snapshot.get(k), v) for k, v in alerted.items() if snapshot.get(k) != v} }")
    check(alerted.get("t1:url_close_streak") == cb.CLOSE_CONFIRM, "연속 카운트가 상한에서 멈춤")

    print("7) 자리가 사라지면 다시 확인하지 않는다 (1번 상태로 복귀)")
    logs, sent, n = run_round([unit("11:00", stock=4, booked=4)], alerted)
    check(n == 0, f"브라우저 확인 0회 (실제 {n}회)")

    print("8) 닫힌 상태에서 자리가 다시 생기면 즉시 확인한다")
    logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted)
    check(n == 1, f"브라우저 확인 1회 (실제 {n}회)")

    print("9) 자동예약 직전에는 주기와 무관하게 확인하고, 닫혀 있으면 시도하지 않는다")
    cb.reset_log_state()
    closed_now = True
    dispatched: list = []
    cb.dispatch_auto_book = lambda *a, **kw: (dispatched.append(a) or (True, ""))
    cb._inline_try_book = lambda *a, **kw: None
    ab_item = {"id": "t2", "name": "자동예약", "url": URL, "enabled": True,
               "target_dates": [D], "auto_book": {"enabled": True}}
    alerted2: dict = {"t2:url_closed": 1, "t2:url_close_streak": cb.CLOSE_CONFIRM}
    logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted2, item=ab_item)
    check(dispatched == [], f"닫혀 있으면 자동예약을 걸지 않음 (실제 {len(dispatched)}건)")

    print("9-1) 열려 있으면 정상적으로 자동예약을 건다")
    closed_now = False
    logs, sent, n = run_round([unit("11:00", stock=4, booked=1)], alerted2, item=ab_item)
    check(len(dispatched) == 1, f"자동예약 디스패치 1건 (실제 {len(dispatched)}건)")

    print("10) 브라우저가 죽으면 다음 확인에서 새로 띄운다")
    launches: list = []

    class DeadBrowser:
        def is_connected(self):
            return False

    class LiveBrowser:
        def is_connected(self):
            return True

    cb._pw_browser = DeadBrowser()
    cb._pw_handle = None
    real_launch = cb._browser_get
    try:
        import playwright.sync_api as pw_api

        class FakeChromium:
            def launch(self, **kw):
                launches.append(kw)
                return LiveBrowser()

        class FakeHandle:
            chromium = FakeChromium()

            def stop(self):
                pass

        pw_api.sync_playwright = lambda: type("S", (), {"start": staticmethod(lambda: FakeHandle())})()
        got = cb._browser_get()
        check(len(launches) == 1 and got.is_connected(), "죽은 인스턴스를 버리고 새로 띄움")
        got2 = cb._browser_get()
        check(len(launches) == 1 and got2 is got, "살아 있으면 재사용 (재기동 없음)")
    finally:
        cb._browser_get = real_launch
        cb._pw_browser = cb._pw_handle = None

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
