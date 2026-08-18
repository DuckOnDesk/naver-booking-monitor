"""롯데온 페이지에서 모니터가 무엇을 읽는지 그대로 덤프하는 진단 도구.

check_booking.py의 롯데온 감시는 공개 API가 없어 chromium으로 페이지를 열고
"상품 상세로 가는 링크"를 기준으로 상품 카드를 잡는다. 롯데온이 화면 구조를
바꾸면 이 가정이 깨지는데, 깨졌을 때 로그에는 "상품 0개"만 남아 원인을 알 수 없다.
이 도구는 같은 추출기를 그대로 돌려 무엇이 잡히고 무엇이 안 잡히는지 보여준다.

    python -u scripts/probe_lotteon.py <URL> [--json 덤프파일] [--html 덤프파일]

보여주는 것:
  - 최종 도착 URL (로그인·에러 페이지로 튕겼는지)
  - 잡힌 상품 카드: id / 이름 / 구매 가능 판정 / 판정 근거 문구 / 카드 텍스트
  - 페이지가 부른 XHR 주소 목록 (상품 목록을 내려주는 내부 API를 찾는 실마리)
  - 본문 텍스트 앞부분 (alert_keywords에 넣을 문구를 고를 때 쓴다)

로컬에서 롯데온이 막혀 있으면 GitHub Actions 러너에서 돌린다.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_booking as cb  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--json", dest="json_path", help="추출 결과를 이 경로에 JSON으로 저장")
    ap.add_argument("--html", dest="html_path", help="렌더링된 HTML을 이 경로에 저장")
    args = ap.parse_args()

    xhr: list[str] = []
    context = None
    try:
        browser = cb._browser_get()
        context = browser.new_context(
            user_agent=cb.LOTTEON_UA,
            locale="ko-KR",
            viewport={"width": 412, "height": 915},
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()
        # 상품 목록을 내려주는 내부 API를 찾으면 브라우저 없이 감시할 수 있다.
        # (브라우저 한 번이 5~10초라 항목이 늘수록 회차가 통째로 밀린다)
        page.on("request", lambda r: xhr.append(f"{r.method} {r.url}")
                if r.resource_type in ("xhr", "fetch") else None)
        page.goto(args.url, wait_until="domcontentloaded", timeout=cb.LOTTEON_TIMEOUT_MS)
        for _ in range(cb.LOTTEON_SCROLLS):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(700)
        page.wait_for_timeout(1500)

        state = page.evaluate(cb._LOTTEON_EXTRACT_JS)
        final_url = page.url
        html = page.content()
    except Exception as exc:
        print(f"[오류] 페이지 조회 실패: {exc}", flush=True)
        return 1
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass
        cb._browser_close()

    products = state.get("products") or []
    text = state.get("text") or ""

    print(f"\n=== 최종 URL ===\n{final_url}")
    if final_url.rstrip("/") != args.url.rstrip("/"):
        print("  ↑ 요청한 주소와 다르다 (로그인·에러 페이지로 튕겼는지 확인)")

    print(f"\n=== 상품 카드 {len(products)}건 ===")
    if not products:
        print("  한 건도 못 잡았다. 아래 XHR 목록과 본문 텍스트를 보고")
        print("  monitors.json의 lotteon.alert_keywords로 문구를 직접 지정하는 편이 낫다.")
    for p in products:
        avail, reason = cb.lotteon_product_status(p.get("text", ""), cb.LOTTEON_SOLD_OUT_TEXTS)
        mark = "🎉 구매가능" if avail else f"❌ 불가({reason})"
        print(f"\n  [{p.get('id')}] {mark}")
        print(f"    이름 : {p.get('name') or '(없음)'}")
        print(f"    링크 : {p.get('url')}")
        print(f"    카드 : {p.get('text')}")

    print(f"\n=== XHR {len(xhr)}건 ===")
    for u in dict.fromkeys(xhr):   # 순서 유지 중복 제거
        print(f"  {u}")

    print(f"\n=== 본문 텍스트 (앞 2000자 / 전체 {len(text)}자) ===")
    print(text[:2000])

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"final_url": final_url, "products": products, "xhr": xhr, "text": text},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ JSON 저장: {args.json_path}")
    if args.html_path:
        Path(args.html_path).write_text(html, encoding="utf-8")
        print(f"→ HTML 저장: {args.html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
