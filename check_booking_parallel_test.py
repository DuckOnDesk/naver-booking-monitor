"""날짜별 동시 조회·운영 기간 백그라운드 재확인 회귀 테스트.

네트워크 없이 돈다 (naver API·ntfy·git은 모두 대체 함수로 교체).

배경 — 한 회차의 대부분이 날짜별 슬롯 조회(날짜당 0.7초, 차례로)였고, 월별 API가
비어 30일을 훑는 항목은 그것만 20초였다. 매시간 운영 기간 재확인(30일 스캔·예약
제한 조회)이 겹치는 회차는 100초를 넘겼다 (2026-10-02 로그).

확인 내용:
  - 여러 날짜를 동시에 조회한다 (결과는 날짜별로 그대로)
  - 동시 수 1이면 종전처럼 차례로 조회한다
  - check_all이 날짜마다 정확히 한 번씩만 조회한다 (동시 조회로 요청이 늘지 않는다)
  - 30일 스캔 결과를 판정에 그대로 써서 같은 날짜를 두 번 부르지 않는다
  - 운영 기간 재확인(TTL 만료)은 회차를 붙잡지 않고, 결과는 다음 회차에 반영한다
  - 처음 보는 항목의 운영 기간 확인은 종전처럼 그 자리에서 기다린다
  - 운영 기간을 못 찾은 항목은 회차에서 30일을 훑지 않고 10분마다 재확인한다
  - 월별 요약이 비어도 운영 기간을 알면 그 기간만 훑는다
  - 속도 제한 카운터는 여러 스레드가 동시에 세도 빠지지 않는다

사용법: python check_booking_parallel_test.py
"""

import json
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


URL = "https://booking.naver.com/booking/13/bizes/111/items/222"
PARSED = {"biz_id": "111", "item_id": "222", "service_id": 13}
CACHE_KEY = "13_111_222"
TODAY = date.today()
DATES = [(TODAY + timedelta(days=i)).isoformat() for i in range(1, 9)]   # 8일

fetched: list = []
fetch_lock = threading.Lock()


def slow_fetch(b, i, s, dk):
    with fetch_lock:
        fetched.append(dk)
    time.sleep(0.2)
    return {"times": [], "total": 1, "queried": True, "api_slot_count": 1,
            "all_slots": [{"unitStartTime": f"{dk} 11:00:00", "unitStock": 4,
                           "unitBookingCount": 4, "isUnitSaleDay": True}]}


def days(keys):
    return [{"dateKey": dk, "stock": 4, "bookingCount": 4, "hasBookableSlots": False,
             "isSaleDay": True} for dk in keys]


