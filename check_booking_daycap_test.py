"""스포티파이 팝업(businessTypeId 6)에서 드러난 "하루 정원" 회귀 테스트.

네트워크 없이 돈다 (check_booking_nosummary_test의 run_check를 그대로 쓴다).

배경 — 2026-10-08 스포티파이 10/17·10/18은 예약 페이지에서 자리가 하나도 없는데,
감시는 회차마다 250석쯤 남았다고 보고 있었다. 이 상품은 회차 정원 280과 별도로
하루 정원 560이 걸려 있고, 하루 정원이 이미 찼다 (daily.date: 560 / 560).
schedule API의 daily.summary는 빈 목록이라 감시는 회차 합계로 요약을 대신 만들었고,
회차 재고(280/27 등)만 보고 자리 있음으로 판정했다.

요약이 비면 같은 응답의 daily.date로 요약을 만든다 (summary_from_date_map).

확인 내용:
  - daily.date → 요약 변환은 판매일이면서 재고>0인 날만, 숫자 그대로
  - 하루 정원이 찬 날은 회차에 자리가 남아 보여도 알림 없이 매진
  - 하루 정원이 남은 날은 종전대로 회차 기준 알림
  - 재고 0인 판매일(회차로만 재고를 관리하는 날)은 요약에 넣지 않는다

사용법: python check_booking_daycap_test.py
"""

import sys

import check_booking as cb
from check_booking_nosummary_test import D, check, fails, run_check, slots_result, unit


def main() -> int:
    print("1) summary_from_date_map — daily.date로 요약 만들기")
    date_map = {
        "2026-10-16": {"date": "2026-10-16", "isSaleDay": False, "stock": 0, "bookingCount": 0},
        "2026-10-17": {"date": "2026-10-17", "isSaleDay": True, "stock": 560, "bookingCount": 560},
        "2026-10-18": {"date": "2026-10-18", "isSaleDay": True, "stock": 560, "bookingCount": 500},
        "2026-10-19": {"date": "2026-10-19", "isSaleDay": True, "stock": 0, "bookingCount": 0},
    }
    made = cb.summary_from_date_map(date_map)
    check([d["dateKey"] for d in made] == ["2026-10-17", "2026-10-18"],
          f"판매일이면서 재고>0인 날만 (실제: {[d['dateKey'] for d in made]})")
    check(made[0]["stock"] == 560 and made[0]["bookingCount"] == 560
          and not made[0]["hasBookableSlots"], "하루 정원이 찬 날은 hasBookableSlots=False")
    check(made[1]["hasBookableSlots"], "하루 정원이 남은 날은 hasBookableSlots=True")
    check(cb.summary_from_date_map(None) == [], "date가 없으면 빈 목록")

    # 회차 정원은 넉넉히 남은 상태 (스포티파이: 280 / 27)
    slots = [unit(f"{h:02d}:00", stock=280, booked=27) for h in range(12, 18)]

    print("2) 하루 정원이 찼으면 회차에 자리가 남아 보여도 매진 (스포티파이 재현)")
    full = {"dateKey": D, "stock": 560, "bookingCount": 560,
            "hasBookableSlots": False, "isSaleDay": True}
    logs, sent, _ = run_check([full], {D: slots})
    check(not sent, f"알림 없음 (실제: {sent})")
    check("🎉" not in logs, f"자리 있음으로 판정하지 않는다 (실제: {logs})")
    check("하루 정원 마감 (재고:560 / 예약:560)" in logs and "예약불가" not in logs,
          f"하루 숫자로 매진이라고 적는다 (실제: {logs})")

    print("3) 하루 정원이 남았으면 종전대로 회차 기준 알림")
    room = {"dateKey": D, "stock": 560, "bookingCount": 500,
            "hasBookableSlots": True, "isSaleDay": True}
    logs, sent, _ = run_check([room], {D: slots})
    check(any("예약 가능" in t for t, _ in sent), f"🎉 알림 발송 (실제: {sent})")

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
