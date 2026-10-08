"""회차 도중 속도 제한이 잡히면 남은 네이버 항목을 건너뛰는지 확인하는 회귀 테스트.

네트워크 없이 돈다 (naver API·playwright·ntfy는 모두 대체 함수로 교체).

배경 — 2026-10-08 13:18 KST, 네이버가 속도 제한을 건 동안 감시 항목 5개가
같은 초에 일제히 "schedule API 요청 실패 — enhanced: HTTP 400 (Invalid request) /
base: 속도 제한 (BookingAPITooManyRequests)"를 찍었다. 막힌 걸 첫 항목에서 이미
알았는데도 나머지 항목마다 요청을 두 번씩 더 보내, 차단만 길어지고 로그가 오류로
덮였다.

확인 내용:
  - 첫 항목에서 속도 제한이 잡히면 같은 회차의 나머지 네이버 항목은 조회하지 않는다
  - 건너뛴 항목은 한 줄로 모아 로그에 남긴다
  - 다음 회차에는 다시 전 항목을 조회한다 (지난 회차의 제한을 끌고 가지 않는다)
  - 속도 제한이 없으면 종전처럼 전 항목을 조회한다
  - RATE_LIMIT_SKIP_ROUND=0이면 끈다 (종전 동작)
  - 카카오 항목은 네이버 제한과 무관하게 그대로 조회한다

사용법: python check_booking_ratelimit_skip_test.py
"""

import builtins
import sys
from datetime import date, timedelta

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


D = (date.today() + timedelta(days=5)).isoformat()
DAY = {"dateKey": D, "stock": 0, "bookingCount": 0,
       "hasBookableSlots": False, "isSaleDay": True}
ITEMS = [{"id": f"t{i}", "name": f"항목{i}",
          "url": f"https://booking.naver.com/booking/13/bizes/11{i}/items/22{i}",
          "enabled": True, "target_dates": [D]} for i in range(1, 4)]
KAKAO = {"id": "k1", "name": "카카오", "type": "kakao", "enabled": True,
         "url": "https://booking.kakao.com/ticket/999", "target_dates": [D]}


def run_round(monitors, limited_ids):
    """check_all 한 회차. limited_ids에 든 항목 조회는 속도 제한으로 실패시킨다.

    (조회한 항목 id 목록, 카카오 조회 횟수, 로그 줄)을 돌려준다.
    """
    called: list = []
    kakao_calls: list = []
    by_biz = {cb.parse_naver_url(m["url"])["biz_id"]: m["id"]
              for m in monitors if m.get("type") != "kakao"}

    def fake_avail(biz, item, svc, targets):
        iid = by_biz[biz]
        called.append(iid)
        if iid in limited_ids:
            cb.note_rate_limit("BookingAPITooManyRequests")
            return None
        return {"days": [DAY], "sale_start_date": None, "sale_end_date": None,
                "_all_summary": [DAY]}

    cb.check_availability = fake_avail
    cb.fetch_slots = lambda b, i, s, dk: cb.slots_failed()
    cb.check_kakao_dates = lambda t, d, c: kakao_calls.append(t) or []
    cb.fetch_calendar_day_status = lambda s, b, dk: None
    cb._playwright_check = lambda u: (False, "")
    cb.probe_schedule_period = lambda p: None
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.send_ntfy = lambda *a, **kw: None
    cb.prune_dead_dates = lambda pruned: None
    cb.maybe_auto_book = lambda *a, **kw: None

    logs: list = []
    real_print = builtins.print
    builtins.print = lambda *args, **kwargs: logs.append(" ".join(str(a) for a in args))
    try:
        cb.check_all([dict(m) for m in monitors], "topic", {})
    finally:
        builtins.print = real_print
    return called, kakao_calls, logs


def main() -> int:
    cb.LOG_DEDUP = False

    print("\n[1] 첫 항목에서 속도 제한 → 나머지 건너뜀")
    called, _, logs = run_round(ITEMS, {"t1"})
    check(called == ["t1"], f"첫 항목만 조회 (실제: {called})")
    skip_lines = [l for l in logs if "건너뜀" in l]
    check(len(skip_lines) == 1 and "항목2" in skip_lines[0] and "항목3" in skip_lines[0],
          f"건너뛴 항목을 한 줄로 남김 ({skip_lines})")

    print("\n[2] 다음 회차는 다시 전 항목 조회")
    called, _, logs = run_round(ITEMS, set())
    check(called == ["t1", "t2", "t3"], f"전 항목 조회 (실제: {called})")
    check(not any("건너뜀" in l for l in logs), "건너뜀 줄 없음")

    print("\n[3] 중간 항목에서 잡히면 그 뒤부터 건너뜀")
    called, _, _ = run_round(ITEMS, {"t2"})
    check(called == ["t1", "t2"], f"앞 두 항목만 조회 (실제: {called})")

    print("\n[4] RATE_LIMIT_SKIP_ROUND=0이면 종전처럼 전부 조회")
    cb.RATE_LIMIT_SKIP_ROUND = False
    try:
        called, _, _ = run_round(ITEMS, {"t1", "t2", "t3"})
        check(called == ["t1", "t2", "t3"], f"전 항목 조회 (실제: {called})")
    finally:
        cb.RATE_LIMIT_SKIP_ROUND = True

    print("\n[5] 카카오는 네이버 제한과 무관하게 조회")
    called, kakao_calls, _ = run_round(ITEMS[:1] + [KAKAO] + ITEMS[1:], {"t1"})
    check(called == ["t1"], f"네이버는 첫 항목만 (실제: {called})")
    check(len(kakao_calls) == 1, f"카카오는 조회함 (실제: {len(kakao_calls)}회)")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
