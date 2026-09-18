"""이력 기록(history/YYYY-MM.jsonl) 회귀 테스트.

네트워크 없이 돈다. HISTORY_DIR을 임시 폴더로 갈아 끼워 저장소의 실제 이력은
건드리지 않는다.

배경 — Actions 로그는 90일이면 사라지고, 자리 확인 로그와 기록용 로그가 한 줄기로
섞여 있어 "언제 자리가 생기고 없어졌나"를 나중에 되짚기 어렵다. 알림(ntfy)도 오래
남지 않는다. 그래서 같은 사건을 기계가 읽을 수 있는 형태로 따로 쌓고, stock.html이
그 파일을 읽는다.

확인 내용:
  - 재고/예약이 움직인 회차마다 구조화된 줄이 남는다 (알림을 끈 항목도 남는다 —
    알림 없이 흐름만 보고 싶다는 게 이 파일을 만든 이유다)
  - 슬롯 변화는 문자열이 아니라 {시간: {from, to}}로 남아 다시 읽을 수 있다
  - 시간이 지나 빠진 슬롯은 변화로 치지 않는다
  - 예약창 열림/닫힘은 '전환된 회차'에만 남는다 (닫힌 채 흐르는 회차는 남기지 않는다)
  - flush 전에는 파일을 건드리지 않고, flush가 커밋할 경로를 돌려준다
  - 보관 기간을 넘긴 월 파일은 정리된다

사용법: python check_booking_history_test.py
"""

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


def read_lines(month: str | None = None) -> list[dict]:
    month = month or datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m")
    path = cb.HISTORY_DIR / f"{month}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def snap(**slots) -> dict:
    """{'11:00': (재고, 예약)} → 스냅샷 dict."""
    return {t.replace("_", ":"): list(v) for t, v in slots.items()}


