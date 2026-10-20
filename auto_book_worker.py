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
import time as time_mod
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import auto_book
from check_booking import (
    _auto_book_cfg,
    _match_time,
    _parse_dt,
    fetch_slots,
    in_booking_period,
    item_booking_period,
    load_monitors,
    load_schedule_cache,
    parse_naver_url,
    send_ntfy,
)

KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).parent
AUTO_BOOK_LOG_FILE = BASE_DIR / "auto_book_log.json"
AUTO_BOOK_LOG_MAX = 300
# 워커가 남기고 모니터가 읽는 실행 결과 (파일 소유자는 워커뿐이다)
AUTO_BOOK_STATE_FILE = BASE_DIR / "auto_book_state.json"

# 예약 시작 시각(scheduled)보다 먼저 뜬 워커가 정각까지 기다려 주는 최대 시간.
# 모니터는 시작 몇 분 전에 미리 띄운다 (check_booking.AUTO_BOOK_PREWAIT_LEAD_MIN).
PREWAIT_MAX_MIN = 15

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
    # 계정별 성공 기록은 실행마다 덮어쓰지 않고 쌓는다 (다른 실행이 잡은 계정을 잃지 않게)
    accts = dict(merged.get("booked_accounts") or {})
    accts.update(state_entry.get("booked_accounts") or {})
    merged.update(state_entry)
    if accts:
        merged["booked_accounts"] = accts
    state[item_id] = merged
    _write_json(AUTO_BOOK_STATE_FILE, state)


def record_results(log_entries: list, item_id: str, state_entry: dict, tries: int = 5) -> bool:
    """결과를 기록하고 main에 커밋/푸시.

    여러 워커(날짜별로 따로 뜬다)가 거의 동시에 끝나면 같은 JSON을 동시에 밀게 된다.
    예전에는 "커밋을 만든 뒤 rebase"였는데, 상대가 같은 파일을 먼저 올려 두면 rebase가
    충돌로 죽어 결과를 아예 못 남겼다(실제로 하룻밤에 여러 건 유실 → 모니터가 결과를
    못 보고 같은 슬롯을 계속 재디스패치). 지금은 매 시도마다

        원격 최신본으로 되감기 → 이번 결과만 다시 얹기 → 커밋 → push

    순서로 돌린다. 커밋이 항상 origin/main 바로 위에 놓이므로 충돌 자체가 없고,
    push가 거절되면(그 사이 누가 또 올림) 같은 과정을 반복한다.
    """
    for attempt in range(1, tries + 1):
        try:
            _git("config", "user.name", "github-actions[bot]")
            _git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
            _git("fetch", "origin", "main")
            # 추적 파일은 이 두 JSON뿐이고 스크린샷은 gitignore라 되감아도 잃을 게 없다
            _git("reset", "--hard", "origin/main")
            _apply_results(log_entries, item_id, state_entry)
            _git("add", AUTO_BOOK_LOG_FILE.name, AUTO_BOOK_STATE_FILE.name)
            if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
                print("  → 기록할 변경 없음", flush=True)
                return True
            _git("commit", "-m", "data: 자동예약 결과 기록 [skip ci]")
            if _git("push", "origin", "HEAD:main", check=False).returncode == 0:
                print("  → 자동예약 결과 커밋/푸시 완료", flush=True)
                return True
            print(f"[경고] 결과 push 거절 ({attempt}/{tries}) — 최신본 위에서 다시 시도", flush=True)
            time_mod.sleep(0.5 * attempt)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:] or [str(exc)]
            print(f"[경고] 결과 커밋 실패 ({attempt}/{tries}): {err[0]}", flush=True)
            time_mod.sleep(0.5 * attempt)
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


def booked_accounts(item_id: str, cfg: dict) -> set:
    """이 항목에서 이미 예약에 성공한 계정 번호들 (설정 재저장 이전 기록은 뺀다)."""
    state = _read_json(AUTO_BOOK_STATE_FILE, {})
    entry = state.get(item_id) if isinstance(state, dict) else None
    recs = (entry or {}).get("booked_accounts") or {}
    saved_dt = _parse_dt(cfg.get("saved_at"))
    done = set()
    for acct, rec in recs.items():
        at = _parse_dt((rec or {}).get("at"))
        if saved_dt and at and at < saved_dt:
            continue          # 설정을 다시 저장하기 전 성공 — 재무장됐으므로 다시 잡는다
        try:
            done.add(int(acct))
        except (TypeError, ValueError):
            pass
    return done


