"""항목별 mute(알림만 끄기) 회귀 테스트.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — 잠깐 조용히 두고 싶은 항목을 enabled=false로 내리면 조회 자체가 멈춰서
그동안의 재고 변화가 로그에도 스냅샷에도 통째로 빈다. mute는 감시를 그대로 두고
알림(ntfy)만 끈다.

확인 내용:
  - mute 항목은 자리 알림(🎉)도 재고 변경 알림(📊)도 나가지 않는다
  - 그래도 로그 줄과 재고 스냅샷(:stock)·자리 기록은 그대로 쌓인다
  - 한 항목의 mute가 같은 회차의 다른 항목 알림까지 끄지 않는다
  - mute를 풀면 다시 알림이 나가고, 조용한 동안 이미 본 자리는 다시 울리지 않는다

사용법: python check_booking_mute_test.py
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


def unit(hhmm, *, stock=1, booked=0):
    return {"unitStartTime": f"{D} {hhmm}:00", "unitStock": stock,
            "unitBookingCount": booked, "isUnitSaleDay": True}


def run_round(hourly, alerted, items):
    """check_all 한 회차를 돌리고 (로그 줄 목록, 보낸 (제목, 본문))을 돌려준다."""
    total_stock = sum(s["unitStock"] for s in hourly)
    total_booked = sum(s["unitBookingCount"] for s in hourly)
    day = {"dateKey": D, "stock": total_stock, "bookingCount": total_booked,
           "hasBookableSlots": total_stock > total_booked, "isSaleDay": True}

    logs: list = []
    sent: list = []

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
    cb._playwright_check = lambda u: (False, "")
    cb.probe_schedule_period = lambda p: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda topic, title, body, u: sent.append((title, body))
    cb.prune_dead_dates = lambda pruned: None
    cb.maybe_auto_book = lambda *a, **kw: None

    monitors = [{"id": i["id"], "name": i["name"], "url": URL, "enabled": True,
                 "target_dates": [D], **({"mute": True} if i.get("mute") else {})}
                for i in items]

    import builtins
    real_print = builtins.print
    builtins.print = lambda *args, **kwargs: logs.append(" ".join(str(a) for a in args))
    try:
        cb.check_all(monitors, "topic", alerted)
    finally:
        builtins.print = real_print
    return logs, sent


def titles(sent):
    return [t for t, _ in sent]


MUTED = [{"id": "t1", "name": "조용이", "mute": True}]
LOUD = [{"id": "t1", "name": "조용이"}]
BOTH = [{"id": "t1", "name": "조용이", "mute": True}, {"id": "t2", "name": "시끄미"}]


def main() -> int:
    cb.LOG_DEDUP = True
    cb.LOG_HEARTBEAT_MIN = 10
    cb.STOCK_CHANGE_NTFY = True

    print("1) mute 항목은 자리 알림이 나가지 않는다 (로그·기록은 남는다)")
    cb.reset_log_state()
    alerted: dict = {}
    logs, sent = run_round([unit("12:00"), unit("15:00"), unit("17:00")], alerted, MUTED)
    check(sent == [], f"알림 없음 (실제: {titles(sent)})")
    check(any("🎉" in l and "조용이" in l for l in logs), "🎉 로그 줄은 그대로 남는다")
    check(alerted.get(f"t1:{D}") == {"12:00": 1, "15:00": 1, "17:00": 1},
          f"자리 기록 저장 (실제: {alerted.get(f't1:{D}')})")
    check(alerted.get(f"t1:{D}:stock") == {"12:00": [1, 0], "15:00": [1, 0], "17:00": [1, 0]},
          f"재고 스냅샷 저장 (실제: {alerted.get(f't1:{D}:stock')})")

    print("2) mute 항목은 재고 변경 알림도 나가지 않는다 (📊 로그는 남는다)")
    logs, sent = run_round([unit("12:00")], alerted, MUTED)
    check(sent == [], f"알림 없음 (실제: {titles(sent)})")
    check(any("📊" in l for l in logs), "📊 로그 줄은 그대로 남는다")

    print("3) mute를 풀면 다시 알림이 나간다 — 조용한 동안 본 자리는 다시 울리지 않는다")
    _, sent = run_round([unit("12:00")], alerted, LOUD)
    check(sent == [], f"이미 기록된 자리는 조용 (실제: {titles(sent)})")
    _, sent = run_round([unit("12:00"), unit("15:00")], alerted, LOUD)
    check(any("자리 추가됨" in t for t in titles(sent)),
          f"새로 난 자리는 알린다 (실제: {titles(sent)})")

    print("4) 한 항목의 mute가 다른 항목 알림까지 끄지 않는다")
    cb.reset_log_state()
    alerted = {}
    _, sent = run_round([unit("12:00")], alerted, BOTH)
    check(len(sent) == 1 and "시끄미" in sent[0][0],
          f"mute 아닌 항목만 알린다 (실제: {titles(sent)})")
    check(alerted.get(f"t1:{D}") == {"12:00": 1}, "mute 항목 기록도 남는다")

    print("5) mute 항목이 뒤에 와도 앞 항목 알림은 그대로다 (주제 되돌림 확인)")
    cb.reset_log_state()
    alerted = {}
    _, sent = run_round([unit("12:00")], alerted,
                        [{"id": "t2", "name": "시끄미"}, {"id": "t1", "name": "조용이", "mute": True}])
    check(len(sent) == 1 and "시끄미" in sent[0][0],
          f"앞 항목만 알린다 (실제: {titles(sent)})")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
