"""재고 변경 감지·🔒 알림 기록 갱신 회귀 테스트.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — 2026-09-08 하겐다즈. 09:11에 09-08 [12:00]/[15:00]/[17:00], 09-09 [18:00]이
있었는데 10:09에는 09-08 [12:00]만 남았다. 예약창은 그동안 계속 닫혀 있었고,
예약 수는 21에서 1도 움직이지 않았다 — 팔린 게 아니라 업체가 재고를 내린 것이다.
로그를 나중에 훑어야만 알 수 있는 이 구분을 그 순간 알리도록 바꿨고, 같은 건에서
드러난 알림 누락(자리가 줄어도 🔒 기록이 갱신되지 않아, 같은 시간대에 자리가 다시
나도 '증가'로 안 잡힘)도 함께 고쳤다.

확인 내용:
  - 시간대가 사라지면 "시간대 내려감", 재고 숫자만 줄면 "업체 재고 회수"로 알린다
  - 예약이 걸린 시간대가 통째로 내려간 것을 "예약 취소"로 읽지 않는다
  - 예약이 늘면 "예약 발생", 같은 시간대에서 빠지면 "예약 취소"(취소표)로 알린다
  - 같은 상태가 이어지면 다시 알리지 않는다 (한 번만)
  - 예약창이 닫혀 있어도 재고 변경을 잡는다 (purge가 스냅샷을 지우지 않는다)
  - 자리 알림이 나가는 회차에는 📊 알림을 접는다 (로그 줄은 그대로 남는다)
  - 🔒 상태에서 자리가 줄었다가 다시 나면 알림이 나간다 (누락 회귀)
  - STOCK_CHANGE_NTFY=0이면 로그만 남고 알림은 안 나간다

사용법: python check_booking_stock_test.py
"""

import sys
from datetime import date, datetime, timedelta, timezone

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


def run_round(hourly, alerted, *, closed=False):
    """check_all 한 회차를 돌리고 (로그 줄 목록, 이번 회차에 보낸 (제목, 본문))을 돌려준다."""
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
    cb._playwright_check = lambda u: (closed, "/error/" if closed else "")
    cb.probe_schedule_period = lambda p: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda topic, title, body, u: sent.append((title, body))
    cb.prune_dead_dates = lambda pruned: None
    cb.maybe_auto_book = lambda *a, **kw: None

    import builtins
    real_print = builtins.print
    builtins.print = lambda *args, **kwargs: logs.append(" ".join(str(a) for a in args))
    try:
        cb.check_all(
            [{"id": "t1", "name": "테스트", "url": URL, "enabled": True, "target_dates": [D]}],
            "topic", alerted,
        )
    finally:
        builtins.print = real_print
    return logs, sent


def titles(sent):
    return [t for t, _ in sent]


def stock_alerts(sent):
    return [(t, b) for t, b in sent if "재고 변경" in t]