def slots_of(*pairs) -> list:
    """ref_slots 목록 만들기. pairs = (시각, 재고, 예약)."""
    return [{"unitStartTime": f"2026-09-23 {t}:00", "unitStock": st,
             "unitBookingCount": bk, "isUnitSaleDay": True} for t, st, bk in pairs]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hist-test-"))
    cb.HISTORY_DIR = tmp
    cb._history_buf.clear()

    print("1) 재고가 움직이면 구조화된 줄이 남는다")
    alerted: dict = {}
    # 첫 회차는 비교 대상이 없어 기록이 없다 (스냅샷만 잡는다)
    cb.note_stock_change(alerted, "t1", "2026-09-23", "테스트", "09-23(수)", "u",
                         slots_of(("11:00", 4, 4), ("12:00", 4, 4)), "10:00:00", False)
    check(cb._history_buf == [], "첫 회차(비교 대상 없음)에는 기록하지 않는다")

    # 11:00에 취소표 한 자리
    cb.note_stock_change(alerted, "t1", "2026-09-23", "테스트", "09-23(수)", "u",
                         slots_of(("11:00", 4, 3), ("12:00", 4, 4)), "10:01:00", False)
    check(len(cb._history_buf) == 1, f"변동 회차에 한 줄 (실제: {len(cb._history_buf)}줄)")

    print("2) flush 전에는 파일이 없고, flush가 경로를 돌려준다")
    check(read_lines() == [], "flush 전에는 파일에 아무것도 없다")
    paths = cb.flush_history()
    check(len(paths) == 1 and paths[0].startswith("history/") and paths[0].endswith(".jsonl"),
          f"커밋할 경로를 돌려준다 (실제: {paths})")
    check(cb._history_buf == [], "flush 후 버퍼는 비워진다")
    check(cb.flush_history() == [], "쌓인 게 없으면 빈 목록 (커밋을 부르지 않는다)")

    recs = read_lines()
    check(len(recs) == 1, f"파일에 한 줄 (실제: {len(recs)}줄)")
    r = recs[0]
    check(r["kind"] == "stock" and r["item"] == "t1" and r["date"] == "2026-09-23",
          f"종류·항목·날짜 (실제: {r.get('kind')}/{r.get('item')}/{r.get('date')})")
    check(r["slots"] == {"11:00": {"from": [4, 4], "to": [4, 3]}},
          f"슬롯 변화가 다시 읽을 수 있는 형태로 남는다 (실제: {r['slots']})")
    check(r["booked"] == [8, 7] and r["stock"] == [8, 8],
          f"총합도 before→after로 (실제: 재고 {r['stock']} / 예약 {r['booked']})")
    check("취소" in r["change"], f"무엇이 움직였는지 (실제: {r['change']})")

    print("3) 알림을 끈 항목도 기록은 남는다")
    cb.note_stock_change(alerted, "t1", "2026-09-23", "테스트", "09-23(수)", "u",
                         slots_of(("11:00", 4, 4), ("12:00", 4, 4)), "10:02:00", False,
                         notify=False)
    check(len(cb._history_buf) == 1, "notify=False여도 기록한다")
    cb.flush_history()

    print("4) 업체가 시간대를 내리면 from/to의 None으로 남는다")
    cb.note_stock_change(alerted, "t1", "2026-09-23", "테스트", "09-23(수)", "u",
                         slots_of(("11:00", 4, 4)), "10:03:00", False)
    cb.flush_history()
    r = read_lines()[-1]
    check(r["slots"] == {"12:00": {"from": [4, 4], "to": None}},
          f"사라진 시간대는 to=None (실제: {r['slots']})")

    print("5) 시간이 지나 빠진 슬롯은 변화로 치지 않는다")
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    past = {"unitStartTime": f"{today} 00:01:00", "unitStock": 2,
            "unitBookingCount": 0, "isUnitSaleDay": True}
    future = {"unitStartTime": f"{today} 23:59:00", "unitStock": 2,
              "unitBookingCount": 0, "isUnitSaleDay": True}
    a2: dict = {}
    cb.note_stock_change(a2, "t2", today, "오늘", "오늘", "u", [past, future], "10:00:00", True)
    cb._history_buf.clear()
    cb.note_stock_change(a2, "t2", today, "오늘", "오늘", "u", [future], "23:00:00", True)
    check(cb._history_buf == [], f"지난 슬롯이 빠진 것만으로는 기록하지 않는다 (실제: {cb._history_buf})")

    print("6) 예약창 열림/닫힘은 '전환된 회차'에만 남는다")
    cb._history_buf.clear()
    gate_alerted: dict = {}
    calls = {"n": 0}
    closed = {"v": True}
    cb._playwright_check = lambda u: ((True, "URL 리다이렉트: /error/") if closed["v"]
                                      else (False, ""))
    cb.send_ntfy = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
    gate = cb.UrlGate({"id": "t3"}, "t3", "https://x/items/1", "게이트",
                      gate_alerted, "", "10:00:00")
    gate._run_check()
    check(len(cb._history_buf) == 1, f"열림→닫힘 전환에 한 줄 (실제: {len(cb._history_buf)}줄)")

    gate2 = cb.UrlGate({"id": "t3"}, "t3", "https://x/items/1", "게이트",
                       gate_alerted, "", "10:01:00")
    gate2._run_check()
    check(len(cb._history_buf) == 1, "닫힌 채로 이어지는 회차는 남기지 않는다")

    closed["v"] = False
    gate3 = cb.UrlGate({"id": "t3"}, "t3", "https://x/items/1", "게이트",
                       gate_alerted, "", "10:02:00")
    gate3._run_check()
    check(len(cb._history_buf) == 2, f"닫힘→열림 전환에 한 줄 더 (실제: {len(cb._history_buf)}줄)")
    cb.flush_history()
    gates = [x for x in read_lines() if x["kind"] == "gate"]
    check([g["open"] for g in gates] == [False, True],
          f"열림 여부가 순서대로 (실제: {[g['open'] for g in gates]})")

    print("7) 보관 기간을 넘긴 월 파일은 정리된다")
    for m in ("2026-05", "2026-06", "2026-07", "2026-08", "2026-09"):
        (tmp / f"{m}.jsonl").write_text("{}\n", encoding="utf-8")
    keep = cb.HISTORY_KEEP_MONTHS
    cb.HISTORY_KEEP_MONTHS = 3
    try:
        # 기준은 '오늘'이라, 테스트가 도는 달보다 과거인 파일만 확실히 지워진다.
        now = datetime.now(timezone(timedelta(hours=9)))
        months = now.year * 12 + (now.month - 1) - 2
        oldest = f"{months // 12:04d}-{months % 12 + 1:02d}"
        dropped = cb.prune_history()
        left = sorted(p.stem for p in tmp.glob("*.jsonl"))
        check(all(m >= oldest for m in left), f"보관 기간 안쪽만 남는다 (남은 것: {left})")
        check(all(d.startswith("history/") for d in dropped),
              f"지운 경로를 커밋용으로 돌려준다 (실제: {dropped})")
    finally:
        cb.HISTORY_KEEP_MONTHS = keep

    print("7-1) 보관 기간 0이면 아무것도 지우지 않는다")
    (tmp / "2020-01.jsonl").write_text("{}\n", encoding="utf-8")
    cb.HISTORY_KEEP_MONTHS = 0
    try:
        check(cb.prune_history() == [], "0이면 정리하지 않는다")
        check((tmp / "2020-01.jsonl").exists(), "옛 파일도 그대로 둔다")
    finally:
        cb.HISTORY_KEEP_MONTHS = keep

    print("8) 이력은 상태 파일과 같은 커밋에 실린다 (커밋이 두 배가 되지 않는다)")
    seen: list = []
    real_commit = cb.commit_files
    cb.commit_files = lambda paths, msg, label="": seen.append(list(paths)) or True
    try:
        cb._history_buf.clear()
        cb.record_history("stock", "t8", "커밋", date="2026-09-23", change="테스트")
        paths = cb.flush_history()
        cb.commit_alerted(paths)
        check(seen == [["booking_alerted.json", "history/"
                        + datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m") + ".jsonl"]],
              f"한 번의 커밋에 상태 파일과 이력이 같이 실린다 (실제: {seen})")

        seen.clear()
        cb.commit_alerted()
        check(seen == [["booking_alerted.json"]],
              f"이력이 없으면 종전과 똑같다 (실제: {seen})")
    finally:
        cb.commit_files = real_commit

    print("9) 기록이 실패해도 감시를 멈추지 않는다")
    cb._history_buf.clear()
    cb.record_history("stock", "t9", "직렬화불가", blob=object())
    check(cb._history_buf == [], "JSON으로 못 만드는 값은 버린다 (예외를 올리지 않는다)")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
