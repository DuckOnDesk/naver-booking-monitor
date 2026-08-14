"""완료 판정 회귀 테스트 (로컬 HTML만 사용, 네트워크 불필요).

2026-08-11 TFT 스탬프투어에서 "예약 안 됐는데 성공"이 난 원인을 다시 만들지
않기 위한 테스트다. 그때 흐름은 이랬다.

  시간대 선택 → '다음' 클릭 → 예약자 정보 입력 화면(.../items/7845950/confirmation)
  → URL의 'confirmation'이 완료 URL 조각 'confirm'에 부분일치 → "예약 완료"로 기록
  → 확정 버튼은 누른 적도 없고, 성공 기록 때문에 이후 자동예약이 통째로 멈춤

확인하는 것:
  1) 확인/동의 단계(confirmation)는 완료가 아니다 — URL로도, 버튼 문구로도
  2) 진짜 완료 화면(예약번호·"예약이 확정되었습니다")만 완료다
  3) try_book 전체 흐름에서 확정 버튼을 눌러야만 성공이 나오고, 예약번호가 기록된다

사용법: python auto_book_success_test.py
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
            self.send_response(200 if body is not None else 404)
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


# 1단계: 날짜·시간 선택 화면 (네이버와 같은 구조: li.time_item > button.btn_time)
SELECT_HTML = """<html><head><meta charset="utf-8"></head><body>
<section class="section_calendar"><div class="calendar_area" id="cal"></div></section>
<div class="time_area"><div class="calendar_time_slot" id="times"></div></div>
<button class="NextButton__btn_next__x NextButton__disabled__y" id="next"
        onclick="location.href='items/7845950/confirmation?theme=place'">다음</button>
<script>
var h = '<div class="calendar_month"><div class="calendar_title">2026.8</div>' +
        '<table class="calendar_table"><tbody class="calendar_body"><tr>';
for (var d = 1; d <= 28; d++) {
  h += '<td class="calendar_cell"><button class="calendar_date" data-date="2026-08-' +
       String(d).padStart(2, '0') + '" onclick="pickDate(this)"><span class="num">' +
       d + '</span></button></td>';
  if (d % 7 === 0) h += '</tr><tr>';
}
document.getElementById('cal').innerHTML = h + '</tr></tbody></table></div>';
function pickDate(b) {
  document.querySelectorAll('[aria-selected]').forEach(function (e) {
    e.removeAttribute('aria-selected'); });
  b.setAttribute('aria-selected', 'true');
  document.getElementById('times').innerHTML =
    '<ul class="time_list"><li class="time_item">' +
    '<button class="btn_time" onclick="pickTime(this)">오후 12:30</button></li></ul>';
}
function pickTime(b) {
  b.className = 'btn_time selected';
  document.getElementById('next').className = 'NextButton__btn_next__x';
}
</script></body></html>"""

# 2단계: 예약자 정보 입력·동의 화면. 실제 네이버처럼 상단 진행 표시에 '예약완료'가
# 들어 있고 URL에도 confirmation이 들어간다 — 둘 다 완료 신호가 아니다.
CONFIRM_HTML = """<html><head><meta charset="utf-8"></head><body>
<ol class="step"><li>예약정보</li><li>예약자정보</li><li>예약완료</li></ol>
<h2>예약자 정보</h2>
<p>2026. 8. 16.(토) 오후 12:30 · 2명</p>
<label><input type="checkbox" class="agree">개인정보 수집에 동의합니다</label>
<button class="BookingConfirm__btn_confirm__a"
        onclick="location.href='/bookings/98765432'">동의하고 예약</button>
