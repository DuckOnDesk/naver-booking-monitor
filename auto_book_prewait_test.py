"""오픈 시각 예약(scheduled) — 미리 대기했다가 정각에 시도하는지 회귀 테스트.

네트워크·브라우저 없이 돈다 (GitHub 디스패치·naver API·playwright는 대체 함수로 교체).

배경 — 2026-10-16 어바웃톤: 17:00 오픈을 걸어 뒀는데, 감시는 17:00이 지나야 자리를
찾기 시작했고 자리를 본 뒤에야 예약 워크플로를 띄웠다. 300석이 41초 안에 매진돼
첫 탐색(17:00:41) 때 이미 자리가 없었다 — 예약을 시도조차 못 했다.

확인 내용:
  - 시작 시각 N분 전 회차에 예약 워크플로를 미리 띄운다 (시작 시각마다 한 번)
  - 아직 이르면 띄우지 않는다
  - 날짜 미지정이면 등록된 예약 기간의 첫날로 띄운다
  - 오픈 대기 실행이 도는 동안 감시가 자리를 봐도 같은 예약을 또 띄우지 않는다
  - 워커는 시작 전에 떠도 거절하지 않고 open_at을 넘겨 정각까지 기다린다 (첫 계정만)
  - 너무 일찍 뜬 워커는 종전대로 거절한다
  - _wait_open: 오픈 전 안내 화면이면 새로고침, 예약 화면이 그려지면 바로 진행

사용법: python auto_book_prewait_test.py
"""

import sys
from datetime import datetime, timedelta, timezone

import auto_book
import auto_book_worker as w
import check_booking as cb

KST = timezone(timedelta(hours=9))
URL = "https://m.booking.naver.com/booking/12/bizes/1357646/items/8124911"
fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


def item_with(start_at: datetime, dates=None) -> dict:
    return {"id": "abt", "name": "어바웃톤", "url": URL, "enabled": True, "target_dates": [],
            "auto_book": {"enabled": True, "dates": dates or [], "times": [], "accounts": [],
                          "count": 1, "mode": "scheduled",
                          "start_at": start_at.isoformat(timespec="seconds")}}


def run_monitor(item: dict, alerted: dict, sched_cache: dict | None = None) -> list:
    """check_all을 한 회차 돌리고 디스패치 요청 목록을 돌려준다."""
    sent: list = []
    cb.dispatch_auto_book = lambda *a, **k: (sent.append(a), (True, ""))[1]
    cb.load_reprobe_requests = lambda from_github=True: {}
    cb.check_availability = lambda *a, **k: (_ for _ in ()).throw(AssertionError("탐색하면 안 됨"))
    cache = sched_cache or {}

    class _F:
        def exists(self):
            return True

        def read_text(self, encoding="utf-8"):
            import json
            return json.dumps(cache)

    orig = cb.SCHEDULE_CACHE_FILE
    cb.SCHEDULE_CACHE_FILE = _F()
    cb.reset_log_state()
    try:
        cb.check_all([item], "", alerted)
    finally:
        cb.SCHEDULE_CACHE_FILE = orig
    return sent


class FakePage:
    """_wait_open용 가짜 페이지. states 순서대로 '로드할 때마다' 화면이 바뀐다."""

    def __init__(self, states):
        self.states = list(states)
        self.cur = self.states.pop(0)
        self.gotos = 0

    def evaluate(self, js, *a):
        if js is auto_book._JS_UI_READY:
            return {"ready": self.cur == "open"}
        if js is auto_book._JS_NOT_OPEN:
            return self.cur == "not_open"
        return None

    def wait_for_timeout(self, ms):
        pass

    def goto(self, url, **kw):
        self.gotos += 1
        if self.states:
            self.cur = self.states.pop(0)


