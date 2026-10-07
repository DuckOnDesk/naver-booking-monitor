"""네이버 예약 표준 화면 경로 회귀 테스트 (로컬 HTML만 사용, 네트워크 불필요).

2026-10-07 드라이런에서 확정 화면 로딩 중 '동의하고 예약하기'에 잠깐 붙는 disabled 클래스
때문에 진짜 버튼을 빼고 헤더 제목 링크(h1>a '예약하기')를 확정 버튼으로 집었다.
그래서 표준 화면은 정확한 셀렉터로 단계마다 확인하며 진행하도록 바꿨다 (auto_book._std_*).

  1) 달력: 앞뒤 달 칸(27~30, 1~)은 건너뛰고 이번 달 날짜를 고른다 / 마감 날짜는 안 고른다
  2) 시간: "16:00" 요청에 '오후 4:00'·'16:00'·'16시'·'16'이 모두 맞고, '오전 4:00'은 안 맞는다
     재고 표시(span.stock)의 숫자에 속지 않는다 / 마감 시간대는 건너뛴다
  3) 수량: Count__btn_plus*를 요청 수량까지 누르고, Count__disabled*면 멈춘다
     가격(strong.Count__num*)이 아닌 span.Count__num*을 읽는다
  4) 다음: NextButton__disabled*가 붙어 있으면 비활성으로 본다
  5) 예약 창: h1>a '예약하기' + btn_request, disabled 클래스가 풀릴 때까지 기다린다
  6) 완료 일시: "10. 10. (토) 오후 4:30" → 10/10 16:30

사용법: python auto_book_std_test.py
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


def html(body: str) -> str:
    return f"<html><head><meta charset='utf-8'></head><body>{body}</body></html>"


def calendar(title: str, cells: list) -> str:
    """cells: [(일, 클래스)] — 한 줄 7칸씩 끊는다. 날짜를 누르면 aria-selected가 켜진다."""
    tds = [f'<td><button type="button" class="calendar_date {cls}" aria-selected="false" '
           f'onclick="this.setAttribute(\'aria-selected\',\'true\')"><span class="num">{d}</span></button></td>'
           for d, cls in cells]
    rows = "".join(f'<tr class="calendar_week">{"".join(tds[i:i + 7])}</tr>' for i in range(0, len(tds), 7))
    return (f'<div class="calendar_area"><div class="calendar_month">'
            f'<div class="calendar_title"><button class="btn_prev"></button>{title}<button class="btn_next"></button></div>'
            f'<table class="calendar_table"><tbody class="calendar_body">{rows}</tbody></table></div></div>')


def times(items: list) -> str:
    """items: [(문구, 클래스)]. 누르면 목록을 다시 그린 뒤 선택 표시 (실제 화면처럼 노드가 바뀐다)."""
    lis = "".join(
        f'<li class="time_item"><button type="button" class="btn_time {cls}" aria-selected="false">'
        f'{t}<span class="stock">{"3매" if i == 0 else ""}</span></button></li>'
        for i, (t, cls) in enumerate(items))
    js = """<script>
    document.querySelector('.time_list').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b || /unselectable/.test(b.className)) return;
      const i = Array.from(document.querySelectorAll('.time_item')).indexOf(b.closest('li'));
      const ul = document.querySelector('.time_list'); ul.innerHTML = ul.innerHTML;
      ul.querySelectorAll('.time_item button')[i].setAttribute('aria-selected', 'true');
    });</script>"""
    return f'<div class="time_area"><ul class="time_list">{lis}</ul></div>{js}'


def main() -> int:
    from playwright.sync_api import sync_playwright

    kw = {"headless": True}
    exe = os.environ.get("AUTO_BOOK_CHROMIUM", "").strip()
    if exe:
        kw["executable_path"] = exe
    with sync_playwright() as p:
        browser = p.chromium.launch(**kw)
        pg = browser.new_page()

        print("1) 달력")
        # 9월 27~30 다음 10월 1~31, 끝에 11월 1 — 10/30을 고르면 이번 달 30이어야 한다
        cells = [(d, "unselectable") for d in (27, 28, 29, 30)] + [(d, "") for d in range(1, 32)] + [(1, "")]
        cells[4 + 9] = (10, "unselectable")       # 10/10 마감
        pg.set_content(html(calendar("2026.10", cells)))
        ok, why = auto_book._std_select_date(pg, "2026-10-30")
        picked = pg.evaluate("() => Array.from(document.querySelectorAll('button.calendar_date'))"
                             ".findIndex(b => b.getAttribute('aria-selected') === 'true')")
        check("10/30은 앞달 30이 아니라 이번 달 30", ok and picked == 4 + 29, (ok, why, picked))
        pg.set_content(html(calendar("2026.10", cells)))
        ok, why = auto_book._std_select_date(pg, "2026-10-10")
        check("마감 날짜는 고르지 않는다", not ok and "선택 불가" in why, why)
        pg.set_content(html(calendar("2026.10", cells)))
        ok, why = auto_book._std_select_date(pg, "2026-10-01")
        picked = pg.evaluate("() => Array.from(document.querySelectorAll('button.calendar_date'))"
                             ".findIndex(b => b.getAttribute('aria-selected') === 'true')")
        check("10/1은 다음 달 1이 아니라 이번 달 1", ok and picked == 4, (ok, why, picked))

        print("2) 시간")
        for label, items, want_idx in (
                ("'오후 4:00'", [("오전 4:00", ""), ("오후 3:30", ""), ("오후 4:00", "")], 2),
                ("'16:00'", [("15:00", ""), ("16:00", "")], 1),
                ("'16시'", [("15시", ""), ("16시", "")], 1),
                ("'16'", [("15", ""), ("16", "")], 1),
                ("마감 칸은 건너뛰고 다음 후보", [("오후 4:00", "unselectable"), ("오후 4:30", "")], 1)):
            pg.set_content(html(times(items)))
            wanted = ["16:00", "16:30"] if "마감" in label else ["16:00"]
            got, _ = auto_book._std_select_time(pg, wanted)
            sel = pg.evaluate("() => Array.from(document.querySelectorAll('.time_item button'))"
                              ".findIndex(b => b.getAttribute('aria-selected') === 'true')")
            check(f"{label} 선택", got is not None and sel == want_idx, (got, sel))
        pg.set_content(html(times([("오전 4:00", ""), ("오후 3:00", "")])))
        got, _ = auto_book._std_select_time(pg, ["16:00"])
        check("'오전 4:00'은 16:00이 아니다", got is None, got)

        print("3) 수량")
        count_js = """<script>(() => {
          const max = MAX; const num = document.querySelector('span[class^="Count__num"]');
          const plus = document.querySelector('button[class^="Count__btn_plus"]');
          plus.onclick = () => { if (+num.textContent < max) num.textContent = +num.textContent + 1;
            if (+num.textContent >= max) plus.classList.add('Count__disabled__dh7Td'); };
        })();</script>"""
        opt = ('<section class="section_option"><strong class="Count__num__lyIDX">0원</strong>'
               '<button class="Count__btn_minus__rio4R"></button><span class="Count__num__lyIDX">1</span>'
               '<button class="Count__btn_plus__441I4"></button></section>')
        pg.set_content(html(opt + count_js.replace("MAX", "5")))
        msg = auto_book._std_set_count(pg, 3)
        check("요청 3매까지 늘린다", msg == "수량 3매", msg)
        pg.set_content(html(opt + count_js.replace("MAX", "2")))
        msg = auto_book._std_set_count(pg, 4)
        check("Count__disabled면 멈춘다", msg.startswith("수량 2매") and "더 늘릴 수 없음" in msg, msg)
        pg.set_content(html("<div></div>"))
        check("수량 섹션이 없으면 그냥 넘어간다", auto_book._std_set_count(pg, 2) == "수량 섹션 없음")

        print("4) 다음 버튼")
        pg.set_content(html('<button class="NextButton__btn_next__4hAoO NextButton__disabled__t72qg">다음</button>'))
        check("NextButton__disabled는 비활성", not pg.evaluate(auto_book._JS_STD_NEXT_READY, auto_book._STD_NEXT))
        pg.set_content(html('<button class="NextButton__btn_next__4hAoO">다음</button>'))
        check("disabled 클래스 없으면 활성", pg.evaluate(auto_book._JS_STD_NEXT_READY, auto_book._STD_NEXT))

        print("5) 예약 창")
        pg.set_content(html(
            '<header><h1><a class="BizItemHeader__text__Bqfdn">예약하기</a></h1></header>'
            '<section class="section_booking_info"><div class="desc date">10. 10. (토)<span class="time"> 오후 4:00</span></div></section>'
            '<div class="section_booking_footer"><div class="booking_inner"><button class="link_back">이전</button>'
            '<button type="button" class="btn_request disabled">동의하고 예약하기</button></div></div>'
            "<script>setTimeout(() => document.querySelector('.btn_request').classList.remove('disabled'), 700)</script>"))
        st = auto_book._std_request_page(pg, 3000)
        check("예약 창으로 인식", st.get("onPage") and st.get("button"), st)
        check("로딩 중 disabled 클래스가 풀릴 때까지 기다린다", st.get("ready"), st.get("cls"))
        check("확정 버튼은 헤더 링크가 아니라 btn_request", st.get("label") == "동의하고 예약하기", st.get("label"))

        print("6) 완료 일시")
        for raw, want in (("10. 10. (토) 오후 4:30", (10, 10, 16, 30)),
                          ("10. 10. (토) 오전 12:00", (10, 10, 0, 0)),
                          ("1. 5. (월) 오후 12:30", (1, 5, 12, 30)),
                          ("10. 9. (금)", (10, 9, -1, -1))):
            got = auto_book._md_hm(raw)
            check(f"'{raw}' → {want}", got == want, got)
        browser.close()

    print(f"\n=== 실패 {len(FAILS)}건 ===")
    for f in FAILS:
        print(f"  - {f}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
