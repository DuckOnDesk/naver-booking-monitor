"""네이버 hourlySchedule 슬롯의 원본 필드를 덤프하는 진단 도구.

모니터가 실제 예약 페이지와 다르게 판단할 때 어떤 필드가 어긋났는지 보기 위한 것.
로컬에서 네이버가 막혀 있으면 GitHub Actions 러너에서 돌린다.

    python -u scripts/probe_naver_schedule.py <URL> <YYYY-MM-DD> [실제표시_시작 실제표시_끝]

세 번째·네 번째 인자로 실제 페이지에 뜨는 시간 범위를 주면, 그 안/밖을 가르는
필드를 자동으로 찾아 ★로 표시한다.

이 도구로 확인된 것 (트루스 오브 뷰티 / businessTypeId 13, 2026-08):
  - 네이버는 하루 24시간을 30분 단위 48개로 전부 내려주고, 영업시간 밖 슬롯도
    isUnitSaleDay=true에 재고(unitStock=10)까지 채워서 준다
  - 실제 페이지가 화면에서 빼는 기준은 isUnitBusinessDay (영업시간 밖 = false)
  - 영업시간 정보는 Business/BizItem/BizItemSchedule의 어느 필드에도 없고,
    페이지 상태의 bizHours·openingHoursSetting도 null이다
"""

import json
import re
import sys

import requests

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://m.booking.naver.com/",
}

# 실제 예약 페이지가 보내는 필드 목록 그대로 (네트워크 요청 가로채기로 확인)
PAGE_FIELDS = (
    "id name slotId scheduleId detailScheduleId unitStartDateTime unitStartTime "
    "unitBookingCount unitStock bookingCount occupiedBookingCount stock "
    "isBusinessDay isSaleDay isUnitSaleDay isUnitBusinessDay isHoliday "
    "duration desc minBookingCount maxBookingCount saleStartDateTime saleEndDateTime"
)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, flush=True)
        return 1
    url, datekey = sys.argv[1], sys.argv[2]
    real_from = sys.argv[3] if len(sys.argv) > 3 else None
    real_to = sys.argv[4] if len(sys.argv) > 4 else None

    m = re.search(r"/booking/(\d+)/bizes/(\d+)/items/(\d+)", url)
    if not m:
        print("URL 파싱 실패", flush=True)
        return 1
    svc, biz, item = int(m.group(1)), m.group(2), m.group(3)
    print(f"대상: businessTypeId={svc} businessId={biz} bizItemId={item} date={datekey}", flush=True)

    resp = requests.post(
        "https://m.booking.naver.com/graphql?opName=hourlySchedule",
        headers=HEADERS, timeout=25,
        json={
            "operationName": "hourlySchedule",
            "variables": {"scheduleParams": {
                "businessTypeId": svc, "businessId": biz, "bizItemId": item,
                "startDateTime": f"{datekey}T00:00:00+09:00",
                "endDateTime": f"{datekey}T23:59:59+09:00",
            }},
            "query": ("query hourlySchedule($scheduleParams: ScheduleParams) {"
                      " schedule(input: $scheduleParams) { bizItemSchedule { hourly { "
                      + PAGE_FIELDS + " } } } }"),
        },
    )
    print(f"HTTP {resp.status_code}", flush=True)
    data = resp.json()
    for e in (data.get("errors") or [])[:5]:
        print(f"  [GraphQL 오류] {e.get('message')}", flush=True)
    hourly = (((data.get("data") or {}).get("schedule") or {}).get("bizItemSchedule") or {}).get("hourly") or []
    print(f"슬롯 {len(hourly)}개\n", flush=True)
    if not hourly:
        return 0

    def hhmm(slot):
        return (slot.get("unitStartTime") or "")[11:16]

    if real_from and real_to:
        inside = [s for s in hourly if real_from <= hhmm(s) <= real_to]
        outside = [s for s in hourly if not (real_from <= hhmm(s) <= real_to)]
        print(f"=== 필드별 값 분포 — 실제 표시({real_from}~{real_to}) {len(inside)}개 "
              f"vs 그 밖 {len(outside)}개 ===", flush=True)
        for k in sorted({k for s in hourly for k in s}):
            def vals(rows):
                out = []
                for s in rows:
                    v = s.get(k)
                    if isinstance(v, (dict, list)):
                        v = f"<{type(v).__name__} len={len(v)}>"
                    if v not in out:
                        out.append(v)
                return out
            vin, vout = vals(inside), vals(outside)
            mark = "  ★ 구분됨" if not (set(map(str, vin)) & set(map(str, vout))) else ""
            print(f"  {k}:\n      안쪽  = {str(vin)[:200]}\n      바깥쪽 = {str(vout)[:200]}{mark}", flush=True)
        print(flush=True)

    print("=== 슬롯 원본 ===", flush=True)
    for s in hourly:
        print("  " + json.dumps(s, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
