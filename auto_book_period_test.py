"""자동예약 "날짜 미지정 = 등록된 예약 기간 전체" 회귀 테스트.

네트워크·브라우저 없이 돈다 (naver API·GitHub 디스패치·playwright는 모두 대체 함수로 교체).

확인 내용:
  - 등록된 예약 기간을 available_start/end → sale_start/end 순으로 읽는다
  - 날짜 미지정 항목은 예약 기간 밖 날짜를 자동예약하지 않는다
  - 날짜를 지정한 항목은 지정 날짜만 시도한다 (사용자 지정이 우선)
  - 감시 날짜(target_dates)를 좁게 잡아도 예약 기간 전체가 자동예약 대상이 된다
  - 예약 기간을 아직 모르면 종전대로 동작한다

사용법: python auto_book_period_test.py
"""

import sys
from datetime import date, timedelta

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


URL = "https://m.booking.naver.com/booking/12/bizes/111/items/222"
PARSED = {"biz_id": "111", "item_id": "222", "service_id": 12}


def item_with(dates: list) -> dict:
    return {"id": "t1", "name": "테스트팝업", "url": URL, "enabled": True, "target_dates": [],
            "auto_book": {"enabled": True, "dates": dates, "times": [], "accounts": [1], "count": 1}}


def slots_for(datekey: str) -> dict:
    return {"queried": True, "times": ["11:00"], "total": 1,
            "all_slots": [{"unitStartTime": f"{datekey}T11:00:00",
                           "unitStock": 2, "unitBookingCount": 0}]}


def main() -> int:
    D = [(date.today() + timedelta(days=i)).isoformat() for i in range(1, 8)]
    period = (D[0], D[4])

    print("1) booking_period — 등록된 예약 기간 읽기")
    check(cb.booking_period({"available_start": D[0], "available_end": D[4]}) == period,
          "available_start/end 사용")
    check(cb.booking_period({"sale_start_date": f"{D[0]}T00:00:00+09:00",
                             "sale_end_date": D[4]}) == period,
          "available_*가 없으면 판매 기간(sale_*) 사용")
    check(cb.booking_period({}) == (None, None), "정보가 없으면 (None, None)")

    print("2) in_booking_period — 경계 판정")
    check(cb.in_booking_period(D[0], period) and cb.in_booking_period(D[4], period), "시작·종료일 포함")
    check(not cb.in_booking_period(D[5], period), "종료일 다음 날 제외")
    check(cb.in_booking_period(D[5], (None, None)), "기간을 모르면 통과")

    # 이하 테스트는 실제 디스패치 대신 날짜만 기록한다
    dispatched: list = []
    orig_dispatch, orig_fetch = cb.dispatch_auto_book, cb.fetch_slots
    cb.dispatch_auto_book = lambda item_id, datekey, times, sig, attempt, detected: (
        dispatched.append(datekey) or (True, ""))
    cb.fetch_slots = lambda biz, item, svc, datekey: slots_for(datekey)
    try:
        print("3) maybe_auto_book — 날짜 미지정 항목의 기간 밖 날짜")
        open_item = item_with([])
        dispatched.clear()
        cb.maybe_auto_book(open_item, "t1", URL, D[5], [("11:00", 1)], "", {}, period)
        check(not dispatched, "예약 기간 밖 날짜는 시도하지 않음")
        dispatched.clear()
        cb.maybe_auto_book(open_item, "t1", URL, D[2], [("11:00", 1)], "", {}, period)
        check(dispatched == [D[2]], "예약 기간 안 날짜는 시도")
        dispatched.clear()
        cb.maybe_auto_book(open_item, "t1", URL, D[5], [("11:00", 1)], "", {}, (None, None))
        check(dispatched == [D[5]], "기간을 모르면 종전대로 시도")

        print("4) maybe_auto_book — 날짜를 지정한 항목은 지정 날짜가 우선")
        fixed = item_with([D[6]])
        dispatched.clear()
        cb.maybe_auto_book(fixed, "t1", URL, D[6], [("11:00", 1)], "", {}, period)
        check(dispatched == [D[6]], "지정 날짜는 기간 밖이라도 시도")
        dispatched.clear()
        cb.maybe_auto_book(fixed, "t1", URL, D[2], [("11:00", 1)], "", {}, period)
        check(not dispatched, "지정하지 않은 날짜는 시도하지 않음")

        print("5) sweep_auto_book_period — 감시 대상 밖 날짜 보강")
        summary = [{"dateKey": d, "isSaleDay": True, "hasBookableSlots": True,
                    "stock": 2, "bookingCount": 0} for d in D]
        dispatched.clear()
        cb.sweep_auto_book_period(item_with([]), "t1", URL, PARSED, summary, period,
                                  {D[0]}, None, "", {})
        check(dispatched == [D[1], D[2], D[3], D[4]],
              f"이미 확인한 날짜만 빼고 기간 전체를 훑음 (시도: {len(dispatched)}일)")
        dispatched.clear()
        cb.sweep_auto_book_period(item_with([D[1]]), "t1", URL, PARSED, summary, period,
                                  set(), None, "", {})
        check(not dispatched, "날짜를 지정한 항목은 스윕하지 않음")
        dispatched.clear()
        cb.sweep_auto_book_period(item_with([]), "t1", URL, PARSED, summary, (None, None),
                                  set(), None, "", {})
        check(not dispatched, "예약 기간을 모르면 스윕하지 않음")

        print("6) check_all 한 회차 — 감시 날짜 1개 + 자동예약 날짜 미지정")
        saved = (cb._playwright_check, cb.probe_schedule_period, cb.check_availability,
                 cb.save_schedule_cache, cb.commit_schedule_cache, cb.send_ntfy)
        cb._playwright_check = lambda url: (False, "")
        cb.send_ntfy = lambda *a, **kw: None
        cb.probe_schedule_period = lambda parsed: {
            "sale_start_date": None, "sale_end_date": None,
            "available_start": D[0], "available_end": D[4],
            "checked_at": cb.datetime.now(cb.timezone(cb.timedelta(hours=9))).isoformat(),
            "booking_available_code": "RI01", "booking_available_value": 0}
        cb.check_availability = lambda biz, item, svc, targets: {
            "days": [d for d in summary if not targets or d["dateKey"] in targets],
            "sale_start_date": None, "sale_end_date": None, "_all_summary": summary}
        cb.save_schedule_cache = lambda cache: False
        cb.commit_schedule_cache = lambda: None
        try:
            narrow = item_with([])
            narrow["target_dates"] = [D[0]]      # 감시는 하루만
            dispatched.clear()
            cb.check_all([narrow], "", {})
            check(sorted(set(dispatched)) == D[:5],
                  f"감시 날짜는 1개지만 예약 기간({D[0]}~{D[4]}) 전체가 자동예약 대상 "
                  f"(시도: {sorted(set(dispatched))})")
        finally:
            (cb._playwright_check, cb.probe_schedule_period, cb.check_availability,
             cb.save_schedule_cache, cb.commit_schedule_cache, cb.send_ntfy) = saved
    finally:
        cb.dispatch_auto_book, cb.fetch_slots = orig_dispatch, orig_fetch

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
