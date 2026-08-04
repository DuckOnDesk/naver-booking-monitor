"""트루스 오브 뷰티(businessTypeId 13)에서 드러난 슬롯 판정 회귀 테스트.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — 두 가지 문제가 연달아 나왔다:
  1) 이 상품의 daily.summary는 hasBookableSlots를 아예 안 내려준다(None). 감시 루프가
     그 값만 보고 예약 가능 경로를 건너뛰어서, 재고 480 / 예약 0인 날짜가 매 회차
     "예약불가"로만 기록되고 알림이 한 번도 나가지 않았다.
  2) 고친 뒤에는 알림이 왔지만 시간대가 엉망이었다. 네이버가 하루 24시간을 30분
     단위 48개로 전부 내려주고 영업시간 밖 슬롯도 isUnitSaleDay=true에 재고까지
     채워서 주기 때문. 실제 페이지는 11:00~18:30 16개만 띄우고, 화면에서 빼는
     기준은 isUnitBusinessDay였다.

확인 내용:
  - 영업시간 밖 슬롯(isUnitBusinessDay=false)은 제외한다
  - 이 필드를 안 내려주는 상품은 종전대로 전부 통과시킨다 (놓치지 않는 쪽 우선)
  - 스키마가 필드를 거부하면 종전 쿼리로 재시도한다
  - hasBookableSlots가 없거나 false여도 판매일 + 일별 재고가 남았으면 예약 가능으로 본다
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
FUTURE = D          # fetch_slots는 지난 시간대를 버리므로 항상 미래 날짜로 본다


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


class FakeResp:
    """requests.post 대체용 최소 응답 객체."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"raise_for_status가 불려선 안 되는 흐름 (HTTP {self.status_code})")


def hourly_payload(slots):
    return {"data": {"schedule": {"bizItemSchedule": {"hourly": slots}}}}


def unit(hhmm, *, stock=10, booked=0, sale=True, biz_day=None):
    s = {"unitStartTime": f"{FUTURE} {hhmm}:00", "unitStock": stock,
         "unitBookingCount": booked, "isUnitSaleDay": sale}
    if biz_day is not None:
        s["isUnitBusinessDay"] = biz_day
    return s


def run_fetch_slots(responder):
    """실제 fetch_slots를 돌리되 requests.post만 갈아끼운다. (요청기록, 결과) 반환."""
    sent = []
    real_post = cb.requests.post

    def fake_post(url, **kw):
        sent.append(kw.get("json") or {})
        return responder(len(sent) - 1)

    cb.requests.post = fake_post
    try:
        result = cb.fetch_slots("111", "222", 13, FUTURE)
    finally:
        cb.requests.post = real_post
    return sent, result


def main() -> int:
    print("0) fetch_slots — 영업시간 밖 슬롯 제외 (isUnitBusinessDay)")
    # 트루스 오브 뷰티 실제 응답 축약: 영업시간 밖은 재고가 있어도 bizDay=false
    slots = [
        unit("00:00", biz_day=False), unit("03:00", biz_day=False),
        unit("11:00", booked=10, biz_day=True), unit("17:30", booked=9, biz_day=True),
        unit("18:00", booked=2, biz_day=True), unit("23:30", biz_day=False),
    ]
    sent, res = run_fetch_slots(lambda i: FakeResp(hourly_payload(slots)))
    check("isUnitBusinessDay" in (sent[0].get("query") or ""), "쿼리에 isUnitBusinessDay 포함")
    got = [s["unitStartTime"][11:16] for s in res["all_slots"]]
    check(got == ["11:00", "17:30", "18:00"], f"영업시간 안쪽만 남김 (실제: {got})")
    check(res["times"] == ["17:30", "18:00"], f"자리 있는 시간대만 (실제: {res['times']})")
    check(res["total"] == 3, f"total은 영업시간 안쪽 기준 (실제: {res['total']})")

    print("0-1) 필드를 안 내려주는 상품은 종전대로 전부 통과")
    plain = [unit("10:00"), unit("11:00", booked=10), unit("12:00")]
    _, res = run_fetch_slots(lambda i: FakeResp(hourly_payload(plain)))
    check([s["unitStartTime"][11:16] for s in res["all_slots"]] == ["10:00", "11:00", "12:00"],
          "isUnitBusinessDay가 없으면 슬롯을 버리지 않는다")
    check(res["times"] == ["10:00", "12:00"], "자리 판정은 종전과 동일")

    print("0-2) 스키마가 필드를 거부하면 종전 쿼리로 재시도")
    def flaky(i):
        if i == 0:
            return FakeResp({"errors": [{"message": 'Cannot query field "isUnitBusinessDay"'}]})
        return FakeResp(hourly_payload(plain))
    sent, res = run_fetch_slots(flaky)
    check(len(sent) == 2, f"두 번 호출 (실제 {len(sent)}회)")
    check("isUnitBusinessDay" not in (sent[1].get("query") or ""), "재시도는 종전 필드만")
    check(res["queried"] and len(res["all_slots"]) == 3, "재시도 결과를 정상 반환")

    def http400(i):
        if i == 0:
            return FakeResp({}, status_code=400)
        return FakeResp(hourly_payload(plain))
    sent, res = run_fetch_slots(http400)
    check(len(sent) == 2 and res["queried"], "HTTP 400도 종전 쿼리로 재시도")

    print("0-3) 두 번째 쿼리까지 실패하면 조회 실패로 처리")
    _, res = run_fetch_slots(lambda i: FakeResp({"errors": [{"message": "boom"}]}))
    check(not res["queried"] and res["all_slots"] == [], "queried=False, 슬롯 없음")

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
