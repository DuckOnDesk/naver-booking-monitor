"""네이버 예약 자동 예약(취소표 잡기) 모듈.

check_booking.py가 예약 가능 슬롯을 감지하면 이 모듈로 실제 예약을 시도한다.
Playwright로 예약 페이지에 로그인 쿠키를 실어 접속 → 날짜 선택 → 시간 선택
→ 인원 확인 → 동의/확인 버튼 클릭까지 자동 진행.

필수 환경변수:
  NAVER_COOKIES       로그인된 네이버 쿠키 문자열 (NID_AUT, NID_SES 포함 필수)
선택 환경변수:
  AUTO_BOOK_DRY_RUN   "1"이면 최종 확정 버튼 직전까지만 진행 (테스트용)
  AUTO_BOOK_CHROMIUM  chromium 실행 파일 경로 override (로컬 테스트용)

결과는 dict로 반환:
  {"success": bool, "message": str, "booked_time": str|None,
   "dry_run": bool, "screenshots": [경로...]}
"""

import os
import re
import time as time_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

SHOT_DIR = Path(__file__).parent / "auto_book_shots"

# 예약 완료로 판정하는 텍스트/URL 패턴
_SUCCESS_TEXT = ["예약이 완료", "예약 완료", "신청이 완료", "예약이 확정", "결제가 완료"]
_SUCCESS_URL = ["bookings", "complete", "done"]

# 단계 진행 버튼 후보 (우선순위 순)
_NEXT_BUTTON_TEXTS = ["동의하고 예약", "예약하기", "바로예약", "예약 신청", "신청하기", "다음", "확인"]
_FINAL_BUTTON_TEXTS = ["동의하고 예약", "결제하기", "예약 신청", "예약하기", "신청하기", "확인"]

KST = timezone(timedelta(hours=9))


def _log(msg: str) -> None:
    print(f"  [자동예약] {msg}", flush=True)


def _shot(page, tag: str, shots: list) -> None:
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        path = SHOT_DIR / f"{datetime.now(KST).strftime('%m%d_%H%M%S')}_{tag}.png"
        page.screenshot(path=str(path), full_page=True)
        shots.append(str(path))
    except Exception:
        pass


def _parse_cookies(cookie_str: str) -> list:
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".naver.com",
                "path": "/",
            })
    return cookies


def _is_login_page(page) -> bool:
    return "nid.naver.com" in page.url


def _with_start_date(url: str, datekey: str) -> str:
    """URL의 startDate/startDateTime 쿼리를 대상 날짜로 교체 (달력이 해당 날짜 기준으로 열리도록)."""
    try:
        parts = urlparse(url)
        q = parse_qs(parts.query)
        q.pop("startDateTime", None)
        q["startDate"] = [datekey]
        return urlunparse(parts._replace(query=urlencode(q, doseq=True)))
    except Exception:
        return url


def _dump_dom_debug(page, tag: str) -> None:
    """셀렉터 디버깅용 DOM 요약을 로그로 출력하고 HTML을 스크린샷 폴더에 저장."""
    try:
        _log(f"--- DOM 디버그 ({tag}) ---")
        _log(f"URL: {page.url}")
        info = page.evaluate(
            """() => {
                const pick = (els, n) => Array.from(els).slice(0, n).map(e => ({
                    tag: e.tagName, cls: (e.className || '').toString().slice(0, 80),
                    txt: (e.innerText || '').trim().replace(/\\s+/g, '|').slice(0, 40),
                    dis: e.disabled || e.getAttribute('aria-disabled') || ''
                }));
                return {
                    calendarish: pick(document.querySelectorAll('[class*=calendar i], [class*=date i], table td'), 15),
                    buttons: pick(document.querySelectorAll('button, a[role=button]'), 40),
                };
            }"""
        )
        for group, items in info.items():
            _log(f"[{group}]")
            for it in items:
                _log(f"  <{it['tag']}> cls={it['cls']!r} dis={it['dis']!r} txt={it['txt']!r}")
        SHOT_DIR.mkdir(exist_ok=True)
        (SHOT_DIR / f"{datetime.now(KST).strftime('%m%d_%H%M%S')}_{tag}.html").write_text(
            page.content(), encoding="utf-8")
    except Exception as exc:
        _log(f"DOM 디버그 실패: {exc}")