</body></html>"""

# 3단계: 진짜 완료 화면
DONE_HTML = """<html><head><meta charset="utf-8"></head><body>
<h2>예약이 확정되었습니다</h2>
<p>예약번호 98765432</p>
<p>2026. 8. 16.(토) 오후 12:30 · 2명</p>
</body></html>"""


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

    print("1) URL 패턴 단위 판정")
    check(not auto_book._SUCCESS_PATH_RE.search(
        "/booking/12/bizes/1693898/items/7845950/confirmation"),
        "확인·동의 단계(/confirmation)는 완료 URL이 아니다 (2026-08-11 오판 지점)")
    check(not auto_book._SUCCESS_PATH_RE.search("/booking/12/bizes/1693898/items/7845950"),
          "예약 선택 화면도 완료 URL이 아니다")
    for done_path in ("/bookings/98765432", "/booking/12/complete", "/my/booking/receipt"):
        check(bool(auto_book._SUCCESS_PATH_RE.search(done_path)),
              f"진짜 완료 경로는 잡는다 ({done_path})")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)

        def open_page(html):
            page = browser.new_page()
            page.set_content(html)
            return page

        print("2) 화면 문구 단위 판정")
        page = open_page(CONFIRM_HTML)
        check(auto_book._success_evidence(page) is None,
              "'예약완료' 단계 표시와 '동의하고 예약' 버튼만으로는 완료가 아니다")

        page = open_page(DONE_HTML)
        ev = auto_book._success_evidence(page)
        check(ev is not None, f"완료 화면은 완료로 본다 ({ev})")
        check(ev and ev[1] == "98765432", f"예약번호를 뽑아낸다 ({ev and ev[1]})")

        browser.close()

    print("3) try_book 전체 흐름 — 확정 버튼을 눌러야만 성공")
    with _serve({"select.html": SELECT_HTML,
                 "items/7845950/confirmation": CONFIRM_HTML,
                 "bookings/98765432": DONE_HTML}) as base:
        res = auto_book.try_book(f"{base}/select.html", "2026-08-16", ["12:30"],
                                 count=2, cookie_str="NID_AUT=x; NID_SES=y")
        check(res["success"] is True, f"완료까지 도달 ({res['message']})")
        check(res.get("confirm_no") == "98765432",
              f"예약번호가 결과에 남는다 ({res.get('confirm_no')})")
        check("예약번호 98765432" in res["message"], f"메시지에도 남는다 ({res['message']})")
        check(bool(res.get("evidence")), f"완료로 본 근거가 남는다 ({res.get('evidence')})")

    print("4) 확정 버튼이 없는 확인 화면에서 멈춘다 (성공으로 오보고하지 않음)")
    stuck = CONFIRM_HTML.replace("동의하고 예약", "예약자 정보를 입력하세요")
    with _serve({"select.html": SELECT_HTML,
                 "items/7845950/confirmation": stuck}) as base:
        res = auto_book.try_book(f"{base}/select.html", "2026-08-16", ["12:30"],
                                 count=2, cookie_str="NID_AUT=x; NID_SES=y",
                                 budget_sec=12)
        check(res["success"] is False,
              f"확정 못 했으면 실패로 남는다 ({res['message']})")
        check("확정" in res["message"], f"사유가 확정 단계로 남는다 ({res['message']})")

    print("5) 예약창이 닫혀도 자동예약 기록은 남는다")
    # 닫힘 처리에서 {id}: 키를 통째로 지우면 다음 회차에 sync_auto_book_state가
    # 워커 기록을 다시 넣는다 → "예약 성공 확인" 로그가 매 회차 반복된다.
    import check_booking
    alerted = {"ms4qtfmh:auto_booked": {"date": "2026-08-16"},
               "ms4qtfmh:auto_book_state": {"sig": "x"},
               "ms4qtfmh:2026-08-16": 1,
               "ms4qtfmh:url_close_streak": 2,
               "other:2026-08-16": 1}
    check_booking.purge_item_keys(alerted, "ms4qtfmh:", keep=("ms4qtfmh:url_close_streak",))
    check("ms4qtfmh:auto_booked" in alerted, "예약 성공 기록은 보존")
    check("ms4qtfmh:auto_book_state" in alerted, "자동예약 진행 상태도 보존")
    check("ms4qtfmh:2026-08-16" not in alerted, "슬롯 알림 상태는 종전대로 비운다")
    check("other:2026-08-16" in alerted, "다른 항목은 건드리지 않는다")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