def main() -> int:
    cb.LOG_DEDUP = True
    cb.LOG_HEARTBEAT_MIN = 10
    cb.STOCK_CHANGE_NTFY = True

    print("1) 첫 관측에는 재고 변경 알림이 없다 (비교 대상이 없다)")
    cb.reset_log_state()
    alerted: dict = {}
    _, sent = run_round([unit("12:00"), unit("15:00"), unit("17:00")], alerted)
    check(stock_alerts(sent) == [], f"첫 회차 재고 알림 없음 (실제: {titles(sent)})")
    check(alerted.get(f"t1:{D}:stock") == {"12:00": [1, 0], "15:00": [1, 0], "17:00": [1, 0]},
          f"스냅샷 저장 (실제: {alerted.get(f't1:{D}:stock')})")

    print("2) 예약은 그대로인데 시간대가 사라지면 '시간대 내려감'으로 알린다")
    logs, sent = run_round([unit("12:00")], alerted)
    sa = stock_alerts(sent)
    check(len(sa) == 1 and "시간대 내려감" in sa[0][0], f"내려감 알림 1건 (실제: {titles(sent)})")
    check("재고 3→1" in sa[0][1] and "예약 0→0" in sa[0][1], f"총합 표기 (실제: {sa and sa[0][1]})")
    check("15:00 내려감" in sa[0][1] and "17:00 내려감" in sa[0][1],
          f"내려간 시간대 표기 (실제: {sa and sa[0][1]})")
    check(any("📊" in l for l in logs), "로그에도 📊 줄이 남는다")

    print("3) 같은 상태가 이어지면 다시 알리지 않는다 (한 번만)")
    _, sent = run_round([unit("12:00")], alerted)
    check(stock_alerts(sent) == [], f"재알림 없음 (실제: {titles(sent)})")

    print("4) 예약이 늘면 '예약 발생'으로 알린다")
    _, sent = run_round([unit("12:00", stock=1, booked=1)], alerted)
    sa = stock_alerts(sent)
    check(len(sa) == 1 and "예약 발생" in sa[0][0], f"예약 발생 알림 (실제: {titles(sent)})")
    check("예약 0→1" in sa[0][1], f"예약 증가 표기 (실제: {sa and sa[0][1]})")

    print("5) 라벨 판정 — note_stock_change 직접 호출")
    #    자리가 새로 나는 순간에는 자리 알림이 같이 나가 📊가 접히므로(7번 참고),
    #    라벨만 보는 확인은 run_round를 거치지 않는다.
    def label_of(prev, cur_slots, *, datekey=D):
        al = {f"t1:{datekey}:stock": prev}
        import builtins
        real_print = builtins.print
        builtins.print = lambda *a, **kw: None
        try:
            payload = cb.note_stock_change(al, "t1", datekey, "테스트", "날짜", URL,
                                           cur_slots, "00:00:00", False)
        finally:
            builtins.print = real_print
        return payload and payload["title"]

    def raw(hhmm, stock, booked):
        return {"unitStartTime": f"{D} {hhmm}:00", "unitStock": stock,
                "unitBookingCount": booked}

    check("시간대 추가" in label_of({"12:00": [1, 0]}, [raw("12:00", 1, 0), raw("15:00", 1, 0)]),
          "새 시간대 → 시간대 추가")
    check("업체 재고 회수" in label_of({"12:00": [2, 0]}, [raw("12:00", 1, 0)]),
          "시간대는 그대로, 재고 숫자만 감소 → 업체 재고 회수")
    check("시간대 내려감" in label_of({"12:00": [8, 7], "15:00": [8, 7]}, [raw("12:00", 8, 7)]),
          "예약이 걸린 시간대가 통째로 내려가도 '예약 취소'로 읽지 않는다")
    check("예약 취소" in label_of({"12:00": [2, 2]}, [raw("12:00", 2, 1)]),
          "같은 시간대에서 예약이 빠짐 → 예약 취소 (취소표)")
    check("예약 발생" in label_of({"12:00": [2, 1]}, [raw("12:00", 2, 2)]),
          "같은 시간대에서 예약이 늘어남 → 예약 발생")
    check(label_of({"12:00": [1, 0]}, [raw("12:00", 1, 0)]) is None,
          "변화가 없으면 payload 없음")

    print("6) 예약창이 닫혀 있어도 재고 변경을 잡는다")
    cb.reset_log_state()
    alerted = {}
    run_round([unit("12:00"), unit("15:00")], alerted, closed=True)
    check(alerted.get("t1:url_closed") == 1, "닫힘으로 확정")
    check(f"t1:{D}:stock" in alerted, "닫힘 purge가 스냅샷을 지우지 않는다")
    _, sent = run_round([unit("12:00")], alerted, closed=True)
    sa = stock_alerts(sent)
    check(len(sa) == 1 and "시간대 내려감" in sa[0][0],
          f"닫힘 상태에서도 재고 변경 알림 (실제: {titles(sent)})")

    print("7) 🔒 상태에서 자리가 줄었다가 다시 나면 알림이 나간다 (누락 회귀)")
    cb.reset_log_state()
    alerted = {}
    _, sent = run_round([unit("12:00"), unit("15:00")], alerted, closed=True)
    check(any("자리 있음" in t for t in titles(sent)), f"최초 🔒 알림 (실제: {titles(sent)})")
    check(alerted.get(f"t1:{D}:closed") == {"12:00": 1, "15:00": 1},
          f"기록 저장 (실제: {alerted.get(f't1:{D}:closed')})")

    _, sent = run_round([unit("12:00")], alerted, closed=True)
    check(alerted.get(f"t1:{D}:closed") == {"12:00": 1},
          f"자리가 줄면 기록도 따라 줄어든다 (실제: {alerted.get(f't1:{D}:closed')})")

    _, sent = run_round([unit("12:00"), unit("15:00")], alerted, closed=True)
    check(any("자리 추가됨" in t and "예약창 닫힘" in t for t in titles(sent)),
          f"돌아온 자리에 다시 알림 (실제: {titles(sent)})")

    print("7-1) 자리 알림이 나가는 회차에는 📊 알림을 접는다 (로그는 남는다)")
    cb.reset_log_state()
    alerted = {}
    run_round([unit("12:00")], alerted, closed=True)          # 기준 회차
    logs, sent = run_round([unit("12:00"), unit("15:00")], alerted, closed=True)
    check(any("자리 추가됨" in t for t in titles(sent)), f"자리 알림은 나간다 (실제: {titles(sent)})")
    check(stock_alerts(sent) == [], f"같은 회차 📊 알림은 접힘 (실제: {titles(sent)})")
    check(any("📊" in l for l in logs), "접혀도 로그의 📊 줄은 남는다")

    print("8) STOCK_CHANGE_NTFY=0이면 로그만 남는다")
    cb.STOCK_CHANGE_NTFY = False
    try:
        logs, sent = run_round([unit("12:00")], alerted, closed=True)
        check(stock_alerts(sent) == [], f"알림 없음 (실제: {titles(sent)})")
        check(any("📊" in l for l in logs), f"로그는 남는다 (실제: {[l for l in logs if '📊' in l]})")
    finally:
        cb.STOCK_CHANGE_NTFY = True

    print("9) 오늘 날짜에서 시간이 지나 빠진 슬롯은 변경으로 치지 않는다")
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    today, now_hhmm = now_kst.strftime("%Y-%m-%d"), now_kst.strftime("%H:%M")
    past, future = "00:00", "23:59"
    if not (past < now_hhmm < future):
        print(f"    SKIP — 자정/23:59 경계({now_hhmm} KST)라 판정 시간대를 못 잡는다")
    else:
        alerted = {f"t1:{today}:stock": {past: [1, 0], future: [1, 0]}}

        def slot(hhmm, stock=1, booked=0):
            return {"unitStartTime": f"{today} {hhmm}:00", "unitStock": stock,
                    "unitBookingCount": booked}

        import builtins
        real_print = builtins.print
        builtins.print = lambda *a, **kw: None
        try:
            # 지난 시간대만 빠졌다 → 변경 아님
            passed_only = cb.note_stock_change(alerted, "t1", today, "테스트", "오늘", URL,
                                               [slot(future)], now_hhmm + ":00", True)
            # 아직 안 지난 시간대가 사라졌다 → 변경
            real = cb.note_stock_change(alerted, "t1", today, "테스트", "오늘", URL,
                                        [], now_hhmm + ":00", True)
        finally:
            builtins.print = real_print
        check(passed_only is None, f"지난 슬롯만 빠진 건 변경이 아니다 (실제: {passed_only})")
        check(real is not None and f"{future} 내려감" in real["body"],
              f"남은 슬롯 소멸은 변경이다 (실제: {real})")
        check("재고 1→0" in real["body"],
              f"총합 비교에서도 지난 슬롯을 뺀다 (실제: {real and real['body']})")

    print("10) 지난 날짜 스냅샷은 정리된다")
    alerted = {f"t1:2000-01-01:stock": {"12:00": [1, 0]}, "t1:url_closed": 1}
    cb.prune_stock_records(alerted, date.today().isoformat())
    check("t1:2000-01-01:stock" not in alerted, "지난 날짜 스냅샷 삭제")
    check("t1:url_closed" in alerted, "다른 키는 건드리지 않음")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
