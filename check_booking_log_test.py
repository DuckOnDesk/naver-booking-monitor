"""상태 변화 기반 로그(log_state) 회귀 테스트.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — 자리가 없는 날짜도 매 회차 로그를 남기다 보니, 항목·날짜가 늘어날수록
한 회차에서 stdout에 쓰는 줄만 수백 줄이 됐다. 실제 변화는 그 안에 묻히고
회차 시간도 그만큼 밀린다. 그래서 정상 상태 로그는 "바뀐 줄"만 남기기로 했다.

확인 내용:
  - 처음 본 날짜는 자리 유무와 관계없이 항상 로그를 남긴다
  - 다음 회차에 상태가 같으면 로그를 생략하고 생략 건수만 요약한다
  - 자리 수가 바뀌면(증가·감소) 즉시 다시 남긴다
  - 매진 ↔ 자리 있음처럼 분기가 바뀌면 즉시 다시 남긴다
  - 변화가 없어도 LOG_HEARTBEAT_MIN이 지나면 한 번 다시 남긴다
  - LOG_DEDUP=0이면 종전처럼 매 회차 전부 남긴다
  - 로그를 생략한 회차에도 알림(ntfy) 판단은 그대로 동작한다

사용법: python check_booking_log_test.py
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


def unit(hhmm, *, stock=10, booked=0):
    return {"unitStartTime": f"{D} {hhmm}:00", "unitStock": stock,
            "unitBookingCount": booked, "isUnitSaleDay": True}


def summary(stock, booked):
    return {"dateKey": D, "stock": stock, "bookingCount": booked,
            "hasBookableSlots": stock > booked, "isSaleDay": True}


def run_round(hourly, alerted, *, stock=None, booked=None):
    """check_all 한 회차를 돌리고 (로그 줄 목록, 이번 회차에 보낸 알림)을 돌려준다."""
    total_stock = sum(s["unitStock"] for s in hourly) if stock is None else stock
    total_booked = sum(s["unitBookingCount"] for s in hourly) if booked is None else booked
    day = summary(total_stock, total_booked)

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
    cb.send_ntfy = lambda topic, title, body, u: sent.append(title)
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


def date_lines(logs):
    """날짜 상태 줄만 추린다 (진단·요약 등 들여쓴 보조 줄 제외)."""
    return [l for l in logs if D[5:] in l and l.startswith("[")]


def main() -> int:
    cb.LOG_DEDUP = True
    cb.LOG_HEARTBEAT_MIN = 10

    print("1) 처음 본 날짜는 항상 남긴다 (자리 있음)")
    cb.reset_log_state()
    alerted: dict = {}
    logs, sent = run_round([unit("11:00", stock=3)], alerted)
    first = date_lines(logs)
    check(len(first) == 1 and "3자리" in first[0], f"최초 관측 1줄 (실제: {first})")
    check(any("예약 가능" in t for t in sent), f"최초 자리 발견 알림 (실제: {sent})")

    print("2) 같은 상태가 이어지면 생략한다")
    logs, sent = run_round([unit("11:00", stock=3)], alerted)
    check(date_lines(logs) == [], f"날짜 줄 없음 (실제: {date_lines(logs)})")
    check(any("생략" in l for l in logs), f"생략 건수 요약 (실제: {logs})")
    check(sent == [], f"상태가 같으면 알림도 없음 (실제: {sent})")

    print("3) 자리 수가 바뀌면 즉시 남긴다")
    logs, sent = run_round([unit("11:00", stock=5)], alerted)
    lines = date_lines(logs)
    check(len(lines) == 1 and "5자리" in lines[0], f"변동 즉시 기록 (실제: {lines})")
    check(any("추가" in t for t in sent), f"자리 추가 알림 (실제: {sent})")

    print("3-1) 자리가 줄어도 변화이므로 남긴다")
    logs, _ = run_round([unit("11:00", stock=4)], alerted)
    lines = date_lines(logs)
    check(len(lines) == 1 and "4자리" in lines[0], f"감소도 기록 (실제: {lines})")

    print("4) 자리 있음 → 매진 전환도 남긴다")
    logs, _ = run_round([unit("11:00", stock=4, booked=4)], alerted)
    lines = date_lines(logs)
    check(len(lines) == 1 and "매진" in lines[0], f"전환 기록 (실제: {lines})")
    logs, _ = run_round([unit("11:00", stock=4, booked=4)], alerted)
    check(date_lines(logs) == [], f"매진이 이어지면 생략 (실제: {date_lines(logs)})")

    print("5) 매진 → 자리 복귀도 남긴다 (취소표)")
    logs, sent = run_round([unit("11:00", stock=4, booked=3)], alerted)
    lines = date_lines(logs)
    check(len(lines) == 1 and "1자리" in lines[0], f"취소표 기록 (실제: {lines})")
    check(sent != [], f"취소표 알림 (실제: {sent})")

    print("6) 변화가 없어도 하트비트 간격마다 한 번은 남긴다")
    cb.LOG_HEARTBEAT_MIN = 0
    try:
        logs, _ = run_round([unit("11:00", stock=4, booked=3)], alerted)
        check(len(date_lines(logs)) == 1, f"하트비트로 재출력 (실제: {date_lines(logs)})")
    finally:
        cb.LOG_HEARTBEAT_MIN = 10
    logs, _ = run_round([unit("11:00", stock=4, booked=3)], alerted)
    check(date_lines(logs) == [], "하트비트 직후에는 다시 생략")

    print("7) LOG_DEDUP=0이면 종전처럼 매 회차 전부 남긴다")
    cb.LOG_DEDUP = False
    try:
        for _ in range(2):
            logs, _ = run_round([unit("11:00", stock=4, booked=3)], alerted)
            check(len(date_lines(logs)) == 1, f"항상 출력 (실제: {date_lines(logs)})")
        check(not any("생략" in l for l in logs), "생략 요약도 없음")
    finally:
        cb.LOG_DEDUP = True

    print("8) 예약창 상태 줄도 회차마다 반복하지 않는다")
    cb.reset_log_state()
    logs, _ = run_round([unit("11:00", stock=3)], {})
    check(any("예약창 열림" in l for l in logs), "첫 회차엔 예약창 상태를 남긴다")
    logs, _ = run_round([unit("11:00", stock=3)], {"t1:" + D: {"11:00": 3}})
    check(not any("예약창 열림" in l for l in logs), "이어지는 회차에선 생략")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
