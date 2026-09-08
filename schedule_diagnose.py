"""예약 가능 시간 제한의 근거 필드를 네이버 API에서 찾는 진단 스크립트.

배경 — 2026-09-08 하겐다즈. API는 09-08 15:00에 자리가 있다고 답해 🎉 알림이 나갔는데
실제 예약 페이지에서는 그 회차를 고를 수 없었다("17시부터 예약 가능"). 모니터가 보는
필드(unitStartTime/unitStock/unitBookingCount/isUnitSaleDay/isUnitBusinessDay)에는
그 제한이 안 담겨 있다는 뜻이다. 페이지가 회차를 막는 근거가 응답 어딘가에 있는지,
있다면 어떤 필드인지 확인한다.

작업 환경(샌드박스)은 m.booking.naver.com 접근이 막혀 있어 여기서는 못 본다.
GitHub Actions 러너에서 돌려 로그로 확인한다. 읽기만 하고 아무 파일도 건드리지 않는다.

    python -u schedule_diagnose.py <예약URL> [날짜 YYYY-MM-DD]

확인 순서:
  1) hourly 타입의 전체 필드 목록 (GraphQL introspection)
  2) 그 필드를 전부 요청한 원본 응답 — 알림이 나간 회차와 못 고른 회차의 값 비교
  3) business 타입에서 예약 제한으로 보이는 필드 전부
  4) 캘린더 REST 응답의 해당 날짜 원본
"""

import json
import sys
from datetime import datetime, timedelta, timezone

import check_booking as cb

KST = timezone(timedelta(hours=9))
GQL = "https://m.booking.naver.com/graphql"

# 이름에 이런 조각이 들어간 필드는 "예약 가능 여부"를 말할 가능성이 있다.
INTEREST = ("book", "sale", "avail", "close", "open", "deadline", "cutoff",
            "limit", "stock", "time", "state", "status", "enable", "disable")


def post(op: str, query: str, variables: dict) -> dict:
    resp = cb.requests.post(
        f"{GQL}?opName={op}",
        json={"operationName": op, "variables": variables, "query": query},
        headers=cb.HEADERS, timeout=20,
    )
    print(f"  HTTP {resp.status_code}")
    try:
        return resp.json()
    except Exception:
        print(f"  (JSON 아님) {resp.text[:300]}")
        return {}


def introspect(type_name: str) -> list[dict]:
    """타입의 필드 목록을 (이름, 종류, 타입) 으로 돌려준다."""
    q = ("query t($n: String!) { __type(name: $n) { name fields { name type "
         "{ kind name ofType { kind name ofType { kind name } } } } } }")
    data = post("t", q, {"n": type_name})
    t = (data.get("data") or {}).get("__type")
    if not t:
        print(f"  introspection 불가 ({type_name}) — errors={data.get('errors')}")
        return []
    return t.get("fields") or []


def type_label(t: dict) -> str:
    while t and not t.get("name"):
        t = t.get("ofType") or {}
    return (t or {}).get("name") or "?"


def scalar_fields(fields: list[dict]) -> list[str]:
    """스칼라/enum 필드 이름만. 객체 필드는 하위 선택이 필요해 제외한다."""
    out = []
    for f in fields:
        t, kind = f["type"], f["type"].get("kind")
        while kind in ("NON_NULL", "LIST"):
            t = t.get("ofType") or {}
            kind = t.get("kind")
        if kind in ("SCALAR", "ENUM"):
            out.append(f["name"])
    return out