def main() -> int:
    now = datetime.now(KST)

    print("1) 시작 시각 몇 분 전 회차에 예약 워크플로를 미리 띄운다")
    soon = now + timedelta(minutes=cb.AUTO_BOOK_PREWAIT_LEAD_MIN - 2)
    item = item_with(soon, dates=["2026-10-21"])
    alerted: dict = {}
    sent = run_monitor(item, alerted)
    check(len(sent) == 1, f"한 번 디스패치 (실제: {sent})")
    if sent:
        item_id, datekey, times, sig, attempt, detected_at = sent[0]
        check((item_id, datekey, sig) == ("abt", "2026-10-21", "2026-10-21|prewait"),
              f"대상 날짜·오픈 대기 시그니처 (실제: {sent[0][:4]})")
        check(detected_at == soon.isoformat(timespec="seconds"), "기준 시각은 오픈 시각")
    st = alerted.get("abt:auto_book_state") or {}
    check(st.get("prewait") and st.get("dispatched_at"), f"실행 중으로 표시 (실제: {st})")
    sent = run_monitor(item, alerted)
    check(not sent, f"다음 회차에는 다시 띄우지 않는다 (실제: {sent})")

    print("2) 아직 이르면 띄우지 않는다")
    alerted = {}
    sent = run_monitor(item_with(now + timedelta(minutes=cb.AUTO_BOOK_PREWAIT_LEAD_MIN + 5),
                                 dates=["2026-10-21"]), alerted)
    check(not sent and "abt:prewait" not in alerted, f"디스패치 없음 (실제: {sent})")

    print("3) 날짜 미지정이면 등록된 예약 기간의 첫날로")
    alerted = {}
    cache = {"12_1357646_8124911": {"available_start": "2026-10-21", "available_end": "2026-10-21"}}
    sent = run_monitor(item_with(soon), alerted, cache)
    check(sent and sent[0][1] == "2026-10-21", f"예약 기간 첫날 (실제: {sent})")
    alerted = {}
    sent = run_monitor(item_with(soon), alerted, {})
    check(not sent and alerted.get("abt:prewait"), f"날짜를 모르면 띄우지 않고 한 번만 알린다 (실제: {sent})")

    print("4) 오픈 대기 실행 중에는 감시가 자리를 봐도 다시 띄우지 않는다")
    alerted = {"abt:auto_book_state": {"sig": "2026-10-21|prewait", "attempts": 1,
                                       "prewait": soon.isoformat(),
                                       "dispatched_at": now.isoformat(timespec="seconds")}}
    sent = []
    cb.dispatch_auto_book = lambda *a, **k: (sent.append(a), (True, ""))[1]
    past = item_with(now - timedelta(minutes=1), dates=["2026-10-21"])
    cb.maybe_auto_book(past, "abt", URL, "2026-10-21", [("13:00", 50)], "", alerted)
    check(not sent, f"중복 디스패치 없음 (실제: {sent})")
    # 워커 결과가 올라오면 오픈 대기 표시를 지워 다음 시도를 연다
    cb.load_auto_book_state = lambda from_github=True: {"abt": {"last_run": {
        "at": (now + timedelta(seconds=30)).isoformat(timespec="seconds"),
        "sig": "2026-10-21|prewait", "success": False, "message": "매진"}}}
    cb.sync_auto_book_state([past], alerted)
    st = alerted["abt:auto_book_state"]
    check("prewait" not in st and "dispatched_at" not in st, f"결과 반영 후 표시 해제 (실제: {st})")
    cb.maybe_auto_book(past, "abt", URL, "2026-10-21", [("13:00", 1)], "", alerted)
    check(len(sent) == 1, f"그 뒤 취소표는 종전대로 띄운다 (실제: {sent})")

    print("5) 워커 — 시작 전에 떠도 거절하지 않고 정각까지 기다린다")
    calls: list = []
    open_soon = datetime.now(KST) + timedelta(minutes=3)
    w.load_monitors = lambda from_github=True: {"monitors": [item_with(open_soon, ["2026-10-21"])]}
    w.fetch_slots = lambda *a: {"queried": True, "times": ["13:00", "14:00"], "all_slots": []}
    w.record_results = lambda *a, **k: True
    w.send_ntfy = lambda *a, **k: None
    w._read_json = lambda path, default: default
    auto_book.get_accounts = lambda pri=None: [(1, "c1"), (2, "c2")]

    def fake_try(url, datekey, times, count=1, cookie_str=None, account=None, open_at=None, **k):
        calls.append((account, open_at))
        return {"success": False, "message": "매진", "booked_time": None, "dry_run": False,
                "account": account}
    auto_book.try_book = fake_try
    w.run("abt", "2026-10-21", [], "2026-10-21|prewait", 1, open_soon.isoformat())
    check(len(calls) == 2, f"계정마다 시도 (실제: {calls})")
    if len(calls) == 2:
        check(calls[0][1] is not None and abs((calls[0][1] - open_soon).total_seconds()) < 1,
              f"첫 계정에 open_at 전달 (실제: {calls[0]})")
        check(calls[1][1] is None, "다음 계정은 오픈 뒤라 대기 없음")

    calls.clear()
    too_early = datetime.now(KST) + timedelta(minutes=w.PREWAIT_MAX_MIN + 5)
    w.load_monitors = lambda from_github=True: {"monitors": [item_with(too_early, ["2026-10-21"])]}
    skipped: list = []
    w.record_skip = lambda *a: skipped.append(a[-1]) or 0
    w.run("abt", "2026-10-21", [], "x", 1, "")
    check(not calls and skipped, f"너무 이르면 종전대로 거절 (실제: {skipped})")

    print("6) _wait_open — 오픈 전 안내면 새로고침, 열리면 바로 진행")
    page = FakePage(["not_open", "not_open", "open"])
    check(auto_book._wait_open(page, URL, cap_sec=5) and page.gotos == 2,
          f"두 번 새로고침 뒤 열림 (새로고침 {page.gotos}회)")
    page = FakePage(["open"])
    check(auto_book._wait_open(page, URL, cap_sec=5) and page.gotos == 0, "이미 열려 있으면 그대로")
    page = FakePage(["not_open"])
    check(not auto_book._wait_open(page, URL, cap_sec=0.5), "끝내 안 열리면 False")

    print()
    if fails:
        print(f"실패 {len(fails)}건:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("전체 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
