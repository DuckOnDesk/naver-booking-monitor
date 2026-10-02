"""예약창 확인 백그라운드 워커 회귀 테스트 — GateWorker.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — 예약창 확인(브라우저)은 한 번에 4~17초라, 자리 있고 닫힌 항목이 여럿이면
자리 확인 회차가 4분 넘게 늘었다 (2026-09-29). 브라우저 확인을 워커 스레드로 빼서
자리 확인 루프는 API 호출만 하도록 나눴다.

확인 내용:
  - 브라우저는 워커 스레드에서만 켠다 (playwright sync API는 만든 스레드 전용)
  - 처음 보는 항목(닫힘 기록 없음)은 기다려서 확인하고 그 회차에 알린다
  - 이미 상태를 아는 항목은 회차에서 기다리지 않는다 (단 열림은 URL_RECHECK_SEC 안의 것만)
  - 자리 있고 닫힌 항목은 회차와 무관하게 워커가 계속 다시 본다
  - 워커가 열림 전환을 잡으면 ✅ 알림과 함께 그 항목의 🎉 자리 알림까지 이어진다
  - 대기 중에 열림 전환이 잡히면 바로 깨어난다
  - 자동예약 직전 확인은 워커를 거쳐서라도 기다려서 본다 (닫혀 있으면 시도 안 함)
  - 자리가 사라진 회차의 확인은 기다리지 않고 워커에 맡긴다
  - 자리 없이 예약창만 열리면 '지금은 자리 없음'으로 알린다
  - 관심이 끊긴 항목은 워커가 그만 본다

사용법: python check_booking_gateworker_test.py
"""

import sys
import threading
import time
from datetime import date, timedelta

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


URL = "https://booking.naver.com/booking/13/bizes/111/items/222"
D = (date.today() + timedelta(days=5)).isoformat()

calls: list = []      # (스레드 이름, url)
closed_now = False
sent: list = []


def fake_check(url):
    calls.append(threading.current_thread().name)
    time.sleep(0.05)
    return (True, "URL 리다이렉트: /error/") if closed_now else (False, "")


def unit(hhmm, *, stock=10, booked=0):
    return {"unitStartTime": f"{D} {hhmm}:00", "unitStock": stock,
            "unitBookingCount": booked, "isUnitSaleDay": True}


def setup(hourly):
    total_stock = sum(s["unitStock"] for s in hourly)
    total_booked = sum(s["unitBookingCount"] for s in hourly)
    day = {"dateKey": D, "stock": total_stock, "bookingCount": total_booked,
           "hasBookableSlots": total_stock > total_booked, "isSaleDay": True}
    cb.check_availability = lambda b, i, s, t: {
        "days": [day], "sale_start_date": None, "sale_end_date": None, "_all_summary": [day],
    }
    cb.fetch_slots = lambda b, i, s, dk: {
        "times": [x["unitStartTime"][11:16] for x in hourly
                  if x.get("unitStock", 0) - x.get("unitBookingCount", 0) > 0],
        "total": len(hourly), "queried": True, "all_slots": list(hourly),
        "api_slot_count": len(hourly),
    }


def run_round(hourly, alerted, item):
    """check_all 한 회차. (로그, 보낸 알림, 메인 스레드 브라우저 호출 수, 걸린 초)."""
    setup(hourly)
    logs: list = []
    before_sent = len(sent)
    import builtins
    real_print = builtins.print
    builtins.print = lambda *a, **kw: logs.append(" ".join(str(x) for x in a))
    main_before = calls.count("MainThread")
    t0 = time.monotonic()
    try:
        cb.check_all([item], "topic", alerted)
    finally:
        builtins.print = real_print
    return logs, sent[before_sent:], calls.count("MainThread") - main_before, time.monotonic() - t0