def _click_if_found(page, locator, timeout_ms: int = 2000) -> bool:
    try:
        locator.first.click(timeout=timeout_ms)
        return True
    except Exception:
        return False


def _select_date(page, datekey: str) -> bool:
    """달력에서 datekey(YYYY-MM-DD) 날짜 클릭. 다음달 이동 최대 6회 시도."""
    target = datetime.strptime(datekey, "%Y-%m-%d")
    day = str(target.day)

    def _try_click_day() -> bool:
        # 1) aria-label에 날짜가 들어간 버튼 (예: "7월 11일", "2026년 7월 11일")
        for pat in (f"{target.month}월 {target.day}일", datekey):
            loc = page.locator(f'[class*=calendar] [aria-label*="{pat}"]:not([disabled])')
            if loc.count() and _click_if_found(page, loc):
                return True
            loc = page.locator(f'[aria-label*="{pat}"]:not([disabled]):not([aria-disabled="true"])')
            if loc.count() and _click_if_found(page, loc):
                return True
        # 2) 달력 영역 내 날짜 숫자와 정확히 일치하는 활성 버튼/셀
        for sel in ('[class*=calendar] button:not([disabled])', "table td button:not([disabled])",
                    '[class*=calendar] td:not([class*=disable]) button', '[class*=Calendar] button:not([disabled])'):
            loc = page.locator(sel).filter(has_text=re.compile(rf"^\s*{day}\s*$"))
            if loc.count() and _click_if_found(page, loc):
                return True
        return False

    def _month_visible() -> bool:
        # 달력 헤더에 대상 연/월 표기가 보이는지 ("2026.07", "7월", "2026년 7월" 등)
        pats = [f"{target.year}.{target.month:02d}", f"{target.year}. {target.month:02d}",
                f"{target.year}년 {target.month}월", f"{target.month}월"]
        try:
            header = page.locator('[class*=calendar], [class*=Calendar]').first.inner_text(timeout=3000)
        except Exception:
            return True  # 헤더를 못 읽으면 그냥 클릭 시도
        return any(p in header for p in pats)

    for _ in range(7):
        if _month_visible() and _try_click_day():
            page.wait_for_timeout(1500)
            return True
        # 다음 달 이동 버튼
        moved = False
        for sel in ('[class*=calendar] button[class*=next]', 'button[class*=next]',
                    '[class*=calendar] [aria-label*="다음"]', '[aria-label*="다음 달"]'):
            if _click_if_found(page, page.locator(sel), 1500):
                page.wait_for_timeout(1000)
                moved = True
                break
        if not moved:
            break
    return False


def _select_time(page, wanted_times: list) -> str | None:
    """wanted_times(["15:00", ...]) 중 클릭 가능한 첫 시간대 선택. 성공 시 시간 문자열 반환."""
    for t in wanted_times:
        for sel in (f'button:has-text("{t}")', f'li:has-text("{t}") button', f'[class*=time] :text("{t}")'):
            loc = page.locator(sel)
            try:
                n = loc.count()
            except Exception:
                continue
            for i in range(min(n, 5)):
                el = loc.nth(i)
                try:
                    if el.is_disabled():
                        continue
                    cls = (el.get_attribute("class") or "")
                    if "disable" in cls or "soldout" in cls or "dimmed" in cls:
                        continue
                    el.click(timeout=2000)
                    page.wait_for_timeout(1200)
                    return t
                except Exception:
                    continue
    return None


