"""업체 사전예약 제한(RI02) 처리 회귀 테스트.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — fetch_item_restrictions()가 읽는 bookingAvailableCode/Value는 businessId
단위 기본값이라 상품별 설정을 반영하지 못한다. 귤메달(biz 1631459)은 업체 기본값이
RI02/1(당일 예약 불가)인데 상품은 실제로 당일 예약을 받고 있었고, 이 값으로 오늘
날짜를 통째로 걸러낸 탓에 예약 가능한데도 알림이 한 번도 나가지 않았다.

오판의 대가가 비대칭이다 — 막으면 알림이 아예 없고, 안 막으면 못 잡는 날에 알림이
한 번 갈 뿐이다. 그래서 이 값은 로그 꼬리표로만 쓴다.

확인 내용:
  - RI02 제한에 걸리는 날짜도 알림이 정상적으로 나간다
  - 그 날짜 로그에는 "사전예약 제한" 꼬리표가 그대로 붙는다
  - 제한이 없는 항목(RI01)에는 꼬리표가 붙지 않는다
  - 캘린더 API가 마감이라고 하면 종전대로 알림을 보내지 않는다

사용법: python check_booking_restriction_test.py
"""

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


URL = "https://m.booking.naver.com/booking/6/bizes/111/items/222"
TODAY = date.today().isoformat()


def run_round(ba_code, ba_value, *, calendar_status=None):
    """오늘 날짜에 자리가 있는 항목으로 한 회차 돌린다."""
    day = {"dateKey": TODAY, "stock": 10, "bookingCount": 8,
           "hasBookableSlots": True, "isSaleDay": True}
    hourly = [{"unitStartTime": f"{TODAY} 23:59:00", "unitStock": 10,
               "unitBookingCount": 8, "isUnitSaleDay": True}]

    cache = {"6_111_222": {
        "sale_start_date": None, "sale_end_date": None,
        "available_start": TODAY,
        "available_end": (date.today() + timedelta(days=1)).isoformat(),
        "checked_at": cb.datetime.now(cb.timezone(cb.timedelta(hours=9))).isoformat(),
        "booking_available_code": ba_code, "booking_available_value": ba_value,
    }}
    tmp = Path(tempfile.mkdtemp()) / "schedule_cache.json"
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    cb.SCHEDULE_CACHE_FILE = tmp

    logs: list = []
    sent: list = []
    cb.check_availability = lambda b, i, s, t: {
        "days": [day], "sale_start_date": None, "sale_end_date": None, "_all_summary": [day],
    }
    cb.fetch_slots = lambda b, i, s, dk: {
        "times": ["23:59"], "total": 1, "queried": True,
        "all_slots": list(hourly), "api_slot_count": 1,
    }
    cb.fetch_calendar_day_status = lambda s, b, dk: calendar_status
    cb._playwright_check = lambda u: (False, "")
    cb.probe_schedule_period = lambda p: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda topic, title, body, u: sent.append(title)
    cb.prune_dead_dates = lambda pruned: None
    cb.reset_log_state()

    import builtins
    real_print = builtins.print
    builtins.print = lambda *a, **kw: logs.append(" ".join(str(x) for x in a))
    try:
        cb.check_all([{"id": "t1", "name": "제한테스트", "url": URL,
                       "enabled": True, "target_dates": [TODAY]}], "topic", {})
    finally:
        builtins.print = real_print
    return "\n".join(logs), sent


def main() -> int:
    cb.LOG_DEDUP = False

    print("1) RI02 제한에 걸리는 날짜도 알림이 나간다")
    logs, sent = run_round("RI02", 1)
    check(any("예약 가능" in t for t in sent),
          f"제한 날짜에도 자리 알림 발송 (실제: {sent})")
    check("사전예약 제한 (1일 전 마감)" in logs,
          f"로그에는 제한 꼬리표가 남는다 (실제: {logs})")

    print("2) 제한이 없으면 꼬리표가 붙지 않는다")
    logs, sent = run_round("RI01", 0)
    check(any("예약 가능" in t for t in sent), f"자리 알림 발송 (실제: {sent})")
    check("사전예약 제한" not in logs, f"꼬리표 없음 (실제: {logs})")

    print("3) 캘린더 API가 마감이면 제한 여부와 무관하게 알림을 보내지 않는다")
    logs, sent = run_round("RI02", 1, calendar_status=False)
    check(sent == [], f"알림 없음 (실제: {sent})")
    check("교차확인" in logs, f"교차확인으로 걸렀다는 기록 (실제: {logs})")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
