"""예약 마감(RI02 일 단위 / RI03 시간 단위) 회귀 테스트.

네트워크 없이 돈다.

배경 — 2026-09-08 하겐다즈. 13:34에 "09-08 15:00 1자리" 알림이 나갔는데 예약 페이지의
시간 선택창에는 17:00부터만 버튼이 있었다(14~16시는 버튼 자체가 없었다). 진단 결과
업체 설정이 RI03/3(시작 3시간 전 마감)이었고, 코드는 RI02(일 단위)만 처리하고 있었다.
슬롯(hourly) 응답에는 이 신호가 없다 — 후보 필드를 하나씩 던져 본 결과
isUnitSaleDay/isUnitBusinessDay 말고는 없었고, 막힌 회차도 둘 다 true로 왔다.

같은 건에서 두 번째 문제도 드러났다. 예약 제한 조회가 실패하면 RI01/0(제한 없음)으로
채워져, 캐시가 RI03/3 ↔ RI01/0을 오갔다(08:08 RI01 / 11:13 RI03 / 12:27 RI01).
조회 실패는 "제한 없음"이 아니라 "모름"이다.

확인 내용:
  - RI03이면 지금부터 N시간 안에 시작하는 회차를 알림에서 뺀다
  - 남은 회차가 전부 마감이면 알리지 않고 기록도 비운다
  - 시각을 못 읽는 회차([종일] 등)는 근거 없이 막지 않는다
  - RI01/RI02/값 0은 시간 단위로 막지 않는다
  - 예약 제한 조회 실패는 직전 값을 유지한다 (RI01로 덮어쓰지 않는다)

사용법: python check_booking_restriction_test.py
"""

import sys
from datetime import datetime, timedelta, timezone

import check_booking as cb

KST = timezone(timedelta(hours=9))
fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


def main() -> int:
    now = datetime(2026, 9, 8, 13, 34, tzinfo=KST)

    print("1) 코드별 마감 경계 계산")
    d, t = cb.booking_cutoffs("RI01", 0, now)
    check(d is None and t is None, "RI01 = 제한 없음")
    d, t = cb.booking_cutoffs("RI02", 1, now)
    check(d == now.date() + timedelta(days=1) and t is None, "RI02는 날짜 경계만")
    d, t = cb.booking_cutoffs("RI03", 3, now)
    check(d is None and t == now + timedelta(hours=3), "RI03는 시각 경계만 (13:34 → 16:34)")
    check(cb.booking_cutoffs("RI03", 0, now) == (None, None), "값 0이면 막지 않는다")
    check(cb.booking_cutoffs("RI02", 0, now) == (None, None), "RI02도 값 0이면 막지 않는다")

    print("2) 하겐다즈 실제 상황 — 13:34 / RI03 3시간")
    _, cutoff = cb.booking_cutoffs("RI03", 3, now)
    per_slot = [("12:00", 1), ("13:00", 1), ("15:00", 1), ("17:00", 2), ("18:00", 1)]
    keep, blocked = cb.split_by_cutoff(per_slot, "2026-09-08", cutoff)
    check([t for t, _ in keep] == ["17:00", "18:00"],
          f"17:00부터만 남는다 — 사용자가 화면에서 본 것과 같다 (실제: {keep})")
    check([t for t, _ in blocked] == ["12:00", "13:00", "15:00"],
          f"15:00은 마감에 걸린다 — 이게 잘못 나간 그 알림이다 (실제: {blocked})")

    print("3) 경계값 — 정확히 마감 시각에 시작하는 회차는 살린다")
    keep, blocked = cb.split_by_cutoff([("16:34", 1)], "2026-09-08", cutoff)
    check(len(keep) == 1 and not blocked, "16:34 == 경계 → 예약 가능")
    keep, blocked = cb.split_by_cutoff([("16:33", 1)], "2026-09-08", cutoff)
    check(not keep and len(blocked) == 1, "16:33 < 경계 → 마감")

    print("4) 다음 날은 경계를 넘으므로 전부 남는다")
    keep, blocked = cb.split_by_cutoff([("12:00", 1), ("18:00", 1)], "2026-09-09", cutoff)
    check(len(keep) == 2 and not blocked, "내일 회차는 안 막힌다")

    print("5) 시각을 못 읽는 회차는 근거 없이 막지 않는다")
    keep, blocked = cb.split_by_cutoff([(cb.DAY_UNIT_TIME, 5)], "2026-09-08", cutoff)
    check(len(keep) == 1 and not blocked, f"[{cb.DAY_UNIT_TIME}] 일 단위 슬롯은 통과")
    keep, _ = cb.split_by_cutoff([("이상한값", 1)], "2026-09-08", cutoff)
    check(len(keep) == 1, "파싱 불가 시각도 통과 (진짜 자리를 놓치지 않는다)")

    print("6) cutoff_dt가 없으면 아무것도 안 막는다")
    keep, blocked = cb.split_by_cutoff(per_slot, "2026-09-08", None)
    check(keep == per_slot and not blocked, "RI01/RI02 항목은 그대로")

    print("7) 마감 안내 문구의 단위")
    check(cb.restriction_unit("RI02") == "일", "RI02 → 일")
    check(cb.restriction_unit("RI03") == "시간", "RI03 → 시간")
    check(cb.restriction_unit("RI01") == "", "RI01 → 단위 없음")

    print("8) 예약 제한 조회 실패는 직전 값을 유지한다")
    old = {"available_start": "2026-09-08", "booking_available_code": "RI03",
           "booking_available_value": 3}
    failed = {"available_start": "2026-09-08", "booking_available_code": "RI01",
              "booking_available_value": 0, "restriction_ok": False}
    merged = cb._merge_probed_period(old, failed)
    check(merged["booking_available_code"] == "RI03" and merged["booking_available_value"] == 3,
          f"실패한 재탐색은 RI03/3을 그대로 들고 간다 (실제: {merged['booking_available_code']}"
          f"/{merged['booking_available_value']})")

    ok_new = {"available_start": "2026-09-08", "booking_available_code": "RI01",
              "booking_available_value": 0, "restriction_ok": True}
    merged = cb._merge_probed_period(old, ok_new)
    check(merged["booking_available_code"] == "RI01",
          f"조회에 성공했으면 새 값을 받아들인다 (실제: {merged['booking_available_code']})")

    print("9) 조회 성공 여부 플래그가 실제로 채워진다")
    real_fetch = cb.fetch_item_restrictions
    real_check = cb.check_availability
    real_slots = cb.fetch_slots
    try:
        cb.check_availability = lambda b, i, s, t: {
            "days": [], "sale_start_date": None, "sale_end_date": None,
            "_all_summary": [{"dateKey": "2026-09-20", "isSaleDay": True}],
        }
        cb.fetch_slots = lambda b, i, s, dk: cb.slots_failed()
        cb.fetch_item_restrictions = lambda biz: {}
        out = cb.probe_schedule_period({"biz_id": "1", "item_id": "2", "service_id": 12})
        check(out is not None and out["restriction_ok"] is False, "조회 실패 → restriction_ok False")

        cb.fetch_item_restrictions = lambda biz: {
            "booking_available_code": "RI03", "booking_available_value": 3}
        out = cb.probe_schedule_period({"biz_id": "1", "item_id": "2", "service_id": 12})
        check(out["restriction_ok"] is True and out["booking_available_code"] == "RI03",
              f"조회 성공 → restriction_ok True (실제: {out['restriction_ok']})")
    finally:
        cb.fetch_item_restrictions = real_fetch
        cb.check_availability = real_check
        cb.fetch_slots = real_slots

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
