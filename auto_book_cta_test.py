"""진행/확정 버튼 탐색 회귀 테스트 (로컬 HTML만 사용, 네트워크 불필요).

2026-10-06 드라이런(booking/12 상품)에서 날짜·시간·'다음'까지는 갔는데 확정 화면에서
"확정 버튼: 'None'"을 찍고도 성공으로 보고했다. 대기·클릭·드라이런 보고가 버튼을
제각각 찾았고, 문구 비교가 공백에 민감했기 때문이다.

  1) 줄바꿈·span으로 쪼개진 "동의하고<br>예약하기"를 찾는다
  2) 공백 없는 "예약신청하기"도 "예약 신청"으로 찾는다
  3) div[role=button] CTA도 찾는다
  4) 같은 문구의 탭·숨김·비활성 버튼은 건너뛰고 진짜 CTA를 누른다
  5) 래퍼 a 안에 button이 있으면 안쪽 button을 누른다
  6) 문구가 하나도 없으면 None (드라이런이 실패로 보고할 근거)
  7) '다음'을 눌러도 화면이 그대로면 "넘어가지 않음"으로 본다
     (같은 '다음' 버튼을 확정 버튼으로 착각하던 회귀) — 넘어가면 넘어간 것으로 본다

사용법: python auto_book_cta_test.py
"""

import os
import sys

os.environ.setdefault("AUTO_BOOK_SHOTS", "off")

import auto_book

FAILS = []


def check(name, cond, actual=None):
    print(f"    {'PASS' if cond else 'FAIL'} — {name}" + (f" (실제: {actual!r})" if actual is not None else ""))
    if not cond:
        FAILS.append(name)


CLICK_JS = """<script>
document.addEventListener('click', e => {
  const b = e.target.closest('[data-id]');
  if (b) document.body.setAttribute('data-clicked', b.getAttribute('data-id'));
});
</script>"""


def page(body: str) -> str:
    return f"<html><head><meta charset='utf-8'></head><body>{body}{CLICK_JS}</body></html>"


CASES = [
    ("1) 줄바꿈으로 쪼개진 '동의하고 예약하기'",
     """<button class="tab_item" data-id="tab">예약하기</button>
        <button class="btn_submit" data-id="cta"><span>동의하고</span><br><span>예약하기</span></button>""",
     "cta"),
    ("2) 공백 없는 '예약신청하기'",
     """<button data-id="cta">예약신청하기</button>""",
     "cta"),
    ("3) div[role=button] CTA",
     """<div role="button" class="BottomButton" data-id="cta">결제하기</div>""",
     "cta"),
    ("4) 숨김·비활성 같은 문구는 건너뛴다",
     """<button data-id="hidden" style="display:none">동의하고 예약하기</button>
        <button data-id="off" disabled>동의하고 예약하기</button>
        <button data-id="dim" class="btn dimmed">동의하고 예약하기</button>
        <div class="bottom"><button data-id="cta">동의하고 예약하기</button></div>""",
     "cta"),
    ("5) 래퍼 a 안의 button을 누른다",
     """<a href="#" class="wrap"><button data-id="cta">예약 신청</button></a>""",
     "cta"),
    ("6) 확정 문구가 없으면 None",
     """<button data-id="x">알림받기</button><a data-id="y">리뷰</a>""",
     None),
]


def main() -> int:
    from playwright.sync_api import sync_playwright

    kw = {"headless": True}
    exe = os.environ.get("AUTO_BOOK_CHROMIUM", "").strip()
    if exe:
        kw["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**kw)
        pg = browser.new_page()
        for name, body, want in CASES:
            print(name)
            pg.set_content(page(body))
            found = auto_book._find_cta(pg, auto_book._FINAL_BUTTON_TEXTS)
            if want is None:
                check("찾지 않는다", found is None, found)
                check("_click_cta도 None", auto_book._click_cta(pg, auto_book._FINAL_BUTTON_TEXTS) is None)
                continue
            check("버튼을 찾는다", found is not None, found)
            clicked = auto_book._click_cta(pg, auto_book._FINAL_BUTTON_TEXTS)
            got = pg.evaluate("() => document.body.getAttribute('data-clicked')")
            check("진짜 CTA를 누른다", clicked is not None and got == want, got)

        print("7) 진행 버튼을 누른 뒤 화면 전환 판정")
        for label, onclick, want in (
                ("화면 그대로", "", False),
                ("버튼이 사라지는 화면 전환", "this.closest('main').innerHTML='<button>동의하고 예약하기</button>'", True)):
            pg.set_content(page(f'<main><button onclick="{onclick}">다음</button></main>'))
            before = pg.url
            auto_book._click_cta(pg, auto_book._NEXT_BUTTON_TEXTS)
            pg.wait_for_timeout(100)
            got = auto_book._left_stage(pg, before)
            check(f"{label} → 넘어감={want}", got == want, got)
        pg.set_content(page('<footer><button>로그인</button></footer>'))
        check("'로그인' 버튼이 보이면 로그아웃 의심", auto_book._looks_logged_out(pg))
        pg.set_content(page('<footer><button>로그아웃</button></footer>'))
        check("'로그아웃'만 있으면 로그인 상태", not auto_book._looks_logged_out(pg))
        browser.close()

    print(f"\n=== 실패 {len(FAILS)}건 ===")
    for f in FAILS:
        print(f"  - {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