def api_slot_snapshot(item: dict, datekey: str) -> list:
    """예약 직전 API가 말하는 슬롯 원본값. API와 실제 페이지가 어긋날 때 판정하려고 남긴다.

    [{"t": "11:00", "stock": 2, "booked": 1}, ...] 형태 (조회 실패면 빈 목록).
    """
    parsed = parse_naver_url(item.get("url", ""))
    if not parsed:
        return []
    try:
        info = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], datekey)
    except Exception:
        return []
    if not info.get("queried"):
        return []
    return [{"t": s.get("unitStartTime", "")[11:16],
             "stock": s.get("unitStock", 0),
             "booked": s.get("unitBookingCount", 0)}
            for s in (info.get("all_slots") or [])][:20]


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


def report_lag(detected_at: str) -> float | None:
    """모니터가 자리를 감지한 뒤 여기까지 걸린 시간(초)을 로그로 남긴다.

    워크플로 큐 대기 + 체크아웃 + 브라우저 설치가 이 지연의 대부분이라,
    예약 실패가 "늦어서"인지 "슬롯이 없어서"인지 구분하는 데 쓴다.
    """
    started = _parse_dt(detected_at)
    if not started:
        return None
    lag = (datetime.now(KST) - started).total_seconds()
    print(f"  감지 → 예약 시작 지연: {lag:.0f}초", flush=True)
    return round(lag, 1)


