"""달력 날짜 선택 회귀 테스트 (네트워크·계정 없이 로컬 HTML로만 검증).

'요청한 달이 아닌 엉뚱한 달의 달력을 보고 예약을 시도하던' 버그를 다시 만들지
않기 위한 테스트다. 네이버 예약 달력에서 자주 보이는 형태를 흉내 낸 가짜 페이지에
auto_book._select_date를 돌려, 아래를 확인한다.

  - 요청한 달로 필요한 방향·횟수만큼만 이동한다 (다음 달을 무작정 7번 누르지 않는다)
  - "12월"을 "2월"로 오인하지 않는다 (예전 부분일치 버그)
  - 한 화면에 두 달이 보일 때 각 날짜가 어느 달 것인지 구분한다
  - 대상 날짜가 마감이면 다른 달로 넘어가지 않고 실패로 끝낸다
  - 다른 날짜가 선택돼 있으면 불일치로 잡아낸다

사용법: python auto_book_date_test.py
"""

import os
import sys

os.environ.setdefault("AUTO_BOOK_SHOTS", "off")

import auto_book

# 네이버 모바일 예약과 비슷한 표(table) 달력 — 헤더 "2026.07", 셀은 td > button > span
TABLE_CAL = """<html><head><meta charset="utf-8"></head><body>
<div class="calendar_area">
  <div class="calendar_head">
    <button class="btn_prev" aria-label="이전 달">&lt;</button>
    <strong class="calendar_title" id="title"></strong>
    <button class="btn_next" aria-label="다음 달">&gt;</button>
  </div>
  <table class="calendar_table"><thead><tr>
    <th>일</th><th>월</th><th>화</th><th>수</th><th>목</th><th>금</th><th>토</th>
  </tr></thead><tbody id="grid"></tbody></table>
</div>
<script>
var Y = %(y)d, M = %(m)d, OFF = %(off)s;
function draw() {
  document.getElementById('title').textContent = Y + '.' + String(M).padStart(2, '0');
  var h = '<tr>';
  for (var d = 1; d <= 28; d++) {
    var key = Y + '-' + String(M).padStart(2, '0') + '-' + String(d).padStart(2, '0');
    var off = OFF.indexOf(key) >= 0;
    h += '<td class="calendar_cell' + (off ? ' calendar_cell--disabled' : '') + '">' +
         '<button class="calendar_date" data-date="' + key + '"' + (off ? ' disabled' : '') +
         ' onclick="pick(this)"><span class="num">' + d + '</span></button></td>';
    if (d %% 7 === 0) h += '</tr><tr>';
  }
  document.getElementById('grid').innerHTML = h + '</tr>';
}
function pick(b) {
  document.querySelectorAll('[aria-selected]').forEach(function (e) { e.removeAttribute('aria-selected'); });
  b.setAttribute('aria-selected', 'true');
}
document.querySelector('.btn_next').onclick = function () { M++; if (M > 12) { M = 1; Y++; } draw(); };
document.querySelector('.btn_prev').onclick = function () { M--; if (M < 1) { M = 12; Y--; } draw(); };
draw();
</script></body></html>"""

# 스크롤형 달력 — 두 달을 한 화면에 그리고, 날짜 칸에 배지 텍스트가 붙는다 (aria-label 없음)
def scroll_cal(months: list) -> str:
    blocks = []
    for y, m, off in months:
        cells = "".join(
            f'<li class="cal_cell{" cal_cell--soldout" if d in off else ""}">'
            f'<button class="day"{" disabled" if d in off else ""} onclick="sel(this)">'
            f'<span class="num">{d}</span>'
            f'<em class="badge">{"마감" if d in off else "예약가능"}</em></button></li>'
            for d in range(1, 29))
        blocks.append(f'<div class="month_block"><h3 class="month_title">{y}년 {m}월</h3>'
                      f'<ul class="days">{cells}</ul></div>')
    return ('<html><head><meta charset="utf-8"></head><body>'
            f'<div class="CalendarScroll">{"".join(blocks)}</div>'
            '<script>function sel(b){'
            "document.querySelectorAll('[aria-selected]')"
            ".forEach(function(e){e.removeAttribute('aria-selected');});"
            "b.setAttribute('aria-selected','true');}</script></body></html>")


