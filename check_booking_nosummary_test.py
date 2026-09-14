"""클리오 팝업(businessTypeId 6)에서 드러난 "일별 요약 없음" 회귀 테스트.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — 클리오 T2~T9는 예약 페이지에서 멀쩡히 시간대가 잡히는데, 모니터는 매 회차
"❌ 클리오 T2 09-23(수) 예약불가 (재고:14 / 예약:0)"만 남기고 알림을 한 번도 보내지
않았다. 이 상품의 schedule API는 daily.summary에 판매일을 한 줄도 안 내려준다.
감시 루프는 요약(days_map)이 있어야 예약 가능 경로로 들어가는데, 요약이 없으니
d=None → 매진 경로로 떨어졌다. 그 경로의 _sold_out_label은 "재고는 남았는데 여기까지
왔다"를 보고 예약불가라고 적는다 — 숫자(14/0)는 정확했고, 경로만 틀렸다.

hourlySchedule은 같은 상품에서도 정상 응답하므로, 요약이 없는 날은 슬롯 합계로
요약을 대신 만들어 준다 (synth_day_summary).

확인 내용:
  - 요약이 없어도 슬롯에 자리가 있으면 🎉 알림이 나간다
  - 그 날의 재고/예약 숫자는 슬롯 합계 그대로 (일별 참고값이 덧붙지 않는다)
  - 요약이 없고 슬롯도 전부 매진이면 종전대로 알림 없이 "매진"으로 적는다
  - 요약이 없고 슬롯 자체가 없는 날은 종전 경로 그대로 (자리로 오해하지 않는다)
  - 감시 시간 범위는 요약 없는 상품에도 그대로 걸린다
  - 요약이 없다고 해서 같은 날짜를 더 조회하지 않는다 (판정 경로의 요청은 1회)
  - 요약이 있는 상품의 동작은 달라지지 않는다

사용법: python check_booking_nosummary_test.py
"""

import sys
from datetime import date, timedelta

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


# 실제 클리오 URL 대신 가짜 id를 쓴다 — schedule_cache.json에 들어 있는 항목이면
# 캐시된 운영 기간(9/23~10/2) 밖이라며 테스트 날짜가 통째로 걸러진다.
URL = "https://m.booking.naver.com/booking/6/bizes/111/items/222"
D = (date.today() + timedelta(days=5)).isoformat()


def unit(hhmm, *, stock=1, booked=0):
    return {"unitStartTime": f"{D} {hhmm}:00", "unitStock": stock,
            "unitBookingCount": booked, "isUnitSaleDay": True,
            "isUnitBusinessDay": True}


def slots_result(slots):
    return {
        "times": [s["unitStartTime"][11:16] for s in slots
                  if s.get("unitStock", 0) - s.get("unitBookingCount", 0) > 0],
        "total": len(slots), "queried": True, "all_slots": list(slots),
        "api_slot_count": len(slots),
    }


def run_check(days, hourly_by_date, target_dates=()):
    """check_all을 한 항목에 대해 돌리고 (로그, 보낸 알림, 슬롯 조회 날짜)를 돌려준다.

    days: schedule API가 내려주는 일별 요약 목록 (클리오 재현은 빈 목록).
    hourly_by_date: {날짜: 슬롯 목록}. 목록에 없는 날짜는 슬롯이 없는 날로 본다.
    """
    logs: list = []
    sent: list = []
    fetched: list = []

    cb.check_availability = lambda b, i, s, t: {
        "days": list(days), "sale_start_date": None, "sale_end_date": None,
        "_all_summary": list(days),
    }

    def fake_fetch_slots(b, i, s, dk):
        fetched.append(dk)
        return slots_result(hourly_by_date.get(dk, []))

    cb.fetch_slots = fake_fetch_slots
    cb.fetch_calendar_day_status = lambda s, b, dk: None
    cb._playwright_check = lambda u: (False, "")
    cb.probe_schedule_period = lambda p: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda topic, title, body, u: sent.append((title, body))
    cb.prune_dead_dates = lambda pruned: None   # monitors.json을 건드리지 않도록

    import builtins
    real_print = builtins.print

    def fake_print(*args, **kwargs):
        logs.append(" ".join(str(a) for a in args))

    # 로그는 상태가 바뀔 때만 남는다. 시나리오끼리 상태를 물려받지 않도록 매번 초기화.
    cb.reset_log_state()

    builtins.print = fake_print
    try:
        cb.check_all(
            [{"id": "clio", "name": "클리오 T2", "url": URL, "enabled": True,
              "target_dates": list(target_dates)}],
            "topic", {},
        )
    finally:
        builtins.print = real_print
    return "\n".join(logs), sent, fetched