def run(item_id: str, datekey: str, requested: list, sig: str, attempt: int,
        detected_at: str = "") -> int:
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
    if cfg["dates"]:
        if datekey not in cfg["dates"]:
            return record_skip(item_id, name, datekey, sig, attempt,
                               f"{datekey}는 자동예약 대상 날짜가 아닙니다")
    else:
        # 날짜 미지정 = 등록된 예약 기간 전체가 대상. 기간을 모르면 그대로 진행한다.
        period = item_booking_period(item, load_schedule_cache(from_github=True))
        if not in_booking_period(datekey, period):
            return record_skip(item_id, name, datekey, sig, attempt,
                               f"{datekey}는 등록된 예약 기간"
                               f"({period[0] or '?'}~{period[1] or '?'}) 밖입니다")
        if period[0] or period[1]:
            print(f"  예약 기간 기준 탐색: {period[0] or '?'}~{period[1] or '?'} "
                  f"(자동예약 날짜 미지정)", flush=True)

    now_kst = datetime.now(KST)
    open_at = None
    if cfg["mode"] == "scheduled" and cfg["start_at"]:
        start_at = _parse_dt(cfg["start_at"])
        if start_at and now_kst < start_at:
            if (start_at - now_kst).total_seconds() > PREWAIT_MAX_MIN * 60:
                return record_skip(item_id, name, datekey, sig, attempt,
                                   f"예약 시작 시각({cfg['start_at']})까지 {PREWAIT_MAX_MIN}분 넘게 남았습니다")
            # 오픈 시각에 맞춰 예약: 미리 떠서 기다렸다가 정각에 누른다
            open_at = start_at
            print(f"  오픈 대기 모드: {start_at.strftime('%H:%M:%S')} 정각에 예약 시도 "
                  f"({(start_at - now_kst).total_seconds():.0f}초 남음)", flush=True)

    # 자동예약은 "모든 계정이 예약에 성공"하거나 사용자가 끌 때까지 계속한다.
    # 이미 성공한 계정은 빼고 남은 계정으로만 시도한다 (같은 계정 중복 예약 방지).
    # 설정을 다시 저장하면(saved_at 갱신) 그 전 성공 기록은 무시하고 처음부터 다시 잡는다.
    done_accts = booked_accounts(item_id, cfg)
    all_accounts = auto_book.get_accounts(cfg["accounts"])
    accounts = [(a, c) for a, c in all_accounts if a not in done_accts]
    if all_accounts and not accounts:
        return record_skip(item_id, name, datekey, sig, attempt,
                           f"모든 계정({len(all_accounts)}개)이 이미 예약에 성공했습니다")
    if done_accts:
        print(f"  이미 성공한 계정 {sorted(done_accts)} 제외 — 남은 계정 "
              f"{[a for a, _ in accounts]}로 시도", flush=True)

    times = resolve_times(item, cfg, datekey, requested)
    if not times and open_at:
        # 오픈 전에는 시간대가 안 보이는 상품도 있다 → 정각까지 기다렸다가 다시 본다
        # (이때는 오픈 뒤에 브라우저를 띄우게 되지만, 시간대를 모른 채 누를 수는 없다)
        time_mod.sleep(max((open_at - datetime.now(KST)).total_seconds(), 0))
        deadline = time_mod.time() + 20
        while not times and time_mod.time() < deadline:
            times = resolve_times(item, cfg, datekey, requested)
            if not times:
                time_mod.sleep(0.5)
        open_at = None
    if not times:
        return record_skip(item_id, name, datekey, sig, attempt, "예약할 시간대가 없습니다")

    print(f"=== 자동예약 실행: {name} {datekey} {times} (시도 {attempt}) ===", flush=True)
    # 오픈 대기 실행은 감지 시각이 아니라 오픈 시각 기준이라 지연을 재지 않는다
    lag_sec = None if open_at else report_lag(detected_at)

    if not accounts:
        res = {"success": False, "message": "사용 가능한 계정 쿠키 없음 (COOKIES_BUNDLE_JSON 또는 NAVER_COOKIES_1~5 시크릿 확인)",
               "booked_time": None, "dry_run": False, "account": None}
        results = [res]
    else:
        def attempt_one(acct_no, cookie_str, wait_until=None) -> dict:
            try:
                return auto_book.try_book(url, datekey, times, count=cfg["count"],
                                          cookie_str=cookie_str, account=acct_no, open_at=wait_until)
            except Exception as exc:
                return {"success": False, "message": f"예외: {exc}", "booked_time": None,
                        "dry_run": False, "account": acct_no}

        def report(r: dict) -> None:
            took = f" [{r['elapsed']}초]" if r.get("elapsed") is not None else ""
            print(f"  [자동예약] 계정{r.get('account')} 결과: {'성공' if r['success'] else '실패'}"
                  f"{took} — {r['message']}", flush=True)

        if open_at:
            # 오픈 선착순: 모든 계정이 각자 브라우저로 로그인해 두고 정각에 동시에 누른다.
            # 한 계정씩 차례로 하면 두 번째 계정은 오픈 10~20초 뒤에야 시작해 소용이 없다
            # (어바웃톤 300석이 41초 만에 매진). 성공한 계정 수만큼 예약이 잡힌다.
            print(f"  오픈 대기: 계정 {len(accounts)}개 동시 시도 (성공한 계정 수만큼 예약)", flush=True)
            with ThreadPoolExecutor(max_workers=len(accounts)) as ex:
                futs = [ex.submit(attempt_one, a, c, open_at) for a, c in accounts]
                results = [f.result() for f in futs]
            for r in results:
                report(r)
        else:
            results = []
            res = None
            for acct_no, cookie_str in accounts:
                res = attempt_one(acct_no, cookie_str)
                results.append(res)
                report(res)
                # 성공해도 멈추지 않는다 — 남은 계정도 같은 자리를 노린다 (모든 계정 성공이 목표)
                # 슬롯이 사라졌거나 페이지가 선택 불가로 막은 실패는 계정을 바꿔도 소용없다
                if res.get("unbookable") or any(p in res["message"] for p in _ACCOUNT_SWITCH_USELESS):
                    break
        wins_any = [r for r in results if r["success"]]
        res = wins_any[0] if wins_any else results[-1]

    finished_at = datetime.now(KST).isoformat(timespec="seconds")
    unbookable = any(r.get("unbookable") for r in results)
    # API는 "자리 있음"이라는데 페이지는 "선택 불가"인 경우가 있다. 어느 쪽이 맞는지
    # 다음 사례에서 바로 판정할 수 있도록, 실패했을 때만 API 원본값을 함께 남긴다.
    api_slots = api_slot_snapshot(item, datekey) if not (res and res["success"]) else []
    if unbookable and api_slots:
        avail = [s for s in api_slots if s["stock"] - s["booked"] > 0]
        print(f"  [진단] 페이지는 선택 불가인데 API 원본은 {api_slots} "
              f"→ API 기준 남은 자리 {len(avail)}개"
              f"{' ⚠️ API·페이지 불일치(유령 자리)' if avail else ' (API도 자리 없음)'}", flush=True)

    log_entries = [{
        "ts": finished_at,
        "item_id": item_id,
        "name": name,
        "date": datekey,
        "times": times,
        "account": r.get("account"),
        "success": bool(r["success"]),
        "dry_run": bool(r.get("dry_run")),
        "elapsed": r.get("elapsed"),
        "unbookable": bool(r.get("unbookable")),
        "api_slots": api_slots if r is results[-1] else None,
        # 성공으로 본 근거 — 오판이었는지 나중에 이 줄만 보고 가릴 수 있어야 한다
        "evidence": r.get("evidence") or None,
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
            "lag_sec": lag_sec,
            "elapsed": res.get("elapsed"),
            "account": res.get("account"),
            "success": bool(res["success"]),
            "dry_run": bool(res.get("dry_run")),
            # 페이지가 이 슬롯을 선택 불가로 막았다는 신호 — 모니터가 잠시 쉬는 근거
            "unbookable": unbookable,
            "api_slots": api_slots or None,
            "evidence": res.get("evidence") or None,
            "message": res["message"],
        },
    }
    wins = [r for r in results if r["success"] and not r.get("dry_run")]
    if wins:
        state_entry["booked_accounts"] = {
            str(r.get("account")): {
                "date": datekey, "time": r.get("booked_time"), "at": finished_at,
                # 예약번호가 없으면 완료 화면을 문구/URL로만 확인했다는 뜻 —
                # 네이버 예약 내역에서 한 번 눈으로 확인하는 게 좋다
                "confirm_no": r.get("confirm_no") or None,
                "evidence": r.get("evidence") or None,
            } for r in wins}
    done_now = done_accts | {r.get("account") for r in wins}
    all_done = bool(all_accounts) and all(a in done_now for a, _ in all_accounts)
    if all_done:
        # 모니터는 이 기록을 보고 자동예약을 멈춘다 — 모든 계정이 성공했을 때만 남긴다
        state_entry["booked"] = {
            "date": datekey,
            "time": res.get("booked_time"),
            "account": res.get("account"),
            "at": finished_at,
            "confirm_no": res.get("confirm_no") or None,
            "evidence": res.get("evidence") or None,
            "all_done": True,
            "accounts": sorted(done_now),
        }

    record_results(log_entries, item_id, state_entry)

    if wins:
        if ntfy_topic:
            lines = [f"계정{r.get('account')}: {r.get('booked_label') or (datekey + ' ' + (r.get('booked_time') or ''))}"
                     + (f" (예약번호 {r.get('confirm_no')})" if r.get("confirm_no") else "")
                     for r in wins]
            progress = (f"모든 계정 예약 완료 ({len(done_now)}/{len(all_accounts)}) — 자동예약 종료"
                        if all_done else
                        f"{len(done_now)}/{len(all_accounts)}개 계정 완료 — 남은 계정은 계속 시도")
            send_ntfy(ntfy_topic, f"🎫 {name} 자동예약 {len(wins)}건 성공!",
                      "\n".join(lines) + f"\n{progress}\n— 네이버 예약 내역에서 확인하세요", url)
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


