"""예약 오픈 예정 시각(bookableSettingJson) 조회 경로를 확인하는 진단 도구.

네이버가 막힌 환경에서는 GitHub Actions 러너에서 돌린다.
    python -u scripts/probe_bookable_setting.py <예약URL>

이 도구로 확인된 것 (트루스 오브 뷰티 / businessTypeId 13, 2026-08) — presale_monitor.py의
fetch_bookable_setting()이 쓰는 경로다:

    query bizItem($businessId: String!, $bizItemId: String!) {
      bizItem(input: { businessId: $businessId, bizItemId: $bizItemId }) {
        name bookableSettingJson } }

    → {"bizItem": {"name": "트루스오브뷰티 별별 문방구 사전예약",
                   "bookableSettingJson": {"isPaused": false, "isUseOpen": true,
                                           "openDateTime": "2026-08-04T00:00:00+09:00",
                                           "isOpened": true}}}

  - bizItems(input: {businessId}) 형태도 되지만 상품 단건이면 bizItem이 더 간단하다
  - 같은 값이 상품 페이지 HTML(__APOLLO_STATE__)에도 들어 있다. 업체 페이지에는 없다
  - schedule.saleStartDate는 이 상품에서 null이라, 오픈 시각은 이쪽이 정확하다
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
GRAPHQL = "https://m.booking.naver.com/graphql"


def scan_pages(svc: int, biz: str, item: str) -> None:
    """페이지 HTML에 bookableSettingJson이 박혀 있는지."""
    print("\n=== 페이지 HTML 확인 ===", flush=True)
    for u in [f"https://m.booking.naver.com/booking/{svc}/bizes/{biz}/items/{item}",
              f"https://m.booking.naver.com/booking/{svc}/bizes/{biz}"]:
        print(f"\n--- {u}", flush=True)
        try:
            body = requests.get(u, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=20).text
        except Exception as exc:
            print(f"  요청 실패 {exc}", flush=True)
            continue
        hits = [m.start() for m in re.finditer(r'"bookableSettingJson"', body)]
        print(f"  bookableSettingJson {len(hits)}회 등장", flush=True)
        for pos in hits[:3]:
            print(f"    {body[pos:pos + 320]}", flush=True)


def try_graphql(label: str, payload: dict) -> dict | None:
    print(f"\n--- {label}", flush=True)
    try:
        resp = requests.post(f"{GRAPHQL}?opName={payload['operationName']}",
                             json=payload, headers=HEADERS, timeout=20)
    except Exception as exc:
        print(f"  요청 실패 {exc}", flush=True)
        return None
    print(f"  HTTP {resp.status_code}", flush=True)
    try:
        data = resp.json()
    except Exception:
        print(f"  JSON 아님: {resp.text[:300]}", flush=True)
        return None
    for e in (data.get("errors") or [])[:6]:
        print(f"  [오류] {e.get('message')[:200]}", flush=True)
    if data.get("data"):
        print(f"  {json.dumps(data['data'], ensure_ascii=False)[:1500]}", flush=True)
    return data


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://booking.naver.com/booking/13/bizes/1707570/items/7913541"
    m = re.search(r"/booking/(\d+)/bizes/(\d+)/items/(\d+)", url)
    if not m:
        print("URL 파싱 실패", flush=True)
        return 1
    svc, biz, item = int(m.group(1)), m.group(2), m.group(3)
    print(f"대상: businessTypeId={svc} businessId={biz} bizItemId={item}", flush=True)

    scan_pages(svc, biz, item)

    print("\n=== GraphQL 시도 ===", flush=True)
    # 1) bizItem 단건에 bookableSettingJson이 있는지
    try_graphql("bizItem { bookableSettingJson }", {
        "operationName": "bizItem",
        "variables": {"businessId": biz, "bizItemId": item},
        "query": ("query bizItem($businessId: String!, $bizItemId: String!) {"
                  " bizItem(input: { businessId: $businessId, bizItemId: $bizItemId }) {"
                  " name bookableSettingJson } }"),
    })

    # 2) 페이지가 쓰는 opName=bizitems — 입력 형태 후보를 몇 가지 시도
    variants = [
        ("bizItems(input: {businessId})", {
            "operationName": "bizItems",
            "variables": {"businessId": biz},
            "query": ("query bizItems($businessId: String!) {"
                      " bizItems(input: { businessId: $businessId }) {"
                      " id name bookableSettingJson } }"),
        }),
        ("bizItems(businessId:)", {
            "operationName": "bizItems",
            "variables": {"businessId": biz},
            "query": ("query bizItems($businessId: String!) {"
                      " bizItems(businessId: $businessId) { id name bookableSettingJson } }"),
        }),
        ("bizItems(input: {businessId, lang})", {
            "operationName": "bizItems",
            "variables": {"businessId": biz, "lang": "ko"},
            "query": ("query bizItems($businessId: String!, $lang: String) {"
                      " bizItems(input: { businessId: $businessId, lang: $lang }) {"
                      " id name bookableSettingJson } }"),
        }),
    ]
    for label, payload in variants:
        data = try_graphql(label, payload)
        if data and data.get("data") and not data.get("errors"):
            print("  ★ 이 형태가 동작함", flush=True)
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
