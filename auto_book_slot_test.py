"""시간대 선택·페이지 준비 대기 회귀 테스트 (로컬 HTML만 사용, 네트워크 불필요).

2026-08-06 새벽 라운즈2차 실패에서 드러난 문제들을 다시 만들지 않기 위한 테스트다.

  1) 예약 UI가 늦게 그려져도 기다렸다가 진행한다
     (빈 <div class="calendar_area">만 보고 "달력 없음"으로 넘어가면 안 된다)
  2) 겉은 멀쩡하고 안쪽 버튼만 'unselectable'인 시간대 칸을 눌러 놓고
     "선택 완료"라고 보고하지 않는다
  3) 선택 불가 슬롯은 "선택 불가(매진·마감)"로 구분해 보고하고 unbookable 플래그를 세운다
  4) 정상 슬롯은 종전대로 선택된다

사용법: python auto_book_slot_test.py
"""

import contextlib
import http.server
import os
import socketserver
import sys
import threading

os.environ.setdefault("AUTO_BOOK_SHOTS", "off")

import auto_book


@contextlib.contextmanager
def _serve(pages: dict):
    """{경로: HTML}을 임시 HTTP 서버로 띄운다 (try_book이 goto할 URL이 필요해서)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path.lstrip("/").split("?")[0])
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((body or "<html></html>").encode("utf-8"))

        def log_message(self, *a):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()

# 네이버 예약 페이지와 같은 구조: li.time_item > button.btn_time[.unselectable]
def page_html(slot_state: str, delay_ms: int = 0) -> str:
    """slot_state: 'ok' | 'unselectable' — delay_ms 뒤에 달력·시간대를 그린다."""
    blocked = " unselectable" if slot_state == "unselectable" else ""
    return f"""<html><head><meta charset="utf-8"></head><body>
    <section class="section_calendar">
      <div class="calendar_area" id="cal"></div>
    </section>
    <div class="time_area"><div class="calendar_time_slot" id="times"></div></div>
    <button class="NextButton__btn_next__x NextButton__disabled__y" id="next">다음</button>
    <script>
    function draw() {{
      var h = '<div class="calendar_month"><div class="calendar_title">2026.8</div>' +
              '<table class="calendar_table"><tbody class="calendar_body"><tr>';
      for (var d = 1; d <= 28; d++) {{
        h += '<td class="calendar_cell"><button class="calendar_date" data-date="2026-08-' +
             String(d).padStart(2, '0') + '" onclick="pickDate(this)"><span class="num">' +
             d + '</span></button></td>';
        if (d % 7 === 0) h += '</tr><tr>';
      }}
      document.getElementById('cal').innerHTML = h + '</tr></tbody></table></div>';
    }}
    function pickDate(b) {{
      document.querySelectorAll('[aria-selected]').forEach(function (e) {{
        e.removeAttribute('aria-selected'); }});
      b.setAttribute('aria-selected', 'true');
      document.getElementById('times').innerHTML =
        '<ul class="time_list"><li class="time_item">' +
        '<button class="btn_time{blocked}" onclick="pickTime(this)">오전 11:00</button>' +
        '</li></ul>';
    }}
    function pickTime(b) {{
      if (b.className.indexOf('unselectable') >= 0) return;   // 죽은 칸은 반응하지 않는다
      b.className = 'btn_time selected';
      document.getElementById('next').className = 'NextButton__btn_next__x';
    }}
    setTimeout(draw, {delay_ms});
    </script></body></html>"""


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
            page = browser.new_page()
            page.set_content(html)
            return page

        print("1) 예약 UI가 1.5초 뒤에 그려지는 페이지")
        page = open_page(page_html("ok", delay_ms=1500))
        # 그려지기 전 상태에서는 달력이 안 보여야 정상 (테스트 전제 확인)
        check(not _scan_ok(page), "직후에는 달력이 아직 없음 (예전 코드가 여기서 포기했다)")
        auto_book._wait_booking_ui(page, 6000)
        check(_scan_ok(page), "_wait_booking_ui가 렌더를 기다림")
        ok, why = auto_book._select_date(page, "2026-08-11")
        check(ok, f"날짜 선택 성공 ({why})")

        print("2) _select_date 자체도 늦게 그려지는 달력을 기다린다")
        page = open_page(page_html("ok", delay_ms=1200))
        ok, why = auto_book._select_date(page, "2026-08-11")
        check(ok, f"no_calendar로 성급히 끝내지 않음 ({why})")

        print("3) 정상 슬롯 선택")
        page = open_page(page_html("ok"))
        auto_book._select_date(page, "2026-08-11")
        picked = auto_book._select_time(page, ["11:00"])
        check(picked == "11:00", f"11:00 선택 ({picked})")
        check(page.locator("#next").get_attribute("class").find("disabled") < 0,
              "다음 버튼이 활성화됨")

        print("4) 안쪽 버튼만 unselectable인 슬롯 (라운즈2차에서 실제로 있었던 상태)")
        page = open_page(page_html("unselectable"))
        auto_book._select_date(page, "2026-08-11")
        picked = auto_book._select_time(page, ["11:00"])
        check(picked is None, f"선택 성공으로 오보고하지 않음 (반환: {picked})")
        blocked = auto_book._blocked_slots(page, ["11:00"])
        check(blocked == ["11:00"], f"선택 불가 슬롯으로 식별 ({blocked})")

        print("5) 슬롯 칸 자체가 없는 경우와 구분")
        page = open_page(page_html("ok"))
        auto_book._select_date(page, "2026-08-11")
        check(auto_book._blocked_slots(page, ["15:00"]) == [],
              "없는 시간대는 '선택 불가'가 아니라 '없음'으로 남는다")

        browser.close()

    print("6) try_book 전체 흐름 (로컬 서버)")
    with _serve({"ok.html": page_html("ok", delay_ms=900),
                 "blocked.html": page_html("unselectable")}) as base:
        res = auto_book.try_book(f"{base}/ok.html", "2026-08-11", ["11:00"],
                                 cookie_str="NID_AUT=x; NID_SES=y")
        # 확정 버튼이 없는 테스트 페이지라 완료까지는 못 가지만, 시간대까지는 정상 진행해야 한다
        check("시간대" not in res["message"], f"정상 슬롯은 시간대 단계를 통과 ({res['message']})")
        check(not res.get("unbookable"), "정상 슬롯은 unbookable이 아님")

        res = auto_book.try_book(f"{base}/blocked.html", "2026-08-11", ["11:00"],
                                 cookie_str="NID_AUT=x; NID_SES=y")
        check(res.get("unbookable") is True, f"선택 불가 슬롯 → unbookable ({res['message']})")
        check("선택 불가" in res["message"],
              "사유가 '선택 불가'로 남는다 (예전엔 '예약 진행 버튼을 찾지 못함'이었다)")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


def _scan_ok(page) -> bool:
    return bool(auto_book._scan_calendar(page, "2026-08-11").get("calendar"))


if __name__ == "__main__":
    sys.exit(main())
