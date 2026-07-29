"""자동예약 실행 워커 (autobook.yml 워크플로 전용).

모니터(check_booking.py)는 예약 가능 슬롯을 발견하면 이 워커를 별도 워크플로로
띄우고 곧바로 다음 감시 주기로 넘어간다. Playwright로 실제 예약을 진행하는
느린 작업은 전부 여기서 돌기 때문에, 예약을 시도하는 동안에도 모니터링이
멈추지 않는다.

동작:
  1. monitors.json에서 item_id에 해당하는 항목·자동예약 설정을 읽는다
  2. 대상 날짜의 슬롯을 다시 조회해 최신 시간대로 갱신한다
     (디스패치 후 시간이 흘렀으므로 모니터가 본 시간대는 이미 바뀌었을 수 있다)
  3. 계정 우선순위대로 예약을 시도한다 (계정당 예약 횟수 제한 대응)
  4. 결과를 auto_book_log.json(웹 리포트용)·auto_book_state.json(모니터 동기화용)에
     기록하고 커밋/푸시한 뒤 ntfy 알림을 보낸다

사용법:
  python auto_book_worker.py --item-id ID --date YYYY-MM-DD
                             [--times "10:00,11:00"] [--sig SIG] [--attempt 1]
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import auto_book
from check_booking import (
    _auto_book_cfg,
    _match_time,
    _parse_dt,
    fetch_slots,
    load_monitors,
    parse_naver_url,
    send_ntfy,
)

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).parent
AUTO_BOOK_LOG_FILE = BASE_DIR / "auto_book_log.json"
AUTO_BOOK_LOG_MAX = 300
# 워커가 남기고 모니터가 읽는 실행 결과 (파일 소유자는 워커뿐이다)
AUTO_BOOK_STATE_FILE = BASE_DIR / "auto_book_state.json"

# 슬롯 자체가 없어진 실패 → 다른 계정으로 재시도해도 소용없는 메시지 패턴
_ACCOUNT_SWITCH_USELESS = ["선택 가능한 것이 없음", "닫혀 있음", "날짜를 선택하지 못함"]


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[경고] {path.name} 읽기 실패: {exc}", flush=True)
    return default


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git(*args, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def _apply_results(log_entries: list, item_id: str, state_entry: dict) -> None:
    """현재 작업 트리의 로그·상태 파일에 이번 실행 결과를 얹는다."""
    log = _read_json(AUTO_BOOK_LOG_FILE, [])
    if not isinstance(log, list):
        log = []
    for entry in reversed(log_entries):     # 최신이 앞에 오도록
        log.insert(0, entry)
    del log[AUTO_BOOK_LOG_MAX:]
    _write_json(AUTO_BOOK_LOG_FILE, log)

    state = _read_json(AUTO_BOOK_STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    merged = state.get(item_id) if isinstance(state.get(item_id), dict) else {}
    merged.update(state_entry)
    state[item_id] = merged
    _write_json(AUTO_BOOK_STATE_FILE, state)


def record_results(log_entries: list, item_id: str, state_entry: dict, tries: int = 3) -> bool:
    """결과를 기록하고 main에 커밋/푸시.

    다른 항목의 워커가 같은 파일을 동시에 밀어 넣을 수 있으므로, 매 시도마다
    원격 최신본을 받아 그 위에 이번 결과만 다시 얹는다 (충돌 대신 병합).
    """
    for attempt in range(1, tries + 1):
        try:
            _git("config", "user.name", "github-actions[bot]")
            _git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
            _git("fetch", "origin", "main")
            for name in (AUTO_BOOK_LOG_FILE.name, AUTO_BOOK_STATE_FILE.name):
                _git("checkout", "origin/main", "--", name, check=False)
            _apply_results(log_entries, item_id, state_entry)
            _git("add", AUTO_BOOK_LOG_FILE.name, AUTO_BOOK_STATE_FILE.name)
            if _git("diff", "--cached", "--quiet", check=False).returncode != 0:
                _git("commit", "-m", "data: 자동예약 결과 기록 [skip ci]")
            _git("rebase", "origin/main")
            _git("push", "origin", "HEAD:main")
            print("  → 자동예약 결과 커밋/푸시 완료", flush=True)
            return True
        except subprocess.CalledProcessError as exc:
            _git("rebase", "--abort", check=False)
            err = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:] or [str(exc)]
            print(f"[경고] 결과 커밋 실패 ({attempt}/{tries}): {err[0]}", flush=True)
    print("[경고] 결과를 저장소에 남기지 못했습니다 — 모니터가 재시도할 수 있습니다", flush=True)
    return False


def record_skip(item_id: str, name: str, datekey: str, sig: str, attempt: int, message: str) -> int:
    """예약을 시도하지 않고 끝난 경우에도 종료 사실만 남긴다.

    모니터는 last_run이 올라와야 "실행 중" 표시를 지우고 다음 시도를 연다.
    로그(auto_book_log.json)에는 남기지 않는다 — 웹 리포트에는 실제 시도만 보이게.
    """
    print(f"[중단] {name or item_id} — {message}", flush=True)
    entry = {"last_run": {
        "at": datetime.now(KST).isoformat(timespec="seconds"),
        "sig": sig, "date": datekey, "attempt": attempt,
        "success": False, "skipped": True, "message": message,
    }}
    if name:
        entry["name"] = name
    record_results([], item_id, entry)
    return 0


def resolve_times(item: dict, cfg: dict, datekey: str, requested: list) -> list:
    """예약 대상 시간대 결정. 지금 다시 조회한 슬롯을 우선 사용한다."""
    parsed = parse_naver_url(item.get("url", ""))
    fresh: list = []
    if parsed:
        try:
            info = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], datekey)
            if info.get("queried"):
                fresh = [t for t in (info.get("times") or []) if _match_time(t, cfg["times"])]
        except Exception as exc:
            print(f"[경고] 슬롯 재조회 실패, 요청받은 시간대 사용: {exc}", flush=True)
    if fresh:
        # 재조회로 사라진 시간대는 버리고, 새로 생긴 시간대는 뒤에 붙여 둔다
        extra = [t for t in requested if t not in fresh]
        if extra:
            print(f"  슬롯 갱신: 요청 {requested} → 현재 {fresh} (+예비 {extra})", flush=True)
        return fresh + extra
    if requested:
        print(f"  현재 가용 슬롯 없음 — 요청받은 시간대로 시도: {requested}", flush=True)
    return requested


def run(item_id: str, datekey: str, requested: list, sig: str, attempt: int) -> int:
    cfg_all = load_monitors(from_github=True)
    ntfy_topic = cfg_all.get("ntfy_topic", "")
    item = next((m for m in cfg_all.get("monitors", [])
                 if m.get("id", m.get("name", "")) == item_id), None)
    if item is None:
        print(f"[오류] monitors.json에 항목이 없습니다: {item_id}", flush=True)
        return 2

    name = item.get("name", item_id)
    url = item.get("url", "")
    cfg = _auto_book_cfg(item)
    if not cfg:
        return record_skip(item_id, name, datekey, sig, attempt, "자동예약이 꺼져 있습니다")
    if cfg["dates"] and datekey not in cfg["dates"]:
        return record_skip(item_id, name, datekey, sig, attempt,
                           f"{datekey}는 자동예약 대상 날짜가 아닙니다")

    now_kst = datetime.now(KST)
    if cfg["mode"] == "scheduled" and cfg["start_at"]:
        start_at = _parse_dt(cfg["start_at"])
        if start_at and now_kst < start_at:
            return record_skip(item_id, name, datekey, sig, attempt,
                               f"예약 시작 시각({cfg['start_at']}) 이전입니다")

    # 이미 예약에 성공한 항목이면 중복 예약하지 않는다 (뒤늦게 도착한 요청 방어)
    state = _read_json(AUTO_BOOK_STATE_FILE, {})
    booked = (state.get(item_id) or {}).get("booked") if isinstance(state, dict) else None
    if isinstance(booked, dict):
        booked_at = _parse_dt(booked.get("at"))
        saved_dt = _parse_dt(cfg.get("saved_at"))
        rearmed = bool(booked_at and saved_dt and booked_at < saved_dt)
        if not rearmed:
            return record_skip(item_id, name, datekey, sig, attempt,
                               f"이미 예약 성공 기록이 있습니다 "
                               f"({booked.get('date')} {booked.get('time') or ''})")

    times = resolve_times(item, cfg, datekey, requested)
    if not times:
        return record_skip(item_id, name, datekey, sig, attempt, "예약할 시간대가 없습니다")

    print(f"=== 자동예약 실행: {name} {datekey} {times} (시도 {attempt}) ===", flush=True)

    accounts = auto_book.get_accounts(cfg["accounts"])
    if not accounts:
        res = {"success": False, "message": "사용 가능한 계정 쿠키 없음 (NAVER_COOKIES_1~5 시크릿 확인)",
               "booked_time": None, "dry_run": False, "account": None}
        results = [res]
    else:
        results = []
        res = None
        for acct_no, cookie_str in accounts:
            try:
                res = auto_book.try_book(url, datekey, times, count=cfg["count"],
                                         cookie_str=cookie_str, account=acct_no)
            except Exception as exc:
                res = {"success": False, "message": f"예외: {exc}", "booked_time": None,
                       "dry_run": False, "account": acct_no}
            results.append(res)
            print(f"  [자동예약] 계정{acct_no} 결과: {'성공' if res['success'] else '실패'} — {res['message']}", flush=True)
            if res["success"]:
                break
            # 슬롯이 사라진 실패는 계정을 바꿔도 소용없으므로 중단
            if any(p in res["message"] for p in _ACCOUNT_SWITCH_USELESS):
                break

    finished_at = datetime.now(KST).isoformat(timespec="seconds")
    log_entries = [{
        "ts": finished_at,
        "item_id": item_id,
        "name": name,
        "date": datekey,
        "times": times,
        "account": r.get("account"),
        "success": bool(r["success"]),
        "dry_run": bool(r.get("dry_run")),
        "message": r["message"],
    } for r in results]

    state_entry = {
        "name": name,
        "last_run": {
            "at": finished_at,
            "sig": sig,
            "date": datekey,
            "times": times,
            "attempt": attempt,
            "account": res.get("account"),
            "success": bool(res["success"]),
            "dry_run": bool(res.get("dry_run")),
            "message": res["message"],
        },
    }
    if res["success"] and not res.get("dry_run"):
        state_entry["booked"] = {
            "date": datekey,
            "time": res.get("booked_time"),
            "account": res.get("account"),
            "at": finished_at,
        }

    record_results(log_entries, item_id, state_entry)

    if res["success"] and not res.get("dry_run"):
        if ntfy_topic:
            send_ntfy(ntfy_topic, f"🎫 {name} 자동예약 성공!",
                      f"{datekey} {res.get('booked_time') or ''} (계정{res.get('account')}) 예약 완료 "
                      f"— 네이버 예약 내역에서 확인하세요", url)
    elif res["success"] and res.get("dry_run"):
        if attempt <= 1 and ntfy_topic:
            send_ntfy(ntfy_topic, f"🧪 {name} 자동예약 드라이런 성공",
                      f"{datekey} {res.get('booked_time') or ''} — {res['message']}", url)
    else:
        # 실패: 같은 시그니처에 대해 첫 실패 때만 알림 (스팸 방지)
        if attempt <= 1 and ntfy_topic:
            send_ntfy(ntfy_topic, f"⚠️ {name} 자동예약 실패",
                      f"{datekey} — {res['message']}\n직접 예약을 시도해보세요!", url)

    # 예약 실패 자체는 job 실패가 아니다. 0이 아닌 종료 코드는 "결과를 남기지 못함"을
    # 뜻하고, 워크플로의 실패 처리 단계가 그때만 대신 기록한다.
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="자동예약 실행 워커")
    ap.add_argument("--item-id", required=True, help="monitors.json 항목 id")
    ap.add_argument("--date", required=True, help="예약 날짜 (YYYY-MM-DD)")
    ap.add_argument("--times", default="", help="모니터가 감지한 시간대 (쉼표 구분)")
    ap.add_argument("--sig", default="", help="슬롯 시그니처 (모니터 상태 대조용)")
    ap.add_argument("--attempt", default="1", help="같은 시그니처에 대한 시도 횟수")
    ap.add_argument("--fail-note", default="",
                    help="예약을 돌리지 않고 실행 실패 사실만 기록 (워크플로 실패 처리용)")
    args = ap.parse_args()

    requested = [t.strip() for t in args.times.split(",") if t.strip()]
    try:
        attempt = int(args.attempt)
    except ValueError:
        attempt = 1
    item_id, datekey, sig = args.item_id.strip(), args.date.strip(), args.sig.strip()
    if args.fail_note:
        # 워커가 죽었을 때 모니터가 15분을 기다리지 않도록 종료 사실만 남긴다
        return record_skip(item_id, "", datekey, sig, attempt, args.fail_note)
    return run(item_id, datekey, requested, sig, attempt)


if __name__ == "__main__":
    sys.exit(main())