def main() -> int:
    full_day = [unit(f"{h:02d}:{m:02d}") for h in range(12, 19) for m in (0, 30)]
    full_day = full_day[:14]                       # 12:00~18:30 = 14회차 (실제 상품과 동일)
    sold_out = [unit(s["unitStartTime"][11:16], stock=1, booked=1) for s in full_day]

    print("1) synth_day_summary — 슬롯 합계로 요약 만들기")
    made = cb.synth_day_summary(D, slots_result(full_day))
    check(made is not None, "슬롯이 있으면 요약을 만든다")
    check(made["stock"] == 14 and made["bookingCount"] == 0,
          f"합계가 슬롯 기준 (실제: {made['stock']}/{made['bookingCount']})")
    check(made["hasBookableSlots"] and made["isSaleDay"], "자리가 남았으면 예약 가능 상태로")
    made_full = cb.synth_day_summary(D, slots_result(sold_out))
    check(made_full is not None and not made_full["hasBookableSlots"],
          "슬롯이 전부 매진이면 hasBookableSlots=False")
    check(cb.synth_day_summary(D, slots_result([])) is None, "슬롯이 없으면 None")
    check(cb.synth_day_summary(D, cb.slots_failed()) is None, "조회 실패면 None")

    print("2) 일별 요약이 비어도 자리가 있으면 알림이 나간다 (클리오 재현)")
    logs, sent, fetched = run_check([], {D: full_day})
    check("예약불가" not in logs, f"예약불가로 적히지 않는다 (실제: {logs})")
    check("🎉" in logs, f"자리 있음으로 판정 (실제: {logs})")
    check(any("예약 가능" in t for t, _ in sent), f"🎉 알림 발송 (실제: {sent})")
    check("재고:14 / 예약:0" in logs, f"슬롯 합계 그대로 (실제: {logs})")
    check("일별:" not in logs, "일별 참고값은 붙지 않는다 (요약 = 슬롯 합계)")
    # 요약이 비면 감시 루프는 종전부터 "전체 날짜 스캔"으로 날짜를 먼저 찾는다.
    # 그 스캔 1회 + 판정 1회 = 2회가 원래 비용이고, 요약을 대신 만드느라 여기서
    # 한 번 더 부르지는 않는다 (fetch_day_slots에 prefetched로 그대로 넘긴다).
    check(fetched.count(D) == 2, f"판정 경로의 조회는 한 번 (실제: 스캔 포함 {fetched.count(D)}회)")

    print("3) 요약이 비고 슬롯도 전부 매진이면 종전대로 매진")
    logs, sent, _ = run_check([], {D: sold_out})
    check("매진" in logs and "예약불가" not in logs, f"매진으로 적는다 (실제: {logs})")
    check(not sent, f"알림 없음 (실제: {sent})")

    print("4) 요약도 슬롯도 없는 날짜는 자리로 보지 않는다")
    logs, sent, _ = run_check([], {})
    check(not sent, f"알림 없음 (실제: {sent})")
    check("🎉" not in logs, "자리 있음으로 판정하지 않는다")

    print("5) 감시 시간 범위는 요약 없는 상품에도 그대로 걸린다")
    logs, sent, fetched = run_check([], {D: full_day}, target_dates=[f"{D} 12:00-13:00"])
    check("[12:00~13:00]" in logs, f"시간 범위 표시 (실제: {logs})")
    body = sent[0][1] if sent else ""
    check(sent and "12:00(1)" in body and "14:00" not in body,
          f"범위 안 시간대만 알린다 (실제: {body})")
    # 감시 날짜를 지정하면 전체 날짜 스캔이 없다 → 요청은 판정 1회뿐이어야 한다.
    check(fetched.count(D) == 1, f"슬롯 조회는 한 번 (실제: {fetched.count(D)}회)")

    print("6) 요약이 있는 상품은 동작이 달라지지 않는다")
    summary = {"dateKey": D, "stock": 14, "bookingCount": 0,
               "hasBookableSlots": True, "isSaleDay": True}
    logs, sent, fetched = run_check([summary], {D: full_day})
    check(any("예약 가능" in t for t, _ in sent), f"종전대로 알림 (실제: {sent})")
    check(fetched.count(D) == 1, f"슬롯 조회는 한 번 (실제: {fetched.count(D)}회)")

    summary_sold = {"dateKey": D, "stock": 14, "bookingCount": 14,
                    "hasBookableSlots": False, "isSaleDay": True}
    logs, sent, _ = run_check([summary_sold], {D: sold_out})
    check(not sent and "매진" in logs, f"매진 상품은 종전대로 (실제: {logs})")

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
