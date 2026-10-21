"""계정끼리 날짜·시간을 나눠 잡는지 (auto_book_worker.plan_accounts) 회귀 테스트.

네트워크 없이 돈다.

배경 — 날짜를 여러 개 넣어도 다섯 계정이 감지된 날짜 하나에서 같은 시간 목록을 같은
순서로 눌러, 전부 한 회차를 두고 다퉜다. 자리가 넉넉하면 계정마다 다른 날짜·시간을
잡아야 한다.

사용법: python auto_book_spread_test.py
"""

import sys

import auto_book
import auto_book_worker as w

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


def slots(**days):
    return {d.replace("_", "-"): {"times": list(ts), "cap": dict(ts)} for d, ts in days.items()}


def first(plans):
    return {a: (p[0][0], p[0][1][0]) for a, p in plans.items()}


def main() -> int:
    print("1) 자리가 넉넉하면 날짜부터 고르게, 그다음 시간이 겹치지 않게")
    s = {"2026-10-24": {"times": ["13:00", "14:00", "15:00"], "cap": {"13:00": 5, "14:00": 5, "15:00": 5}},
         "2026-10-25": {"times": ["13:00", "14:00", "15:00"], "cap": {"13:00": 5, "14:00": 5, "15:00": 5}}}
    got = first(w.plan_accounts([1, 2, 3, 4, 5], s, {}))
    days = [d for d, _ in got.values()]
    check(sorted(days.count(d) for d in set(days)) == [2, 3], f"3/2로 나뉜다 (실제: {got})")
    check(len(set(got.values())) == 5, f"다섯 계정 모두 다른 날짜·시간 (실제: {got})")

    print("2) 남은 자리가 모자란 회차는 피한다")
    s = {"2026-10-24": {"times": ["13:00", "14:00"], "cap": {"13:00": 1, "14:00": 3}}}
    got = first(w.plan_accounts([1, 2, 3], s, {}, count=1))
    check(got[1] == ("2026-10-24", "13:00") and got[2][1] == "14:00" and got[3][1] == "14:00",
          f"13:00은 한 계정만 (실제: {got})")
    got = first(w.plan_accounts([1, 2], s, {}, count=2))
    check(got[1][1] == "14:00", f"2명 예약이면 1자리 남은 13:00은 고르지 않는다 (실제: {got})")

    print("3) 자리가 전부 모자라도 시도는 한다 (겹치더라도)")
    s = {"2026-10-24": {"times": ["13:00"], "cap": {"13:00": 1}}}
    plans = w.plan_accounts([1, 2], s, {})
    check(all(p and p[0] == ("2026-10-24", ["13:00"]) for p in plans.values()),
          f"두 계정 모두 13:00 시도 (실제: {plans})")

    print("4) 계정별 우선 날짜·시간을 먼저 따른다")
    s = {"2026-10-24": {"times": ["13:00", "14:00"], "cap": {"13:00": 5, "14:00": 5}},
         "2026-10-25": {"times": ["13:00", "14:00"], "cap": {"13:00": 5, "14:00": 5}}}
    prefs = {"2": {"dates": ["2026-10-25"], "times": ["14:00"]}, "3": {"dates": ["2026-10-25"]}}
    got = first(w.plan_accounts([1, 2, 3], s, prefs))
    check(got[2] == ("2026-10-25", "14:00"), f"계정2는 지정한 25일 14:00 (실제: {got[2]})")
    check(got[3][0] == "2026-10-25" and got[3][1] != "14:00",
          f"계정3은 25일 중 계정2와 다른 시간 (실제: {got[3]})")
    check(got[1][0] == "2026-10-24", f"우선순위 없는 계정1은 비어 있는 24일 (실제: {got[1]})")
    prefs = {"1": {"times": ["14:00-15:00"]}}
    got = first(w.plan_accounts([1], s, prefs))
    check(got[1][1] == "14:00", f"시간 범위 지정도 따른다 (실제: {got})")

    print("5) 이미 다른 실행에서 잡은 자리는 쓰인 것으로 본다")
    got = first(w.plan_accounts([2], s, {}, booked=[("2026-10-24", "13:00")]))
    check(got[2][0] == "2026-10-25", f"계정1이 24일을 잡았으면 계정2는 25일 (실제: {got})")

    print("6) 배정 날짜가 다 나가면 다음 날짜로 넘어갈 순서가 붙는다")
    plans = w.plan_accounts([1], s, {})
    check([d for d, _ in plans[1]] == ["2026-10-24", "2026-10-25"],
          f"예비 날짜 포함 (실제: {plans[1]})")

    print("7) 워커 — 날짜를 여러 개 지정하면 계정마다 다른 날짜로 시도, 자리 없으면 다음 날짜")
    calls: list = []
    recorded: list = []
    w.load_monitors = lambda from_github=True: {
        "ntfy_topic": "", "account_names": {"1": "betty", "2": "goog"},
        "monitors": [{"id": "x", "name": "팝업",
                      "url": "https://m.booking.naver.com/booking/12/bizes/1/items/2",
                      "auto_book": {"enabled": True, "dates": ["2099-01-01", "2099-01-02"],
                                    "times": [], "accounts": [], "count": 1, "mode": "immediate"}}]}
    w.fetch_slots = lambda b, i, s_, d: {
        "queried": True, "times": ["13:00", "14:00"], "api_slot_count": 2,
        "all_slots": [{"unitStartTime": f"{d} 13:00:00", "unitStock": 5, "unitBookingCount": 0},
                      {"unitStartTime": f"{d} 14:00:00", "unitStock": 5, "unitBookingCount": 0}]}
    w.record_results = lambda log, iid, state, **k: recorded.append((log, state)) or True
    w._read_json = lambda path, default: default
    auto_book.get_accounts = lambda pri=None: [(1, "c1"), (2, "c2")]

    def fake_try(url, datekey, times, count=1, cookie_str=None, account=None, open_at=None, **k):
        calls.append((account, datekey, list(times)))
        if account == 2 and datekey == "2099-01-02":
            return {"success": False, "message": "선택 가능한 것이 없음", "booked_time": None,
                    "dry_run": False, "account": account}
        return {"success": True, "message": "성공", "booked_time": times[0], "dry_run": False,
                "account": account}
    auto_book.try_book = fake_try
    w.run("x", "2099-01-01", ["13:00", "14:00"], "sig", 1, "")
    c1 = [c for c in calls if c[0] == 1]
    c2 = [c for c in calls if c[0] == 2]
    check([c[1] for c in c1] == ["2099-01-01"] and [c[1] for c in c2] == ["2099-01-02", "2099-01-01"],
          f"계정1은 01일, 계정2는 02일부터 (실제: {calls})")
    check(c2[1][2][0] != c1[0][2][0],
          f"계정2는 02일이 막히자 01일의 다른 시간으로 (실제: {calls})")
    log, state = recorded[-1]
    check({e["account"]: e["date"] for e in log} == {1: "2099-01-01", 2: "2099-01-01"},
          f"로그에 실제 날짜 (실제: {[(e['account'], e['date']) for e in log]})")
    check({e["account"]: e.get("account_name") for e in log} == {1: "betty", 2: "goog"}, "로그에 별명")
    check(auto_book.ACCOUNT_NICKNAMES == {1: "betty", 2: "goog"}, "브라우저 로그용 별명 등록")
    check(w.acct_label(3, {"1": "betty"}) == "계정3", "별명 없으면 계정N")

    print("8) 나눠 잡기를 끄면 종전처럼 같은 날짜·시간 목록")
    calls.clear()
    cfg_all = w.load_monitors()
    cfg_all["monitors"][0]["auto_book"]["spread"] = False
    w.load_monitors = lambda from_github=True: cfg_all
    auto_book.try_book = lambda url, d, t, count=1, cookie_str=None, account=None, open_at=None, **k: (
        calls.append((account, d, list(t))) or
        {"success": True, "message": "성공", "booked_time": t[0], "dry_run": False, "account": account})
    w.run("x", "2099-01-01", ["13:00", "14:00"], "sig", 1, "")
    check([c[1:] for c in calls] == [("2099-01-01", ["13:00", "14:00"])] * 2,
          f"두 계정 모두 01일 13:00부터 (실제: {calls})")

    print("10) 바로 예약 모드도 계정들이 동시에 돈다 (차례로가 아니라)")
    import threading
    barrier = threading.Barrier(2, timeout=5)
    together: list = []

    def sync_try(url, d, t, count=1, cookie_str=None, account=None, open_at=None, **k):
        try:
            barrier.wait()
            together.append(True)
        except threading.BrokenBarrierError:
            together.append(False)
        return {"success": False, "message": "매진", "booked_time": None, "dry_run": False,
                "account": account, "unbookable": account == 1}
    auto_book.try_book = sync_try
    w.run("x", "2099-01-01", ["13:00", "14:00"], "sig", 1, "")
    check(together == [True, True], f"두 계정이 동시에 시도 (실제: {together})")
    check(recorded[-1][1]["last_run"]["unbookable"] is False,
          "한 계정만 선택 불가면 모니터를 쉬게 하지 않는다")

    print("11) AUTO_BOOK_SEQUENTIAL=1이면 종전처럼 차례로")
    calls.clear()
    w.SEQUENTIAL = True
    auto_book.try_book = lambda url, d, t, count=1, cookie_str=None, account=None, open_at=None, **k: (
        calls.append((account, d, list(t))) or
        {"success": True, "message": "성공", "booked_time": t[0], "dry_run": False, "account": account})
    w.run("x", "2099-01-01", ["13:00", "14:00"], "sig", 1, "")
    w.SEQUENTIAL = False
    check([c[0] for c in calls] == [1, 2], f"계정1 → 계정2 순서 (실제: {calls})")

    print("9) 모니터 — 날짜를 지정한 항목은 워커가 돌고 있으면 다른 날짜로 또 띄우지 않는다")
    import check_booking as cb
    sent: list = []
    orig = cb.dispatch_auto_book
    cb.dispatch_auto_book = lambda iid, d, t, sig, att, det: sent.append(d) or (True, "")
    try:
        item = {"id": "x", "name": "팝업", "url": "u",
                "auto_book": {"enabled": True, "dates": ["2099-01-01", "2099-01-02"],
                              "times": [], "accounts": [], "count": 1, "mode": "immediate"}}
        alerted: dict = {}
        cb.maybe_auto_book(item, "x", "u", "2099-01-01", [("13:00", 1)], "", alerted)
        cb.maybe_auto_book(item, "x", "u", "2099-01-02", [("13:00", 1)], "", alerted)
        check(sent == ["2099-01-01"], f"워커 하나만 (실제: {sent})")
    finally:
        cb.dispatch_auto_book = orig

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
