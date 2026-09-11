"""monitors.json 날짜 정리 커밋 경로 회귀 테스트.

네트워크도 git도 건드리지 않는다 (임시 파일 + 대체 함수).

배경 — 2026-09-11 16:43. 재고 없는 날짜를 정리한 회차에서 이런 로그가 났다:

    → 재고 없는 날짜 정리: 스포티파이: 2026-09-12
    [main eb2249b70] chore: 재고 없는 추적 날짜 정리
    error: cannot rebase: You have unstaged changes.
    [경고] monitors.json 커밋 실패: ...

prune_dead_dates가 git을 직접 부르면서 --autostash를 빠뜨린 탓이다. 같은 회차에
booking_alerted.json 같은 다른 데이터 파일이 이미 수정돼 있으면 rebase가 거부한다.
커밋은 로컬에만 남고 푸시는 안 된 채 경고만 찍혔다 — 뒤이은 알림 상태 푸시가 그
커밋까지 얹어 가면서 겨우 살아났지, 회차가 그 전에 끝났으면 사라졌을 것이다.

확인 내용:
  - 날짜를 지운 뒤 commit_files로 넘긴다 (git을 직접 부르지 않는다)
  - 지울 게 없으면 파일도 안 쓰고 커밋도 안 한다
  - 매진(재고>0)은 취소표가 날 수 있어 남긴다 — 호출자가 넘기지 않는다는 규약

사용법: python check_booking_commit_test.py
"""

import json
import sys
import tempfile
from pathlib import Path

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


CFG = {
    "ntfy_topic": "topic",
    "monitors": [
        {"id": "m1", "name": "스포티파이", "url": "u", "enabled": True,
         "target_dates": ["2026-09-12", "2026-09-13 11:00-12:00"]},
        {"id": "m2", "name": "하겐다즈", "url": "u", "enabled": True,
         "target_dates": ["2026-09-12"]},
    ],
}


def run_prune(pruned):
    """(남은 target_dates, commit_files 호출 인자, git 직접 호출 여부)."""
    calls: list = []
    git_calls: list = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "monitors.json"
        path.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")
        real_file, real_commit, real_run = cb.MONITORS_FILE, cb.commit_files, cb.subprocess.run
        cb.MONITORS_FILE = path
        cb.commit_files = lambda paths, message, label="": calls.append((paths, message)) or True
        cb.subprocess.run = lambda *a, **kw: git_calls.append(a)
        import builtins
        real_print = builtins.print
        builtins.print = lambda *a, **kw: None
        try:
            cb.prune_dead_dates(pruned)
        finally:
            builtins.print = real_print
            cb.MONITORS_FILE, cb.commit_files, cb.subprocess.run = real_file, real_commit, real_run
        saved = json.loads(path.read_text(encoding="utf-8"))
    dates = {m["id"]: m["target_dates"] for m in saved["monitors"]}
    return dates, calls, git_calls


def main() -> int:
    print("1) 지운 날짜는 파일에 반영하고 커밋은 commit_files로 넘긴다")
    dates, calls, git_calls = run_prune([("m1", "2026-09-12")])
    check(dates["m1"] == ["2026-09-13 11:00-12:00"],
          f"해당 날짜만 빠진다 (실제: {dates['m1']})")
    check(dates["m2"] == ["2026-09-12"], f"다른 항목은 그대로 (실제: {dates['m2']})")
    check(calls == [(["monitors.json"], "chore: 재고 없는 추적 날짜 정리")],
          f"commit_files 한 번 (실제: {calls})")
    check(git_calls == [], f"git을 직접 부르지 않는다 (실제: {git_calls})")

    print("2) 시간대가 붙은 날짜도 날짜 부분으로 걸러낸다")
    dates, calls, _ = run_prune([("m1", "2026-09-13")])
    check(dates["m1"] == ["2026-09-12"], f"11:00-12:00 항목이 빠진다 (실제: {dates['m1']})")
    check(len(calls) == 1, "커밋은 한 번")

    print("3) 지울 게 없으면 아무것도 하지 않는다")
    dates, calls, git_calls = run_prune([("m1", "2026-10-01")])
    check(dates["m1"] == ["2026-09-12", "2026-09-13 11:00-12:00"], "날짜 그대로")
    check(calls == [] and git_calls == [], f"커밋 없음 (실제: {calls}, {git_calls})")

    print("4) 모르는 항목 id는 조용히 넘어간다")
    _, calls, _ = run_prune([("없는항목", "2026-09-12")])
    check(calls == [], f"커밋 없음 (실제: {calls})")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