NO_CALENDAR = ('<html><head><meta charset="utf-8"></head><body><h1>공연 예약</h1>'
               '<ul><li><button>10:00</button></li></ul></body></html>')


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright 미설치 — 테스트를 건너뜁니다")
        return 0

    fails: list = []

    def check(cond, msg):
        print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
        if not cond:
            fails.append(msg)

    launch_kwargs = {"headless": True}
    exe = os.environ.get("AUTO_BOOK_CHROMIUM", "").strip()
    if exe:
        launch_kwargs["executable_path"] = exe

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)

        def open_page(html):
            # set_content을 한 페이지에서 반복하면 전역 변수 재선언으로 스크립트가 죽는다
            page = browser.new_page()
            page.set_content(html)
            return page

        def table(y, m, off="[]"):
            return open_page(TABLE_CAL % {"y": y, "m": m, "off": off})

        print("1) 대상 월이 이미 열려 있는 경우")
        page = table(2026, 7)
        ok, why = auto_book._select_date(page, "2026-07-11")
        check(ok, f"같은 달 날짜 선택 ({why})")

        print("2) 다음 달로 한 번만 이동 (7월 달력 → 2026-08-01)")
        page = table(2026, 7)
        ok, why = auto_book._select_date(page, "2026-08-01")
        check(ok, f"선택 성공 ({why})")
        check(page.locator("#title").inner_text() == "2026.08",
              "8월에서 멈춤 — 다음 달을 7번 눌러 2027년까지 밀리지 않는다")

        print("3) 이전 달 방향 이동 (12월 달력 → 2026-08-02)")
        page = table(2026, 12)
        ok, why = auto_book._select_date(page, "2026-08-02")
        check(ok, f"선택 성공 ({why})")
        check(page.locator("#title").inner_text() == "2026.08", "이전 달 이동이 정확히 멈춤")

        print("4) '12월'을 '2월'로 오인하지 않음 (12월 달력 → 2027-02-05)")
        page = table(2026, 12)
        ok, why = auto_book._select_date(page, "2027-02-05")
        check(ok, f"선택 성공 ({why})")
        check(page.locator("#title").inner_text() == "2027.02", "연도를 넘겨 2027.02 도달")

        print("5) 대상 날짜가 마감된 경우 (2026-08-01 품절)")
        page = table(2026, 8, '["2026-08-01"]')
        ok, why = auto_book._select_date(page, "2026-08-01")
        check(not ok, "실패로 끝남")
        check(page.locator("#title").inner_text() == "2026.08",
              "엉뚱한 달로 넘어가지 않음 (핵심)")
        check("선택할 수 없음" in why, f"사유가 명확함: {why}")

        print("6) 두 달이 한 화면에 보이는 달력")
        page = open_page(scroll_cal([(2026, 7, set()), (2026, 8, set())]))
        ok, why = auto_book._select_date(page, "2026-07-03")
        check(ok, f"7월 3일 선택 ({why})")
        block = page.evaluate("() => document.querySelector('[aria-selected=true]')"
                              ".closest('.month_block').querySelector('.month_title').textContent")
        check(block == "2026년 7월", f"7월 블록에서 선택됨 (선택된 블록: {block})")

        page = open_page(scroll_cal([(2026, 7, set()), (2026, 8, set())]))
        ok, why = auto_book._select_date(page, "2026-08-03")
        check(ok, f"8월 3일 선택 ({why})")
        block = page.evaluate("() => document.querySelector('[aria-selected=true]')"
                              ".closest('.month_block').querySelector('.month_title').textContent")
        check(block == "2026년 8월", f"8월 블록에서 선택됨 (선택된 블록: {block})")

        print("7) 표시된 달에도 없고 이동 버튼도 없는 경우")
        page = open_page(scroll_cal([(2026, 7, set()), (2026, 8, set())]))
        ok, why = auto_book._select_date(page, "2027-02-05")
        check(not ok, "엉뚱한 날짜를 고르지 않고 실패")
        check("이동하지 못함" in why, f"사유가 이동 실패로 기록됨: {why}")

        print("8) 달력이 없는 페이지")
        page = open_page(NO_CALENDAR)
        ok, why = auto_book._select_date(page, "2026-08-01")
        check((not ok) and why == "no_calendar", f"no_calendar로 보고 ({why})")

        print("9) 선택된 날짜 대조")
        page = table(2026, 8)
        auto_book._select_date(page, "2026-08-05")
        check(auto_book._selected_conflict(page, "2026-08-05") is None, "맞는 날짜면 충돌 없음")
        check(auto_book._selected_conflict(page, "2026-08-06") is not None, "다른 날짜면 충돌 감지")

        print("10) 확정 화면 날짜 대조")
        page = open_page('<html><head><meta charset="utf-8"></head><body>'
                         "<p>이용 일시 2026. 8. 1.(토) 오후 3:00</p>"
                         "<p>무료 취소 2026.07.31까지</p></body></html>")
        check(auto_book._page_date_conflict(page, "2026-08-01") is None,
              "요청 날짜가 화면에 있으면 통과")
        check(auto_book._page_date_conflict(page, "2027-02-05") is not None,
              "요청 날짜가 없으면 불일치 감지")

        # 예약 날짜는 연도 없이 적히고, 약관 개정일만 연도까지 적힌 화면 (오탐 방지)
        page = open_page('<html><head><meta charset="utf-8"></head><body>'
                         "<p>이용 일시 8월 1일(토) 오후 3:00</p>"
                         "<p>본 약관은 2025.01.01부터 적용됩니다</p></body></html>")
        check(auto_book._page_date_conflict(page, "2026-08-01") is None,
              "연도 없이 '8월 1일'만 적힌 화면은 통과 (약관 날짜에 속지 않음)")
        check(auto_book._page_date_conflict(page, "2026-09-09") is not None,
              "그래도 다른 날짜면 불일치 감지")

        browser.close()

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