def run_round(item, summary_keys):
    summary = days(summary_keys)
    cb.check_availability = lambda b, i, s, t: {
        "days": summary, "sale_start_date": None, "sale_end_date": None, "_all_summary": summary,
    }
    import builtins
    logs: list = []
    real_print = builtins.print
    builtins.print = lambda *a, **kw: logs.append(" ".join(str(x) for x in a))
    t0 = time.monotonic()
    try:
        cb.check_all([item], "topic", {})
    finally:
        builtins.print = real_print
    return logs, time.monotonic() - t0


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    cb.SCHEDULE_CACHE_FILE = tmp / "schedule_cache.json"
    cb.commit_schedule_cache = lambda *a, **kw: None
    cb.fetch_slots = slow_fetch
    cb.fetch_calendar_day_status = lambda s, b, dk: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda *a, **kw: None
    cb.prune_dead_dates = lambda pruned: None
    cb.record_history = lambda *a, **kw: None
    cb.fetch_item_restrictions = lambda biz: {}
    cb.LOG_DEDUP = False
    cb.SLOT_FETCH_WORKERS = 4

    print("1) 여러 날짜를 동시에 조회한다")
    fetched.clear()
    t0 = time.monotonic()
    got = cb.fetch_slots_many(PARSED, DATES)
    took = time.monotonic() - t0
    check(list(got) == DATES and all(got[d]["queried"] for d in DATES), "날짜별 결과 그대로")
    check(took < 0.2 * len(DATES) / 2, f"동시 조회로 빨라짐 ({took:.2f}초, 차례로면 {0.2 * len(DATES):.1f}초)")

    print("1-1) 동시 수 1이면 차례로")
    cb.SLOT_FETCH_WORKERS = 1
    t0 = time.monotonic()
    cb.fetch_slots_many(PARSED, DATES[:3])
    check(time.monotonic() - t0 >= 0.55, "차례로 조회")
    cb.SLOT_FETCH_WORKERS = 4

    item = {"id": "p1", "name": "병렬", "url": URL, "enabled": True}
    fresh_entry = {"available_start": DATES[0], "available_end": DATES[-1],
                   "checked_at": datetime.now(timezone(timedelta(hours=9))).isoformat()}

    print("2) check_all이 날짜마다 정확히 한 번 조회하고, 동시에 받는다")
    cb.SCHEDULE_CACHE_FILE.write_text(json.dumps({CACHE_KEY: fresh_entry}), encoding="utf-8")
    cb.probe_schedule_period = lambda p: (_ for _ in ()).throw(AssertionError("probe 안 불려야 함"))
    fetched.clear()
    logs, took = run_round(item, DATES)
    check(sorted(fetched) == sorted(DATES), f"날짜당 한 번 (실제 {len(fetched)}회: {sorted(fetched)})")
    check(took < 0.2 * len(DATES) / 2, f"회차가 빨라짐 ({took:.2f}초)")

    print("3) 30일 스캔 결과를 판정에 그대로 쓴다 (같은 날짜 두 번 X)")
    fetched.clear()
    logs, took = run_round(item, [])          # 월별 요약이 비어 전체 날짜 스캔
    dup = {d for d in fetched if fetched.count(d) > 1}
    check(not dup, f"중복 조회 없음 (중복: {sorted(dup)})")
    check(set(DATES) <= set(fetched), "운영 기간 날짜를 모두 조회")

    print("4) 운영 기간 재확인(TTL 만료)은 회차를 붙잡지 않는다")
    stale_entry = dict(fresh_entry, checked_at=(datetime.now(timezone(timedelta(hours=9)))
                                                - timedelta(hours=5)).isoformat())
    cb.SCHEDULE_CACHE_FILE.write_text(json.dumps({CACHE_KEY: stale_entry}), encoding="utf-8")
    probe_started = threading.Event()
    probe_release = threading.Event()

    def slow_probe(p):
        probe_started.set()
        probe_release.wait(5)
        return dict(fresh_entry, available_end=(TODAY + timedelta(days=20)).isoformat(),
                    checked_at=datetime.now(timezone(timedelta(hours=9))).isoformat())

    cb.probe_schedule_period = slow_probe
    logs, took = run_round(item, DATES)
    check(probe_started.wait(2), "재확인이 백그라운드에서 시작됨")
    check(took < 2, f"회차는 재확인을 기다리지 않음 ({took:.2f}초)")
    check(CACHE_KEY in cb._probe_futures, "진행 중으로 표시")

    print("4-1) 진행 중에는 다시 걸지 않는다")
    calls_before = len(cb._probe_futures)
    run_round(item, DATES)
    check(len(cb._probe_futures) == calls_before, "중복으로 걸지 않음")

    print("4-2) 끝난 결과는 다음 회차에 반영된다")
    probe_release.set()
    cb._probe_futures[CACHE_KEY].result(5)
    logs, took = run_round(item, DATES)
    saved = json.loads(cb.SCHEDULE_CACHE_FILE.read_text(encoding="utf-8"))
    check(saved[CACHE_KEY]["available_end"] == (TODAY + timedelta(days=20)).isoformat(),
          f"새 운영 기간이 캐시에 반영 (실제: {saved[CACHE_KEY].get('available_end')})")
    check(any("운영 기간" in l for l in logs), f"반영 로그 (실제: {[l for l in logs if '운영' in l]})")
    check(CACHE_KEY not in cb._probe_futures, "진행 중 표시 해제")

    print("5) 처음 보는 항목은 그 자리에서 기다려 확인한다")
    cb.SCHEDULE_CACHE_FILE.write_text("{}", encoding="utf-8")
    first_calls: list = []
    cb.probe_schedule_period = lambda p: (first_calls.append(p) or dict(fresh_entry))
    run_round(item, DATES)
    saved = json.loads(cb.SCHEDULE_CACHE_FILE.read_text(encoding="utf-8"))
    check(len(first_calls) == 1 and CACHE_KEY in saved, "그 회차에 확인하고 캐시 저장")

    print("7) 운영 기간을 못 찾은 항목은 회차에서 30일을 훑지 않는다 (2026-10-02 아이쁘)")
    now = datetime.now(timezone(timedelta(hours=9)))
    no_period = {"available_start": None, "available_end": None, "checked_at": now.isoformat()}
    cb.SCHEDULE_CACHE_FILE.write_text(json.dumps({CACHE_KEY: no_period}), encoding="utf-8")
    cb.probe_schedule_period = lambda p: (_ for _ in ()).throw(AssertionError("probe 안 불려야 함"))
    cb._probe_futures.clear()
    fetched.clear()
    logs, took = run_round(item, [])
    check(fetched == [], f"슬롯 조회 없음 (실제 {len(fetched)}회)")
    check(any("운영 기간 없음" in l for l in logs), f"건너뛴 이유를 로그로 (실제: {logs})")

    print("7-1) 그런 항목은 NO_PERIOD_REPROBE_MIN마다 재확인한다")
    old = dict(no_period, checked_at=(now - timedelta(minutes=cb.NO_PERIOD_REPROBE_MIN + 1)).isoformat())
    check(cb._cache_entry_stale(old, now), f"{cb.NO_PERIOD_REPROBE_MIN}분 지나면 재확인 대상")
    check(not cb._cache_entry_stale(no_period, now), "그 전에는 아님")
    known = dict(fresh_entry, checked_at=(now - timedelta(minutes=cb.NO_PERIOD_REPROBE_MIN + 1)).isoformat())
    check(not cb._cache_entry_stale(known, now), "기간을 아는 항목은 종전 TTL 그대로")

    print("7-2) 월별 요약이 비어도 운영 기간을 알면 그 기간만 훑는다 (딥티크)")
    cb.SCHEDULE_CACHE_FILE.write_text(json.dumps({CACHE_KEY: fresh_entry}), encoding="utf-8")
    fetched.clear()
    run_round(item, [])
    check(sorted(set(fetched)) == sorted(DATES), f"운영 기간 {len(DATES)}일만 조회 (실제 {len(set(fetched))}일)")

    print("6) 속도 제한 카운터는 여러 스레드가 세도 빠지지 않는다")
    cb._rate_limit_hits = 0
    ts = [threading.Thread(target=lambda: [cb.note_rate_limit() for _ in range(2000)]) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check(cb._rate_limit_hits == 16000, f"16000건 (실제 {cb._rate_limit_hits})")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
