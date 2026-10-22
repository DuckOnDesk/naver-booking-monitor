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
    python -u schedule_diagnose.py --gate-compare [예약URL ... | all]

--gate-compare: 예약창 확인(UrlGate)을 로그인 없이 할 수 있는지 본다 (2026-10-22 betty
접속 제한). 같은 페이지를 비로그인/로그인으로 한 번씩 열어 최종 URL과 지금 모니터의
판정을 나란히 찍는다. 열린 항목과 닫힌 항목이 비로그인에서도 갈리면 쿠키 없이 감시할
수 있다. all(또는 생략)이면 monitors.json의 네이버 항목 전부(꺼진 것 포함 — 닫힌
항목 표본).
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
            full = resp.text()
            body = full[:1500]
        except Exception:
            full, body = "", "(본문 못 읽음)"
        seen.append((f"{resp.status} {u[:160]}", body))
        # bizItems 본문은 길어서 앞부분만 남기면 1인 예약 매수 같은 설정이 잘린다.
        # 매수·인원 관련 필드만 따로 뽑아 둔다.
        if "opName=bizItems" in u and full:
            try:
                items = json.loads(full)["data"]["bizItems"] or []
            except Exception:
                items = []
            for it in items:
                picked = {k: v for k, v in it.items()
                          if any(w in k.lower() for w in ("count", "person", "quantity", "limit", "max", "min"))}
                print(f"  [bizItem {it.get('bizItemId')}] 매수·인원 관련 필드: "
                      f"{json.dumps(picked, ensure_ascii=False)[:1500]}")

    target = url + ("&" if "?" in url else "?") + f"startDateTime={datekey}T00:00:00%2B09:00"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(locale="ko-KR")
        # 모니터(UrlGate)와 같은 조건으로 본다. 로그인 쿠키가 없으면 네이버가
        # 예약 페이지를 로그인 화면으로 돌려보내, 화면에 뭐가 있었는지 알 수 없다.
        cookies = cb.naver_cookies()
        print(f"  로그인 쿠키 {len(cookies)}개 적용" if cookies else "  로그인 쿠키 없음(비로그인 조회)")
        if cookies:
            ctx.add_cookies(cookies)
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
            # 버튼이 그려졌는지만 보면 "회색으로 막힌 회차"를 놓친다. 화면에 뜬
            # 회차/날짜 후보마다 비활성 표시(disabled·aria-disabled·클래스)를 같이 남긴다.
            states = page.evaluate(
                "() => Array.from(document.querySelectorAll('button,li,a'))"
                ".filter(e => /\\d{1,2}[:시]|\\d{1,2}일/.test((e.innerText||'').trim()))"
                ".slice(0, 60).map(e => ({"
                "  t: (e.innerText||'').trim().replace(/\\s+/g, ' ').slice(0, 24),"
                "  disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true'"
                "            || e.getAttribute('disabled') !== null,"
                "  cls: (e.className || '').toString().slice(0, 80)"
                "}))")
            for st in states:
                mark = "X" if st["disabled"] else "O"
                print(f"    {mark} {st['t']!r} class={st['cls']!r}")
            # 예약창이 왜 닫혀 있는지는 대개 본문에 문장으로 적혀 있다. 닫힘 판정
            # 문구를 늘리려면 실제 문구를 봐야 하므로 통째로 남긴다.
            try:
                body_text = " ".join(page.inner_text("body").split())
            except Exception:
                body_text = ""
            print(f"  본문({len(body_text)}자): {body_text[:1200]!r}")
        except Exception as exc:
            print(f"  페이지 로드 실패: {cb._exc_label(exc)}")
        finally:
            ctx.close()
            browser.close()

    print(f"\n  잡힌 응답 {len(seen)}건")
    for head, body in seen[:24]:
        print(f"\n  --- {head}")
        print(f"      {body[:1200]}")


def _gate_view(page, url: str) -> str:
    """지금 모니터(_playwright_check)가 이 화면을 어떻게 판정할지 한 줄로."""
    import re
    m = re.search(r"/items/\d+", url)
    item_path = m.group(0) if m else None
    final_url = page.url
    try:
        text = " ".join(page.inner_text("body").split())
    except Exception:
        text = ""
    if any(p in text for p in cb._RATE_LIMIT_PAGE_TEXT):
        verdict = "모름(접속 제한 페이지)"
    elif "nid.naver.com" in final_url:
        verdict = "닫힘(로그인 화면으로 이동)"
    elif any(p in final_url for p in cb._CLOSED_URL_PATTERNS):
        verdict = "닫힘(URL /error/)"
    elif item_path and item_path not in final_url:
        verdict = "닫힘(상품 페이지 이탈)"
    elif any(p in text for p in cb._CLOSED_TEXT_PATTERNS):
        verdict = "닫힘(본문 문구)"
    else:
        verdict = "열림"
    return f"{verdict}\n      최종 URL: {final_url}\n      본문: {text[:300]!r}"


def gate_compare(urls: list[str]) -> int:
    """같은 예약 페이지를 비로그인/로그인으로 열어 판정을 비교한다."""
    from playwright.sync_api import sync_playwright
    if not urls or urls == ["all"]:
        monitors = json.load(open("monitors.json", encoding="utf-8")).get("monitors", [])
        urls = [m["url"] for m in monitors if m.get("url") and cb.parse_naver_url(m["url"])]
    picked = cb._pick_monitor_account(anon=False)
    acct, cookies = picked if picked else ("", [])
    print(f"=== 예약창 확인: 비로그인 vs 로그인({acct or '쿠키 없음'}) ===")
    print(f"  현재 {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} KST, 항목 {len(urls)}개")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for url in urls:
                print(f"\n  ● {url}")
                for label, ck in (("비로그인", []), (f"로그인({acct})", cookies)):
                    if label != "비로그인" and not ck:
                        print("    [로그인] 쿠키 없음 — 건너뜀")
                        continue
                    ctx = browser.new_context(locale="ko-KR")
                    try:
                        if ck:
                            ctx.add_cookies(ck)
                        page = ctx.new_page()
                        page.goto(url, wait_until="load", timeout=20000)
                        page.wait_for_timeout(3000)
                        print(f"    [{label}] {_gate_view(page, url)}")
                    except Exception as exc:
                        print(f"    [{label}] 로드 실패: {cb._exc_label(exc)}")
                    finally:
                        ctx.close()
                    time.sleep(2)   # 진단이 또 제한을 부르지 않게 천천히
        finally:
            browser.close()
    print("\n  비로그인 판정이 로그인 판정과 같으면 그 항목은 쿠키 없이 감시할 수 있다.")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--gate-compare":
        return gate_compare([u for a in sys.argv[2:] for u in a.split() if u.strip()])
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

    # 모니터가 🎉/🔒 라벨을 붙일 때 쓰는 바로 그 판정. API가 자리 있다고 하는데
    # 실제 예약창은 닫혀 있는 경우, 이 줄이 어느 쪽을 틀리게 봤는지 가른다.
    print("\n=== D) 모니터의 예약창 판정 (_playwright_check) ===")
    try:
        is_closed, reason = cb._playwright_check(url)
        print(f"  닫힘={is_closed} 사유={reason or '(없음)'}")
    except Exception as exc:
        print(f"  실패: {cb._exc_label(exc)}")
    finally:
        cb._browser_close()

    print("\n=== 끝 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
