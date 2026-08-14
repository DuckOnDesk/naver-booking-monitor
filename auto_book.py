"""네이버 예약 자동 예약(취소표 잡기) 모듈.

check_booking.py가 예약 가능 슬롯을 감지하면 이 모듈로 실제 예약을 시도한다.
Playwright로 예약 페이지에 로그인 쿠키를 실어 접속 → 날짜 선택 → 시간 선택
→ 인원 확인 → 동의/확정 버튼 클릭까지 자동 진행.

날짜 안전장치: 요청한 날짜를 달력에서 확실히 고르지 못하면 시간 선택으로 넘어가지
않고 즉시 실패로 끝낸다. 확정 화면의 날짜 표기도 한 번 더 대조한다 — 예약이 안 되는
것보다 엉뚱한 날짜가 예약되는 쪽이 훨씬 나쁘기 때문이다.

계정 환경변수 (여러 계정 지원, 크롬에서 로그인 후 쿠키 복사):
  NAVER_COOKIES_1 ~ NAVER_COOKIES_5   계정별 로그인 쿠키 (NID_AUT, NID_SES 포함)
  NAVER_COOKIES                        (하위 호환) 계정1로 취급
선택 환경변수:
  AUTO_BOOK_DRY_RUN     "1"이면 최종 확정 버튼 직전까지만 진행 (테스트용)
  AUTO_BOOK_CHROMIUM    chromium 실행 파일 경로 override (로컬 테스트용)
  AUTO_BOOK_SHOTS       스크린샷 정책: fail(기본, 실패·완료만) | all(단계별 전부) | off
  AUTO_BOOK_BLOCK_ASSETS "0"이면 이미지/폰트/트래커 차단 끔 (기본 켬 — 페이지 로딩 단축)
  AUTO_BOOK_DEBUG_DOM   "1"이면 실패 시 DOM 덤프를 매번 남김 (기본: 프로세스당 1회)

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
# (네이버 완료 페이지는 "예약이 확정되었습니다" + "예약번호 …"로 표시됨)
#
# 여기가 헐거우면 피해가 크다. 완료로 오판하면 그 자리에서 확정 버튼을 안 누르고
# 끝내는 데다, 성공 기록이 남아 이후 자동예약이 통째로 멈춘다.
#   2026-08-11 TFT 스탬프투어: '다음'을 누르자 예약자 정보 입력 화면
#   (…/items/7845950/confirmation)으로 넘어갔을 뿐인데 URL의 'confirmation'이
#   아래 목록의 'confirm'에 부분일치해 "예약 완료"로 기록됐다.
# 그래서 URL은 경로에서만, 그것도 경로 조각을 통째로 맞춰본다. 확인/동의 단계
# (confirmation·agreement 등)는 완료가 아니므로 어떤 패턴에도 걸리면 안 된다.
# 공백을 지운 뒤 비교한다 ("예약이 완료" → "예약이완료").
# 문장형만 넣는다 — 단계 표시줄의 '예약완료'나 버튼의 '예약 확정' 같은 조각은
# 확인 화면에도 그대로 떠 있어서 완료 신호가 될 수 없다.
_SUCCESS_TEXT = [
    "예약이완료", "예약완료되었", "신청이완료", "예약신청이완료",
    "예약이확정", "예약확정되었", "결제가완료", "예약이접수", "예약해주셔서",
]
_SUCCESS_PATH_RE = re.compile(
    r"/(?:bookings/\d+|booking-?complete|completed?|done|receipt)(?:/|$)"
)
# 완료 근거를 브라우저 안에서 한 번에 뽑는다 (문구 + 예약번호)
_SUCCESS_JS = """(pats) => {
    const raw = document.body ? document.body.textContent || '' : '';
    // 번호는 원문에서 뽑는다 — 공백을 지운 뒤 찾으면 뒷줄 숫자까지 붙어 나온다
    const m = raw.match(/예약\\s*번호\\s*[:.]?\\s*([0-9]{4,})/);
    const t = raw.replace(/\\s+/g, '');
    return {text: pats.find(p => t.includes(p)) || '', no: m ? m[1] : ''};
}"""

# 단계 진행 버튼 후보 (우선순위 순)
_NEXT_BUTTON_TEXTS = ["동의하고 예약", "예약하기", "바로예약", "예약 신청", "신청하기", "다음", "확인"]
_FINAL_BUTTON_TEXTS = ["동의하고 예약", "결제하기", "예약 신청", "예약하기", "신청하기", "다음", "확인"]

# CTA로 오인하면 안 되는 버튼 — 클래스에 포함되면 제외
#   tab: 예약하기/상세정보/리뷰 탭 (제출 버튼 아님)
#   btn_time/calendar/count: 시간·날짜·수량 컨트롤
#   alert/btn_top/anchor: 알림받기/맨위로/로그인 등
_CTA_EXCLUDE_CLASS = ("tab", "disable", "dimmed", "btn_time", "calendar",
                      "count__", "btn_top", "alert", "footer__anchor")
# 텍스트에 포함되면 제외 (알림받기 등)
_CTA_EXCLUDE_TEXT = ("알림받기", "상세정보", "리뷰", "맨위로", "로그인", "수량")

KST = timezone(timedelta(hours=9))

# 취소표는 몇 초 만에 사라지므로 속도가 곧 성공률이다. 아래 상수들은
# "느려서 놓치는" 구간(전체 페이지 렌더 대기, 고정 sleep, 불필요한 스크린샷)을
# 줄이기 위한 것으로, 모두 조건 확인 후 즉시 진행하는 형태다.

# 스크린샷 정책 — 전체 페이지 캡처는 장당 1~2초라 단계별로 찍으면 그것만 10초가 된다.
SHOT_MODE = os.environ.get("AUTO_BOOK_SHOTS", "fail").strip().lower()
# 예약 흐름에 필요 없는 리소스 (이미지·폰트·광고/로그 수집)는 아예 받지 않는다.
BLOCK_ASSETS = os.environ.get("AUTO_BOOK_BLOCK_ASSETS", "1").strip() not in ("0", "false", "no")
_BLOCKED_TYPES = {"image", "media", "font"}
_BLOCKED_URL_PARTS = (
    "google-analytics", "googletagmanager", "doubleclick", "googlesyndication",
    "facebook.net", "connect.facebook", "criteo", "adsystem", "adservice",
    "wcs.naver.net", "nlog.naver.com", "siape.veta.naver.com", "ssl.pstatic.net/tveta",
)
# 시간대/달력 UI가 그려졌는지 판단하는 셀렉터 (고정 sleep 대신 이게 보이면 바로 진행)
_TIME_UI_SELECTOR = '[class*=time i] button, [class*=time i] a, li button:has-text(":")'
_CALENDAR_SELECTOR = '[class*=calendar i], [class*=Calendar]'

_dom_dumped = False   # DOM 덤프는 진단용이라 계정마다 반복할 필요가 없다


def _log(msg: str) -> None:
    print(f"  [자동예약] {msg}", flush=True)


def _shot(page, tag: str, shots: list, always: bool = False) -> None:
    """스크린샷 저장. always=True(실패·완료 시점)가 아니면 AUTO_BOOK_SHOTS=all일 때만."""
    if SHOT_MODE == "off" or (SHOT_MODE != "all" and not always):
        return
    try:
        SHOT_DIR.mkdir(exist_ok=True)
        path = SHOT_DIR / f"{datetime.now(KST).strftime('%m%d_%H%M%S')}_{tag}.png"
        page.screenshot(path=str(path), full_page=True)
        shots.append(str(path))
    except Exception:
        pass


def _install_fast_routes(context) -> None:
    """예약에 필요 없는 리소스를 차단해 페이지 로딩을 앞당긴다.

    CSS·JS는 그대로 둔다 (버튼 표시/활성 판정이 스타일에 걸려 있어서 끊으면 위험).
    """
    if not BLOCK_ASSETS:
        return

    def handler(route):
        try:
            req = route.request
            if req.resource_type in _BLOCKED_TYPES or any(p in req.url for p in _BLOCKED_URL_PARTS):
                route.abort()
                return
            route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    try:
        context.route("**/*", handler)
    except Exception as exc:
        _log(f"리소스 차단 설정 실패(무시하고 진행): {exc}")


def _wait_any(page, selector: str, timeout_ms: int) -> bool:
    """selector가 하나라도 붙으면 즉시 True. 없으면 timeout까지 기다렸다 False."""
    try:
        page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
        return True
    except Exception:
        return False


# 예약 UI가 "실제로 그려졌는지" 판정하는 스크립트.
#   - 껍데기만 있는 <div class="calendar_area"></div>는 SSR HTML에 처음부터 들어 있어서
#     요소 부착(attached)만 기다리면 0초에 통과해 버린다. 실제로 그것 때문에
#     달력이 비어 있는 상태로 진행해 "달력 없음 → 시간대 없음"으로 3초 만에 실패했다.
#   - 그래서 날짜 칸(숫자 셀) 또는 시간대 버튼이 눈에 보일 때까지 기다린다.
_JS_UI_READY = r"""() => {
    const txt = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();
    const visible = (el) => el.getClientRects().length > 0;
    let days = 0;
    for (const el of document.querySelectorAll(
            '[class*="calendar" i] button,[class*="calendar" i] td,[class*="calendar" i] li')) {
        if (/^\d{1,2}$/.test(txt(el)) || /^\d{1,2}$/.test(txt(el.firstElementChild || el))) {
            if (visible(el)) days++;
        }
    }
    let times = 0;
    for (const el of document.querySelectorAll('button,a,li')) {
        if (/\d{1,2}\s*:\s*\d{2}/.test(txt(el)) && visible(el)) times++;
    }
    return {days, times, ready: days >= 10 || times >= 1};
}"""


def _wait_booking_ui(page, timeout_ms: int = 8000) -> bool:
    """달력 날짜 칸이나 시간대 버튼이 실제로 그려질 때까지 기다린다 (준비되면 즉시 반환)."""
    def ready() -> bool:
        try:
            return bool((page.evaluate(_JS_UI_READY) or {}).get("ready"))
        except Exception:
            return False

    if _poll_until(page, ready, timeout_ms, 150):
        return True
    # 구조가 다른 페이지(달력·시간대가 없는 단일 상품 등)일 수 있으므로 실패로 끊지는 않는다
    _log(f"예약 UI가 {timeout_ms / 1000:.0f}초 안에 그려지지 않음 — 현재 상태로 계속 진행")
    return False


def get_accounts(priority: list | None = None) -> list:
    """사용 가능한 (계정번호, 쿠키) 목록. priority가 주어지면 그 순서·그 계정만 사용.

    NAVER_COOKIES_1~5 환경변수에서 읽고, 없으면 NAVER_COOKIES를 계정 1로 취급."""
    accounts = []
    for i in range(1, 6):
        c = os.environ.get(f"NAVER_COOKIES_{i}", "").strip()
        if c:
            accounts.append((i, c))
    if not accounts:
        c = os.environ.get("NAVER_COOKIES", "").strip()
        if c:
            accounts.append((1, c))
    if priority:
        by_id = dict(accounts)
        ordered = [(int(i), by_id[int(i)]) for i in priority if int(i) in by_id]
        if ordered:
            return ordered
    return accounts


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
    """URL의 startDate/startDateTime 쿼리를 대상 날짜로 교체 (달력이 해당 날짜 기준으로 열리도록).

    예전에는 startDateTime을 지우기만 했는데, 네이버 예약 링크(booking.naver.com/booking/5/...)는
    달력의 기준 월을 startDateTime으로 잡는 경우가 많아 지워 버리면 "오늘" 기준 달이 열렸다.
    두 파라미터를 모두 대상 날짜로 맞춰 준다.
    """
    try:
        parts = urlparse(url)
        q = parse_qs(parts.query)
        q["startDate"] = [datekey]
        if "startDateTime" in q or "endDateTime" in q:
            q["startDateTime"] = [f"{datekey}T00:00:00+09:00"]
            q.pop("endDateTime", None)   # 대상 날짜보다 앞선 종료일이 남아 있으면 범위가 어긋난다
        return urlunparse(parts._replace(query=urlencode(q, doseq=True)))
    except Exception:
        return url


def _dump_dom_debug(page, tag: str) -> None:
    """셀렉터 디버깅용 DOM 요약을 로그로 출력하고 HTML을 스크린샷 폴더에 저장.

    page.content()가 1~3초씩 걸려 계정마다 반복하면 다음 계정 시도가 그만큼
    늦어진다. 진단에는 한 번이면 충분하므로 프로세스당 1회만 남긴다
    (AUTO_BOOK_DEBUG_DOM=1이면 매번).
    """
    global _dom_dumped
    if _dom_dumped and os.environ.get("AUTO_BOOK_DEBUG_DOM", "").strip() not in ("1", "true", "yes"):
        _log(f"DOM 디버그 생략 ({tag}) — 이번 실행에서 이미 남김")
        return
    _dom_dumped = True
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
                const notCal = Array.from(document.querySelectorAll('button, a[role=button], li'))
                    .filter(e => !(e.className || '').toString().includes('calendar'));
                return {
                    timeish: pick(document.querySelectorAll('[class*=time i], [class*=Time]'), 25),
                    calendarish: pick(document.querySelectorAll('[class*=calendar i]'), 8),
                    noncal_buttons: pick(notCal, 60),
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


def _poll_until(page, check, cap_ms: int, step_ms: int = 200) -> bool:
    """조건이 참이 되면 즉시 True (고정 sleep 대신 사용). cap_ms까지만 기다린다."""
    deadline = time_mod.time() + cap_ms / 1000
    while True:
        try:
            if check():
                return True
        except Exception:
            pass
        if time_mod.time() >= deadline:
            return False
        page.wait_for_timeout(step_ms)


def _has_any_button(page, texts: list) -> bool:
    """texts 중 하나가 들어간 "진짜" CTA가 화면에 있는지 — 한 번의 evaluate로 확인.

    탭("예약하기" 탭처럼 같은 글자를 쓰는 요소)·비활성·아직 숨겨진 버튼은 제외한다.
    이걸 빼면 탭 하나 때문에 항상 참이 돼서, 이 함수로 기다리는 의미가 없어진다
    (실제로 CTA가 나타나기 전에 클릭을 시도해 실패했다).
    """
    try:
        return bool(page.evaluate(
            """([texts, badClass, badText]) =>
                Array.from(document.querySelectorAll('button, a')).some(b => {
                    const cls = (b.className || '').toString().toLowerCase();
                    if (badClass.some(x => cls.includes(x))) return false;
                    const t = (b.textContent || '').trim();
                    if (badText.some(x => t.includes(x))) return false;
                    if (b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
                    if (!b.getClientRects().length) return false;   // 아직 안 보이는 버튼
                    return texts.some(x => t.includes(x));
                })""",
            [texts, [c.lower() for c in _CTA_EXCLUDE_CLASS], list(_CTA_EXCLUDE_TEXT)],
        ))
    except Exception:
        return False


def _wait_next_after_time(page) -> None:
    """시간 선택 후 진행 버튼이 뜨면 즉시 다음 단계로 (고정 1.2초 대기 대체)."""
    page.wait_for_timeout(250)
    _poll_until(page, lambda: _has_any_button(page, _NEXT_BUTTON_TEXTS), 950, 150)


def _click_if_found(page, locator, timeout_ms: int = 2000) -> bool:
    try:
        locator.first.click(timeout=timeout_ms)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 날짜 선택
#
# 예전 구현은 "대상 월이 보이는가"를 헤더 문자열 부분일치로 판단하고, 아니면
# '다음 달'을 눌렀다. 그런데 (1) 날짜 클릭이 실패해도 다음 달로 넘어가고
# (2) "2월"이 "12월"에도 부분일치하며 (3) 헤더를 못 읽으면 무조건 클릭을 시도해서,
# 2026-07을 요청해도 다음 달 버튼을 7번 눌러 2027-02 달력을 보는 일이 생겼다.
# 그 상태로 시간 선택까지 그대로 진행됐으니 "엉뚱한 날짜 예약" 위험도 있었다.
#
# 지금은 달력을 브라우저 안에서 한 번에 훑어(_JS_SCAN_CALENDAR):
#   - 표시 중인 연/월을 실제로 파싱하고
#   - 날짜 칸이 어느 달에 속하는지 확인한 뒤 대상 날짜 칸만 찍고
#   - 대상 월이 이미 보이는데 그 날짜를 못 고르면 달 이동을 하지 않는다.
# 이동이 필요할 때만, 필요한 방향으로, 필요한 횟수만큼 움직인다.
# ---------------------------------------------------------------------------

_JS_SCAN_CALENDAR = r"""(target) => {
    const [ty, tm, td] = target.split('-').map(Number);
    const txt = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim();

    // --- 달력 루트: 날짜 숫자 칸을 가장 많이 품은 요소 (같으면 바깥쪽) ---
    const roots = Array.from(document.querySelectorAll(
        '[class*="calendar" i],[id*="calendar" i],[data-testid*="calendar" i]'));
    const dayCount = (el) => Array.from(el.querySelectorAll('button,a,td,li,span,div'))
        .filter(e => /^\d{1,2}$/.test(txt(e))).length;
    let root = null, best = 0;
    for (const r of roots) {
        const n = dayCount(r);
        if (n > best) { best = n; root = r; }
    }
    if (!root || best < 10) return {calendar: false};

    // --- 월 헤더 파싱 ("2026.07", "2026. 7.", "2026년 7월", "7월") ---
    const parseYm = (s) => {
        let m = s.match(/(\d{4})\s*[.\-\/년]\s*(\d{1,2})\s*(?:월|[.\-\/]|$)/);
        if (m) return {y: +m[1], m: +m[2]};
        m = s.match(/(?:^|[^\d])(\d{1,2})\s*월(?!\s*[가-힣])/);
        if (m) return {y: null, m: +m[1]};
        return null;
    };
    const headers = [];
    for (const el of root.querySelectorAll('*')) {
        const s = txt(el);
        if (!s || s.length > 24) continue;
        const ym = parseYm(s);
        if (!ym || ym.m < 1 || ym.m > 12) continue;
        // 같은 표기를 가진 더 안쪽 요소가 있으면 그쪽을 헤더로 삼는다
        if (Array.from(el.children).some(c => parseYm(txt(c)))) continue;
        headers.push({el, y: ym.y, m: ym.m, label: s});
    }
    // 연도 없는 헤더("8월")는 앞선 헤더의 연도를 잇고, 그래도 없으면 오늘 기준 추정
    const now = new Date();
    let carry = null;
    for (const h of headers) { if (h.y) carry = h.y; else if (carry) h.y = carry; }
    for (const h of headers) {
        if (!h.y) h.y = h.m < now.getMonth() + 1 ? now.getFullYear() + 1 : now.getFullYear();
    }
    const monthOf = (el) => {
        let found = null;   // 문서 순서상 이 칸보다 앞에 있는 마지막 헤더
        for (const h of headers) {
            if (h.el.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) found = h;
        }
        return found;
    };

    // --- 날짜 칸의 날짜 읽기: 속성 우선, 없으면 숫자 텍스트 + 소속 월 ---
    const DATE_ATTRS = ['data-date', 'data-datekey', 'data-value', 'data-day',
                        'data-testid', 'aria-label', 'title'];
    const attrDate = (el) => {
        if (!el.getAttribute) return null;
        for (const a of DATE_ATTRS) {
            const v = el.getAttribute(a);
            if (!v) continue;
            let m = v.match(/(\d{4})[.\-\/](\d{1,2})[.\-\/](\d{1,2})/);
            if (m) return [+m[1], +m[2], +m[3]];
            m = v.match(/(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일/);
            if (m) return [+m[1], +m[2], +m[3]];
            m = v.match(/(\d{1,2})월\s*(\d{1,2})일/);
            if (m) return [null, +m[1], +m[2]];
        }
        return null;
    };
    const BAD = /disable|dimmed|soldout|sold_out|unselectable|blocked|closed|impossible|past|off\b/;
    const isDisabled = (el) => {
        let e = el;
        for (let i = 0; i < 4 && e; i++, e = e.parentElement) {
            if (e.disabled) return true;
            if (e.getAttribute && e.getAttribute('aria-disabled') === 'true') return true;
            if (BAD.test((e.className || '').toString().toLowerCase())) return true;
        }
        return false;
    };

    // 날짜 칸의 "일" 숫자. 칸 안에 배지/가격 같은 텍스트가 더 있어도(예: "1 예약가능")
    // 숫자만 든 말단 요소가 정확히 하나면 그것을 날짜로 본다.
    const isNum = (el) => /^\d{1,2}$/.test(txt(el));
    const dayNum = (el) => {
        const s = txt(el);
        if (/^\d{1,2}$/.test(s)) return +s;
        if (!s || s.length > 40) return null;
        const leaves = Array.from(el.querySelectorAll('*')).filter(
            e => isNum(e) && !Array.from(e.children).some(isNum));
        return leaves.length === 1 ? +txt(leaves[0]) : null;
    };

    let picked = null, sawDay = false, disabledDay = false;
    for (const el of root.querySelectorAll('button,a,td,li,[role="button"],[role="gridcell"]')) {
        let y = null, mo = null, d = null;
        const ad = attrDate(el);
        if (ad) { y = ad[0]; mo = ad[1]; d = ad[2]; }
        if (d === null) {
            d = dayNum(el);
            if (d === null) continue;
            const h = monthOf(el);
            if (!h) continue;          // 어느 달인지 모르는 칸은 절대 클릭하지 않는다
            y = h.y; mo = h.m;
        } else if (y === null) {
            const h = monthOf(el);
            y = h ? h.y : ty;
        }
        if (mo !== tm || d !== td || y !== ty) continue;
        if (!el.getClientRects().length) continue;
        sawDay = true;
        if (isDisabled(el)) { disabledDay = true; continue; }
        picked = el;
        break;
    }
    if (picked) {   // td/li를 잡았으면 실제 클릭 대상인 안쪽 버튼으로 내려간다
        const inner = picked.querySelector('button,a,[role="button"]');
        if (inner && inner.getClientRects().length) picked = inner;
        document.querySelectorAll('[data-ab-pick]').forEach(e => e.removeAttribute('data-ab-pick'));
        picked.setAttribute('data-ab-pick', '1');
    }
    return {
        calendar: true,
        found: !!picked,
        sawDay, disabledDay,
        months: headers.map(h => ({y: h.y, m: h.m})),
        header: headers.map(h => h.label).join(' / ') || txt(root).slice(0, 40),
    };
}"""

# 선택된 날짜 칸을 읽어 "지금 무슨 날짜가 선택돼 있는지" 확인 (엉뚱한 날짜 방지용).
# aria-current는 '오늘'을 표시하는 데도 쓰여 선택과 헷갈리므로 보지 않는다.
_JS_SELECTED_DATES = r"""() => {
    const out = [];
    for (const el of document.querySelectorAll('[aria-selected="true"],[aria-pressed="true"]')) {
        for (const a of ['data-date', 'data-datekey', 'data-value', 'aria-label', 'title']) {
            const v = el.getAttribute(a);
            if (!v) continue;
            const m = v.match(/(\d{4})[.\-\/](\d{1,2})[.\-\/](\d{1,2})/)
                   || v.match(/(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일/);
            if (m) { out.push([+m[1], +m[2], +m[3]]); break; }
        }
    }
    return out.slice(0, 5);
}"""


def _scan_calendar(page, datekey: str) -> dict:
    try:
        return page.evaluate(_JS_SCAN_CALENDAR, datekey) or {}
    except Exception as exc:
        _log(f"달력 파싱 실패: {exc}")
        return {}


def _selected_conflict(page, datekey: str) -> str | None:
    """달력에서 '선택됨'으로 표시된 날짜가 대상과 다르면 그 사유 문자열을 돌려준다."""
    target = datetime.strptime(datekey, "%Y-%m-%d")
    try:
        picked = page.evaluate(_JS_SELECTED_DATES) or []
    except Exception:
        return None
    full = [p for p in picked if p[0] and p[1]]
    if not full:
        return None
    if any(p[0] == target.year and p[1] == target.month and p[2] == target.day for p in full):
        return None
    shown = ", ".join(f"{p[0]}-{p[1]:02d}-{p[2]:02d}" for p in full[:3])
    return f"달력에 선택된 날짜가 다름 (선택됨: {shown})"


def _months_label(months: list) -> str:
    return ", ".join(f"{m['y']}년 {m['m']}월" for m in months[:4]) or "(월 표기 없음)"


def _move_month(page, forward: bool) -> bool:
    """달력을 다음/이전 달로 이동. 클릭에 성공하면 True."""
    if forward:
        sels = ('[class*="calendar" i] button[class*="next" i]', 'button[class*="next" i]',
                '[class*="calendar" i] [aria-label*="다음"]', '[aria-label*="다음 달"]',
                '[aria-label*="다음달"]')
    else:
        sels = ('[class*="calendar" i] button[class*="prev" i]', 'button[class*="prev" i]',
                '[class*="calendar" i] [aria-label*="이전"]', '[aria-label*="이전 달"]',
                '[aria-label*="이전달"]')
    for sel in sels:
        loc = page.locator(sel)
        try:
            if not loc.count():
                continue
        except Exception:
            continue
        if _click_if_found(page, loc, 1500):
            page.wait_for_timeout(400)
            return True
    return False


def _select_date(page, datekey: str) -> tuple[bool, str]:
    """달력에서 datekey(YYYY-MM-DD)를 선택한다.

    반환: (성공 여부, 사유). 실패 사유가 "no_calendar"면 달력이 없는 페이지라는 뜻이고,
    그 외에는 호출부가 그대로 실패 메시지로 쓸 수 있는 설명이다.
    대상 월로 갈 수 없거나 그 날짜를 고를 수 없으면 **다른 달을 헤매지 않고** 실패로 끝낸다.
    """
    target = datetime.strptime(datekey, "%Y-%m-%d")
    tkey = target.year * 12 + target.month

    state = _scan_calendar(page, datekey)
    if not state.get("calendar"):
        # 달력이 늦게 그려지는 경우가 있어 한 번 더 짧게 기다려 본다.
        # (예전에는 여기서 곧바로 "달력 없음"으로 판정해 빈 페이지에서 예약을 포기했다)
        if _poll_until(page, lambda: bool(_scan_calendar(page, datekey).get("calendar")), 2500, 300):
            state = _scan_calendar(page, datekey)
        else:
            return False, "no_calendar"

    # 이동 한도는 처음 본 달과의 실제 거리(+여유 2)로 잡는다. 예전처럼 무조건 7번씩
    # 넘기면 도달 못 할 달을 향해 헛돌며 시간만 쓴다 (그리고 엉뚱한 달에서 끝났다).
    budget = None
    moved = 0
    while True:
        months = state.get("months") or []
        header = state.get("header") or ""
        if state.get("found"):
            loc = page.locator('[data-ab-pick="1"]')
            if not _click_if_found(page, loc, 2500):
                return False, f"{datekey} 날짜 칸 클릭 실패 (표시 중: {header})"
            # 시간대 목록이 다시 그려질 시간만 주고, 준비되면 바로 진행
            page.wait_for_timeout(500)
            _wait_any(page, _TIME_UI_SELECTOR, 1200)
            conflict = _selected_conflict(page, datekey)
            if conflict:
                return False, conflict
            _log(f"날짜 선택 완료: {datekey} (달력 표시: {header})")
            return True, "ok"

        shown = [m["y"] * 12 + m["m"] for m in months if m.get("y") and m.get("m")]
        if tkey in shown:
            # 대상 월은 이미 보인다 → 달을 넘겨도 그 날짜가 나올 리 없다.
            # (예전 코드가 여기서 다음 달로 넘어가며 엉뚱한 달까지 밀려났다)
            why = "품절/비활성" if state.get("disabledDay") else "달력에 없음"
            return False, f"{datekey}를 선택할 수 없음 — {why} (표시 중: {header})"
        if not shown:
            return False, f"달력의 연/월 표기를 읽지 못해 날짜를 특정할 수 없음 (표시 중: {header})"

        # 필요한 방향으로만 한 달씩 이동 (가장 가까운 표시 월 기준)
        nearest = min(shown, key=lambda k: abs(k - tkey))
        forward = tkey > nearest
        if budget is None:
            budget = min(14, abs(tkey - nearest) + 2)
        if moved >= budget:
            return False, (f"달력 이동 {moved}회로도 {target.year}년 {target.month}월에 도달하지 못함 "
                           f"(표시 중: {_months_label(months)})")
        moved += 1
        if not _move_month(page, forward):
            return False, (f"달력을 {target.year}년 {target.month}월로 이동하지 못함 "
                           f"({'다음' if forward else '이전'} 달 버튼 없음, 표시 중: {_months_label(months)})")
        state = _scan_calendar(page, datekey)
        new_shown = [m["y"] * 12 + m["m"] for m in (state.get("months") or []) if m.get("y") and m.get("m")]
        if new_shown == shown:
            return False, (f"달력이 {target.year}년 {target.month}월로 넘어가지 않음 "
                           f"(표시 중: {_months_label(months)})")


def _page_date_conflict(page, datekey: str) -> str | None:
    """확정 화면에 적힌 예약 날짜가 대상과 다르면 사유를 돌려준다 (마지막 안전장치).

    연도까지 있는 날짜 표기를 모두 긁어, 그중 대상 날짜가 하나도 없을 때만 불일치로 본다.
    (취소 기한·약관 개정일 같은 다른 날짜와의 공존은 허용하고, 연도 없이 "8월 1일"로만
    적힌 화면도 통과시킨다 — 확실한 불일치가 아니면 예약을 막지 않는다.)
    """
    target = datetime.strptime(datekey, "%Y-%m-%d")
    try:
        found = page.evaluate(
            r"""() => {
                const t = document.body ? document.body.innerText || '' : '';
                const full = [], md = [];
                let m;
                const reFull = /(\d{4})\s*[.\-년]\s*(\d{1,2})\s*[.\-월]\s*(\d{1,2})/g;
                while ((m = reFull.exec(t)) !== null) full.push([+m[1], +m[2], +m[3]]);
                const reMd = /(\d{1,2})\s*월\s*(\d{1,2})\s*일/g;
                while ((m = reMd.exec(t)) !== null) md.push([+m[1], +m[2]]);
                return {full: full.slice(0, 40), md: md.slice(0, 40)};
            }"""
        ) or {}
    except Exception:
        return None
    full = found.get("full") or []
    if not full:
        return None
    if any(d[0] == target.year and d[1] == target.month and d[2] == target.day for d in full):
        return None
    # 연도 없이 "8월 1일"로만 적혀 있어도 대상 날짜가 화면에 있는 것으로 본다
    if any(d[0] == target.month and d[1] == target.day for d in (found.get("md") or [])):
        return None
    shown = ", ".join(f"{d[0]}-{d[1]:02d}-{d[2]:02d}" for d in full[:3])
    return f"확정 화면의 날짜가 요청과 다름 (화면: {shown} / 요청: {datekey})"


def _time_patterns(t: str, bare_12h: bool = True) -> list:
    """"19:30" → 페이지에 표기될 수 있는 형태들의 정규식 목록 (24시간/12시간/오전·오후).

    bare_12h=False면 오전/오후가 안 붙은 12시간 표기("7:30")는 후보에서 뺀다.
    페이지가 오전/오후를 구분해 표시하는데도 이 패턴을 쓰면, 예를 들어 22:00을
    요청했을 때 "오전 10:00" 버튼을 눌러 엉뚱한 시간을 예약할 수 있다.
    """
    h, m = t.split(":")
    h = int(h)
    pats = [rf"(?<!\d){h:02d}:{m}(?!\d)"]
    if h == 0:
        pats.append(rf"오전\s*12:{m}(?!\d)")
    elif h < 12:
        pats.append(rf"오전\s*{h}:{m}(?!\d)")
        if bare_12h:
            pats.append(rf"(?<!\d){h}:{m}(?!\d)")  # 앞자리 0 없는 표기 (예: "9:00")
    elif h == 12:
        pats.append(rf"오후\s*12:{m}(?!\d)")
    else:
        pats.append(rf"오후\s*{h - 12}:{m}(?!\d)")
        if bare_12h:
            pats.append(rf"(?<!\d){h - 12}:{m}(?!\d)")  # 12시간제 단독 표기 (예: "7:30")
    return [re.compile(p) for p in pats]


def _page_has_ampm(page) -> bool:
    """페이지가 오전/오후를 구분해 표기하는지 (모호한 12시간 매칭 허용 여부 판단용)."""
    try:
        return bool(page.evaluate(
            "() => { const t = document.body ? document.body.textContent || '' : '';"
            "        return t.includes('오전') || t.includes('오후'); }"))
    except Exception:
        return True   # 판단 불가하면 안전한 쪽(모호한 매칭 금지)으로


def _find_time_button(page, t: str):
    """페이지의 모든 클릭 가능한 버튼에서 시간대 버튼을 찾는다.
    12시간/24시간 표기를 동일하게 취급: "15:00" 요청 시 "15:00", "오후 3:00", "3:00"
    (섹션 제목이 오후이거나 오전/오후 정보가 없는 경우) 모두 매칭.
    오전/오후 판정 우선순위: 버튼 자체 텍스트 → 부모 li 텍스트 → 앞쪽 섹션 제목."""
    try:
        idx = page.evaluate(
            """(target) => {
                const [thS, tmS] = target.split(':');
                const targetH = parseInt(thS), targetM = tmS;
                const bad = /unselectable|disable|dimmed|soldout|calendar/;
                // 오전/오후 섹션 제목 후보 (텍스트가 정확히 '오전'/'오후'인 말단 요소)
                const markers = Array.from(document.querySelectorAll('div,span,strong,em,dt,h3,h4'))
                    .filter(e => { const s = (e.textContent || '').trim(); return s === '오전' || s === '오후'; });
                function ampmFor(el) {
                    const own = (el.textContent || '');
                    if (own.includes('오후')) return '오후';
                    if (own.includes('오전')) return '오전';
                    const li = el.closest('li');
                    if (li) {
                        const lt = (li.textContent || '');
                        if (lt.includes('오후')) return '오후';
                        if (lt.includes('오전')) return '오전';
                    }
                    let best = '';
                    for (const m of markers) {
                        if (m.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)
                            best = (m.textContent || '').trim();
                    }
                    return best;
                }
                const all = Array.from(document.querySelectorAll('button, a'));
                for (let i = 0; i < all.length; i++) {
                    const b = all[i];
                    const cls = (b.className || '').toString();
                    if (b.disabled || bad.test(cls)) continue;
                    if (b.getAttribute('aria-disabled') === 'true') continue;
                    const mm = (b.textContent || '').trim().match(/(\\d{1,2})\\s*:\\s*(\\d{2})/);
                    if (!mm || mm[2] !== targetM) continue;
                    const h = parseInt(mm[1]);
                    const ap = ampmFor(b);
                    let ok;
                    if (ap === '오후') {
                        ok = (h >= 13 ? h : (h % 12) + 12) === targetH;   // "오후 3:00"도 "오후 15:00"도 15시
                    } else if (ap === '오전') {
                        ok = (h === 12 ? 0 : h) === targetH;
                    } else {
                        // 오전/오후 정보 없음: 24시간제 그대로 or 12시간제로 간주해 둘 다 허용
                        ok = h === targetH || (h <= 12 && h + 12 === targetH);
                    }
                    if (ok) return i;
                }
                return -1;
            }""",
            t,
        )
    except Exception:
        return None
    if idx is None or idx < 0:
        return None
    return page.locator("button, a").nth(idx)


# 시간대 칸이 "선택 불가"인지 — 자기 자신뿐 아니라 안쪽 버튼까지 본다.
# 네이버는 <li class="time_item"><button class="btn_time unselectable">오전 11:00</button></li>
# 처럼 li에는 아무 표시가 없어서, li만 보고 클릭하면 죽은 칸을 눌러 놓고 성공으로 착각한다.
_JS_TIME_BLOCKED = r"""(sel) => {
    const el = document.querySelector(sel);
    if (!el) return true;
    const BAD = /unselectable|disable|dimmed|soldout|sold_out|impossible|blocked|closed/;
    const bad = (e) => e && ((e.className || '').toString().toLowerCase().match(BAD)
                             || e.disabled || e.getAttribute('aria-disabled') === 'true');
    if (bad(el)) return true;
    for (const inner of el.querySelectorAll('button,a,[role="button"]')) {
        if (bad(inner)) return true;
    }
    return false;
}"""

# 클릭한 시간대가 실제로 "선택됨"으로 바뀌었는지
_JS_TIME_SELECTED = r"""() => {
    const SEL = /select|active|\bon\b|checked|current|chosen/;
    for (const el of document.querySelectorAll(
            '[class*="time" i] button,[class*="time" i] a,[class*="time" i] li')) {
        if (!/\d{1,2}\s*:\s*\d{2}/.test((el.textContent || ''))) continue;
        if (el.getAttribute('aria-selected') === 'true' || el.getAttribute('aria-pressed') === 'true') return true;
        if (SEL.test((el.className || '').toString().toLowerCase())) return true;
    }
    return false;
}"""


def _time_click_took(page) -> bool:
    """시간대 클릭이 실제로 먹혔는지 — 선택 표시가 생겼거나 진행 버튼이 활성화됐으면 True.

    둘 다 아니면 그 칸은 사실상 죽은 칸이다. 예전에는 이 확인이 없어서 선택 불가
    슬롯을 누르고도 "시간대 선택 완료"라고 보고했고, 다음 단계에서 "예약 진행 버튼을
    찾지 못함"이라는 엉뚱한 사유로 실패해 계정만 3개씩 소모했다.
    """
    def ok() -> bool:
        try:
            if page.evaluate(_JS_TIME_SELECTED):
                return True
        except Exception:
            pass
        return _has_any_button(page, _NEXT_BUTTON_TEXTS)

    return _poll_until(page, ok, 1500, 150)


def _select_time(page, wanted_times: list) -> str | None:
    """wanted_times(["15:00", ...]) 중 클릭 가능한 첫 시간대 선택. 성공 시 시간 문자열 반환.

    클릭 후 선택이 실제로 반영됐는지까지 확인한다 (반영 안 되면 선택 실패로 본다).
    """
    # 1차: 12시간/24시간 표기 통합 정밀 매칭
    for t in wanted_times:
        el = _find_time_button(page, t)
        if el is not None:
            try:
                el.scroll_into_view_if_needed(timeout=1500)
                el.click(timeout=2000)
                _wait_next_after_time(page)
                if _time_click_took(page):
                    return t
                _log(f"{t} 클릭했지만 선택이 반영되지 않음 — 다른 후보를 계속 찾는다")
            except Exception:
                pass
    # 2차: 텍스트 패턴 기반 탐색 (1차 정밀 매칭이 구조 변경 등으로 실패했을 때)
    bare_12h = not _page_has_ampm(page)
    for t in wanted_times:
        for pat in _time_patterns(t, bare_12h):
            # 1) 시간 텍스트가 들어간 버튼 직접 클릭
            # 2) 시간 텍스트가 들어간 li 내부의 버튼/a 클릭
            # 3) li 자체 클릭
            candidates = [
                page.locator("button").filter(has_text=pat),
                page.locator("li").filter(has_text=pat).locator("button, a"),
                page.locator("li").filter(has_text=pat),
                page.locator("a, span[role=button], label").filter(has_text=pat),
            ]
            for loc in candidates:
                try:
                    n = loc.count()
                except Exception:
                    continue
                for i in range(min(n, 6)):
                    el = loc.nth(i)
                    try:
                        cls = (el.get_attribute("class") or "")
                        if any(k in cls for k in ("disable", "soldout", "dimmed", "unselectable", "calendar")):
                            continue
                        try:
                            if el.is_disabled():
                                continue
                        except Exception:
                            pass
                        # li처럼 겉은 멀쩡한데 안쪽 버튼이 선택 불가인 칸을 걸러 낸다
                        el.evaluate("e => e.setAttribute('data-ab-time', '1')")
                        blocked = page.evaluate(_JS_TIME_BLOCKED, '[data-ab-time="1"]')
                        el.evaluate("e => e.removeAttribute('data-ab-time')")
                        if blocked:
                            continue
                        el.scroll_into_view_if_needed(timeout=1500)
                        el.click(timeout=2000)
                        _wait_next_after_time(page)
                        if _time_click_took(page):
                            return t
                    except Exception:
                        continue
    return None


_JS_TIME_SLOTS = r"""() => {
    const BAD = /unselectable|disable|dimmed|soldout|sold_out|impossible|blocked|closed/;
    const out = [];
    for (const el of document.querySelectorAll('button,a,[role="button"]')) {
        const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
        const m = t.match(/(\d{1,2})\s*:\s*(\d{2})/);
        if (!m || !el.getClientRects().length) continue;
        const cls = (el.className || '').toString().toLowerCase();
        out.push({
            text: t.slice(0, 24),
            h: +m[1], mm: m[2],
            ampm: t.includes('오후') ? 'pm' : (t.includes('오전') ? 'am' : ''),
            blocked: !!(BAD.test(cls) || el.disabled || el.getAttribute('aria-disabled') === 'true'),
        });
    }
    return out.slice(0, 40);
}"""


def _blocked_slots(page, wanted_times: list) -> list:
    """요청한 시간대가 페이지에 "선택 불가" 상태로 떠 있으면 그 목록을 돌려준다.

    "칸 자체가 없다"(=슬롯 소멸)와 "칸은 있는데 못 누른다"(=매진·마감)를 구분하기 위한 것.
    후자는 계정을 바꿔도 결과가 같으므로 즉시 중단하고, 모니터도 잠시 쉬어야 한다.
    """
    try:
        slots = page.evaluate(_JS_TIME_SLOTS) or []
    except Exception:
        return []
    if slots:
        _log("페이지 시간대 상태: "
             + ", ".join(f"{s['text']}{'(선택불가)' if s['blocked'] else ''}" for s in slots[:8]))
    hit = []
    for t in wanted_times:
        th, tm = t.split(":")
        th = int(th)
        for s in slots:
            if s["mm"] != tm:
                continue
            h = s["h"]
            if s["ampm"] == "pm":
                h = h if h >= 13 else (h % 12) + 12
            elif s["ampm"] == "am":
                h = 0 if h == 12 else h
            if (h == th or (not s["ampm"] and h <= 12 and h + 12 == th)) and s["blocked"]:
                hit.append(t)
                break
    return hit


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
    """약관 동의 체크박스 처리: '모두 동의'가 있으면 그것만, 없으면 미체크 박스 전부.

    확정 루프에서 반복 호출되므로, 없는 셀렉터를 기다리며 시간을 버리지 않도록
    count()로 존재를 먼저 확인하고 타임아웃도 짧게 잡는다 (예전에는 '모두 동의'가
    없는 페이지에서 매번 4×1.5초를 그냥 버렸다).
    """
    for sel in ('label:has-text("모두 동의")', 'label:has-text("전체 동의")',
                ':text("모두 동의")', ':text("전체 동의")'):
        loc = page.locator(sel)
        try:
            if not loc.count():
                continue
        except Exception:
            continue
        if _click_if_found(page, loc, 800):
            page.wait_for_timeout(300)
            return
    try:
        boxes = page.locator('input[type=checkbox]')
        if not boxes.count():
            return
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


def _is_cta_button(el) -> bool:
    """제출/진행 버튼으로 볼 수 있는지 — 탭·컨트롤·알림받기 등은 제외."""
    try:
        cls = (el.get_attribute("class") or "").lower()
        if any(x in cls for x in _CTA_EXCLUDE_CLASS):
            return False
        txt = (el.inner_text() or "").strip()
        if any(x in txt for x in _CTA_EXCLUDE_TEXT):
            return False
        if el.is_disabled():
            return False
        return True
    except Exception:
        return False


def _click_cta(page, texts: list) -> str | None:
    """하단 진행 버튼 클릭. 탭/알림받기 등 가짜 버튼은 건너뛰고 진짜 CTA만 클릭.

    클릭이 곧바로 페이지 이동을 일으키면 뒤따르는 조작에서 예외가 날 수 있는데,
    그때 "버튼을 못 찾았다"고 보고하면 실제로는 진행됐는데도 예약이 중단된다.
    URL이 바뀌었으면 클릭에 성공한 것으로 본다.
    """
    try:
        before_url = page.url
    except Exception:
        before_url = ""
    for t in texts:
        loc = page.locator(f'button:has-text("{t}"), a:has-text("{t}")')
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 6)):
            el = loc.nth(i)
            try:
                # 텍스트가 정확히 t이거나 t로 시작하는 버튼 우선 (부분일치 오탐 방지)
                label = (el.inner_text() or "").strip()
                if t not in label:
                    continue
                if not _is_cta_button(el):
                    continue
                el.scroll_into_view_if_needed(timeout=1500)
                el.click(timeout=2500)
                return t
            except Exception:
                try:
                    if before_url and page.url != before_url:
                        return t     # 클릭 직후 이동 — 성공으로 처리
                except Exception:
                    pass
                continue
    try:
        if before_url and page.url != before_url:
            return "(페이지 이동 감지)"
    except Exception:
        pass
    return None


def _success_evidence(page) -> tuple[str, str] | None:
    """완료 페이지 도달 근거 (사람이 읽을 근거 문구, 예약번호). 완료가 아니면 None.

    확정 루프에서 반복 호출되므로 최대한 싸게 판정한다. page.inner_text("body")는
    문서 전체에 레이아웃 계산을 강제해 페이지가 클수록 느리므로, 판정에 충분한
    textContent를 브라우저 안에서 한 번에 매칭하고 결과만 받아온다.

    근거를 문자열로 돌려주는 건 진단용이다. "왜 완료라고 봤는지"가 로그·기록에
    남아야 이번 같은 오판을 사후에 짚을 수 있다.
    """
    try:
        path = urlparse(page.url).path.lower()
    except Exception:
        return None
    try:
        hit = page.evaluate(_SUCCESS_JS, _SUCCESS_TEXT) or {}
    except Exception:
        hit = {}
    text, no = hit.get("text") or "", hit.get("no") or ""
    if text:
        return f"완료 문구 '{text}'", no
    if no:
        return f"예약번호 {no}", no
    if _SUCCESS_PATH_RE.search(path):
        return f"완료 URL {path}", ""
    return None


def try_book(url: str, datekey: str, wanted_times: list, count: int = 1,
             cookie_str: str | None = None, account: int | None = None,
             budget_sec: float | None = None) -> dict:
    """예약 시도. wanted_times는 우선순위 순 시간 목록 (예: ["15:00", "16:00"]).

    cookie_str 미지정 시 사용 가능한 첫 계정의 쿠키 사용."""
    shots: list = []
    t0 = time_mod.time()
    dry_run = os.environ.get("AUTO_BOOK_DRY_RUN", "").strip() in ("1", "true", "yes")
    if cookie_str is None:
        accounts = get_accounts()
        if accounts:
            account, cookie_str = accounts[0]
        else:
            cookie_str = ""
    acct_label = f"계정{account}" if account else ("비로그인" if not cookie_str else "계정?")

    def result(success: bool, message: str, booked_time: str | None = None,
               unbookable: bool = False, evidence: str = "", confirm_no: str = "") -> dict:
        # elapsed: 이 계정 시도에 걸린 초. 어디서 늦어지는지 로그로 추적하기 위한 값.
        # unbookable: 페이지가 그 슬롯을 "선택 불가"로 표시 — 계정을 바꿔도, 곧 다시
        #             시도해도 결과가 같다는 신호 (워커·모니터가 백오프에 쓴다).
        # evidence/confirm_no: 완료로 판정한 근거와 예약번호. 성공 기록은 이후 시도를
        #             멈추게 하므로 "무엇을 보고 성공이라 했는지"가 남아 있어야 한다.
        return {"success": success, "message": message, "booked_time": booked_time,
                "dry_run": dry_run, "screenshots": shots, "account": account,
                "unbookable": unbookable, "evidence": evidence, "confirm_no": confirm_no,
                "elapsed": round(time_mod.time() - t0, 1)}

    def done(ev: tuple[str, str] | None) -> dict:
        """완료 판정 → 성공 결과. 근거와 예약번호를 메시지·기록에 함께 남긴다."""
        why, no = ev or ("완료 화면", "")
        _log(f"완료 화면 확인 — {why}")
        tail = f", 예약번호 {no}" if no else ""
        return result(True, f"예약 완료 ({datekey} {booked_time}{tail})", booked_time,
                      evidence=why, confirm_no=no)

    if not cookie_str:
        # 드라이런은 비로그인으로도 날짜/시간 선택 검증까지 진행 가능
        if not dry_run:
            return result(False, "로그인 쿠키 없음 — NAVER_COOKIES_1~5 시크릿을 설정하세요")
        _log("쿠키 없음 → 비로그인 드라이런 (날짜/시간 선택 검증까지만)")
    elif not any(k in cookie_str for k in ("NID_AUT", "NID_SES")):
        if not dry_run:
            return result(False, f"{acct_label} 쿠키에 NID_AUT/NID_SES 없음 — 로그인 상태 쿠키 필요")
        _log(f"{acct_label} 쿠키에 로그인 토큰 없음 → 드라이런이므로 계속 진행")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return result(False, "playwright 미설치")

    launch_kwargs = {"headless": True}
    exe = os.environ.get("AUTO_BOOK_CHROMIUM", "").strip()
    if exe:
        launch_kwargs["executable_path"] = exe

    _log(f"예약 시도 시작 [{acct_label}]: {datekey} {wanted_times} (인원 {count}){' [드라이런]' if dry_run else ''}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    locale="ko-KR",
                )
                if cookie_str:
                    context.add_cookies(_parse_cookies(cookie_str))
                _install_fast_routes(context)
                page = context.new_page()
                # load(모든 리소스 완료) + 고정 3초 대신, DOM이 오면 바로 받아서
                # 달력/시간 UI가 그려지는 즉시 진행한다.
                def page_problem() -> tuple[str, str] | None:
                    """예약을 진행할 수 없는 페이지면 (스크린샷 태그, 사유)를 돌려준다.

                    상품 페이지(/items/)에서 업체 홈 등으로 밀려나면 달력도 시간대도
                    없으므로, 찾아 헤매지 말고 바로 다음 계정으로 넘어가야 한다.
                    """
                    if _is_login_page(page):
                        return "login_required", "네이버 로그인 페이지로 리다이렉트 — NAVER_COOKIES 만료됨"
                    if "/error/" in page.url:
                        return "page_closed", "예약 페이지가 닫혀 있음 (에러 페이지 리다이렉트)"
                    if "/items/" in url and "/items/" not in page.url:
                        return "redirected", f"예약 화면으로 진입하지 못함 (리다이렉트: {page.url})"
                    return None

                page.goto(_with_start_date(url, datekey), wait_until="domcontentloaded", timeout=25000)
                # 리다이렉트는 URL만 보면 알 수 있다 — UI를 기다리며 몇 초 버리기 전에 먼저 판정
                problem = page_problem()
                if not problem:
                    _wait_booking_ui(page)
                    problem = page_problem()         # 로딩 중 뒤늦게 이동하는 경우까지
                if problem:
                    _shot(page, problem[0], shots, always=True)
                    return result(False, problem[1])
                _log(f"페이지 준비 완료 ({time_mod.time() - t0:.1f}초)")

                _shot(page, "01_landing", shots)

                picked, reason = _select_date(page, datekey)
                if picked:
                    _shot(page, "02_date", shots)
                elif reason == "no_calendar":
                    # 달력이 없는 페이지 — URL의 startDate로 이미 그 날짜가 열려 있을 수 있다.
                    # 다만 화면이 다른 날짜를 선택 중이면 그대로 진행하면 안 된다.
                    conflict = _selected_conflict(page, datekey)
                    if conflict:
                        _shot(page, "date_mismatch", shots, always=True)
                        _dump_dom_debug(page, "date_mismatch")
                        return result(False, f"날짜를 선택하지 못함 — {conflict}")
                    _log("달력 영역이 없는 페이지 — URL로 열린 날짜 그대로 진행")
                else:
                    # 다른 달의 시간대를 눌러 엉뚱한 날짜를 예약하는 사고를 막기 위해 여기서 중단한다.
                    _log(f"날짜 선택 실패 — {reason}")
                    _shot(page, "date_fail", shots, always=True)
                    _dump_dom_debug(page, "date_fail")
                    return result(False, f"날짜를 선택하지 못함: {reason}")

                booked_time = _select_time(page, wanted_times)
                if not booked_time:
                    blocked = _blocked_slots(page, wanted_times)
                    _shot(page, "time_fail", shots, always=True)
                    _dump_dom_debug(page, "time_fail")
                    if blocked:
                        # 칸은 떠 있는데 누를 수 없는 상태 — 계정을 바꿔도 같다
                        return result(False, f"시간대 {blocked}이(가) 예약 페이지에서 선택 불가 상태 "
                                             f"(매진·마감) — 계정을 바꿔도 동일", unbookable=True)
                    return result(False, f"시간대 {wanted_times} 중 선택 가능한 것이 없음 (이미 선점됐을 수 있음)")
                _log(f"시간대 선택: {booked_time} ({time_mod.time() - t0:.1f}초)")
                _shot(page, "03_time", shots)

                _ensure_quantity(page, count)
                _check_agreements(page)

                clicked = _click_cta(page, _NEXT_BUTTON_TEXTS)
                if not clicked:
                    _shot(page, "cta_fail", shots, always=True)
                    _dump_dom_debug(page, "cta_fail")
                    return result(False, "예약 진행 버튼을 찾지 못함")
                _log(f"진행 버튼 클릭: '{clicked}'")
                # 다음 화면(완료 또는 확정 단계)이 뜨는 즉시 진행 — 최대 3초
                _poll_until(page, lambda: _success_evidence(page) or _is_login_page(page)
                            or _has_any_button(page, _FINAL_BUTTON_TEXTS), 3000)
                _shot(page, "04_after_next", shots)

                if _is_login_page(page):
                    if dry_run:
                        return result(True, "[드라이런] 로그인 페이지 도달 — 날짜/시간 선택 검증 완료, 실제 예약엔 로그인 쿠키 필요", booked_time)
                    return result(False, f"예약 단계에서 로그인 요구 — {acct_label} 쿠키 만료됨")

                # 이미 완료됐는지 (1단계 예약인 경우)
                ev = _success_evidence(page)
                if ev:
                    _shot(page, "05_done", shots, always=True)
                    return done(ev)

                # 2단계: 예약 확인/동의 페이지
                # 확정 버튼을 누르기 전 마지막 안전장치 — 화면에 적힌 날짜가 요청과 다르면 멈춘다
                conflict = _page_date_conflict(page, datekey)
                if conflict:
                    _shot(page, "date_mismatch", shots, always=True)
                    _dump_dom_debug(page, "date_mismatch")
                    return result(False, f"날짜를 선택하지 못함 — {conflict}")

                # 계정별로 오래 붙잡지 않도록 타임아웃 단축 (다계정 스윕이 5분씩 걸리던 원인).
                # budget_sec이 주어지면(모니터 안에서의 선시도) 남은 예산 안에서만 확정을 기다린다.
                confirm_wait = 18.0
                if budget_sec:
                    left = budget_sec - (time_mod.time() - t0)
                    confirm_wait = max(6.0, min(confirm_wait, left))
                deadline = time_mod.time() + confirm_wait
                step = 0
                final_clicks = 0
                while time_mod.time() < deadline:
                    step += 1
                    _check_agreements(page)
                    if dry_run:
                        _shot(page, "dryrun_stop", shots, always=True)
                        final_btn = None
                        for t in _FINAL_BUTTON_TEXTS:
                            if page.locator(f'button:has-text("{t}")').count():
                                final_btn = t
                                break
                        return result(True, f"[드라이런] 최종 확정 직전 중단 — 확정 버튼: '{final_btn}'", booked_time)
                    clicked = _click_cta(page, _FINAL_BUTTON_TEXTS)
                    if clicked:
                        final_clicks += 1
                        _log(f"확정 버튼 클릭: '{clicked}'")
                        # 완료 화면이 뜨면 즉시 성공 처리 — 최대 2.5초
                        _poll_until(page, lambda: _success_evidence(page) or _is_login_page(page), 2500)
                        _shot(page, f"06_after_final_{step}", shots)
                        ev = _success_evidence(page)
                        if ev:
                            _shot(page, "07_success", shots, always=True)
                            return done(ev)
                        if _is_login_page(page):
                            if dry_run:
                                return result(True, "[드라이런] 로그인 페이지 도달 — 날짜/시간 선택 검증 완료, 실제 예약엔 로그인 쿠키 필요", booked_time)
                            return result(False, f"확정 단계에서 로그인 요구 — {acct_label} 쿠키 만료됨")
                    else:
                        # 확정 버튼이 아직 없음 — 완료 화면이 뜨는지 보며 대기
                        if _poll_until(page, lambda: _success_evidence(page), 1500, 250):
                            _shot(page, "07_success", shots, always=True)
                            return done(_success_evidence(page))

                # 확정 실패 — 다음 진단을 위해 확정 페이지 구조를 반드시 남긴다
                _shot(page, "timeout", shots, always=True)
                _dump_dom_debug(page, "confirm_fail")
                hint = "확정 버튼을 찾지 못함 (동의 미완료 가능)" if final_clicks == 0 else "확정 후 완료 페이지 미감지"
                return result(False, f"확정 단계 실패: {hint} — 수동 확인 필요(예약됐을 수도 있음)")
            finally:
                browser.close()
    except Exception as exc:
        return result(False, f"자동 예약 중 예외: {exc}")
