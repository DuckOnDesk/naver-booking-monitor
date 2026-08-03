"""일 단위 상품(시간대 슬롯 없음) 감지 회귀 테스트.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경:
  트루스 오브 뷰티(businessTypeId 13)는 hourlySchedule에 시간대 슬롯이 하나도
  없고 일별 요약에만 재고가 실려 온다. 이때 daily.summary의 hasBookableSlots가
  false로 오기 때문에, 재고가 480 남아 있는데도 매 회차 "예약불가"로 기록되고
  알림이 한 번도 나가지 않았다.

확인 내용:
  - hasBookableSlots=false여도 판매일 + 일별 재고가 남았으면 예약 가능으로 본다
  - 시간대 슬롯이 없으면 하루 전체를 슬롯 하나(종일)로 취급해 알림이 나간다
  - 시간대 슬롯이 있으면 종전대로 슬롯별로 판단한다 (일 단위 보정 안 함)
  - 감시 시간 범위(11:00-13:00)를 걸어 둬도 일 단위 상품은 걸러지지 않는다
  - 재고가 없으면(매진) 예전처럼 알림을 보내지 않는다
  - 캘린더 API가 "마감"이라고 하면 알림을 보내지 않는다 (오탐 방지 장치 유지)

사용법: python check_booking_dayunit_test.py
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
PARSED = {"biz_id": "111", "item_id": "222", "service_id": 13}
D = (date.today() + timedelta(days=5)).isoformat()


def summary(has_bookable, stock, booked, is_sale_day=True):
    return {"dateKey": D, "stock": stock, "bookingCount": booked,
            "hasBookableSlots": has_bookable, "isSaleDay": is_sale_day}


def run_check(day, hourly, target_dates, calendar_status=None):
    """check_all을 한 항목·한 날짜에 대해 돌리고 (로그, 보낸 알림)을 돌려준다."""
    logs: list = []
    sent: list = []

    cb.check_availability = lambda b, i, s, t: {
        "days": [day], "sale_start_date": None, "sale_end_date": None, "_all_summary": [day],
    }
    cb.fetch_slots = lambda b, i, s, dk: {
        "times": [x["unitStartTime"][11:16] for x in hourly
                  if x.get("unitStock", 0) - x.get("unitBookingCount", 0) > 0],
        "total": len(hourly), "queried": True, "all_slots": list(hourly),
    }
    cb.fetch_calendar_day_status = lambda s, b, dk: calendar_status
    cb._playwright_check = lambda u: (False, "")
    cb.probe_schedule_period = lambda p: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda topic, title, body, u: sent.append((title, body))
    cb.prune_dead_dates = lambda pruned: None   # monitors.json을 건드리지 않도록

    import builtins
    real_print = builtins.print

    def fake_print(*args, **kwargs):
        logs.append(" ".join(str(a) for a in args))

    builtins.print = fake_print
    try:
        cb.check_all(
            [{"id": "t1", "name": "테스트", "url": URL, "enabled": True, "target_dates": target_dates}],
            "topic", {},
        )
    finally:
        builtins.print = real_print
    return "\n".join(logs), sent


def main() -> int:
    print("1) day_has_stock — 일별 요약 기준 잔여 판정")
    check(cb.day_has_stock(summary(False, 480, 0)), "판매일 + 재고 480/예약 0 → 자리 있음")
    check(not cb.day_has_stock(summary(False, 480, 480)), "재고 = 예약 → 자리 없음")
    check(not cb.day_has_stock(summary(True, 480, 0, is_sale_day=False)), "판매일이 아니면 제외")
    check(not cb.day_has_stock(None), "요약이 없으면 제외")

    print("2) 일 단위 상품 (hourly 비어 있음, hasBookableSlots=false)")
    logs, sent = run_check(summary(False, 480, 0), [], [])
    check("예약불가" not in logs, "더 이상 '예약불가'로 흘려보내지 않는다")
    check(f"[{cb.DAY_UNIT_TIME}] 480자리" in logs, f"하루 전체를 [{cb.DAY_UNIT_TIME}] 480자리로 기록")
    check(len(sent) == 1 and "예약 가능" in sent[0][0], f"예약 가능 알림 발송 ({sent})")

    print("3) 일 단위 상품 + 감시 시간 범위 지정")
    logs, sent = run_check(summary(False, 480, 0), [], [f"{D} 11:00-13:00"])
    check(len(sent) == 1, "시간 범위를 걸어 둬도 일 단위 상품은 걸러지지 않는다")
    check("[11:00~13:00]" not in logs, "고를 시간대가 없으므로 시간 범위는 표시하지 않는다")

    print("4) 일 단위 상품이지만 매진")
    logs, sent = run_check(summary(False, 480, 480), [], [])
    check(not sent, "재고가 없으면 알림 없음")
    check("매진" in logs, f"매진으로 기록 ({logs.strip().splitlines()[-1:]})")

    print("5) 캘린더 API가 마감이라고 하면 (오탐 방지)")
    logs, sent = run_check(summary(False, 480, 0), [], [], calendar_status=False)
    check(not sent, "캘린더 교차 확인에서 마감이면 알림 없음")

    print("6) 시간대 슬롯이 있는 상품 (기존 동작 유지)")
    hourly = [
        {"unitStartTime": f"{D} 11:00:00", "unitStock": 10, "unitBookingCount": 8, "isUnitSaleDay": True},
        {"unitStartTime": f"{D} 15:00:00", "unitStock": 10, "unitBookingCount": 10, "isUnitSaleDay": True},
    ]
    logs, sent = run_check(summary(True, 20, 18), hourly, [])
    check(cb.DAY_UNIT_TIME not in logs, "시간대가 있으면 일 단위 보정을 하지 않는다")
    check("[11:00] 2자리" in logs, "슬롯별 잔여 좌석 그대로")

    print("7) 시간대 슬롯 + 감시 시간 범위 (범위 밖 슬롯 제외)")
    logs, sent = run_check(summary(True, 20, 18), hourly, [f"{D} 14:00-16:00"])
    check("[14:00~16:00]" in logs, "시간 범위 표시")
    check(not sent, "범위 안 슬롯이 모두 매진이면 알림 없음")

    print()
    if fails:
        print(f"실패 {len(fails)}건:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("전체 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