def show_fields(title: str, fields: list[dict]) -> None:
    print(f"\n  [{title}] 전체 {len(fields)}개")
    for f in fields:
        mark = "*" if any(w in f["name"].lower() for w in INTEREST) else " "
        print(f"   {mark} {f['name']}: {type_label(f['type'])}")
    print("   (* = 예약 가능 여부와 관련 있어 보이는 이름)")


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print("사용법: python -u schedule_diagnose.py <예약URL> [YYYY-MM-DD]")
        return 2
    url = sys.argv[1].strip()
    datekey = sys.argv[2].strip() if len(sys.argv) > 2 and sys.argv[2].strip() \
        else datetime.now(KST).strftime("%Y-%m-%d")

    parsed = cb.parse_naver_url(url)
    if not parsed:
        print(f"URL 파싱 실패: {url}")
        return 2
    print(f"=== 진단 대상 ===\n  {url}\n  {parsed} / 날짜 {datekey}\n"
          f"  현재 {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} KST")

    print("\n=== 1) hourly 타입 필드 목록 ===")
    hourly_fields = introspect("BizItemScheduleHourly") or introspect("ScheduleHourly")
    if not hourly_fields:
        for name in ("Hourly", "BizItemHourly", "ScheduleUnit", "BizItemScheduleUnit"):
            hourly_fields = introspect(name)
            if hourly_fields:
                print(f"  → 타입명 {name}")
                break
    if hourly_fields:
        show_fields("hourly", hourly_fields)

    print("\n=== 2) 전체 필드를 요청한 원본 응답 ===")
    names = scalar_fields(hourly_fields) if hourly_fields else []
    if not names:
        # introspection이 막혔을 때의 대비 — 있을 법한 이름을 넓게 던져 본다.
        # 하나라도 없는 이름이 섞이면 쿼리 전체가 400이므로, 종전 필드로 물러선다.
        names = ["unitStartTime", "unitEndTime", "unitStock", "unitBookingCount",
                 "isUnitSaleDay", "isUnitBusinessDay"]
        print(f"  introspection 실패 → 알려진 필드로만 조회: {names}")
    q = ("query hourlySchedule($scheduleParams: ScheduleParams) {"
         "  schedule(input: $scheduleParams) { bizItemSchedule { hourly { "
         + " ".join(names) + " __typename } __typename } __typename } }")
    data = post("hourlySchedule", q, {"scheduleParams": {
        "businessId": parsed["biz_id"], "businessTypeId": parsed["service_id"],
        "bizItemId": parsed["item_id"],
        "startDateTime": f"{datekey}T00:00:00+09:00",
        "endDateTime": f"{datekey}T00:00:00+09:00",
    }})
    if data.get("errors"):
        print(f"  errors: {json.dumps(data['errors'], ensure_ascii=False)[:500]}")
    hourly = (((data.get("data") or {}).get("schedule") or {})
              .get("bizItemSchedule") or {}).get("hourly") or []
    print(f"  회차 {len(hourly)}개 — 값이 회차마다 다른 필드가 곧 단서다")
    for h in hourly:
        t = (h.get("unitStartTime") or "?")[11:16]
        rest = {k: v for k, v in h.items()
                if k not in ("unitStartTime", "__typename")}
        print(f"    {t}  {json.dumps(rest, ensure_ascii=False)}")

    print("\n=== 3) business 타입 필드 (예약 제한 설정) ===")
    biz_fields = introspect("Business")
    if biz_fields:
        show_fields("Business", biz_fields)
        names = [f["name"] for f in biz_fields
                 if f["name"] in scalar_fields(biz_fields)
                 and any(w in f["name"].lower() for w in INTEREST)]
        if names:
            print(f"\n  관심 필드만 조회: {names}")
            q = ("query business($businessId: String) { business(input: "
                 "{ businessId: $businessId }) { " + " ".join(names) + " __typename } }")
            d = post("business", q, {"businessId": parsed["biz_id"]})
            print(f"  {json.dumps(d.get('data') or d.get('errors'), ensure_ascii=False)[:1200]}")

    print("\n=== 4) 캘린더 REST 응답 (해당 날짜) ===")
    try:
        r = cb.requests.get(
            f"https://m.booking.naver.com/booking/{parsed['service_id']}"
            f"/bizes/{parsed['biz_id']}/calendars/{datekey[:7]}",
            headers=cb.HEADERS, timeout=20)
        print(f"  HTTP {r.status_code}")
        body = r.json()
        cals = body.get("calendars") or body.get("data") or body
        day = None
        if isinstance(cals, list):
            day = next((c for c in cals if datekey in json.dumps(c, ensure_ascii=False)), None)
        elif isinstance(cals, dict):
            day = cals.get(datekey)
        print(f"  {json.dumps(day, ensure_ascii=False)[:1200] if day else '해당 날짜 없음'}")
    except Exception as exc:
        print(f"  실패: {cb._exc_label(exc)}")

    print("\n=== 끝 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