def check_cookies(item_id: str) -> int:
    """오픈 전 쿠키 점검: 자동예약에 쓸 계정마다 로그인 상태로 예약 페이지에 들어가지는지 본다.

    만료된 쿠키를 갱신하는 데 시간이 걸리므로, 모니터가 시작 시각 30분 전쯤 띄운다
    (check_booking.AUTO_BOOK_COOKIE_CHECK_LEAD_MIN). 예약은 하지 않고 결과만 알린다.
    """
    cfg_all = load_monitors(from_github=True)
    ntfy_topic = cfg_all.get("ntfy_topic", "")
    item = next((m for m in cfg_all.get("monitors", [])
                 if m.get("id", m.get("name", "")) == item_id), None)
    if item is None:
        print(f"[오류] monitors.json에 항목이 없습니다: {item_id}", flush=True)
        return 2
    name = item.get("name", item_id)
    cfg = _auto_book_cfg(item)
    accounts = auto_book.get_accounts(cfg["accounts"] if cfg else [])
    when = ""
    if cfg and cfg.get("start_at"):
        st = _parse_dt(cfg["start_at"])
        when = f" ({st.strftime('%H:%M')} 오픈)" if st else ""
    if not accounts:
        msg = "사용 가능한 계정 쿠키가 없습니다 (COOKIES_BUNDLE_JSON 또는 NAVER_COOKIES_1~5 확인)"
        print(f"[쿠키 점검] {name} — {msg}", flush=True)
        if ntfy_topic:
            send_ntfy(ntfy_topic, f"⚠️ {name} 쿠키 점검{when}", msg, item.get("url", ""))
        return 0

    print(f"=== 쿠키 점검: {name}{when} — 계정 {len(accounts)}개 ===", flush=True)
    with ThreadPoolExecutor(max_workers=len(accounts)) as ex:
        futs = [ex.submit(auto_book.check_login, item.get("url", ""), c, a) for a, c in accounts]
        results = [f.result() for f in futs]
    for r in results:
        mark = {True: "✅", False: "❌", None: "❓"}[r["ok"]]
        print(f"  {mark} {r['label']}: {r['message']}", flush=True)

    bad = [r for r in results if r["ok"] is False]
    unknown = [r for r in results if r["ok"] is None]
    if bad or unknown:
        lines = [f"❌ {r['label']}: {r['message']}" for r in bad] + \
                [f"❓ {r['label']}: {r['message']}" for r in unknown]
        title = (f"⚠️ {name} 쿠키 만료 {len(bad)}개{when} — 오픈 전에 갱신하세요" if bad
                 else f"❓ {name} 쿠키 점검 확인 필요{when}")
        body = "\n".join(lines) + f"\n(정상 {len(results) - len(bad) - len(unknown)}/{len(results)})"
        if ntfy_topic:
            send_ntfy(ntfy_topic, title, body, item.get("url", ""))
    else:
        # 문제가 없으면 알리지 않는다 (로그에만 남긴다)
        print(f"  → 계정 {len(results)}개 모두 로그인 정상 — 알림 없음", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="자동예약 실행 워커")
    ap.add_argument("--item-id", required=True, help="monitors.json 항목 id")
    ap.add_argument("--date", required=True, help="예약 날짜 (YYYY-MM-DD)")
    ap.add_argument("--times", default="", help="모니터가 감지한 시간대 (쉼표 구분)")
    ap.add_argument("--sig", default="", help="슬롯 시그니처 (모니터 상태 대조용)")
    ap.add_argument("--attempt", default="1", help="같은 시그니처에 대한 시도 횟수")
    ap.add_argument("--detected-at", default="", help="모니터가 자리를 감지한 시각 (지연 측정용)")
    ap.add_argument("--fail-note", default="",
                    help="예약을 돌리지 않고 실행 실패 사실만 기록 (워크플로 실패 처리용)")
    ap.add_argument("--check-cookies", default="",
                    help="1이면 예약 없이 계정 쿠키(로그인 상태)만 점검하고 알린다")
    args = ap.parse_args()
    if args.check_cookies.strip() in ("1", "true", "yes"):
        return check_cookies(args.item_id.strip())

    requested = [t.strip() for t in args.times.split(",") if t.strip()]
    try:
        attempt = int(args.attempt)
    except ValueError:
        attempt = 1
    item_id, datekey, sig = args.item_id.strip(), args.date.strip(), args.sig.strip()
    if args.fail_note:
        # 워커가 죽었을 때 모니터가 15분을 기다리지 않도록 종료 사실만 남긴다
        return record_skip(item_id, "", datekey, sig, attempt, args.fail_note)
    return run(item_id, datekey, requested, sig, attempt, args.detected_at.strip())


if __name__ == "__main__":
    sys.exit(main())
