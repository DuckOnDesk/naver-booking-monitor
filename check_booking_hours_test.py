"""운영 시간(hour_from/hour_to) 탐색 회귀 테스트.

네트워크 없이 돈다 (naver API는 대체 함수로 교체).

웹앱의 감시 시간 선택은 00시부터 24시까지 다 띄워서, 11~20시만 여는 팝업을 고를 때도
쓸데없는 스크롤을 지났다. 모니터가 운영 시간을 캐시에 적어 주면 웹앱이 그 안쪽만
띄운다 (index.html의 makeTimeOptions).

확인 내용:
  - 스캔 중 이미 받아 본 슬롯에서 운영 시간을 줍는다 (추가 조회 없이)
  - 그렇게 못 잡으면 딱 한 번 더 조회한다 — 오늘은 지난 회차가 빠져 나오므로
    내일 이후 날짜를 고른다
  - 날짜마다 운영 시간이 다르면 합집합으로 넓힌다 (좁히면 있는 회차를 못 고른다)
  - 시간대 없는 일 단위 상품은 None으로 남긴다 (웹앱이 종전 목록을 쓴다)

사용법: python check_booking_hours_test.py
"""

import sys
from datetime import date, timedelta

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


TODAY = date.today()
PARSED = {"biz_id": "111", "item_id": "222", "service_id": 12}


def slot(dk, hhmm):
    return {"unitStartTime": f"{dk} {hhmm}:00", "unitStock": 5, "unitBookingCount": 0}


def probe(sale_days, slots_by_date, *, day_unit=False):
    """probe_schedule_period를 돌리고 (결과, 슬롯을 조회한 날짜들)을 돌려준다."""
    queried: list = []

    cb.check_availability = lambda b, i, s, t: {
        "sale_start_date": None, "sale_end_date": None,
        "_all_summary": [{"dateKey": d, "isSaleDay": True} for d in sale_days],
    }

    def fake_slots(b, i, s, dk):
        queried.append(dk)
        times = slots_by_date.get(dk, [])
        if day_unit and dk in slots_by_date:
            # 일 단위 상품: 시각 없는 [종일] 슬롯 하나
            return {"times": [], "total": 1, "queried": True, "api_slot_count": 0,
                    "all_slots": [{"unitStock": 5, "unitBookingCount": 0}]}
        return {"times": times, "total": len(times), "queried": True,
                "api_slot_count": len(times),
                "all_slots": [slot(dk, t) for t in times]}

    cb.fetch_slots = fake_slots
    cb.fetch_item_restrictions = lambda b: {"booking_available_code": "RI01",
                                            "booking_available_value": 0}
    import builtins
    real_print = builtins.print
    builtins.print = lambda *a, **kw: None
    try:
        return cb.probe_schedule_period(PARSED), queried
    finally:
        builtins.print = real_print


def main() -> int:
    d = lambda n: (TODAY + timedelta(days=n)).isoformat()

    print("1) 월별 API로 기간이 잡히면 딱 한 번 더 조회해 운영 시간을 잡는다")
    #    스캔 구간(기간 끝 다음날~+30일)은 빈 응답이라 시간을 못 줍는다.
    res, queried = probe([d(1), d(2), d(3)], {d(1): ["11:00", "14:00", "20:00"]})
    check(res["hour_from"] == "11:00" and res["hour_to"] == "20:00",
          f"운영 시간 (실제: {res['hour_from']}~{res['hour_to']})")
    check(queried.count(d(1)) == 1, f"보충 조회는 한 번뿐 (실제: {queried.count(d(1))}회)")

    print("2) 오늘이 아니라 내일 이후 날짜로 조회한다 (오늘은 지난 회차가 빠진다)")
    res, queried = probe([d(0), d(1)], {d(0): ["18:00"], d(1): ["11:00", "19:00"]})
    check(res["hour_from"] == "11:00" and res["hour_to"] == "19:00",
          f"미래 날짜 기준 (실제: {res['hour_from']}~{res['hour_to']})")
    check(d(0) not in queried, f"오늘은 조회 대상이 아니다 (실제: {queried[:3]})")

    print("3) 스캔 중 본 슬롯이 있으면 그걸로 잡는다 — 날짜마다 다르면 합집합")
    #    월별 API가 비어 있는 팝업: 30일치를 날짜별로 훑으며 슬롯을 이미 받아 본다.
    res, queried = probe([], {d(2): ["13:00", "18:00"], d(3): ["11:00", "16:00"]})
    check(res["hour_from"] == "11:00" and res["hour_to"] == "18:00",
          f"두 날짜의 합집합 (실제: {res['hour_from']}~{res['hour_to']})")
    check(res["available_start"] == d(2) and res["available_end"] == d(3),
          f"운영 기간도 그대로 (실제: {res['available_start']}~{res['available_end']})")

    print("4) 시간대 없는 일 단위 상품은 운영 시간을 남기지 않는다")
    res, _ = probe([d(1)], {d(1): ["종일"]}, day_unit=True)
    check(res["hour_from"] is None and res["hour_to"] is None,
          f"None으로 남는다 (실제: {res['hour_from']}~{res['hour_to']})")

    print("5) 예약 가능한 날짜가 없으면 운영 시간도 없다")
    res, _ = probe([], {})
    check(res["available_start"] is None and res["hour_from"] is None,
          f"둘 다 None (실제: {res['available_start']}, {res['hour_from']})")

    print("6) _merge_hour_range — 합집합으로만 넓어진다")
    m = cb._merge_hour_range
    check(m(None, [slot(d(1), "12:00"), slot(d(1), "15:00")]) == ("12:00", "15:00"), "첫 관측")
    check(m(("12:00", "15:00"), [slot(d(1), "11:00")]) == ("11:00", "15:00"), "앞으로 넓어짐")
    check(m(("12:00", "15:00"), [slot(d(1), "18:30")]) == ("12:00", "18:30"), "뒤로 넓어짐")
    check(m(("12:00", "15:00"), []) == ("12:00", "15:00"), "빈 슬롯은 그대로")
    check(m(("12:00", "15:00"), [{"unitStock": 1}]) == ("12:00", "15:00"), "시각 없는 슬롯은 무시")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
