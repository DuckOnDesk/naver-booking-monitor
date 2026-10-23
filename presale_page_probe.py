"""네이버 팝업 검색 화면을 실제 브라우저로 열어 찍고, 다음 페이지가 어떻게 오는지 본다.

2026-10-23: 검색 요청에 display=200·start=101·page=2를 줘도 100곳만 왔다. 화면에서
2페이지(또는 아래로 스크롤)를 넘길 때 브라우저가 실제로 무슨 요청을 보내는지 기록한다.
결과(스크린샷·요청 기록)는 diag/에 남긴다.

    python -u presale_page_probe.py "서울 팝업"
"""

import json
import sys
import time
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

import presale_monitor as pm

Q = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else "서울 팝업"
X, Y = "126.978", "37.566"
OUT = "diag"


def _count(payload) -> str:
    """graphql 응답에서 장소 수와 total을 뽑는다."""
    try:
        res = payload[0] if isinstance(payload, list) else payload
        data = res.get("data") or {}
        for v in data.values():
            if isinstance(v, dict) and "items" in v:
                return f"items {len(v.get('items') or [])} / total {v.get('total')}"
        return f"구조 다름: {json.dumps(res, ensure_ascii=False)[:300]}"
    except Exception as e:
        return f"파싱 실패 {e}"


def replay(req: dict) -> None:
    """브라우저가 보낸 다음 페이지 요청을 requests로 그대로/헤더를 줄여 다시 보내 본다."""
    print("\n==== 다음 페이지 API 재현 ====")
    print(f"URL: {req['url']}")
    print("요청 헤더: " + json.dumps({k: (v[:80] + '…' if len(v) > 80 else v)
                                      for k, v in req["headers"].items()}, ensure_ascii=False))
    with open(f"{OUT}/graphql_body.json", "w", encoding="utf-8") as f:
        f.write(req["body"])
    body = json.loads(req["body"])
    trials = [
        ("브라우저 헤더 그대로", dict(req["headers"])),
        ("기본 헤더만(UA·Referer·Content-Type)", {
            "User-Agent": pm.SESSION.headers["User-Agent"], "Content-Type": "application/json",
            "Referer": req["headers"].get("referer", "https://pcmap.place.naver.com/")}),
    ]
    for label, headers in trials:
        for start in (101, 201, 301):
            b = json.loads(json.dumps(body))
            for op in (b if isinstance(b, list) else [b]):
                inp = (op.get("variables") or {}).get("input")
                if isinstance(inp, dict):
                    inp["start"] = start
            try:
                r = pm.SESSION.post(req["url"], json=b, headers=headers, timeout=20)
                info = _count(r.json()) if r.headers.get("content-type", "").startswith("application/json") \
                    else r.text[:200]
                print(f"  [{label}] start={start} → {r.status_code} {info}")
            except Exception as e:
                print(f"  [{label}] start={start} → 실패 {e}")
            time.sleep(2)


def main() -> None:
    params = {"query": Q, "x": X, "y": Y, "clientX": X, "clientY": Y, "display": "100",
              "locale": "ko", "mapUrl": f"https://map.naver.com/p/search/{Q}"}
    url = f"{pm.LIST_URL}?{urlencode(params)}"
    log: list = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(locale="ko-KR", viewport={"width": 420, "height": 1400},
                                  user_agent=pm.SESSION.headers["User-Agent"])
        page = ctx.new_page()

        gql: list = []

        def on_request(req):
            if req.resource_type in ("xhr", "fetch"):
                log.append({"t": round(time.time(), 1), "method": req.method, "url": req.url[:300],
                            "body": (req.post_data or "")[:1500]})
                if "pcmap-api.place.naver.com/graphql" in req.url:
                    gql.append({"url": req.url, "headers": req.headers, "body": req.post_data or ""})
        page.on("request", on_request)

        print(f"열기: {url}")
        page.goto(url, wait_until="load", timeout=30000)
        page.wait_for_timeout(4000)
        print(f"최종 URL: {page.url}")
        page.screenshot(path=f"{OUT}/1_first.png")
        names = page.evaluate("() => document.querySelectorAll('li').length")
        print(f"목록 li 수: {names}")
        log.append({"mark": "---- 첫 화면 끝 ----"})

        # 목록 끝까지 스크롤 (무한 스크롤이면 여기서 다음 요청이 나간다)
        for _ in range(15):
            page.mouse.wheel(0, 3000)
            page.evaluate("() => { for (const el of document.querySelectorAll('*')) {"
                          " if (el.scrollHeight > el.clientHeight + 50) el.scrollTop = el.scrollHeight; } }")
            page.wait_for_timeout(700)
        page.screenshot(path=f"{OUT}/2_scrolled_bottom.png")
        print(f"스크롤 뒤 li 수: {page.evaluate('() => document.querySelectorAll(\"li\").length')}")
        log.append({"mark": "---- 스크롤 끝 ----"})

        # 페이지 번호 버튼 찾기
        btns = page.evaluate(
            "() => Array.from(document.querySelectorAll('a,button'))"
            ".map(e => (e.innerText||'').trim()).filter(t => /^(\\d{1,2}|다음페이지|다음|이전)$/.test(t))")
        print(f"페이지 버튼 후보: {btns}")
        clicked = False
        for sel in ("a:text-is('2')", "button:text-is('2')", "a:has-text('다음페이지')"):
            loc = page.locator(sel)
            if loc.count():
                loc.first.click()
                clicked = True
                print(f"클릭: {sel}")
                break
        page.wait_for_timeout(4000)
        page.screenshot(path=f"{OUT}/3_page2.png")
        print(f"2페이지 클릭 {'함' if clicked else '못 함(버튼 없음)'} / li 수: "
              f"{page.evaluate('() => document.querySelectorAll(\"li\").length')}")
        browser.close()
    if gql:
        replay(gql[-1])
    with open(f"{OUT}/requests.json", "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    print(f"요청 기록 {len(log)}건 → {OUT}/requests.json")
    for r in log:
        if "mark" in r:
            print(r["mark"])
        else:
            print(f"  {r['method']} {r['url'][:160]}  {r['body'][:200]}")


if __name__ == "__main__":
    main()
