"""자동 예약 드라이런 테스트 러너 (GitHub Actions workflow_dispatch용).

주어진 URL에서 예약 가능한 첫 날짜/시간을 자동으로 찾아 auto_book 흐름을
최종 확정 직전까지 실행한다. 이 러너는 항상 드라이런 — 실제 예약은 하지 않는다.

사용법: python auto_book_test.py <URL> [YYYY-MM-DD]
"""

import os
import sys
from datetime import datetime, timedelta, timezone

os.environ["AUTO_BOOK_DRY_RUN"] = "1"  # 안전장치: 이 러너는 무조건 드라이런

import auto_book
from check_booking import check_availability, fetch_slots, parse_naver_url

KST = timezone(timedelta(hours=9))


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    date_arg = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
    if not url:
        print("사용법: python auto_book_test.py <URL> [YYYY-MM-DD]")
        return 2

    parsed = parse_naver_url(url)
    if not parsed:
        print(f"URL 파싱 실패: {url}")
        return 2

    print(f"=== 드라이런 테스트: bizId={parsed['biz_id']} itemId={parsed['item_id']} ===", flush=True)

    result = check_availability(
        parsed["biz_id"], parsed["item_id"], parsed["service_id"],
        [date_arg] if date_arg else [],
    )
    if result is None:
        print("schedule API 조회 실패")
        return 1

    all_summary = result.get("_all_summary") or []
    print(f"월별 요약: {len(all_summary)}개 항목, isSaleDay={sum(1 for d in all_summary if d.get('isSaleDay'))}개", flush=True)

    if date_arg:
        candidates = [date_arg]
    else:
        days = result.get("days") or []
        candidates = sorted(d["dateKey"] for d in days if d.get("hasBookableSlots"))
        if not candidates:
            candidates = sorted(d["dateKey"] for d in all_summary if d.get("isSaleDay"))
        if not candidates:
            # 일부 서비스 타입은 월별 요약이 비어 있음 → 오늘부터 14일 개별 조회
            today = datetime.now(KST).date()
            candidates = [(today + timedelta(days=i)).isoformat() for i in range(14)]
            print("월별 요약에 후보 없음 → 날짜별 개별 조회로 전환 (14일)", flush=True)

    target_date = None
    times: list = []
    for datekey in candidates:
        si = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], datekey)
        if si["queried"] and si["times"]:
            target_date, times = datekey, si["times"]
            break
        print(f"  {datekey}: 가용 시간대 없음 (조회 {'성공' if si['queried'] else '실패'}, 미래 슬롯 {si['total']}개)", flush=True)

    if not target_date:
        print("예약 가능한 날짜/시간을 찾지 못함 — 테스트할 슬롯이 없습니다")
        return 1

    print(f"테스트 대상 슬롯: {target_date} {times[:5]}", flush=True)

    res = auto_book.try_book(url, target_date, times[:5])

    print("\n=== 결과 ===", flush=True)
    print(f"  성공 여부 : {res['success']}")
    print(f"  메시지    : {res['message']}")
    print(f"  선택 시간 : {res.get('booked_time')}")
    print(f"  스크린샷  : {len(res.get('screenshots') or [])}장 (아티팩트로 업로드됨)")
    return 0 if res["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
