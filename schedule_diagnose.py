"""예약 가능 시간 제한의 근거를 네이버 응답에서 찾는 진단 스크립트.

배경 — 2026-09-08 하겐다즈. API는 09-08 15:00에 자리가 있다고 답해 🎉 알림이 나갔는데
실제 예약 페이지의 시간 선택창에는 17:00부터만 버튼이 있었다(14~16시는 버튼 자체가
없었다). 페이지가 받아 본 데이터에는 그 회차가 아예 없다는 뜻이다. 모니터가 보는
필드(unitStartTime/unitStock/unitBookingCount/isUnitSaleDay/isUnitBusinessDay)로는
그 차이가 안 보인다.

1차 시도에서 GraphQL introspection이 INTROSPECTION_DISABLED로 막혀 있는 걸 확인했다.
그래서 두 갈래로 접근한다.
  A. 후보 필드를 '한 번에 하나씩' 던져 존재 여부를 가린다. 여러 개를 같이 넣으면
     하나만 틀려도 쿼리 전체가 400이라 아무것도 못 본다 (1차 시도의 실패 원인).
  B. 예약 페이지를 실제로 띄워 네트워크를 캡처한다. 페이지가 우리와 다른 엔드포인트를
     쓰는 경우까지 잡히는 유일한 방법이고, 화면에 뜬 시간 버튼 목록도 같이 남긴다.

작업 환경(샌드박스)은 m.booking.naver.com 접근이 막혀 있어 여기서는 못 본다.
GitHub Actions 러너에서 돌린다. 읽기만 하고 데이터 파일은 건드리지 않는다.

    python -u schedule_diagnose.py <예약URL> [날짜 YYYY-MM-DD]
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone

import check_booking as cb

KST = timezone(timedelta(hours=9))
GQL = "https://m.booking.naver.com/graphql"

# 모니터가 이미 쓰고 있어 존재가 확실한 필드. 프로빙의 기준선이 된다.
KNOWN = ["unitStartTime", "unitStock", "unitBookingCount", "isUnitSaleDay"]

# "이 회차를 지금 예약할 수 있나"를 담고 있을 법한 이름들. 존재 여부만 가린다.
CANDIDATES = [
    "isUnitBusinessDay", "isBookable", "bookable", "isAvailable", "available",
    "isSaleTime", "isUnitSaleTime", "isUnitAvailable", "isUnitBookable",
    "bookingState", "unitState", "state", "status", "unitStatus",
    "isClosed", "closed", "isSoldOut", "soldOut", "isFull",
    "bookingCloseDateTime", "closeDateTime", "unitCloseTime", "deadline",
    "bookingDeadline", "reservationDeadline", "cutoffTime", "minBookingTime",
    "bookingAvailableTime", "startBookingDateTime", "endBookingDateTime",
    "unitEndTime", "unitDesc", "unitName", "price", "isUnitHoliday",
    "isUnitRestDay", "restDay", "holiday", "remainCount", "maxCount",
]


def gql(op: str, query: str, variables: dict, quiet: bool = False):
    """(status, json) 반환."""
    resp = cb.requests.post(
        f"{GQL}?opName={op}",
        json={"operationName": op, "variables": variables, "query": query},
        headers=cb.HEADERS, timeout=20,
    )
    try:
        body = resp.json()
    except Exception:
        body = {"_raw": resp.text[:200]}
    if not quiet:
        print(f"  HTTP {resp.status_code}")
    return resp.status_code, body


def hourly_query(fields: list[str]) -> str:
    return ("query hourlySchedule($scheduleParams: ScheduleParams) {"
            "  schedule(input: $scheduleParams) { bizItemSchedule { hourly { "
            + " ".join(fields) + " __typename } __typename } __typename } }")


def params(parsed: dict, datekey: str) -> dict:
    return {"scheduleParams": {
        "businessId": parsed["biz_id"], "businessTypeId": parsed["service_id"],
        "bizItemId": parsed["item_id"],
        "startDateTime": f"{datekey}T00:00:00+09:00",
        "endDateTime": f"{datekey}T00:00:00+09:00",
    }}


def probe_fields(parsed: dict, datekey: str) -> list[str]:
    """후보를 하나씩 붙여 보며 살아남는 필드만 추린다."""
    found = []
    for name in CANDIDATES:
        if name in KNOWN:
            continue
        status, body = gql("hourlySchedule", hourly_query(KNOWN + [name]),
                           params(parsed, datekey), quiet=True)
        ok = status == 200 and not body.get("errors")
        if ok:
            found.append(name)
            print(f"    O {name}")
        elif cb.looks_rate_limited(json.dumps(body, ensure_ascii=False)):
            print(f"    ! {name} — 속도 제한, 프로빙 중단")
            break
        time.sleep(0.4)          # 네이버 속도 제한을 부르지 않도록 천천히
    return found


def dump_hourly(parsed: dict, datekey: str, fields: list[str]) -> None:
    status, body = gql("hourlySchedule", hourly_query(fields), params(parsed, datekey))
    if body.get("errors"):
        print(f"  errors: {json.dumps(body['errors'], ensure_ascii=False)[:400]}")
        return
    hourly = (((body.get("data") or {}).get("schedule") or {})
              .get("bizItemSchedule") or {}).get("hourly") or []
    print(f"  회차 {len(hourly)}개 — 회차마다 값이 갈리는 필드가 곧 단서다")
    for h in hourly:
        t = (h.get("unitStartTime") or "?")[11:16]
        rest = {k: v for k, v in h.items() if k not in ("unitStartTime", "__typename")}
        print(f"    {t}  {json.dumps(rest, ensure_ascii=False)}")


def dump_calendar(parsed: dict, datekey: str) -> None:
    """캘린더 REST 원본. 1차 시도에서 200인데 JSON이 아니었다 — 무엇이 오는지 본다."""
    url = (f"https://m.booking.naver.com/booking/{parsed['service_id']}"
           f"/bizes/{parsed['biz_id']}/calendars/{datekey[:7]}")
    print(f"  GET {url}")
    try:
        r = cb.requests.get(url, headers=cb.HEADERS, timeout=20)
        print(f"  HTTP {r.status_code} / content-type={r.headers.get('content-type')}")
        print(f"  본문 앞부분: {r.text[:300]!r}")
    except Exception as exc:
        print(f"  실패: {cb._exc_label(exc)}")


def capture_page(url: str, datekey: str) -> None:
    """예약 페이지를 띄워 네트워크와 화면의 시간 버튼을 남긴다.

    페이지가 우리와 다른 엔드포인트를 쓰는 경우는 이 방법으로만 잡힌다.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright 없음 — 건너뜀")
        return

    seen: list[tuple[str, str]] = []

    def on_response(resp):
        u = resp.url
        if not any(k in u for k in ("graphql", "schedule", "calendar", "bizitem", "booking/")):
            return
        if any(u.endswith(ext) for ext in (".js", ".css", ".png", ".jpg", ".svg", ".woff2")):
            return
        try:
            body = resp.text()[:1500]
        except Exception:
            body = "(본문 못 읽음)"
        seen.append((f"{resp.status} {u[:160]}", body))

    target = url + ("&" if "?" in url else "?") + f"startDateTime={datekey}T00:00:00%2B09:00"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(locale="ko-KR")
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(target, wait_until="load", timeout=30000)
            page.wait_for_timeout(6000)
            print(f"  최종 URL: {page.url}")
            texts = page.evaluate(
                "() => Array.from(document.querySelectorAll('button,li,a'))"
                ".map(e => (e.innerText||'').trim())"
                ".filter(t => /^\\d{1,2}[:시]/.test(t)).slice(0, 40)")
            print(f"  화면의 시간 후보: {texts}")
        except Exception as exc:
            print(f"  페이지 로드 실패: {cb._exc_label(exc)}")
        finally:
            ctx.close()
            browser.close()

    print(f"\n  잡힌 응답 {len(seen)}건")
    for head, body in seen[:12]:
        print(f"\n  --- {head}")
        print(f"      {body[:1200]}")


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

    print("\n=== A) 필드 프로빙 (하나씩) — O 표시가 존재하는 필드 ===")
    found = probe_fields(parsed, datekey)
    print(f"  살아남은 후보: {found or '없음'}")

    print("\n=== A-2) 알려진 필드 + 살아남은 후보로 원본 조회 ===")
    dump_hourly(parsed, datekey, KNOWN + found)

    print("\n=== B) 캘린더 REST 원본 ===")
    dump_calendar(parsed, datekey)

    print("\n=== C) 예약 페이지 네트워크 캡처 ===")
    capture_page(url, datekey)

    print("\n=== 끝 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