def _ensure_quantity(page, count: int) -> None:
    """인원/수량 스텝퍼가 있으면 count가 되도록 + 버튼 클릭 (기본값 유지가 안전하므로 best-effort)."""
    try:
        area = page.locator('[class*=quantity], [class*=count], [class*=stepper], [class*=people]').first
        if not area.count():
            return
        txt = area.inner_text(timeout=2000)
        m = re.search(r"\d+", txt)
        current = int(m.group(0)) if m else None
        if current is None or current >= count:
            return
        plus = area.locator('button[class*=plus], button[class*=up], button:has-text("+")')
        for _ in range(count - current):
            if not _click_if_found(page, plus, 1500):
                break
            page.wait_for_timeout(300)
    except Exception:
        pass


def _check_agreements(page) -> None:
    """약관 동의 체크박스 처리: '모두 동의'가 있으면 그것만, 없으면 미체크 박스 전부."""
    for sel in ('label:has-text("모두 동의")', 'label:has-text("전체 동의")',
                ':text("모두 동의")', ':text("전체 동의")'):
        if _click_if_found(page, page.locator(sel), 1500):
            page.wait_for_timeout(500)
            return
    try:
        boxes = page.locator('input[type=checkbox]')
        for i in range(min(boxes.count(), 10)):
            box = boxes.nth(i)
            try:
                if not box.is_checked():
                    # 네이버 UI는 input이 숨겨진 경우가 많아 label 클릭이 안전
                    box_id = box.get_attribute("id")
                    if box_id and _click_if_found(page, page.locator(f'label[for="{box_id}"]'), 1000):
                        pass
                    else:
                        box.check(timeout=1000, force=True)
                    page.wait_for_timeout(200)
            except Exception:
                continue
    except Exception:
        pass


def _click_cta(page, texts: list) -> str | None:
    """하단 진행 버튼 클릭. 클릭한 버튼 텍스트 반환."""
    for t in texts:
        loc = page.locator(f'button:has-text("{t}"), a:has-text("{t}")')
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 4)):
            el = loc.nth(i)
            try:
                if el.is_disabled():
                    continue
                cls = (el.get_attribute("class") or "")
                if "disable" in cls or "dimmed" in cls:
                    continue
                el.click(timeout=2500)
                return t
            except Exception:
                continue
    return None


def _is_success(page) -> bool:
    url = page.url.lower()
    if any(p in url for p in _SUCCESS_URL):
        return True
    try:
        body = " ".join(page.inner_text("body").split())
        return any(p in body for p in _SUCCESS_TEXT)
    except Exception:
        return False