def poke():
    """테스트가 워커 상태를 직접 바꾼 뒤 워커를 깨운다 (실제로는 워커만 바꾼다)."""
    with cb._gate_worker._cv:
        cb._gate_worker._cv.notify()


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def main() -> int:
    global closed_now
    cb.LOG_DEDUP = False
    cb.URL_RECHECK_SEC = 300
    cb.GATE_CLOSED_RECHECK_SEC = 0.2
    cb._playwright_check = fake_check
    cb._browser_close = lambda: None
    cb.fetch_calendar_day_status = lambda s, b, dk: None
    cb.probe_schedule_period = lambda p: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda topic, title, body, u: sent.append(title)
    cb.prune_dead_dates = lambda pruned: None
    cb.reset_log_state()
    cb.start_gate_worker()
    item = {"id": "w1", "name": "워커", "url": URL, "enabled": True, "target_dates": [D]}
    try:
        print("1) 처음 보는 항목은 기다려서 확인하고 그 회차에 알린다 (브라우저는 워커 스레드)")
        alerted: dict = {}
        closed_now = False
        logs, got, main_calls, _ = run_round([unit("11:00", stock=4, booked=1)], alerted, item)
        check(main_calls == 0, f"메인 스레드에서 브라우저를 켜지 않음 (실제 {main_calls}회)")
        check(calls and set(calls) == {"gate-worker"}, f"워커 스레드가 확인 (실제: {set(calls)})")
        check(any("예약 가능" in t for t in got), f"그 회차에 🎉 알림 (실제: {got})")

        print("2) 상태를 아는 항목은 회차에서 기다리지 않는다")
        n_before = len(calls)
        logs, got, main_calls, took = run_round([unit("11:00", stock=4, booked=1)], alerted, item)
        check(len(calls) == n_before, f"열림이고 주기 전이면 확인 없음 (실제 {len(calls) - n_before}회)")

        print("2-1) 오래된 '열림'은 믿지 않고 기다려서 다시 본다 (2026-10-01 리베르)")
        # 16:07에 열림으로 본 뒤 자리가 없어 워커가 손을 뗐고, 18:19에 자리가 나자 묵은
        # 열림을 믿고 🎉를 보냈다. 실제로는 그사이 닫혀 7초 뒤 🔒가 이어졌다.
        stale = {"id": "w3", "name": "묵은열림", "url": URL, "enabled": True, "target_dates": [D]}
        stale_alerted: dict = {}
        cb._url_checked_at["w3"] = time.monotonic() - cb.URL_RECHECK_SEC - 1
        closed_now = True
        n_before = len(calls)
        logs, got, main_calls, _ = run_round([unit("11:00", stock=4, booked=1)], stale_alerted, stale)
        check(len(calls) - n_before >= 1, f"그 회차에 다시 확인 (실제 {len(calls) - n_before}회)")
        check(not any("예약 가능" in t for t in got), f"닫힌 예약창에 🎉 없음 (실제: {got})")
        check(any("🔒" in t for t in got), f"바로 🔒 알림 (실제: {got})")

        print("3) 자리 있고 닫힌 항목은 회차와 무관하게 워커가 계속 본다")
        closed_now = True
        alerted["w1:url_closed"] = 1          # 닫힘으로 알던 상태
        cb._gate_worker._watches["w1"]["closed"] = True
        poke()
        logs, got, main_calls, took = run_round([unit("11:00", stock=4, booked=1)], alerted, item)
        check(main_calls == 0, "회차에서 브라우저를 기다리지 않음")
        n_before = len(calls)
        time.sleep(0.8)
        check(len(calls) - n_before >= 2,
              f"회차 없이도 워커가 반복 확인 (0.8초에 {len(calls) - n_before}회)")
        cb.drain_gate_results(alerted)
        check(alerted.get("w1:url_closed") == 1, "닫힘 유지")
        check(not any("예약 가능" in t for t in got), f"닫힌 동안 🎉 없음 (실제: {got})")

        print("4) 워커가 열림 전환을 잡으면 ✅ 알림과 🎉 자리 알림이 이어진다")
        closed_now = False
        ok = wait_for(lambda: cb._gate_worker.watch_info("w1")["closed"] is False)
        check(ok, "워커가 열림을 잡음")
        logs, got, main_calls, _ = run_round([unit("11:00", stock=4, booked=1)], alerted, item)
        check(any("예약창 열림" in t for t in got), f"✅ 예약창 열림 알림 (실제: {got})")
        check(any("예약 가능" in t for t in got), f"같은 회차에 🎉 자리 알림 (실제: {got})")
        check("w1:url_closed" not in alerted, "닫힘 기록 정리")

        print("5) 대기 중에 열림 전환이 잡히면 바로 깨어난다")
        closed_now = True
        alerted["w1:url_closed"] = 1
        cb._gate_worker._watches["w1"]["closed"] = True
        poke()
        wait_for(lambda: False, 0.3)
        cb.drain_gate_results(alerted)
        closed_now = False
        nw = cb.NewItemWatcher()
        nw.known = {"w1"}
        nw._next_poll = time.monotonic() + 999
        t0 = time.monotonic()
        woke = nw.sleep(10, lambda: bool(_opened := cb.drain_gate_results(alerted)) and
                        (nw.known.difference_update(_opened) or True))
        took = time.monotonic() - t0
        check(woke and took < 3, f"열림 전환으로 깨어남 ({took:.1f}초)")
        check("w1" not in nw.known, "다음 회차 머리로 당겨지도록 표시")

        print("6) 자동예약 직전 확인은 기다려서 본다 — 닫혀 있으면 시도하지 않는다")
        dispatched: list = []
        cb.dispatch_auto_book = lambda *a, **kw: (dispatched.append(a) or (True, ""))
        ab_item = {"id": "w2", "name": "자동예약", "url": URL, "enabled": True,
                   "target_dates": [D], "auto_book": {"enabled": True}}
        alerted2: dict = {}
        closed_now = True
        cb._url_checked_at["w2"] = time.monotonic()     # 직전에 '열림'으로 본 것처럼
        run_round([unit("11:00", stock=4, booked=1)], alerted2, ab_item)
        check(dispatched == [], f"닫힘을 직전 확인으로 잡아 시도 안 함 (실제 {len(dispatched)}건)")
        closed_now = False
        cb._gate_worker._watches.pop("w2", None)
        alerted2.pop("w2:url_closed", None)
        cb._url_checked_at["w2"] = time.monotonic()
        run_round([unit("11:00", stock=4, booked=1)], alerted2, ab_item)
        check(len(dispatched) == 1, f"열려 있으면 자동예약 (실제 {len(dispatched)}건)")

        print("7) 자리가 사라진 회차의 확인은 워커에 맡기고 기다리지 않는다")
        logs, got, main_calls, took = run_round([unit("11:00", stock=4, booked=4)], alerted, item)
        check(main_calls == 0, "메인 스레드 확인 없음")

        print("7-1) 자리 없이 예약창만 열리면 문구가 다르다")
        closed_now = True
        alerted["w1:url_closed"] = 1
        cb._gate_worker.watch(item, "w1", URL, "topic", True)
        cb._gate_worker._watches["w1"]["closed"] = True
        poke()
        wait_for(lambda: False, 0.3)
        cb.drain_gate_results(alerted)
        before = len(sent)
        closed_now = False
        wait_for(lambda: cb._gate_worker.watch_info("w1")["closed"] is False)
        cb.drain_gate_results(alerted)
        got = sent[before:]
        check(any("예약창 열림" in t and "지금은 자리 없음" in t for t in got),
              f"자리 없음 문구 (실제: {got})")

        print("7-2) 자리가 남아 있으면 기존 문구 그대로")
        closed_now = True
        alerted["w1:url_closed"] = 1
        alerted[f"w1:{D}:stock"] = {"11:00": [4, 1]}
        cb._gate_worker._watches["w1"]["closed"] = True
        poke()
        wait_for(lambda: False, 0.3)
        cb.drain_gate_results(alerted)
        before = len(sent)
        closed_now = False
        wait_for(lambda: cb._gate_worker.watch_info("w1")["closed"] is False)
        cb.drain_gate_results(alerted)
        got = sent[before:]
        check(bool(got) and all("자리 없음" not in t for t in got)
              and any("예약창 열림" in t for t in got), f"기존 문구 (실제: {got})")

        print("8) 관심이 끊긴 항목은 워커가 그만 본다")
        cb.GATE_WATCH_TTL_SEC = 0.3
        time.sleep(0.6)
        poke()
        check(wait_for(lambda: cb._gate_worker.watch_info("w1") is None), "관심 목록에서 빠짐")
        n_before = len(calls)
        time.sleep(0.5)
        check(len(calls) - n_before == 0, f"더는 확인하지 않음 (실제 {len(calls) - n_before}회)")
    finally:
        cb.stop_gate_worker()

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