def try_book(url: str, datekey: str, wanted_times: list, count: int = 1) -> dict:
    """예약 시도. wanted_times는 우선순위 순 시간 목록 (예: ["15:00", "16:00"])."""
    shots: list = []
    dry_run = os.environ.get("AUTO_BOOK_DRY_RUN", "").strip() in ("1", "true", "yes")
    cookie_str = os.environ.get("NAVER_COOKIES", "").strip()

    def result(success: bool, message: str, booked_time: str | None = None) -> dict:
        return {"success": success, "message": message, "booked_time": booked_time,
                "dry_run": dry_run, "screenshots": shots}

    if not cookie_str:
        return result(False, "NAVER_COOKIES 환경변수 없음 — 로그인 쿠키가 있어야 예약 가능")
    if not any(k in cookie_str for k in ("NID_AUT", "NID_SES")):
        return result(False, "NAVER_COOKIES에 NID_AUT/NID_SES 없음 — 로그인 상태 쿠키 필요")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return result(False, "playwright 미설치")

    launch_kwargs = {"headless": True}
    exe = os.environ.get("AUTO_BOOK_CHROMIUM", "").strip()
    if exe:
        launch_kwargs["executable_path"] = exe

    _log(f"예약 시도 시작: {datekey} {wanted_times} (인원 {count}){' [드라이런]' if dry_run else ''}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    locale="ko-KR",
                )
                context.add_cookies(_parse_cookies(cookie_str))
                page = context.new_page()
                page.goto(_with_start_date(url, datekey), wait_until="load", timeout=25000)
                page.wait_for_timeout(3000)

                if _is_login_page(page):
                    _shot(page, "login_required", shots)
                    return result(False, "네이버 로그인 페이지로 리다이렉트 — NAVER_COOKIES 만료됨")
                if "/error/" in page.url:
                    _shot(page, "page_closed", shots)
                    return result(False, "예약 페이지가 닫혀 있음 (에러 페이지 리다이렉트)")

                _shot(page, "01_landing", shots)

                if _select_date(page, datekey):
                    _shot(page, "02_date", shots)
                else:
                    # 날짜가 자동 선택되는 페이지(startDate 반영)일 수 있으므로 시간 선택으로 계속 진행
                    _log(f"달력에서 {datekey} 클릭 실패 — 시간대가 이미 보이는지 확인 후 계속")
                    _shot(page, "date_fail", shots)
                    _dump_dom_debug(page, "date_fail")

                booked_time = _select_time(page, wanted_times)
                if not booked_time:
                    _shot(page, "time_fail", shots)
                    _dump_dom_debug(page, "time_fail")
                    return result(False, f"시간대 {wanted_times} 중 선택 가능한 것이 없음 (이미 선점됐을 수 있음)")
                _log(f"시간대 선택: {booked_time}")
                _shot(page, "03_time", shots)

                _ensure_quantity(page, count)
                _check_agreements(page)

                clicked = _click_cta(page, _NEXT_BUTTON_TEXTS)
                if not clicked:
                    _shot(page, "cta_fail", shots)
                    _dump_dom_debug(page, "cta_fail")
                    return result(False, "예약 진행 버튼을 찾지 못함")
                _log(f"진행 버튼 클릭: '{clicked}'")
                page.wait_for_timeout(3500)
                _shot(page, "04_after_next", shots)

                if _is_login_page(page):
                    return result(False, "예약 단계에서 로그인 요구 — NAVER_COOKIES 만료됨")

                # 이미 완료됐는지 (1단계 예약인 경우)
                if _is_success(page):
                    _shot(page, "05_done", shots)
                    return result(True, f"예약 완료 ({datekey} {booked_time})", booked_time)

                # 2단계: 예약 확인/동의 페이지
                deadline = time_mod.time() + 45
                step = 0
                while time_mod.time() < deadline:
                    step += 1
                    _check_agreements(page)
                    if dry_run:
                        _shot(page, "dryrun_stop", shots)
                        final_btn = None
                        for t in _FINAL_BUTTON_TEXTS:
                            if page.locator(f'button:has-text("{t}")').count():
                                final_btn = t
                                break
                        return result(True, f"[드라이런] 최종 확정 직전 중단 — 확정 버튼: '{final_btn}'", booked_time)
                    clicked = _click_cta(page, _FINAL_BUTTON_TEXTS)
                    if clicked:
                        _log(f"확정 버튼 클릭: '{clicked}'")
                        page.wait_for_timeout(4000)
                        _shot(page, f"06_after_final_{step}", shots)
                        if _is_success(page):
                            _shot(page, "07_success", shots)
                            return result(True, f"예약 완료 ({datekey} {booked_time})", booked_time)
                        if _is_login_page(page):
                            return result(False, "확정 단계에서 로그인 요구 — NAVER_COOKIES 만료됨")
                    else:
                        page.wait_for_timeout(2000)
                        if _is_success(page):
                            _shot(page, "07_success", shots)
                            return result(True, f"예약 완료 ({datekey} {booked_time})", booked_time)

                _shot(page, "timeout", shots)
                return result(False, "확정 단계에서 완료 확인 실패 (수동 확인 필요 — 예약이 됐을 수도 있음)")
            finally:
                browser.close()
    except Exception as exc:
        return result(False, f"자동 예약 중 예외: {exc}")
