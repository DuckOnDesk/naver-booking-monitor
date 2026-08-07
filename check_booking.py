"""
네이버 예약 자리 모니터 — GitHub Actions 클라우드 버전
monitors.json 파일에서 설정 읽기 (enabled 필드로 항목별 ON/OFF)

자동예약은 이 프로세스에서 직접 실행하지 않는다. 예약 가능 슬롯을 발견하면
autobook.yml 워크플로(auto_book_worker.py)를 디스패치하고 바로 다음 감시 주기로
넘어가므로, 예약을 시도하는 몇 분 동안에도 감시가 멈추지 않는다. 워커의 결과는
auto_book_state.json을 통해 되돌려 받는다 (sync_auto_book_state).

로그는 상태가 바뀔 때만 남긴다. 같은 자리 상황이 이어지는 회차는 줄을 생략하고
회차 끝에 생략 건수만 한 줄로 요약한다 (log_state 참고).

예약창 열림/닫힘 확인(chromium)은 알릴 자리를 찾은 항목에만 한다. 자리가 없으면
🎉로도 🔒로도 알릴 게 없어 확인할 이유가 없다 (UrlGate 참고).

환경변수: NTFY_TOPIC (선택, monitors.json 값 override)
          CHECK_INTERVAL_SEC, LOOP_HOURS
          LOG_DEDUP (0이면 종전처럼 매 회차 전부 출력)
          LOG_HEARTBEAT_MIN (변화가 없어도 이 간격마다 한 번은 출력, 기본 10분)
          URL_RECHECK_SEC (열려 있는 항목의 예약창 재확인 간격, 기본 300초)

monitors.json 항목 선택 필드:
  booking_open_datetime  예약 오픈 일시 (ISO 형식, 예: "2026-06-01T20:00:00+09:00")
                         설정 시 해당 시각 이후 + 자리 있을 때만 알림 발송
"""

import builtins
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

GRAPHQL_URL = "https://m.booking.naver.com/graphql?opName=schedule"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://m.booking.naver.com/",
}

GITHUB_RAW_BASE = "https://raw.githubusercontent.com/DuckOnDesk/naver-booking-monitor/main"
GITHUB_RAW_URL = f"{GITHUB_RAW_BASE}/monitors.json"
GITHUB_RAW_REPROBE_URL = f"{GITHUB_RAW_BASE}/.schedule_reprobe_request.json"
SCHEDULE_CACHE_FILE = Path(__file__).parent / "schedule_cache.json"
ALERTED_FILE = Path(__file__).parent / "booking_alerted.json"
# 자동예약 워커(auto_book_worker.py)가 남기는 실행 결과. 모니터는 읽기만 한다.
AUTO_BOOK_STATE_FILE = Path(__file__).parent / "auto_book_state.json"
GITHUB_RAW_AUTOBOOK_STATE_URL = f"{GITHUB_RAW_BASE}/auto_book_state.json"
# 웹앱이 "운영 기간 초기화" 버튼으로 기록하는 재탐색 요청 파일.
# {"requests": {"<cache_key>": "<요청 시각 ISO>"}} 형태이며, 요청 시각이 캐시의
# checked_at보다 나중이면 TTL과 무관하게 즉시 재탐색한다.
REPROBE_REQUEST_FILE = Path(__file__).parent / ".schedule_reprobe_request.json"


def _env_num(name: str, default, cast=int):
    """설정하지 않은 저장소 변수를 안전하게 기본값으로 되돌린다.

    워크플로가 `AUTO_BOOK_INLINE_BUDGET_SEC: ${{ vars.X }}` 처럼 값을 넘길 때,
    저장소 변수가 없으면 환경변수가 '빈 문자열'로 들어온다. os.environ.get의
    기본값은 키가 없을 때만 쓰이므로 int("")가 그대로 터져 모듈 임포트 단계에서
    감시 전체가 죽는다 (실제로 그렇게 멈춘 적이 있다). 빈 값·잘못된 값은
    모두 기본값으로 처리하고, 잘못된 값일 때만 경고를 남긴다.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        print(f"[경고] 환경변수 {name}={raw!r} 을(를) 숫자로 읽을 수 없어 기본값 {default} 사용", flush=True)
        return default


# 같은 슬롯 조합(signature)에 대한 자동 예약 최대 시도 횟수 (0 = 무제한:
# 예약 성공 / 슬롯 소멸 / 설정 OFF 전까지 계속 시도)
AUTO_BOOK_MAX_ATTEMPTS = _env_num("AUTO_BOOK_MAX_ATTEMPTS", 0)

# 실제 예약 시도는 별도 워크플로(autobook.yml → auto_book_worker.py)에서 돈다.
# 모니터는 디스패치만 하고 바로 다음 감시 주기로 넘어가므로, 예약을 시도하는
# 동안에도 감시가 멈추지 않는다.
AUTO_BOOK_WORKFLOW = os.environ.get("AUTO_BOOK_WORKFLOW", "autobook.yml")
# 디스패치한 실행이 이 시간(분) 안에 결과를 남기지 않으면 죽은 것으로 보고 재시도.
AUTO_BOOK_DISPATCH_TIMEOUT_MIN = _env_num("AUTO_BOOK_DISPATCH_TIMEOUT_MIN", 15)
# 감지 즉시 모니터 러너에서 첫 계정으로 예약을 한 번 눌러 볼지 (1=켬).
# 워크플로 디스패치는 큐 대기 + 러너 준비로 30~70초가 걸려 취소표를 놓치기 쉽다.
AUTO_BOOK_INLINE = (os.environ.get("AUTO_BOOK_INLINE") or "1").strip() not in ("0", "false", "no")
# 선시도에 쓸 시간 예산(초). 이 시간을 넘기면 감시가 밀리므로 브라우저 단계별 대기를 줄인다.
AUTO_BOOK_INLINE_BUDGET_SEC = _env_num("AUTO_BOOK_INLINE_BUDGET_SEC", 25)
# 예약 페이지가 "선택 불가"로 막은 슬롯을 다시 시도하기까지 쉬는 시간(분).
# API는 자리가 있다고 답하는데 페이지에서는 못 누르는 경우가 있어, 그대로 두면
# 같은 슬롯에 2분 간격으로 워크플로가 계속 뜬다 (실제로 15분에 14번 실행됐다).
AUTO_BOOK_BLOCKED_BACKOFF_MIN = _env_num("AUTO_BOOK_BLOCKED_BACKOFF_MIN", 10)
# 자동예약 날짜 미지정 항목에서, 감시 대상 밖 날짜를 한 회차에 몇 개까지 추가 조회할지.
# (예약 기간이 아주 긴 항목이 한 회차의 API 호출을 독차지하지 않도록 하는 상한)
AUTO_BOOK_SWEEP_MAX = _env_num("AUTO_BOOK_SWEEP_MAX", 31)

# schedule_cache.json 항목의 유효 시간(분). 이 시간이 지난 항목은 루프 도중에도
# 다시 조회해 운영 기간 변경을 반영한다 (0 = 재탐색 끔 = 종전 동작).
SCHEDULE_CACHE_TTL_MIN = _env_num("SCHEDULE_CACHE_TTL_MIN", 60)
# 한 회차에 재탐색할 최대 항목 수. 캐시가 한꺼번에 만료돼도 루프가 멈추지 않도록
# 회차당 1건씩만 갱신해 자연스럽게 분산시킨다.
SCHEDULE_REPROBE_PER_ROUND = _env_num("SCHEDULE_REPROBE_PER_ROUND", 1)

_rate_limit_hits = 0  # 현재 루프 회차 중 429/403 발생 횟수

KAKAO_API_URL = "https://booking.kakao.com/api/product/public/ticket/tickets/availableDates"
KAKAO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": "https://booking.kakao.com/",
}


# ── 상태 변화 기반 로그 ────────────────────────────────────────────────
# 자리 상황은 대부분의 회차에서 직전 회차와 똑같다. 같은 줄을 매 회차 다시 찍으면
# 로그가 수천 줄로 불어나 정작 변화가 묻히고, 항목·날짜가 많을수록 stdout 쓰기가
# 한 회차 시간을 갉아먹는다. 그래서 "정상 상태" 로그는 바뀐 줄만 남긴다.
#   - 최초 관측(프로세스 시작 후 그 항목/날짜를 처음 본 회차)은 항상 남긴다
#   - 이후에는 직전 상태와 다를 때만 남긴다 (자리 수 증감 포함)
#   - 바뀐 게 하나도 없는 회차는 회차 머리글까지 통째로 침묵한다
# 오류·경고·전환 알림은 이 규칙을 타지 않고 항상 그대로 출력한다.
# 알림(ntfy)·자동예약 판단은 이 규칙과 무관하다 — 출력만 줄인다.
#
# 완전히 조용해지면 감시가 도는지 멈췄는지 구분이 안 되므로 두 단계로 살아 있음을 알린다.
#   LOG_TICK_MIN      무변동이 이어질 때 이 간격마다 한 줄 (회차·남은 시간·무변동 건수)
#   LOG_HEARTBEAT_MIN 변화가 없어도 이 간격마다 상태 줄 전체를 다시 찍는다 (스냅샷)
LOG_DEDUP = os.environ.get("LOG_DEDUP", "1") != "0"
LOG_HEARTBEAT_MIN = _env_num("LOG_HEARTBEAT_MIN", 60.0, float)
LOG_TICK_MIN = _env_num("LOG_TICK_MIN", 10.0, float)

_log_state: dict[str, tuple[str, float]] = {}  # key → (마지막 출력 상태 서명, 출력 시각)
_log_skipped = 0                               # 이번 회차에 생략한 줄 수
_round_header: str | None = None               # 아직 안 찍은 회차 머리글
_round_printed = False                         # 이번 회차에 뭐라도 찍었는지
_last_tick_at = 0.0                            # 마지막으로 살아 있음을 알린 시각


def set_round_header(text: str) -> None:
    """회차 머리글을 예약해 둔다. 실제 출력은 이 회차에 남길 게 생겼을 때."""
    global _round_header, _round_printed
    _round_header, _round_printed = text, False


def flush_round_header() -> None:
    """예약된 머리글이 있으면 지금 찍는다 (다른 줄보다 먼저 나오도록)."""
    global _round_header, _round_printed, _last_tick_at
    _round_printed = True
    _last_tick_at = time.monotonic()
    if _round_header is not None:
        # 아래 print 래퍼를 거치면 무한 재귀가 되므로 먼저 비우고 원본으로 찍는다
        header, _round_header = _round_header, None
        builtins.print(header, flush=True)


def print(*args, **kwargs):     # noqa: A001 — 이 모듈 안의 print를 의도적으로 가린다
    """출력 직전에 회차 머리글을 먼저 내보낸다.

    머리글을 늦게 찍는 방식이라, 어느 줄이 먼저 나오든 그 앞에 머리글이 와야 한다.
    출력 지점이 수십 군데(상태 줄·경고·오류·자동예약·진단)라 호출부를 일일이
    고치는 대신 모듈의 print를 한 겹 감싼다. builtins.print를 호출 시점에
    찾으므로 테스트가 print를 바꿔치기해도 그대로 동작한다.
    """
    flush_round_header()
    builtins.print(*args, **kwargs)


def log_state(key: str, body: str, *, sig: str | None = None, now_str: str | None = None,
              stamp: bool = True) -> bool:
    """상태가 직전과 달라졌을 때만 로그를 남긴다. 실제로 출력했으면 True.

    key  : 상태 단위(항목 또는 항목:날짜). 같은 key끼리 직전 값과 비교한다.
    body : 타임스탬프를 뺀 본문. 본문이 곧 상태이므로 그대로 서명으로 쓴다.
    sig  : 본문에 '남은 시간'처럼 매 회차 변하는 값이 섞여 있을 때만 따로 넘긴다.
    stamp: False면 타임스탬프 없이 본문만 출력한다 (들여쓴 보조 로그용).
    """
    global _log_skipped
    signature = body if sig is None else sig
    now = time.monotonic()
    prev = _log_state.get(key)
    if LOG_DEDUP and prev is not None:
        prev_sig, prev_at = prev
        if prev_sig == signature and (now - prev_at) < LOG_HEARTBEAT_MIN * 60:
            _log_skipped += 1
            return False
    _log_state[key] = (signature, now)
    flush_round_header()
    if not stamp:
        print(body, flush=True)
        return True
    ts = now_str or datetime.now(timezone(timedelta(hours=9))).strftime("%H:%M:%S")
    print(f"[{ts}] {body}", flush=True)
    return True


def reset_log_state() -> None:
    """상태 기억을 지운다 (테스트에서 회차 간 독립성을 확보할 때 사용)."""
    global _log_skipped, _round_header, _round_printed, _last_tick_at
    _log_state.clear()
    _log_skipped = 0
    _round_header, _round_printed, _last_tick_at = None, False, 0.0
    _url_checked_at.clear()   # 예약창 재확인 주기도 같이 리셋


def log_round_summary() -> None:
    """이번 회차에 생략한 줄 수를 한 줄로 요약한다.

    아무것도 안 찍힌 회차에서는 이 줄도 찍지 않는다. 그 회차는 통째로 침묵한다.
    """
    if _round_printed and _log_skipped:
        print(f"  → 직전과 동일한 상태 {_log_skipped}줄 생략 "
              f"(변화 시 즉시 기록 / 최대 {LOG_HEARTBEAT_MIN:g}분 간격 재출력)", flush=True)


def log_round_tick(iteration: int, remaining_min: float) -> None:
    """무변동으로 조용히 지나간 회차에, 가끔 살아 있다는 한 줄만 남긴다."""
    global _round_header, _last_tick_at
    if _round_printed:
        return                                   # 이미 뭔가 찍힌 회차
    now = time.monotonic()
    if now - _last_tick_at >= LOG_TICK_MIN * 60:
        _last_tick_at = now
        print(f"--- [{iteration}회차] 남은 시간: {remaining_min:.1f}분 | "
              f"무변동 ({len(_log_state)}건 동일) ---", flush=True)
    _round_header = None


def load_monitors(from_github: bool = False) -> dict:
    if from_github:
        try:
            resp = requests.get(GITHUB_RAW_URL, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            print(f"[경고] GitHub에서 monitors.json 읽기 실패, 로컬 파일 사용: {exc}", flush=True)
    path = Path(__file__).parent / "monitors.json"
    return json.loads(path.read_text(encoding="utf-8"))


def parse_naver_url(url: str) -> dict | None:
    m = re.search(r"/booking/(\d+)/bizes/(\d+)/items/(\d+)", url)
    if not m:
        return None
    return {"service_id": int(m.group(1)), "biz_id": m.group(2), "item_id": m.group(3)}


def parse_kakao_url(url: str) -> str | None:
    m = re.search(r"/ticket/(\d+)", url)
    return m.group(1) if m else None


def check_availability(biz_id: str, item_id: str, service_id: int, target_dates: list) -> dict | None:
    today = datetime.now(timezone(timedelta(hours=9)))
    schedule_params = {
        "businessId": biz_id,
        "bizItemId": item_id,
        "businessTypeId": service_id,
        "startDateTime": today.strftime("%Y-%m-%dT00:00:00+09:00"),
        "endDateTime": (today + timedelta(days=90)).strftime("%Y-%m-%dT23:59:59+09:00"),
    }

    def _post(query: str) -> requests.Response:
        return requests.post(
            GRAPHQL_URL,
            json={"operationName": "schedule", "variables": {"scheduleParams": schedule_params}, "query": query},
            headers=HEADERS,
            timeout=15,
        )

    enhanced_query = (
        "query schedule($scheduleParams: ScheduleParams) {"
        "  schedule(input: $scheduleParams) {"
        "    bizItemSchedule { saleStartDate saleEndDate daily { date summary {"
        "      dateKey stock bookingCount hasBookableSlots isSaleDay __typename"
        "    } __typename } __typename } __typename } }"
    )
    base_query = (
        "query schedule($scheduleParams: ScheduleParams) {"
        "  schedule(input: $scheduleParams) {"
        "    bizItemSchedule { daily { date summary {"
        "      dateKey stock bookingCount hasBookableSlots isSaleDay __typename"
        "    } __typename } __typename } __typename } }"
    )

    for i, (query, has_window) in enumerate([(enhanced_query, True), (base_query, False)]):
        try:
            resp = _post(query)
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                continue
            sched = data["data"]["schedule"]["bizItemSchedule"]
            summary = sched["daily"]["summary"]
            days = (
                [d for d in summary if d["dateKey"] in target_dates]
                if target_dates
                else [d for d in summary if d["isSaleDay"]]
            )
            return {
                "days": days,
                "sale_start_date": sched.get("saleStartDate") if has_window else None,
                "sale_end_date": sched.get("saleEndDate") if has_window else None,
                "_all_summary": summary,
            }
        except requests.HTTPError as e:
            status = e.response.status_code
            if status == 400 and i == 0:
                # enhanced_query의 saleStartDate/saleEndDate 필드가 이 서비스 타입에서 미지원 → base_query로 재시도
                continue
            print(f"  [오류] schedule API HTTP {status}", flush=True)
            if status in (429, 403):
                global _rate_limit_hits
                _rate_limit_hits += 1
            continue
        except Exception:
            continue

    print("  [오류] schedule API 요청 실패", flush=True)
    return None


def fetch_slots(biz_id: str, item_id: str, service_id: int, target_date: str) -> dict:
    """
    hourlySchedule API로 시간대별 슬롯 조회. 이미 지난 시간대·영업시간 밖은 제외.
      times   : 예약 가능한 미래 시간대 목록 (HH:MM)
      total   : 미래 슬롯 수 (지난 슬롯 제외, 가용 여부 무관)
      queried : API 호출 성공 여부
    """
    KST = timezone(timedelta(hours=9))
    now_kst = datetime.now(KST)

    # 네이버는 하루 24시간을 예약 단위(보통 30분)로 전부 내려준다. 영업시간 밖 슬롯도
    # isUnitSaleDay=true에 재고까지 채워서 오기 때문에, 이것만 보면 새벽 3시가 "자리
    # 있음"으로 잡힌다 (트루스 오브 뷰티: 실제 페이지는 11:00~18:30 16개인데 48개가 옴).
    # 실제 예약 페이지가 화면에서 빼는 기준이 isUnitBusinessDay이므로 같이 조회한다.
    # 이 필드를 지원하지 않는 스키마에 대비해, 실패하면 종전 필드만으로 재시도한다.
    def _query(fields: str) -> str:
        return (
            "query hourlySchedule($scheduleParams: ScheduleParams) {"
            "  schedule(input: $scheduleParams) {"
            "    bizItemSchedule {"
            f"      hourly {{ {fields} __typename }} __typename"
            "    } __typename"
            "  }"
            "}"
        )

    base_fields = "unitStartTime unitBookingCount unitStock isUnitSaleDay"
    queries = [_query(base_fields + " isUnitBusinessDay"), _query(base_fields)]

    try:
        data = None
        for i, query in enumerate(queries):
            resp = requests.post(
                "https://m.booking.naver.com/graphql?opName=hourlySchedule",
                json={
                    "operationName": "hourlySchedule",
                    "variables": {
                        "scheduleParams": {
                            "businessId": biz_id,
                            "businessTypeId": service_id,
                            "bizItemId": item_id,
                            "startDateTime": f"{target_date}T00:00:00+09:00",
                            "endDateTime": f"{target_date}T00:00:00+09:00",
                        }
                    },
                    "query": query,
                },
                headers=HEADERS,
                timeout=15,
            )
            if resp.status_code == 400 and i == 0:
                continue          # isUnitBusinessDay 미지원 스키마 → 종전 쿼리로
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                if i == 0:
                    data = None
                    continue
                return {"times": [], "total": 0, "queried": False, "all_slots": []}
            break
        if data is None:
            return {"times": [], "total": 0, "queried": False, "all_slots": []}

        hourly = data["data"]["schedule"]["bizItemSchedule"].get("hourly") or []

        future_slots = []
        for slot in hourly:
            if not slot.get("isUnitSaleDay"):
                continue
            # 필드를 안 내려주는 상품(None)은 종전대로 통과시킨다 — false일 때만 제외
            if slot.get("isUnitBusinessDay") is False:
                continue
            t_str = slot.get("unitStartTime")
            if t_str:
                try:
                    slot_dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
                    if slot_dt <= now_kst:
                        continue
                except ValueError:
                    pass
            future_slots.append(slot)

        available_times = [
            s["unitStartTime"][11:16]
            for s in future_slots
            if s.get("unitStock", 0) - s.get("unitBookingCount", 0) > 0
        ]

        # api_slot_count: 걸러내기 전 API가 내려준 시간대 개수.
        # "시간대가 원래 없는 상품"과 "시간대는 있는데 지금 살 게 없는 날"을 구분하는 데 쓴다.
        return {"times": available_times, "total": len(future_slots), "queried": True,
                "all_slots": future_slots, "api_slot_count": len(hourly)}

    except requests.HTTPError as e:
        print(f"  [오류] hourlySchedule API HTTP {e.response.status_code}", flush=True)
        if e.response.status_code in (429, 403):
            global _rate_limit_hits
            _rate_limit_hits += 1
        return {"times": [], "total": 0, "queried": False, "all_slots": [], "api_slot_count": 0}
    except Exception:
        return {"times": [], "total": 0, "queried": False, "all_slots": [], "api_slot_count": 0}


# 시간대(hourly) 슬롯이 없는 일 단위 상품에서 "하루 전체"를 가리키는 슬롯 이름.
# unitStartTime[11:16] 슬라이스가 그대로 이 값이 되도록 슬롯을 만들어,
# 시간대 상품과 같은 코드 경로로 잔여 좌석·알림·상태 저장이 돌게 한다.
DAY_UNIT_TIME = "종일"


def day_has_stock(day: dict | None) -> bool:
    """일별 요약(daily summary) 기준으로 아직 살 자리가 남아 있는 판매일인지."""
    if not day or not day.get("isSaleDay"):
        return False
    return (day.get("stock") or 0) - (day.get("bookingCount") or 0) > 0


def fetch_day_slots(parsed: dict, datekey: str, day: dict | None) -> dict:
    """해당 날짜의 시간대 슬롯 조회 (fetch_slots + 일 단위 상품 보정).

    시간대(hourly)를 아예 안 쓰고 일별 재고만 파는 상품이 있다. 그런 상품은 시간대가
    없다는 이유로 흘려보내면 자리가 나도 알림이 안 가므로, 하루 전체를 슬롯 하나로
    만들어 준다.

    단, "API가 시간대를 하나도 안 준 상품"일 때만 그렇게 한다. 시간대가 있는데
    지금 살 수 있는 게 없어서 목록이 비는 날(영업시간 밖만 남음·판매일 아님 등)까지
    일 단위로 보면, 일별 재고를 통째로 "자리 있음"으로 오해한다. 실제로 트루스
    오브 뷰티가 매진인데 "[종일] 521자리" 알림이 나갔다. 이 상품의 일별 재고는
    영업시간 밖 유령 슬롯까지 더한 값이라 실제 정원보다 훨씬 크다.
    """
    info = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], datekey)
    if info["queried"] and info.get("api_slot_count") == 0 and day_has_stock(day):
        stock = day.get("stock") or 0
        booked = day.get("bookingCount") or 0
        return {
            "times": [DAY_UNIT_TIME],
            "total": 1,
            "queried": True,
            "day_unit": True,
            "api_slot_count": 0,
            "all_slots": [{
                "unitStartTime": f"{datekey} {DAY_UNIT_TIME}",
                "unitStock": stock,
                "unitBookingCount": booked,
                "isUnitSaleDay": True,
            }],
        }
    return info


def fetch_calendar_day_status(service_id: int, biz_id: str, datekey: str) -> bool | None:
    """네이버 예약 캘린더(월별) API로 특정 날짜의 실제 마감 여부를 재확인.

    hourlySchedule/schedule GraphQL의 재고·예약수는 인기 상품에서 몇 분~몇십 분씩
    캐시가 지연돼, 실제로는 매진된 날짜인데도 자리 있음으로 보일 때가 있다(실제
    예약 페이지 캘린더에는 "마감"으로 뜨는데 우리 쪽은 계속 재고>0으로 판단하는
    사례 확인됨). 캘린더 API는 페이지가 실제로 쓰는 값을 그대로 반환하므로, 알림
    발송 직전 교차 확인용으로만 쓴다.
    True=예약 가능 확인, False=마감 확인, None=조회 실패/판단 불가(알림 보류하지 않음)."""
    ym = datekey[:7]
    try:
        resp = requests.get(
            f"https://m.booking.naver.com/booking/{service_id}/bizes/{biz_id}/calendars/{ym}",
            headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    calendars = data.get("calendars") or data.get("data")
    day = None
    if isinstance(calendars, list):
        day = next((d for d in calendars if d.get("date") == datekey or d.get("dateKey") == datekey), None)
    elif isinstance(calendars, dict):
        day = calendars.get(datekey)
    if not isinstance(day, dict):
        return None

    if day.get("available") or day.get("bookable"):
        return True
    status = (day.get("status") or "").upper()
    if status in ("AVAILABLE", "A"):
        return True
    if status:
        return False
    return None


def _log_alert_diagnostics(name: str, date_str: str, day_summary: dict | None,
                           slot_info: dict, ref_slots: list, cal_status: bool | None,
                           ba_code: str, ba_value: int, tag: str) -> None:
    """알림 발송을 판단하는 시점의 API 원본 값을 로그로 남긴다 (동작에는 영향 없음).

    실제 예약 페이지는 "마감"인데 우리 쪽은 자리 있음으로 판단해 알림이 나가는 사례가
    있었다(뷰오리: 재고 30 / 예약 0인 채로 상품이 내려갔는데도 hasBookableSlots=true).
    그런 일이 또 생겼을 때 어떤 필드가 실제 화면과 어긋났는지 로그만 보고 판단할 수
    있도록, 판단 근거가 된 값을 원본 그대로 남긴다.
    """
    d = day_summary or {}
    slots = ", ".join(
        f"{(s.get('unitStartTime') or '?')[11:16]}"
        f"(saleDay={s.get('isUnitSaleDay')},bizDay={s.get('isUnitBusinessDay')},"
        f"stock={s.get('unitStock')},booked={s.get('unitBookingCount')})"
        for s in (ref_slots or [])
    ) or "없음"
    print(f"  [진단:{tag}] {name} {date_str}", flush=True)
    print(f"  [진단:{tag}]   daily  = hasBookableSlots={d.get('hasBookableSlots')} "
          f"isSaleDay={d.get('isSaleDay')} stock={d.get('stock')} bookingCount={d.get('bookingCount')}", flush=True)
    print(f"  [진단:{tag}]   hourly = {slots}", flush=True)
    print(f"  [진단:{tag}]   기타   = calendarAPI={cal_status}(True=가능/False=마감/None=판단불가) "
          f"queried={slot_info.get('queried')} total={slot_info.get('total')} "
          f"예약제한={ba_code}/{ba_value}일", flush=True)


def fetch_item_restrictions(biz_id: str) -> dict:
    """업체(business) API에서 예약 가능 제한 코드·값을 조회.
    네이버 예약 업체 설정의 bookingAvailableCode / bookingAvailableValue 필드를 읽는다.
      RI01 → 제한 없음 (실시간 예약)
      RI02 → 일(day) 단위 사전 마감 (value=1 이면 당일예약 불가)
    조회 실패 시 빈 dict 반환."""
    query = (
        "query business($businessId: String) {"
        "  business(input: { businessId: $businessId }) {"
        "    bookingAvailableCode bookingAvailableValue __typename"
        "  }"
        "}"
    )
    try:
        resp = requests.post(
            "https://m.booking.naver.com/graphql?opName=business",
            json={"operationName": "business", "variables": {"businessId": biz_id}, "query": query},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            return {}
        biz = data["data"]["business"]
        return {
            "booking_available_code":  biz.get("bookingAvailableCode") or "RI01",
            "booking_available_value": int(biz.get("bookingAvailableValue") or 0),
        }
    except requests.HTTPError as e:
        if e.response.status_code in (429, 403):
            global _rate_limit_hits
            _rate_limit_hits += 1
        return {}
    except Exception:
        return {}


def check_kakao_dates(ticket_id: str, target_dates: list, kakao_cookies: str) -> list | None:
    today = datetime.now(timezone(timedelta(hours=9)))
    today_str = today.strftime("%Y-%m-%d")
    end_str = (today + timedelta(days=120)).strftime("%Y-%m-%d")
    headers = {**KAKAO_HEADERS}
    if kakao_cookies:
        headers["Cookie"] = kakao_cookies
    target_set = set(target_dates)
    try:
        resp = requests.get(
            KAKAO_API_URL,
            params={"ticketId": ticket_id, "preview": "false", "startDate": today_str, "endDate": end_str},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        return [
            d for d in resp.json()
            if d["date"] >= today_str
            and (not target_set or d["date"] in target_set)
        ]
    except requests.HTTPError as e:
        print(f"  [오류] 카카오 API HTTP {e.response.status_code}", flush=True)
        if e.response.status_code in (429, 403):
            global _rate_limit_hits
            _rate_limit_hits += 1
        return None
    except Exception as e:
        print(f"  [오류] 카카오 API 실패: {e}", flush=True)
        return None


def _parse_dt(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=9)))
        return dt
    except ValueError:
        return None


def _cache_entry_stale(cache_entry: dict, now_kst: datetime) -> bool:
    """schedule_cache 항목이 TTL을 넘겨 재탐색이 필요한지 여부.

    업체가 운영 기간을 도중에 바꾸면 캐시가 옛 기간을 물고 있게 되고,
    늘어난 날짜는 탐색 범위 제한에 걸려 아예 조회되지 않는다.
    checked_at이 TTL보다 오래된 항목은 루프 도중에도 다시 조회한다.
    """
    if SCHEDULE_CACHE_TTL_MIN <= 0:
        return False
    checked_at = _parse_dt(cache_entry.get("checked_at"))
    if checked_at is None:
        return True
    return (now_kst - checked_at) >= timedelta(minutes=SCHEDULE_CACHE_TTL_MIN)


def load_reprobe_requests(from_github: bool = True) -> dict:
    """웹앱이 남긴 운영 기간 재탐색 요청 로드 → {cache_key: 요청 시각 문자열}.

    실행 중인 job은 로컬 파일만 보면 요청을 볼 수 없으므로 GitHub raw를 먼저 읽는다.
    파일이 없거나 읽기에 실패하면 빈 dict (요청 없음으로 간주).
    """
    data = None
    if from_github:
        try:
            resp = requests.get(f"{GITHUB_RAW_REPROBE_URL}?_={int(time.time())}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
        except Exception as exc:
            print(f"[경고] 재탐색 요청 읽기 실패(GitHub), 로컬 파일 사용: {exc}", flush=True)
    if data is None:
        try:
            if REPROBE_REQUEST_FILE.exists():
                data = json.loads(REPROBE_REQUEST_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[경고] 재탐색 요청 파일 읽기 실패: {exc}", flush=True)
    if not isinstance(data, dict):
        return {}
    reqs = data.get("requests")
    return reqs if isinstance(reqs, dict) else {}


def _reprobe_requested(cache_entry: dict, requested_at: str | None) -> bool:
    """재탐색 요청 시각이 캐시의 checked_at보다 나중인지 (= 아직 처리 안 된 요청)."""
    req_dt = _parse_dt(requested_at)
    if req_dt is None:
        return False
    checked_at = _parse_dt(cache_entry.get("checked_at"))
    return checked_at is None or req_dt > checked_at


def _merge_probed_period(old: dict, new: dict) -> dict:
    """재탐색 결과(new)를 기존 캐시(old)와 병합하되, 이미 시작된 운영 기간의
    available_start는 유지한다.

    schedule API는 startDateTime=오늘 기준으로만 날짜를 돌려주므로(check_availability),
    이미 시작된 팝업을 재탐색하면 과거 시작일은 API 응답 범위 밖이라 보이지 않고 '오늘'이
    새 시작일처럼 관측된다. 이 값을 그대로 받아들이면 실제로는 기간이 바뀌지 않았는데도
    available_start가 하루 지날 때마다 오늘 날짜로 계속 밀려나며 "운영 기간 변경"으로
    오탐된다. 기존 시작일이 이미 지났다면(오늘 >= 기존 시작일) 재탐색으로는 그 값을 다시
    확인할 방법이 없으므로 새로 관측된 값을 버리고 기존 값을 그대로 유지한다.
    """
    old_start = old.get("available_start")
    if not old_start:
        return new
    try:
        already_started = date.fromisoformat(old_start) <= datetime.now(timezone(timedelta(hours=9))).date()
    except ValueError:
        already_started = False
    if not already_started:
        return new
    merged = dict(new)
    merged["available_start"] = old_start
    return merged


def _period_changed(old: dict, new: dict) -> bool:
    """운영 기간/예약 제한이 실제로 바뀌었는지 (checked_at 갱신만인 경우는 제외)."""
    keys = ("available_start", "available_end", "sale_start_date", "sale_end_date",
            "booking_available_code", "booking_available_value")
    return any(old.get(k) != new.get(k) for k in keys)


def _fmt_range(entry: dict, start_key: str, end_key: str) -> str:
    """캐시 항목의 기간을 'MM-DD~MM-DD' 형태로. 값이 없으면 '없음'."""
    s, e = entry.get(start_key), entry.get(end_key)
    if not s and not e:
        return "없음"
    return f"{(s or '?')[5:] if s else '?'}~{(e or '?')[5:] if e else '?'}"


def _describe_period_change(old: dict, new: dict) -> str:
    """운영 기간 변경 내용을 알림 본문용 문자열로. 바뀐 항목만 줄 단위로 나열."""
    lines = []

    if (old.get("available_start") != new.get("available_start")
            or old.get("available_end") != new.get("available_end")):
        before, after = _fmt_range(old, "available_start", "available_end"), _fmt_range(new, "available_start", "available_end")
        note = ""
        o_end, n_end = old.get("available_end"), new.get("available_end")
        if o_end and n_end:
            diff = (date.fromisoformat(n_end) - date.fromisoformat(o_end)).days
            if diff > 0:
                note = f" (종료일 {diff}일 연장)"
            elif diff < 0:
                note = f" (종료일 {-diff}일 단축)"
        lines.append(f"운영 기간: {before} → {after}{note}")

    if (old.get("sale_start_date") != new.get("sale_start_date")
            or old.get("sale_end_date") != new.get("sale_end_date")):
        lines.append(f"판매 기간: {_fmt_range(old, 'sale_start_date', 'sale_end_date')} → "
                     f"{_fmt_range(new, 'sale_start_date', 'sale_end_date')}")

    if (old.get("booking_available_code") != new.get("booking_available_code")
            or old.get("booking_available_value") != new.get("booking_available_value")):
        lines.append(f"예약 제한: {old.get('booking_available_code') or '없음'}/{old.get('booking_available_value') or 0}일 → "
                     f"{new.get('booking_available_code') or '없음'}/{new.get('booking_available_value') or 0}일")

    return "\n".join(lines)


def booking_window_status(item: dict, sale_start_date: str | None, sale_end_date: str | None) -> tuple[bool, str]:
    """(is_open, reason) 반환. is_open=True 이면 지금 예약 가능한 상태."""
    now = datetime.now(timezone(timedelta(hours=9)))

    manual_open = _parse_dt(item.get("booking_open_datetime"))
    manual_close = _parse_dt(item.get("booking_close_datetime"))

    if manual_open and now < manual_open:
        return False, f"예약 오픈 전 ({manual_open.strftime('%m/%d %H:%M')} 오픈)"
    if manual_close and now > manual_close:
        return False, f"예약 마감 ({manual_close.strftime('%m/%d %H:%M')} 종료)"

    api_start = _parse_dt(sale_start_date)
    api_end = _parse_dt(sale_end_date)

    if api_start and now < api_start:
        return False, f"예약 오픈 전 ({api_start.strftime('%m/%d %H:%M')} 오픈)"
    if api_end and now > api_end:
        return False, "예약 기간 종료"

    return True, ""


def _open_time_label(target: datetime | None) -> str:
    """오픈 예정 시각을 '오픈전-D일 H시 오픈' 형태로. target을 모르면 '오픈전'."""
    if not target:
        return "오픈전"
    time_part = target.strftime("%d일 %H시") if target.minute == 0 else target.strftime("%d일 %H시%M분")
    return f"오픈전-{time_part} 오픈"


def send_ntfy(topic: str, title: str, body: str, url: str) -> None:
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "urgent",
                "Click": url,
                "Tags": "bell",
            },
            timeout=10,
        )
        print("  → ntfy 전송 완료", flush=True)
    except Exception as exc:
        print(f"  [ntfy 오류] {exc}", flush=True)


def _format_slot_parts(per_slot: list[tuple[str, int]], prev_slots: dict | None) -> tuple[list[str], list[tuple[str, int]]]:
    """슬롯별 (시간, 잔여) 목록을 로그용 문자열로 변환.
    Returns (log_parts, increased) — increased는 이전 대비 증가한 (시간, 증가분) 목록.
    """
    increased = []
    if prev_slots is not None:
        for t, c in per_slot:
            d = c - prev_slots.get(t, 0)
            if d > 0:
                increased.append((t, d))
    inc_map = dict(increased)

    log_parts = []
    for t, c in per_slot:
        d = inc_map.get(t, 0)
        if d > 0:
            log_parts.append(f"[{t}] {c}자리(+{d})")
        else:
            log_parts.append(f"[{t}] {c}자리")
    return log_parts, increased


def _auto_book_cfg(item: dict) -> dict | None:
    """monitors.json의 auto_book 설정 정규화. 비활성/미설정이면 None.

    auto_book: true (하위 호환) 또는 객체:
      {enabled, dates[], times[](HH:MM 또는 HH:MM-HH:MM), accounts[](우선순위),
       count, mode("immediate"|"scheduled"), start_at(ISO)}
    빈 목록 = 모든 날짜/시간/계정 대상.
    """
    ab = item.get("auto_book")
    if not ab:
        return None
    if not isinstance(ab, dict):
        ab = {}
    cfg = {
        "enabled": ab.get("enabled", True),
        "dates": ab.get("dates", item.get("auto_book_dates") or []),
        "times": ab.get("times") or [],
        "accounts": ab.get("accounts") or [],
        "count": int(ab.get("count") or item.get("auto_book_count") or 1),
        "mode": ab.get("mode", "immediate"),
        "start_at": ab.get("start_at"),
        "saved_at": ab.get("saved_at"),
    }
    return cfg if cfg["enabled"] else None


def _match_time(t: str, patterns: list) -> bool:
    """"HH:MM"이 설정 패턴(정확한 시각 또는 "HH:MM-HH:MM" 범위)과 일치하는지."""
    if not patterns:
        return True
    for p in patterns:
        p = str(p).strip()
        if "-" in p[3:]:
            a, b = p.split("-", 1)
            if a.strip() <= t <= b.strip():
                return True
        elif t == p:
            return True
    return False


def load_auto_book_state(from_github: bool = True) -> dict:
    """자동예약 워커가 남긴 실행 결과(auto_book_state.json)를 읽는다.

    워커는 모니터와 다른 job/러너에서 돌기 때문에 결과는 저장소를 통해서만
    전달된다. 커밋 직후에도 바로 보이도록 캐시를 우회해서 받아온다.
    """
    if from_github:
        try:
            resp = requests.get(f"{GITHUB_RAW_AUTOBOOK_STATE_URL}?t={int(time.time())}",
                                headers={"Cache-Control": "no-cache"}, timeout=10)
            if resp.status_code == 404:
                return {}   # 아직 자동예약이 한 번도 돌지 않음
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            print(f"[경고] auto_book_state.json 읽기 실패, 로컬 파일 사용: {exc}", flush=True)
    try:
        if AUTO_BOOK_STATE_FILE.exists():
            return json.loads(AUTO_BOOK_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[경고] 로컬 auto_book_state.json 읽기 실패: {exc}", flush=True)
    return {}


def sync_auto_book_state(monitors: list, alerted: dict) -> None:
    """워커의 실행 결과를 모니터 상태(alerted)에 반영.

      - 예약 성공 기록을 {id}:auto_booked로 가져와 이후 디스패치를 멈춘다
      - 디스패치한 실행이 끝났으면 dispatched_at을 지워 다음 시도를 허용한다
    상태 파일을 못 읽으면 아무것도 건드리지 않는다 (실행 중으로 간주하고 대기).
    """
    remote = load_auto_book_state()
    for item in monitors:
        item_id = item.get("id", item.get("name", ""))
        if not item_id:
            continue
        name = item.get("name", item_id)
        entry = remote.get(item_id) or {}
        cfg = _auto_book_cfg(item)
        booked_key = f"{item_id}:auto_booked"

        # 워커 기록이 없으면 기존 로컬 기록(구버전 인라인 실행분)을 그대로 존중한다
        booked = entry.get("booked")
        if not isinstance(booked, dict):
            prev = alerted.get(booked_key)
            booked = prev if isinstance(prev, dict) else None
        if booked:
            booked_at = _parse_dt(booked.get("at"))
            saved_dt = _parse_dt(cfg.get("saved_at")) if cfg else None
            if booked_at and saved_dt and booked_at < saved_dt:
                # 예약 성공 이후 설정을 다시 저장하면(saved_at 갱신) 자동예약 재무장
                if booked_key in alerted:
                    print(f"  [자동예약] {name} — 설정 재저장 감지, 자동예약 재무장", flush=True)
                alerted.pop(booked_key, None)
            else:
                if booked_key not in alerted:
                    print(f"  [자동예약] {name} 예약 성공 확인 ({booked.get('date')} "
                          f"{booked.get('time') or ''}) — 이후 시도 중단", flush=True)
                alerted[booked_key] = booked

        state_key = f"{item_id}:auto_book_state"
        state = alerted.get(state_key)
        if not isinstance(state, dict) or not state.get("dispatched_at"):
            continue
        last = entry.get("last_run")
        if not isinstance(last, dict):
            continue
        last_at = _parse_dt(last.get("at"))
        disp_at = _parse_dt(state["dispatched_at"])
        if last_at and disp_at and last_at >= disp_at:
            state.pop("dispatched_at", None)
            if last.get("unbookable") and last.get("sig") == state.get("sig"):
                # 예약 페이지가 이 슬롯을 "선택 불가"로 막았다 → 지금 다시 띄워도 결과가 같다.
                # API가 계속 "자리 있음"이라고 답하는 동안 2분마다 헛도는 것을 막는다.
                state["blocked_until"] = (datetime.now(timezone(timedelta(hours=9)))
                                          + timedelta(minutes=AUTO_BOOK_BLOCKED_BACKOFF_MIN)
                                          ).isoformat(timespec="seconds")
                print(f"  [자동예약] {name} — 페이지가 선택 불가로 막은 슬롯 "
                      f"({state.get('sig')}) → {AUTO_BOOK_BLOCKED_BACKOFF_MIN}분간 재시도 보류", flush=True)
            alerted[state_key] = state
            print(f"  [자동예약] {name} 실행 종료: "
                  f"{'성공' if last.get('success') else '실패'} — {last.get('message', '')}", flush=True)


def dispatch_auto_book(item_id: str, datekey: str, times: list, sig: str,
                       attempt: int, detected_at: str = "") -> tuple[bool, str]:
    """autobook.yml 워크플로를 실행 요청한다. (성공여부, 오류메시지) 반환.

    detected_at(자리 감지 시각)은 워커가 "감지 → 예약 시작" 지연을 로그로 남기는 데 쓴다.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPOSITORY", "DuckOnDesk/naver-booking-monitor")
    ref = os.environ.get("AUTO_BOOK_WORKFLOW_REF") or os.environ.get("GITHUB_REF_NAME") or "main"
    if not token:
        return False, "GITHUB_TOKEN 없음 (monitor.yml의 env 설정 확인)"
    api = f"https://api.github.com/repos/{repo}/actions/workflows/{AUTO_BOOK_WORKFLOW}/dispatches"
    payload = {
        "ref": ref,
        "inputs": {
            "item_id": item_id,
            "date": datekey,
            "times": ",".join(times),
            "sig": sig,
            "attempt": str(attempt),
            "detected_at": detected_at,
        },
    }
    try:
        resp = requests.post(api, json=payload, timeout=15, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
    except Exception as exc:
        return False, f"디스패치 요청 실패: {exc}"
    if resp.status_code in (201, 204):
        return True, ""
    return False, f"디스패치 실패 (HTTP {resp.status_code}): {resp.text[:200]}"


def _date_only(value) -> str | None:
    """"2026-08-01T00:00:00+09:00" 같은 값에서 날짜(YYYY-MM-DD)만 뽑는다."""
    if not value:
        return None
    s = str(value).strip()[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def booking_period(cache_entry: dict) -> tuple[str | None, str | None]:
    """해당 예약 장소에 등록된 예약 기간 (시작일, 종료일). 모르면 (None, None).

    schedule_cache.json이 탐색해 둔 실제 예약 가능 기간(available_start~available_end)을
    우선 쓰고, 그게 없으면 네이버가 알려주는 판매 기간(saleStartDate~saleEndDate)을 쓴다.
    자동예약에서 날짜를 지정하지 않았을 때의 탐색 범위가 이 기간이다.
    """
    if not isinstance(cache_entry, dict):
        return None, None
    start = (_date_only(cache_entry.get("available_start"))
             or _date_only(cache_entry.get("sale_start_date")))
    end = (_date_only(cache_entry.get("available_end"))
           or _date_only(cache_entry.get("sale_end_date")))
    return start, end


def in_booking_period(datekey: str, period: tuple | None) -> bool:
    """datekey가 등록된 예약 기간 안인지. 기간을 모르면(양쪽 다 None) 항상 True."""
    start, end = period if period else (None, None)
    if start and datekey < start:
        return False
    if end and datekey > end:
        return False
    return True


def _period_label(period: tuple | None) -> str:
    start, end = period if period else (None, None)
    return f"{start or '?'}~{end or '?'}"


def load_schedule_cache(from_github: bool = False) -> dict:
    """schedule_cache.json 로드 (팝업별 등록된 예약 기간).

    워커처럼 체크아웃 시점이 뒤처질 수 있는 곳에서는 from_github=True로 최신본을 읽는다.
    """
    if from_github:
        try:
            resp = requests.get(f"{GITHUB_RAW_BASE}/schedule_cache.json", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data
        except Exception as exc:
            print(f"[경고] GitHub에서 schedule_cache.json 읽기 실패, 로컬 파일 사용: {exc}", flush=True)
    try:
        if SCHEDULE_CACHE_FILE.exists():
            data = json.loads(SCHEDULE_CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        print(f"[경고] schedule_cache.json 읽기 실패: {exc}", flush=True)
    return {}


def item_booking_period(item: dict, cache: dict) -> tuple[str | None, str | None]:
    """monitors.json 항목 + schedule_cache로 그 장소에 등록된 예약 기간을 구한다."""
    parsed = parse_naver_url(item.get("url", ""))
    if not parsed:
        return None, None
    key = f"{parsed['service_id']}_{parsed['biz_id']}_{parsed['item_id']}"
    return booking_period(cache.get(key) or {})


AUTO_BOOK_LOG_FILE = Path(__file__).parent / "auto_book_log.json"


def record_inline_attempt(entry: dict, tries: int = 2) -> None:
    """선(先)시도 결과를 auto_book_log.json에 남긴다 (웹 리포트용, best-effort).

    이 파일의 주인은 워커라, 모니터는 매번 원격 최신본을 받아 이번 한 줄만 얹는다.
    실패해도 예약 자체와는 무관하므로 경고만 남기고 넘어간다.
    """
    for attempt in range(1, tries + 1):
        try:
            subprocess.run(["git", "fetch", "origin", "main"], check=True, capture_output=True)
            subprocess.run(["git", "checkout", "origin/main", "--", AUTO_BOOK_LOG_FILE.name],
                           check=False, capture_output=True)
            log = []
            if AUTO_BOOK_LOG_FILE.exists():
                try:
                    log = json.loads(AUTO_BOOK_LOG_FILE.read_text(encoding="utf-8"))
                except Exception:
                    log = []
            if not isinstance(log, list):
                log = []
            log.insert(0, entry)
            del log[300:]
            AUTO_BOOK_LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
            if commit_files([AUTO_BOOK_LOG_FILE.name],
                            "data: 자동예약 선시도 결과 기록 [skip ci]", AUTO_BOOK_LOG_FILE.name):
                return
        except Exception as exc:
            print(f"[경고] 선시도 결과 기록 실패 ({attempt}/{tries}): {exc}", flush=True)


def _inline_try_book(item: dict, item_id: str, url: str, datekey: str,
                     times: list, cfg: dict, name: str) -> dict | None:
    """감지 즉시 모니터 러너에서 첫 계정으로 한 번 예약을 시도한다 (실패해도 워커가 이어받음).

    워크플로를 새로 띄우면 큐 대기 + 러너 준비로 30~70초가 지나간다. 취소표는 그 사이
    사라지므로, 이미 chromium이 깔려 있는 이 프로세스에서 먼저 한 번 눌러 보는 것이
    성공률에 가장 크게 기여한다. 대신 감시가 멈추는 시간을 짧게 유지하려고
      - 계정 1개만 (나머지는 워커가 담당)
      - AUTO_BOOK_INLINE_BUDGET_SEC(기본 25초) 안에 끝나지 않으면 포기
    로 제한한다. AUTO_BOOK_INLINE=0으로 끄면 종전처럼 워크플로만 쓴다.

    반환: try_book 결과 dict, 또는 시도하지 않았으면 None.
    """
    if not AUTO_BOOK_INLINE or not times:
        return None      # 일 단위 상품(고를 시간대가 없음)은 워커가 페이지에서 직접 찾는다
    try:
        import auto_book
    except Exception as exc:
        print(f"  [자동예약] 즉시 시도 불가(모듈 로드 실패): {exc}", flush=True)
        return None
    accounts = auto_book.get_accounts(cfg["accounts"])
    if not accounts:
        return None
    acct_no, cookie_str = accounts[0]
    print(f"  [자동예약] {name} {datekey} — 감지 즉시 계정{acct_no}로 선(先)시도 "
          f"(예산 {AUTO_BOOK_INLINE_BUDGET_SEC}초)", flush=True)
    # 예약창 확인용으로 열어 둔 chromium을 먼저 닫는다. playwright sync API는 한
    # 스레드에 세션이 살아 있으면 두 번째 세션을 거부한다 ("Sync API inside the
    # asyncio loop"). auto_book.try_book은 자기 세션을 새로 열기 때문에, 세션을
    # 물고 있으면 선시도가 0초 만에 예외로 죽는다. 다음 확인 때 알아서 다시 뜬다.
    _browser_close()
    started = time.time()
    try:
        res = auto_book.try_book(url, datekey, times, count=cfg["count"],
                                 cookie_str=cookie_str, account=acct_no,
                                 budget_sec=AUTO_BOOK_INLINE_BUDGET_SEC)
    except Exception as exc:
        print(f"  [자동예약] 즉시 시도 예외: {exc}", flush=True)
        return None
    took = time.time() - started
    print(f"  [자동예약] 즉시 시도 결과 [{took:.1f}초]: "
          f"{'성공' if res.get('success') else '실패'} — {res.get('message')}", flush=True)
    record_inline_attempt({
        "ts": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
        "item_id": item_id, "name": name, "date": datekey, "times": times,
        "account": res.get("account"), "success": bool(res.get("success")),
        "dry_run": bool(res.get("dry_run")), "elapsed": res.get("elapsed"),
        "unbookable": bool(res.get("unbookable")), "inline": True,
        "message": res.get("message", ""),
    })
    return res


def maybe_auto_book(item: dict, item_id: str, url: str, datekey: str,
                    per_slot: list, ntfy_topic: str, alerted: dict,
                    period: tuple | None = None, gate=None) -> None:
    """auto_book이 켜진 항목에서 예약 가능 슬롯 발견 시 자동예약 워크플로를 띄운다.

    실제 예약(Playwright, 계정별 재시도)은 autobook.yml → auto_book_worker.py가
    별도 러너에서 수행한다. 이 함수는 요청만 보내고 몇 초 안에 끝나므로
    예약을 시도하는 동안에도 모니터링 주기가 밀리지 않는다.

    상태는 booking_alerted.json에 저장:
      {id}:auto_booked      예약 성공 기록 (워커 결과를 sync_auto_book_state가 반영)
      {id}:auto_book_state  {"sig": 슬롯 시그니처, "attempts": 횟수,
                             "dispatched_at": 실행 중인 디스패치 시각}
    시도 내역은 워커가 auto_book_log.json에 기록한다 (웹 리포트에서 조회).
    """
    cfg = _auto_book_cfg(item)
    if not cfg:
        return
    if alerted.get(f"{item_id}:auto_booked"):
        return
    name = item.get("name", item_id)
    if cfg["dates"]:
        if datekey not in cfg["dates"]:
            return
    elif not in_booking_period(datekey, period):
        # 날짜 미지정 = "등록된 예약 기간 전체"가 대상 → 기간 밖 날짜는 시도하지 않는다
        print(f"  [자동예약] {name} {datekey} — 등록된 예약 기간({_period_label(period)}) 밖이라 건너뜀",
              flush=True)
        return

    now_kst = datetime.now(timezone(timedelta(hours=9)))
    if cfg["mode"] == "scheduled" and cfg["start_at"]:
        start_at = _parse_dt(cfg["start_at"])
        if start_at and now_kst < start_at:
            return

    times = [t for t, _ in per_slot if _match_time(t, cfg["times"])]
    if not times:
        return
    # 일 단위 상품은 워커에 넘길 시간대가 없다. 시그니처는 그대로 두고 시간 목록만
    # 비워 보내, 워커가 예약 페이지에서 직접 슬롯을 찾게 한다.
    dispatch_times = [] if times == [DAY_UNIT_TIME] else times
    sig = f"{datekey}|{','.join(times)}"
    state_key = f"{item_id}:auto_book_state"
    state = alerted.get(state_key)
    if not isinstance(state, dict) or state.get("sig") != sig:
        state = {"sig": sig, "attempts": 0}

    # 이미 실행 중이면 중복 디스패치하지 않는다. 결과가 올라오면
    # sync_auto_book_state가 dispatched_at을 지워 다음 시도를 열어준다.
    dispatched_at = _parse_dt(state.get("dispatched_at"))
    if dispatched_at:
        waited_min = (now_kst - dispatched_at).total_seconds() / 60
        if waited_min < AUTO_BOOK_DISPATCH_TIMEOUT_MIN:
            print(f"  [자동예약] {name} {datekey} — 예약 실행 중 ({waited_min:.1f}분 경과), 감시 계속", flush=True)
            alerted[state_key] = state
            return
        print(f"  [자동예약] {name} — {AUTO_BOOK_DISPATCH_TIMEOUT_MIN}분간 실행 결과 없음, 재시도", flush=True)
        state.pop("dispatched_at", None)

    # 예약 페이지가 "선택 불가"로 막았던 슬롯이면 잠시 쉰다 (API가 계속 자리 있다고 답해도)
    blocked_until = _parse_dt(state.get("blocked_until"))
    if blocked_until:
        if now_kst < blocked_until:
            print(f"  [자동예약] {name} {datekey} — 페이지에서 선택 불가로 확인된 슬롯, "
                  f"{(blocked_until - now_kst).total_seconds() / 60:.0f}분 뒤 재시도", flush=True)
            alerted[state_key] = state
            return
        state.pop("blocked_until", None)

    if AUTO_BOOK_MAX_ATTEMPTS and state["attempts"] >= AUTO_BOOK_MAX_ATTEMPTS:
        alerted[state_key] = state
        return

    # 예약을 누르기 직전에는 예약창 상태를 반드시 최신으로 본다. 확인 주기를
    # 5분으로 늘린 탓에 닫힌 페이지에 대고 예약을 시도하는 것이 가장 큰 손해다.
    if gate is not None:
        fresh = not gate.checked
        if not gate.verify_open():
            print(f"  [자동예약] {name} {datekey} — 예약 직전 예약창 재확인: 닫힘, 시도 생략", flush=True)
            alerted[state_key] = state
            return
        if fresh:
            print(f"  [자동예약] {name} — 예약 직전 예약창 재확인: 열림", flush=True)

    state["attempts"] += 1

    cap_label = f"/{AUTO_BOOK_MAX_ATTEMPTS}" if AUTO_BOOK_MAX_ATTEMPTS else " (성공/매진/OFF까지 계속)"
    detected_at = now_kst.isoformat(timespec="seconds")

    # 러너를 새로 띄우면 큐 대기 + 준비에 30~70초가 걸린다. 취소표는 그 사이 사라지므로,
    # 모니터 러너(이미 chromium이 있다)에서 첫 계정으로 즉시 한 번 시도해 본다.
    # 여기서 성공하면 워크플로는 띄우지 않고, 실패하면 종전대로 워커에 넘긴다.
    inline = _inline_try_book(item, item_id, url, datekey, dispatch_times, cfg, name)
    if inline is not None:
        if inline.get("success"):
            booked = {"date": datekey, "time": inline.get("booked_time"),
                      "account": inline.get("account"), "at": detected_at, "inline": True}
            alerted[f"{item_id}:auto_booked"] = booked
            alerted[state_key] = state
            if ntfy_topic:
                send_ntfy(ntfy_topic, f"🎫 {name} 자동예약 성공!",
                          f"{datekey} {inline.get('booked_time') or ''} "
                          f"(계정{inline.get('account')}, 즉시 시도) 예약 완료 "
                          f"— 네이버 예약 내역에서 확인하세요", url)
            return
        if inline.get("unbookable"):
            # 페이지가 막은 슬롯 — 워커를 띄워 봐야 같은 결과다
            state["blocked_until"] = (now_kst + timedelta(minutes=AUTO_BOOK_BLOCKED_BACKOFF_MIN)
                                      ).isoformat(timespec="seconds")
            print(f"  [자동예약] {name} {datekey} — 즉시 시도에서 선택 불가 확인, "
                  f"{AUTO_BOOK_BLOCKED_BACKOFF_MIN}분간 보류 (워크플로 생략)", flush=True)
            alerted[state_key] = state
            return

    ok, err = dispatch_auto_book(item_id, datekey, dispatch_times, sig, state["attempts"], detected_at)
    if ok:
        state["dispatched_at"] = detected_at
        print(f"  [자동예약] {name} {datekey} {','.join(times[:5])} — "
              f"예약 워크플로 실행 요청 {state['attempts']}{cap_label}", flush=True)
    else:
        print(f"  [자동예약] {name} — {err}", flush=True)
        if state["attempts"] == 1 and ntfy_topic:
            send_ntfy(ntfy_topic, f"⚠️ {name} 자동예약 실행 요청 실패",
                      f"{datekey} — {err}\n직접 예약을 시도해보세요!", url)
    alerted[state_key] = state


def sweep_auto_book_period(item: dict, item_id: str, url: str, parsed: dict,
                           all_summary: list, period: tuple, covered: set,
                           cutoff_date, ntfy_topic: str, alerted: dict, gate=None) -> None:
    """자동예약 날짜를 지정하지 않은 항목의 남은 예약 기간을 마저 훑는다.

    자동예약에서 날짜를 비워 두면 "등록된 예약 기간 전체"가 대상이다. 그런데 감시
    날짜(target_dates)를 좁게 잡아 둔 항목은 메인 루프가 그 날짜만 돌기 때문에,
    기간 안의 나머지 날짜는 자리가 나도 자동예약이 걸리지 않았다.

    메인 루프가 이미 확인한 날짜(covered)는 건너뛰므로, 추가 조회는 기간 안에서
    아직 안 본 "예약 가능 슬롯이 있는 날짜"에만 발생한다.
    """
    cfg = _auto_book_cfg(item)
    if not cfg or cfg["dates"]:
        return                      # 날짜를 지정한 항목은 그 날짜만 대상
    if alerted.get(f"{item_id}:auto_booked"):
        return
    start, end = period if period else (None, None)
    if not start and not end:
        return                      # 예약 기간을 모르면 기존 동작(메인 루프)만 유지

    name = item.get("name", item_id)
    today_str = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    by_date = {d.get("dateKey"): d for d in all_summary}
    extra = sorted(
        d["dateKey"] for d in all_summary
        if (d.get("hasBookableSlots") or day_has_stock(d)) and d.get("dateKey") not in covered
        and d.get("dateKey", "") >= today_str
        and in_booking_period(d["dateKey"], period)
        and not (cutoff_date and date.fromisoformat(d["dateKey"]) < cutoff_date)
    )
    if not extra:
        return
    if len(extra) > AUTO_BOOK_SWEEP_MAX:
        # 기간이 아주 긴 항목에서 한 회차에 API를 과도하게 쓰지 않도록 앞쪽(가까운 날짜)만
        print(f"  [자동예약] {name} — 예약 기간 내 추가 날짜 {len(extra)}개 중 "
              f"가까운 {AUTO_BOOK_SWEEP_MAX}개만 이번 회차에 확인", flush=True)
        extra = extra[:AUTO_BOOK_SWEEP_MAX]
    log_state(f"{item_id}:sweep",
              f"  [자동예약] {name} — 날짜 미지정: 예약 기간({_period_label(period)}) 중 "
              f"감시 대상 밖 날짜 {len(extra)}개 추가 확인: {', '.join(extra[:5])}"
              f"{' 외' if len(extra) > 5 else ''}", stamp=False)
    for datekey in extra:
        slot_info = fetch_day_slots(parsed, datekey, by_date.get(datekey))
        if not slot_info["queried"]:
            continue
        per_slot = [
            (s["unitStartTime"][11:16], s.get("unitStock", 0) - s.get("unitBookingCount", 0))
            for s in slot_info.get("all_slots", [])
            if s.get("unitStock", 0) - s.get("unitBookingCount", 0) > 0
        ]
        if not per_slot:
            continue
        maybe_auto_book(item, item_id, url, datekey, per_slot, ntfy_topic, alerted, period, gate)


_CLOSED_URL_PATTERNS  = ["/error/"]
_CLOSED_TEXT_PATTERNS = [
    "운영하지 않는 예매 페이지",
    "판매 기간이 아닙니다",
    "판매기간이 아닙니다",
    "예약을 받고 있지 않습니다",
    "예약이 마감되었습니다",
    "더 이상 예약할 수 없습니다",
]

_pw_handle = None    # sync_playwright() 핸들
_pw_browser = None   # 재사용하는 chromium 인스턴스


def _browser_get():
    """살아 있는 chromium을 돌려준다 (없거나 죽었으면 새로 띄운다).

    확인할 때마다 launch/close를 하면 그것만으로 2초가 나간다(실측). 이 프로세스는
    5시간 넘게 살아 있으니 한 번 띄워 계속 쓰는 편이 훨씬 싸다. 다만 브라우저가
    중간에 죽으면 이후 확인이 전부 예외로 떨어지고, 예외는 '열림 간주'로 처리되기
    때문에 닫힘을 영영 감지하지 못한 채 조용히 흘러간다. 그래서 쓸 때마다 연결을
    확인하고 끊겼으면 새로 띄운다.
    """
    global _pw_handle, _pw_browser
    if _pw_browser is not None:
        try:
            if _pw_browser.is_connected():
                return _pw_browser
        except Exception:
            pass
        _browser_close()
    from playwright.sync_api import sync_playwright
    _pw_handle = sync_playwright().start()
    _pw_browser = _pw_handle.chromium.launch(headless=True)
    return _pw_browser


def _browser_close() -> None:
    """브라우저와 playwright 핸들을 정리한다 (실패해도 넘어간다)."""
    global _pw_handle, _pw_browser
    for obj, meth in ((_pw_browser, "close"), (_pw_handle, "stop")):
        try:
            if obj is not None:
                getattr(obj, meth)()
        except Exception:
            pass
    _pw_browser = _pw_handle = None


def _playwright_check(url: str) -> tuple[bool, str]:
    """(is_closed, reason) 반환. URL/텍스트 기반으로 예약창 닫힘 감지."""
    item_match = re.search(r"/items/\d+", url)
    item_path = item_match.group(0) if item_match else None
    context = None
    try:
        browser = _browser_get()
        # 컨텍스트는 매번 새로 만든다. 재사용하면 이전 페이지의 URL·쿠키가 남아
        # 리다이렉트 판정이 오염된다.
        context = browser.new_context()
        cookie_str = os.environ.get("NAVER_COOKIES", "").strip()
        if cookie_str:
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
            if cookies:
                context.add_cookies(cookies)
        page = context.new_page()
        page.goto(url, wait_until="load", timeout=15000)
        page.wait_for_timeout(2000)
        final_url = page.url
        for pat in _CLOSED_URL_PATTERNS:
            if pat in final_url:
                return True, f"URL 리다이렉트: {pat}"
        if item_path and item_path not in final_url:
            return True, f"URL 리다이렉트: 상품 페이지({item_path}) 이탈"
        visible_text = " ".join(page.inner_text("body").split())
        for pat in _CLOSED_TEXT_PATTERNS:
            if pat in visible_text:
                return True, f"페이지 텍스트: {pat}"
        return False, ""
    except Exception as exc:
        print(f"  [경고] playwright 확인 실패 → 열림으로 간주: {exc}", flush=True)
        # 페이지 하나가 느린 것과 브라우저가 죽은 것은 다르다. 연결이 끊겼을 때만
        # 인스턴스를 버려서, 단발 타임아웃 때문에 매번 재기동하지 않도록 한다.
        try:
            if _pw_browser is not None and not _pw_browser.is_connected():
                _browser_close()
        except Exception:
            _browser_close()
        return False, ""
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass


# ── 예약창 확인(브라우저) 정책 ──────────────────────────────────────────
# 예약창 열림/닫힘 확인은 항목당 4초가 넘는다(고정비 실측 4.07초 + 페이지 로드).
# 항목이 8개면 한 회차의 절반 이상을 여기에 쓴다. 그런데 이 값이 실제로 필요한
# 순간은 "알릴 자리가 있을 때"뿐이다. 자리가 없으면 🎉로도 🔒로도 알릴 게 없다.
# 그래서 자리를 찾기 전에는 브라우저를 켜지 않는다 (UrlGate).
CLOSE_CONFIRM = 2   # 오탐 방지: 연속 이 횟수만큼 닫힘이어야 확정
# 열려 있는 것으로 확인된 항목을 다시 확인하기까지의 간격(초).
URL_RECHECK_SEC = _env_num("URL_RECHECK_SEC", 300)

_url_checked_at: dict[str, float] = {}   # item_id → 마지막 확인 시각(monotonic)
_url_checks_done = 0                     # 이번 회차에 실제로 브라우저를 켠 횟수
_url_skips: dict[str, int] = {}          # 이번 회차에 건너뛴 사유별 건수


def _note_url_skip(reason: str) -> None:
    _url_skips[reason] = _url_skips.get(reason, 0) + 1


class UrlGate:
    """예약창 열림/닫힘을 '필요해질 때만' 확인하는 게이트.

    자리 판정은 API로만 하고, 브라우저는 그 결과에 라벨을 붙이는 용도다.
    따라서 자리를 찾기 전에는 켤 이유가 없다. 첫 자리를 만났을 때 한 번 확인해
    그 회차 내내 재사용한다.

      - 자리 없음         → 확인하지 않음 (예약창 상태는 '모름'으로 남는다)
      - 자리 있고 닫힘    → 매 회차 확인 (열리는 순간을 놓치지 않기 위해)
      - 자리 있고 열림    → URL_RECHECK_SEC(기본 5분)마다
      - 닫힘 감지 진행 중 → 확정될 때까지 매 회차 (확정이 5분×2로 늘어나지 않도록)
      - 자동예약 직전     → 주기와 무관하게 확인 (verify_open)

    닫힘/열림 상태와 연속 카운트는 종전처럼 alerted에 남아 프로세스 재시작을 넘어간다.
    """

    def __init__(self, item: dict, item_id: str, url: str, name: str,
                 alerted: dict, ntfy_topic: str, now_str: str):
        self.item, self.item_id, self.url, self.name = item, item_id, url, name
        self.alerted, self.ntfy_topic, self.now_str = alerted, ntfy_topic, now_str
        self.closed_key = f"{item_id}:url_closed"
        self.streak_key = f"{item_id}:url_close_streak"
        self.checked = False          # 이번 회차에 브라우저를 켰는지
        self.consulted = False        # 이번 회차에 상태를 물어보기는 했는지(=자리를 찾았는지)
        self.just_reopened = False
        self._closed = bool(alerted.get(self.closed_key))

    @property
    def known_closed(self) -> bool:
        """마지막으로 알던 상태. 브라우저를 켜지 않는다 (스윕 게이트용)."""
        return self._closed

    @property
    def closed(self) -> bool:
        """지금 닫혀 있는가. 필요하면 여기서 브라우저를 켠다."""
        self._ensure()
        return self._closed

    def verify_open(self) -> bool:
        """자동예약 직전 확인. 이번 회차에 아직 안 봤으면 주기를 무시하고 지금 본다."""
        if not self.checked:
            self._run_check()
        return not self._closed

    def _ensure(self) -> None:
        self.consulted = True
        if self.checked:
            return
        # 닫혀 있거나 닫힘 확정을 기다리는 중이면 매 회차 본다. 열려 있는 항목만
        # 주기를 둔다 — 열림→닫힘은 늦게 알아도 손해가 작지만, 닫힘→열림은
        # 오픈 순간이라 늦으면 그대로 놓친다.
        if self._closed or self.alerted.get(self.streak_key):
            self._run_check()
            return
        last = _url_checked_at.get(self.item_id)
        if last is not None and (time.monotonic() - last) < URL_RECHECK_SEC:
            _note_url_skip("주기 대기")
            return
        self._run_check()

    def _run_check(self) -> None:
        global _url_checks_done
        raw_closed, reason = _playwright_check(self.url)
        self.checked = True
        _url_checks_done += 1
        _url_checked_at[self.item_id] = time.monotonic()

        alerted, name, now_str = self.alerted, self.name, self.now_str
        if raw_closed:
            # 오탐(느린 로딩·리다이렉트)이 있어 연속 2회여야 확정한다. 값에 상한을
            # 두지 않으면 닫힌 항목이 있는 한 booking_alerted.json이 매 회차 바뀌어
            # 회차마다 git 커밋·푸시가 돈다.
            streak = min(int(alerted.get(self.streak_key, 0)) + 1, CLOSE_CONFIRM)
            alerted[self.streak_key] = streak
            self._closed = streak >= CLOSE_CONFIRM
            if not self._closed:
                print(f"[{now_str}] ⚠️ {name} — 닫힘 감지 ({streak}/{CLOSE_CONFIRM}, "
                      f"확정 전 대기): {reason}", flush=True)
        else:
            alerted.pop(self.streak_key, None)
            self._closed = False

        if self._closed:
            item_prefix = f"{self.item_id}:"
            for k in list(alerted.keys()):
                if (k.startswith(item_prefix) and k != self.closed_key
                        and k != self.streak_key and not k.endswith(":closed")):
                    alerted.pop(k)
            alerted[self.closed_key] = 1
            log_state(f"{self.item_id}:status", f"🔒 {name} — 예약창 닫힘 ({reason})", now_str=now_str)
        elif self.closed_key in alerted:
            self.just_reopened = True
            alerted.pop(self.closed_key)
            item_prefix = f"{self.item_id}:"
            for k in list(alerted.keys()):
                if k.startswith(item_prefix) and k.endswith(":closed"):
                    # :closed 항목을 삭제하지 않고 일반 키로 복사 → 이미 본 슬롯 재알림 방지
                    base_key = k[: -len(":closed")]
                    alerted[base_key] = alerted.pop(k)
            print(f"[{now_str}] ✅ {name} — 예약창 열림 (방금 전환됨)", flush=True)
            if self.ntfy_topic:
                send_ntfy(self.ntfy_topic, f"✅ {name} 예약창 열림",
                          "예약창이 열렸습니다. 직접 확인해보세요!", self.url)
            _log_state[f"{self.item_id}:status"] = ("열림", time.monotonic())
        elif not raw_closed:
            # raw_closed인데 여기 오면 '닫힘 감지, 확정 전 대기' 상태다. 그 회차에
            # "예약창 열림"까지 찍으면 로그가 서로 반대되는 말을 하게 되므로 ⚠️ 줄만 남긴다.
            log_state(f"{self.item_id}:status", f"✅ {name} — 예약창 열림",
                      sig="열림", now_str=now_str)


def log_url_check_summary() -> None:
    """이번 회차에 브라우저를 몇 번 켰고 몇 건을 건너뛰었는지 한 줄로.

    아무것도 안 찍힌 회차에서는 이 줄도 찍지 않는다 (통째로 침묵).
    """
    skipped = sum(_url_skips.values())
    if not _round_printed or not (_url_checks_done or skipped):
        return
    detail = ", ".join(f"{r} {n}" for r, n in sorted(_url_skips.items()))
    log_state("round:urlcheck",
              f"  → 예약창 확인 {_url_checks_done}건 / 생략 {skipped}건"
              + (f" ({detail})" if detail else ""), stamp=False)


def _playwright_final_url(url: str) -> str:
    """하위 호환용. 예약창 닫힘이면 '/error/' 포함 문자열 반환."""
    is_closed, _ = _playwright_check(url)
    return "/error/" if is_closed else url


def check_booking_accessible(url: str) -> bool:
    """예약 URL 접근 가능 여부. True = 열림."""
    is_closed, _ = _playwright_check(url)
    return not is_closed


def check_all(monitors: list, ntfy_topic: str, alerted: dict) -> None:
    now_kst = datetime.now(timezone(timedelta(hours=9)))
    now_str = now_kst.strftime("%H:%M:%S")
    today_str = now_kst.strftime("%Y-%m-%d")
    active = [m for m in monitors if m.get("enabled", True)]

    try:
        sched_cache = json.loads(SCHEDULE_CACHE_FILE.read_text(encoding="utf-8")) if SCHEDULE_CACHE_FILE.exists() else {}
    except Exception:
        sched_cache = {}

    global _log_skipped, _url_checks_done, _round_printed
    _log_skipped = 0
    _url_checks_done = 0
    _round_printed = False      # 이 회차에 뭐라도 찍혔는지 (조용한 회차 판정용)
    _url_skips.clear()

    _pruned_dates: list[tuple[str, str]] = []
    _reprobed_this_round = 0  # 이번 회차에 TTL 만료로 재탐색한 항목 수
    reprobe_reqs = load_reprobe_requests()  # 웹앱의 "운영 기간 초기화" 요청

    for item in active:
        name = item.get("name", "?")
        url = item.get("url", "")

        ab_cfg = _auto_book_cfg(item)
        if ab_cfg and ab_cfg["mode"] == "scheduled" and ab_cfg["start_at"]:
            start_at = _parse_dt(ab_cfg["start_at"])
            if start_at and now_kst < start_at:
                remain_min = int((start_at - now_kst).total_seconds() // 60)
                # 남은 시간은 매 회차 줄어드니 서명에서 뺀다 (상태는 '대기 중' 하나)
                log_state(f"{item.get('id', name)}:status",
                          f"⏸ {name} — 자동예약 시작 대기 중 "
                          f"({start_at.strftime('%m/%d %H:%M')}, {remain_min}분 남음, 탐색 보류)",
                          sig=f"ab_wait:{ab_cfg['start_at']}", now_str=now_str)
                continue

        if item.get("type") == "kakao":
            item_id = item.get("id", name)
            ticket_id = parse_kakao_url(url)
            if not ticket_id:
                print(f"[{now_str}] URL 파싱 실패 (카카오): {name}", flush=True)
                continue
            kakao_cookies = os.environ.get("KAKAO_COOKIES", "").strip()
            all_kakao = check_kakao_dates(ticket_id, item.get("target_dates", []), kakao_cookies)
            if all_kakao is None:
                print(f"[{now_str}] {name} — 카카오 API 실패", flush=True)
                continue

            closed_key = f"{item_id}:kakao_closed"
            item_prefix = f"{item_id}:"
            weekdays_k = ["월", "화", "수", "목", "금", "토", "일"]

            # available=true인 날짜가 하나도 없으면 예약창 닫힘
            sale_dates = [d for d in all_kakao if d.get("available")]
            if not sale_dates:
                log_state(f"{item_id}:status", f"🔒 {name} — 예약창 닫힘", now_str=now_str)
                for k in list(alerted.keys()):
                    if k.startswith(item_prefix) and k != closed_key:
                        alerted.pop(k)
                alerted[closed_key] = 1
                continue

            # 예약창 열림 (이전에 닫혔다가 열린 경우 알림)
            if closed_key in alerted:
                alerted.pop(closed_key)
                print(f"[{now_str}] ✅ {name} — 예약창 열림 (방금 전환됨)", flush=True)
                if ntfy_topic:
                    send_ntfy(ntfy_topic, f"✅ {name} 예약창 열림", "예약창이 열렸습니다. 직접 확인해보세요!", url)
                _log_state[f"{item_id}:status"] = ("열림", time.monotonic())
            else:
                log_state(f"{item_id}:status", f"✅ {name} — 예약창 열림",
                          sig="열림", now_str=now_str)

            new_date_details = []
            current_available_set = set()
            for d in sale_dates:
                dk = d["date"]
                stock = d.get("stock") or 0
                dow = weekdays_k[date.fromisoformat(dk).weekday()]
                ds = f"{dk[5:]}({dow})"
                ak = f"{item_id}:{dk}"
                if stock > 0:
                    current_available_set.add(dk)
                    log_state(f"log:{ak}", f"🎉 {name} {ds} {stock}자리 (예약가능:{stock})", now_str=now_str)
                    if ak not in alerted:
                        new_date_details.append(f"{ds} {stock}자리")
                        alerted[ak] = stock
                else:
                    alerted.pop(ak, None)
                    log_state(f"log:{ak}", f"❌ {name} {ds} 매진 (예약가능:0)", now_str=now_str)

            for k in list(alerted.keys()):
                if k.startswith(item_prefix) and k != closed_key and k[len(item_prefix):] not in current_available_set:
                    alerted.pop(k)

            if new_date_details and ntfy_topic:
                send_ntfy(ntfy_topic, f"🎉 {name} 예약 가능!", ", ".join(new_date_details), url)
            continue

        target_time_map: dict[str, tuple[str, str] | None] = {}
        for entry in item.get("target_dates", []):
            parts = entry.strip().split(" ", 1)
            d_part = parts[0]
            if len(parts) > 1:
                t_str = parts[1]
                if "-" in t_str[3:]:
                    t_from, t_to = t_str.split("-", 1)
                else:
                    t_from = t_to = t_str[:5]
                if d_part not in target_time_map:
                    target_time_map[d_part] = (t_from, t_to)
            else:
                if d_part not in target_time_map:
                    target_time_map[d_part] = None
        target_dates_only = list(target_time_map.keys())
        has_target_dates = bool(target_dates_only)

        parsed = parse_naver_url(url)
        if not parsed:
            print(f"[{now_str}] URL 파싱 실패: {name}", flush=True)
            continue

        cache_key   = f"{parsed['service_id']}_{parsed['biz_id']}_{parsed['item_id']}"
        cache_entry = sched_cache.get(cache_key, {})

        is_first_probe = not cache_entry
        # 웹앱에서 "운영 기간 초기화"를 누른 항목은 사용자가 결과를 기다리고 있으므로
        # TTL·회차당 건수 제한을 모두 무시하고 이번 회차에 바로 재탐색한다.
        forced = _reprobe_requested(cache_entry, reprobe_reqs.get(cache_key))
        # 캐시가 없으면 즉시, 있으면 TTL 경과 시 재탐색 (회차당 최대 SCHEDULE_REPROBE_PER_ROUND건)
        need_probe = is_first_probe or forced or (
            _reprobed_this_round < SCHEDULE_REPROBE_PER_ROUND
            and _cache_entry_stale(cache_entry, now_kst)
        )
        if forced and not is_first_probe:
            print(f"[{now_str}] ↻ {name} 운영 기간 초기화 요청 감지 — 즉시 재탐색", flush=True)
        if need_probe:
            probed = probe_schedule_period(parsed)
            if probed:
                if not is_first_probe:
                    _reprobed_this_round += 1
                cache_entry_before = cache_entry
                probed = _merge_probed_period(cache_entry_before, probed)
                changed = _period_changed(cache_entry_before, probed)
                old_range = f"{cache_entry_before.get('available_start')}~{cache_entry_before.get('available_end')}"
                new_range = f"{probed.get('available_start')}~{probed.get('available_end')}"
                sched_cache[cache_key] = probed
                cache_entry = probed
                # checked_at만 바뀐 재탐색까지 커밋하면 커밋이 과도하게 쌓이므로,
                # 파일은 항상 갱신하되(다음 TTL 기준점) 커밋은 실제 변경 시에만 한다.
                save_schedule_cache(sched_cache)
                if is_first_probe:
                    print(f"[{now_str}] — {name} 운영 기간 최초 확인: {new_range}", flush=True)
                elif changed:
                    prefix = "↻ 운영 기간 초기화 완료" if forced else "🔄 운영 기간 변경 감지"
                    print(f"[{now_str}] {prefix}: {name} {old_range} → {new_range}", flush=True)
                elif forced:
                    print(f"[{now_str}] ↻ {name} 운영 기간 초기화 완료: {new_range} (변경 없음)", flush=True)
                else:
                    print(f"[{now_str}] — {name} 운영 기간 재확인: {new_range} (변경 없음)", flush=True)

                # forced일 때 변경이 없어도 커밋해야 한다. 요청 처리 완료 판정이 원격
                # checked_at 기준이라, 안 올리면 요청이 계속 유효해 매 회차 재탐색이 반복된다.
                if is_first_probe or changed or forced:
                    commit_schedule_cache()

                if changed and not is_first_probe and ntfy_topic:
                    body = _describe_period_change(cache_entry_before, probed)
                    # 자동예약 날짜를 직접 지정해 둔 항목은 기간이 늘어나도 그 날짜만 시도한다.
                    # 늘어난 날짜를 놓치기 쉬운 지점이라 알림에 같이 알려준다.
                    _ab = _auto_book_cfg(item)
                    if _ab and _ab["dates"]:
                        body += "\n⚠️ 자동예약 날짜가 지정돼 있어 새로 늘어난 날짜는 대상이 아닙니다."
                    send_ntfy(ntfy_topic, f"🔄 {name} 운영 기간 변경", body, url)
            elif not is_first_probe:
                _reprobed_this_round += 1
                print(f"[{now_str}] [경고] {name} 운영 기간 재확인 실패 — 기존 캐시 유지", flush=True)

        avail_start = cache_entry.get("available_start")
        avail_end   = cache_entry.get("available_end")
        # 자동예약에서 날짜를 지정하지 않았을 때의 탐색 범위 (= 등록된 예약 기간)
        ab_period = booking_period(cache_entry)

        # 업체 설정 사전예약 제한 (RI02 = 일 단위 마감)
        ba_code  = cache_entry.get("booking_available_code", "RI01")
        ba_value = int(cache_entry.get("booking_available_value") or 0)
        # cutoff_date: 이 날짜 미만인 datekey는 예약 불가 (당일 포함)
        if ba_code == "RI02" and ba_value > 0:
            cutoff_date = now_kst.date() + timedelta(days=ba_value)
        else:
            cutoff_date = None

        item_id   = item.get("id", name)
        # 예약창 확인은 여기서 하지 않는다. 알릴 자리를 실제로 찾은 뒤에야
        # 게이트가 필요할 때 브라우저를 켠다 (UrlGate 참고).
        gate = UrlGate(item, item_id, url, name, alerted, ntfy_topic, now_str)

        result = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], target_dates_only)
        if result is None:
            print(f"[{now_str}] {name} — API 실패 (자리 미확인, 예약창 확인 생략)", flush=True)
            _note_url_skip("API 실패")
            continue

        days_map = {d["dateKey"]: d for d in result["days"]}
        window_open, window_reason = booking_window_status(item, result["sale_start_date"], result["sale_end_date"])
        weekdays = ["월", "화", "수", "목", "금", "토", "일"]

        if not target_dates_only:
            all_summary = result.get("_all_summary") or []
            discovered = [d["dateKey"] for d in all_summary if d.get("isSaleDay")]

            scan_start = now_kst.date()
            if avail_start and avail_start > scan_start.isoformat():
                scan_start = date.fromisoformat(avail_start)
            scan_end = date.fromisoformat(avail_end) if avail_end else scan_start + timedelta(days=30)

            if not discovered:
                log_state(f"{item_id}:scan", f"— {name} 전체 날짜 스캔 중...", now_str=now_str)
                cur = scan_start
                while cur <= scan_end:
                    dk = cur.isoformat()
                    si = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], dk)
                    if si["queried"] and si.get("all_slots"):
                        discovered.append(dk)
                    cur += timedelta(days=1)
            elif not avail_end:
                last_known = date.fromisoformat(max(discovered))
                ext_start = last_known + timedelta(days=1)
                if ext_start <= scan_end:
                    print(f"[{now_str}] — {name} API 윈도우 너머 스캔 중 ({ext_start}~{scan_end})...", flush=True)
                    cur = ext_start
                    while cur <= scan_end:
                        dk = cur.isoformat()
                        si = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], dk)
                        if si["queried"] and si.get("all_slots"):
                            discovered.append(dk)
                        cur += timedelta(days=1)

            if not discovered:
                log_state(f"{item_id}:nosale", f"— {name} 판매 중인 날짜 없음 (캐시 기간 내)", now_str=now_str)
                continue
            effective_dates = discovered
        else:
            effective_dates = target_dates_only

        # schedule_cache.json의 알려진 운영 기간 내로 탐색 범위 제한
        if avail_start or avail_end:
            trimmed = [
                d for d in effective_dates
                if (not avail_start or d >= avail_start) and (not avail_end or d <= avail_end)
            ]
            if len(trimmed) < len(effective_dates):
                log_state(f"{item_id}:trim",
                          f"— {name} 운영 기간({avail_start}~{avail_end}) 외 날짜 "
                          f"{len(effective_dates)-len(trimmed)}개 제외", now_str=now_str)
            effective_dates = trimmed

        for datekey in effective_dates:
            dow       = weekdays[date.fromisoformat(datekey).weekday()]
            date_str  = f"{datekey[5:]}({dow})"
            alert_key = f"{item_id}:{datekey}"
            # 날짜 하나 = 상태 하나. 어느 분기로 가든 같은 key로 비교해야
            # 매진 ↔ 자리 있음 같은 분기 전환이 '변화'로 잡힌다.
            log_key   = f"log:{item_id}:{datekey}"
            time_range = target_time_map.get(datekey)

            if datekey < today_str:
                continue
            # 업체 사전예약 제한: cutoff_date 미만 날짜는 예약 불가 → 알림만 제외, 로그는 그대로 표시
            is_restricted = bool(cutoff_date and date.fromisoformat(datekey) < cutoff_date)
            if is_restricted:
                alerted.pop(alert_key, None)
                alerted.pop(f"{alert_key}:pre", None)
            restriction_note = f" — 사전예약 제한 ({ba_value}일 전 마감)" if is_restricted else ""
            if datekey == today_str and time_range is not None:
                _, t_to = time_range
                if now_kst.strftime("%H:%M") > t_to:
                    continue

            d = days_map.get(datekey)

            # hasBookableSlots는 시간대 슬롯이 있는 상품 기준이라, 일 단위로만 재고를
            # 내려주는 상품에서는 판매 중인데도 false로 온다. 판매일이고 일별 재고가
            # 남아 있으면 슬롯을 한 번 더 확인한다 (실제 마감 여부는 알림 직전
            # 캘린더 API 교차 확인이 걸러 준다).
            if d is not None and (d["hasBookableSlots"] or day_has_stock(d)):
                slot_info = fetch_day_slots(parsed, datekey, d)
                # 일 단위 상품은 고를 시간대가 없다 → 시간 범위 필터는 적용하지 않는다
                is_day_unit = bool(slot_info.get("day_unit"))

                if time_range is not None:
                    t_from, t_to = time_range
                    if slot_info["queried"] and not is_day_unit:
                        range_slots = [
                            s for s in slot_info.get("all_slots", [])
                            if t_from <= s["unitStartTime"][11:16] <= t_to
                        ]
                        slot_info = {
                            **slot_info,
                            "times": [t for t in slot_info["times"] if t_from <= t <= t_to],
                            "range_stock":   sum(s.get("unitStock",        0) for s in range_slots),
                            "range_booking": sum(s.get("unitBookingCount", 0) for s in range_slots),
                            "range_slots":   range_slots,
                        }
                time_hint = f" [{t_from}~{t_to}]" if (time_range is not None and not is_day_unit) else ""

                # 볼 수 있는 시간대가 하나도 없는 날. 일별 재고가 남아 있어도 살 수 있는
                # 시간대가 없으면 자리가 아니다 (일별 재고에는 영업시간 밖 몫까지 들어 있다).
                if slot_info["queried"] and slot_info["total"] == 0:
                    alerted.pop(alert_key, None)
                    alerted.pop(f"{alert_key}:pre", None)
                    reason = "오늘 남은 시간대 없음 (모두 지남)" if datekey == today_str \
                        else "예약 가능한 시간대 없음"
                    log_state(log_key, f"⏭ {name} {date_str} {reason}", now_str=now_str)
                    continue

                if slot_info["queried"] and slot_info["total"] > 0 and not slot_info["times"]:
                    alerted.pop(alert_key, None)
                    alerted.pop(f"{alert_key}:pre", None)
                    r_stock   = slot_info.get("range_stock",   d["stock"])
                    r_booking = slot_info.get("range_booking", d["bookingCount"])
                    log_state(log_key,
                              f"❌ {name} {date_str}{time_hint} 예약 가능 자리 없음 "
                              f"(재고:{r_stock} / 예약:{r_booking})", now_str=now_str)
                    continue

                r_stock   = slot_info.get("range_stock",   d["stock"])
                r_booking = slot_info.get("range_booking", d["bookingCount"])
                available = r_stock - r_booking

                ref_slots = slot_info.get("range_slots", slot_info.get("all_slots", []))
                per_slot = [
                    (s["unitStartTime"][11:16], s.get("unitStock", 0) - s.get("unitBookingCount", 0))
                    for s in ref_slots
                    if s.get("unitStock", 0) - s.get("unitBookingCount", 0) > 0
                ]
                stock_info = f"재고:{r_stock} / 예약:{r_booking}"

                if gate.closed:
                    closed_alert_key = f"{alert_key}:closed"
                    prev_slots = alerted.get(closed_alert_key)
                    log_parts, increased = _format_slot_parts(per_slot, prev_slots)

                    log_state(log_key,
                              f"🔒 {name} {date_str}{time_hint} {', '.join(log_parts)} "
                              f"({stock_info}) - 예약창 닫힘{restriction_note}", now_str=now_str)

                    if not is_restricted and available > 0 and (prev_slots is None or increased):
                        cal_ok = fetch_calendar_day_status(parsed["service_id"], parsed["biz_id"], datekey)
                        _log_alert_diagnostics(name, date_str, d, slot_info, ref_slots,
                                               cal_ok, ba_code, ba_value, "닫힘")
                        if cal_ok is False:
                            print(f"  [교차확인] 캘린더 API 기준 {date_str} 마감 — 알림 생략 (재고 정보 지연 의심)", flush=True)
                        else:
                            if increased:
                                title = f"🔒 {name} 자리 추가됨 (예약창 닫힘)"
                            else:
                                title = f"🔒 {name} 자리 있음 (예약창 닫힘)"
                            body = f"{date_str}{time_hint} " + " ".join(f"{t}({c})" for t, c in per_slot)
                            if ntfy_topic:
                                send_ntfy(ntfy_topic, title, body, url)
                            alerted[closed_alert_key] = dict(per_slot)
                elif window_open:
                    prev_slots = alerted.get(alert_key)
                    log_parts, increased = _format_slot_parts(per_slot, prev_slots)

                    log_state(log_key,
                              f"🎉 {name} {date_str}{time_hint} {', '.join(log_parts)} "
                              f"({stock_info}){restriction_note}", now_str=now_str)

                    if not is_restricted:
                        cal_ok = True
                        if prev_slots is None or increased:
                            if gate.just_reopened:
                                # 예약창 닫힘 → 열림 전환 직후: 이번 주기는 알림 생략, 상태만 기록
                                print(f"  [전환 직후] 알림 생략 (다음 주기에 재확인)", flush=True)
                            else:
                                cal_ok = fetch_calendar_day_status(parsed["service_id"], parsed["biz_id"], datekey)
                                _log_alert_diagnostics(name, date_str, d, slot_info, ref_slots,
                                                       cal_ok, ba_code, ba_value, "오픈")
                                if cal_ok is False:
                                    print(f"  [교차확인] 캘린더 API 기준 {date_str} 마감 — 알림·자동예약 생략 "
                                          f"(재고 정보 지연 의심)", flush=True)
                                else:
                                    if prev_slots is None:
                                        title = f"🎉 {name} 예약 가능!"
                                    else:
                                        inc_str = ", ".join(f"{t}(+{d})" for t, d in increased)
                                        title = f"🎉 {name} 자리 추가됨 - {inc_str}"
                                    body = f"{date_str}{time_hint} " + " ".join(f"{t}({c})" for t, c in per_slot)
                                    if ntfy_topic:
                                        send_ntfy(ntfy_topic, title, body, url)
                        if cal_ok is not False:
                            alerted[alert_key] = dict(per_slot)
                            maybe_auto_book(item, item_id, url, datekey, per_slot, ntfy_topic,
                                            alerted, ab_period, gate)
                else:
                    alerted.pop(alert_key, None)
                    pre_key = f"{alert_key}:pre"
                    log_parts, _ = _format_slot_parts(per_slot, None)
                    log_state(log_key,
                              f"⏳ {name} {date_str}{time_hint} {', '.join(log_parts)} "
                              f"({stock_info}) · {window_reason}{restriction_note}", now_str=now_str)
                    if not is_restricted and pre_key not in alerted:
                        if item.get("booking_open_datetime"):
                            open_dt = _parse_dt(item["booking_open_datetime"])
                            open_str = open_dt.strftime("%m/%d %H:%M") if open_dt else "?"
                            title = f"⏳ {name} 자리있음 ({open_str} 오픈)"
                            body = f"{date_str}{time_hint} " + " ".join(f"{t}({c})" for t, c in per_slot)
                        else:
                            open_dt = _parse_dt(result.get("sale_start_date"))
                            title = f"⏳ {name} 자리 있음 ({_open_time_label(open_dt)})"
                            body = f"{date_str}{time_hint} " + " ".join(f"{t}({c})" for t, c in per_slot) + f"\n{window_reason}"
                        if ntfy_topic:
                            send_ntfy(ntfy_topic, title, body, url)
                        alerted[pre_key] = 1

            else:
                alerted.pop(alert_key, None)
                alerted.pop(f"{alert_key}:pre", None)

                slot_info = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], datekey)
                all_slots = slot_info.get("all_slots", [])

                if datekey == today_str and slot_info["queried"] and not all_slots:
                    continue

                def _sold_out_label(r_stock: int, r_booking: int) -> str:
                    if r_stock > r_booking:
                        # 재고가 남았는데 여기까지 왔다 = 판매일이 아니거나 일별 요약이 없는 날
                        if d is not None and not d.get("isSaleDay"):
                            return f"판매일 아님 (재고:{r_stock} / 예약:{r_booking})"
                        return f"예약불가 (재고:{r_stock} / 예약:{r_booking})"
                    return f"매진 (재고:{r_stock} / 예약:{r_booking})"

                if time_range is not None:
                    t_from, t_to = time_range
                    time_hint = f" [{t_from}~{t_to}]"
                    range_slots = [s for s in all_slots if t_from <= s["unitStartTime"][11:16] <= t_to]
                    if range_slots:
                        r_stock   = sum(s.get("unitStock",        0) for s in range_slots)
                        r_booking = sum(s.get("unitBookingCount", 0) for s in range_slots)
                    elif slot_info["queried"]:
                        r_stock = r_booking = 0
                    elif d is not None:
                        r_stock, r_booking = d["stock"], d["bookingCount"]
                    else:
                        r_stock = r_booking = None
                else:
                    time_hint = ""
                    if all_slots:
                        r_stock   = sum(s.get("unitStock",        0) for s in all_slots)
                        r_booking = sum(s.get("unitBookingCount", 0) for s in all_slots)
                    elif d is not None:
                        r_stock, r_booking = d["stock"], d["bookingCount"]
                    else:
                        r_stock = r_booking = None

                if r_stock is None:
                    log_state(log_key, f"❌ {name} {date_str}{time_hint} 매진 (재고 정보 없음)", now_str=now_str)
                    if has_target_dates:
                        _pruned_dates.append((item_id, datekey))
                    continue

                log_state(log_key,
                          f"❌ {name} {date_str}{time_hint} {_sold_out_label(r_stock, r_booking)}",
                          now_str=now_str)
                if has_target_dates and r_stock == 0:
                    _pruned_dates.append((item_id, datekey))

        # 자동예약 날짜 미지정 항목은 등록된 예약 기간 전체가 대상이다.
        # 감시 날짜를 좁게 잡아 위 루프가 그 날짜만 돌았다면, 기간 안의 나머지 날짜를 여기서 마저 본다.
        if not gate.known_closed and window_open:
            sweep_auto_book_period(item, item_id, url, parsed, result.get("_all_summary") or [],
                                   ab_period, set(effective_dates), cutoff_date, ntfy_topic, alerted, gate)

        # 여기까지 왔는데 게이트를 한 번도 안 건드렸다 = 알릴 자리가 없어서
        # 예약창을 볼 이유가 없었다는 뜻. 이 회차의 절감이 어디서 났는지 남긴다.
        if not gate.consulted:
            _note_url_skip("자리 없음")
            log_state(f"{item_id}:status", f"⏸ {name} — 자리 없음, 예약창 확인 생략",
                      sig="확인생략", now_str=now_str)

    if _pruned_dates:
        prune_dead_dates(_pruned_dates)

    log_url_check_summary()
    log_round_summary()


def prune_dead_dates(pruned: list) -> None:
    """재고가 0이거나 재고 정보가 없는 날짜를 monitors.json의 target_dates에서 제거.
    매진(재고>0, 예약마감)은 취소표 발생 가능성이 있어 계속 추적 대상으로 남겨야 하므로 건드리지 않는다."""
    try:
        path = Path(__file__).parent / "monitors.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[경고] monitors.json 읽기 실패, 날짜 정리 건너뜀: {exc}", flush=True)
        return

    dead_by_item: dict[str, set] = {}
    for item_id, datekey in pruned:
        dead_by_item.setdefault(item_id, set()).add(datekey)

    changed = False
    removed_log = []
    for m in cfg.get("monitors", []):
        item_id = m.get("id", m.get("name", ""))
        dead = dead_by_item.get(item_id)
        if not dead:
            continue
        kept = [d for d in m.get("target_dates", []) if d.strip().split(" ", 1)[0] not in dead]
        if len(kept) != len(m.get("target_dates", [])):
            removed = [d for d in m.get("target_dates", []) if d not in kept]
            removed_log.append(f"{m.get('name', item_id)}: {', '.join(removed)}")
            m["target_dates"] = kept
            changed = True

    if not changed:
        return

    try:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  → 재고 없는 날짜 정리: {'; '.join(removed_log)}", flush=True)
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "monitors.json"], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "chore: 재고 없는 추적 날짜 정리"], check=True)
        subprocess.run(["git", "fetch", "origin"], check=True)
        subprocess.run(["git", "rebase", "origin/main"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print("  → monitors.json 커밋/푸시 완료", flush=True)
    except Exception as exc:
        subprocess.run(["git", "rebase", "--abort"], check=False, capture_output=True)
        print(f"[경고] monitors.json 커밋 실패: {exc}", flush=True)


def print_startup_info(active: list) -> None:
    """시작 시 각 모니터 항목의 예약 오픈 시각을 조회해 출력."""
    print("=== 예약 오픈 정보 조회 중... ===", flush=True)
    for m in active:
        name = m.get("name", "?")
        url  = m.get("url", "")
        if m.get("type") == "kakao":
            ticket_id = parse_kakao_url(url)
            print(f"  • {name} | 카카오 예약 (ticketId={ticket_id})", flush=True)
            continue
        parsed = parse_naver_url(url)
        if not parsed:
            print(f"  • {name}: URL 파싱 실패", flush=True)
            continue

        final_url = _playwright_final_url(url)
        print(f"    [진단] URL 최종 도착지: {final_url[:120]}", flush=True)
        if "/error/" in final_url:
            print(f"  • {name} | 예약창: 닫힘 🔒 (에러 페이지로 리다이렉트)", flush=True)
            continue

        raw = m.get("target_dates", [])
        dates_only = [e.split(" ")[0] for e in raw]
        result = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], dates_only)
        dates_label = ", ".join(raw) or "전체"

        if result is None:
            print(f"  • {name} [{dates_label}] | 예약창: 조회 실패", flush=True)
            continue

        is_open, _ = booking_window_status(m, result["sale_start_date"], result["sale_end_date"])
        open_src = m.get("booking_open_datetime") or result.get("sale_start_date")
        dt = _parse_dt(open_src)
        all_summary = result.get("_all_summary") or []

        print(f"    [진단] saleStartDate={result['sale_start_date']} / saleEndDate={result['sale_end_date']}", flush=True)

        if not is_open and dt:
            status = f"오픈 예정 → {dt.strftime('%Y/%m/%d %H:%M')} ⏳"
        elif not is_open:
            status = "오픈 시각 정보 없음 (monitors.json에 booking_open_datetime 설정 가능)"
        elif not all_summary:
            status = "오픈됨 ✅ (월별 스케줄 없음 — 날짜별 개별 조회로 모니터링)"
        else:
            status = "오픈됨 ✅"

        if dates_only:
            range_label = f"{dates_only[0]}~{dates_only[-1]} ({len(dates_only)}일)" if len(dates_only) > 3 else ", ".join(dates_only)
        else:
            range_label = "전체"
        print(f"  • {name} [{range_label}] | 예약창: {status}", flush=True)


def probe_schedule_period(parsed: dict) -> dict | None:
    """단일 팝업(URL 파싱 결과)의 실제 판매기간/예약 가능 기간을 조회해 캐시 항목으로 반환.
    조회에 실패하면 None을 반환한다."""
    now_kst = datetime.now(timezone(timedelta(hours=9))).isoformat()
    result = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], [])
    if result is None:
        return None
    all_summary = result.get("_all_summary") or []
    discovered = sorted(d["dateKey"] for d in all_summary if d.get("isSaleDay"))

    scan_end_cache = datetime.now(timezone(timedelta(hours=9))).date() + timedelta(days=30)
    if not discovered:
        # 월별 스케줄 API가 비어있는 경우 (예: 일부 팝업) — 날짜별 개별 조회로 운영 기간 추정
        scan_start = datetime.now(timezone(timedelta(hours=9))).date()
        for i in range(30):
            dk = (scan_start + timedelta(days=i)).isoformat()
            si = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], dk)
            if si["queried"] and si.get("all_slots"):
                discovered.append(dk)
        discovered.sort()
    else:
        # API 슬라이딩 윈도우 너머 날짜 추가 스캔
        last_known = date.fromisoformat(max(discovered))
        cur = last_known + timedelta(days=1)
        while cur <= scan_end_cache:
            dk = cur.isoformat()
            si = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], dk)
            if si["queried"] and si.get("all_slots"):
                discovered.append(dk)
            cur += timedelta(days=1)
        discovered.sort()

    restriction = fetch_item_restrictions(parsed["biz_id"])
    if restriction.get("booking_available_code") and restriction["booking_available_code"] != "RI01":
        print(
            f"  [예약 제한] {restriction['booking_available_code']} / {restriction['booking_available_value']}일 전 마감",
            flush=True,
        )

    return {
        "sale_start_date": result.get("sale_start_date"),
        "sale_end_date": result.get("sale_end_date"),
        "available_start": discovered[0] if discovered else None,
        "available_end": discovered[-1] if discovered else None,
        "checked_at": now_kst,
        "booking_available_code":  restriction.get("booking_available_code", "RI01"),
        "booking_available_value": restriction.get("booking_available_value", 0),
    }


def build_schedule_cache(monitors: list) -> dict:
    """각 모니터(URL)별 팝업의 실제 판매기간/예약 가능 기간을 조회해 캐시 데이터로 정리.
    웹앱이 raw.githubusercontent.com에서 이 파일을 읽어 등록 폼/목록에 활용한다."""
    cache: dict = {}
    for m in monitors:
        parsed = parse_naver_url(m.get("url", ""))
        if not parsed:
            continue
        key = f"{parsed['service_id']}_{parsed['biz_id']}_{parsed['item_id']}"
        if key in cache:
            continue
        probed = probe_schedule_period(parsed)
        if probed is None:
            continue
        cache[key] = probed
    return cache


def load_alerted() -> dict:
    """job 재시작 시 이전 알림 상태 복원 — alerted 딕셔너리를 파일에서 로드."""
    try:
        if ALERTED_FILE.exists():
            return json.loads(ALERTED_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[경고] booking_alerted.json 읽기 실패: {e}", flush=True)
    return {}


def save_alerted(alerted: dict) -> bool:
    """alerted 딕셔너리를 파일에 저장. 내용이 바뀐 경우에만 True 반환."""
    try:
        new_content = json.dumps(alerted, ensure_ascii=False, indent=2) + "\n"
        old_content = ALERTED_FILE.read_text(encoding="utf-8") if ALERTED_FILE.exists() else ""
        if old_content == new_content:
            return False
        ALERTED_FILE.write_text(new_content, encoding="utf-8")
        return True
    except Exception as e:
        print(f"[경고] booking_alerted.json 저장 실패: {e}", flush=True)
        return False


def commit_files(paths: list, message: str, label: str = "") -> bool:
    """지정한 파일을 main에 커밋/푸시. 실패해도 예외를 올리지 않는다.

    모니터 job과 자동예약 워커 job이 동시에 푸시할 수 있으므로 파일 소유를
    나눠 둔다 (모니터: booking_alerted/schedule_cache, 워커: auto_book_*).
    같은 파일을 양쪽이 건드리지 않는 한 rebase가 자동으로 합쳐진다.

    --autostash 필수: 이 함수는 한 번에 한 파일만 커밋하는데, 같은 회차에
    다른 데이터 파일(schedule_cache.json 등)이 이미 수정돼 있으면 rebase가
    "You have unstaged changes"로 거부한다.
    """
    label = label or ", ".join(paths)
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", *paths], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return False
        # git이 stdout을 그대로 물려받아 찍으므로, 회차 머리글을 먼저 내보낸다
        # (조용한 회차에 커밋 로그만 머리글 없이 뜨는 것을 막는다).
        flush_round_header()
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "fetch", "origin"], check=True)
        subprocess.run(["git", "rebase", "--autostash", "origin/main"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print(f"  → {label} 커밋/푸시 완료", flush=True)
        return True
    except Exception as exc:
        subprocess.run(["git", "rebase", "--abort"], check=False, capture_output=True)
        print(f"[경고] {label} 커밋 실패: {exc}", flush=True)
        return False


def commit_alerted() -> None:
    """booking_alerted.json을 저장소에 커밋/푸시 (실패해도 모니터링에는 영향 없음).

    auto_book_log.json은 자동예약 워커가 소유하므로 여기서 건드리지 않는다.
    """
    commit_files(["booking_alerted.json"], "data: 알림 상태 저장 [skip ci]", "booking_alerted.json")


def save_schedule_cache(cache: dict) -> bool:
    """schedule_cache.json 갱신. 내용이 바뀐 경우에만 True 반환."""
    try:
        old = json.loads(SCHEDULE_CACHE_FILE.read_text(encoding="utf-8")) if SCHEDULE_CACHE_FILE.exists() else {}
    except Exception:
        old = {}
    if old == cache:
        return False
    SCHEDULE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def commit_schedule_cache() -> None:
    """변경된 schedule_cache.json을 저장소에 커밋/푸시 (실패해도 모니터링에는 영향 없음)"""
    commit_files(["schedule_cache.json"], "chore: 팝업 예약 가능 기간 캐시 갱신", "schedule_cache.json")


def run_reprobe_requests() -> int:
    """웹앱의 "운영 기간 초기화" 요청만 처리하고 끝나는 단발 실행 (reprobe 워크플로용).

    모니터 job이 돌지 않는 시간대에도 버튼이 동작하게 하는 경로다.
    모니터가 돌고 있으면 양쪽이 같은 결과를 쓰므로 중복 처리돼도 무해하다.
    """
    reqs = load_reprobe_requests(from_github=False)
    if not reqs:
        print("재탐색 요청 없음", flush=True)
        return 0

    cfg = load_monitors()
    # cache_key → 모니터 이름 (로그용)
    names: dict[str, str] = {}
    parsed_by_key: dict[str, dict] = {}
    for m in cfg.get("monitors", []):
        p = parse_naver_url(m.get("url", ""))
        if not p:
            continue
        k = f"{p['service_id']}_{p['biz_id']}_{p['item_id']}"
        parsed_by_key.setdefault(k, p)
        names.setdefault(k, m.get("name", k))

    try:
        cache = json.loads(SCHEDULE_CACHE_FILE.read_text(encoding="utf-8")) if SCHEDULE_CACHE_FILE.exists() else {}
    except Exception:
        cache = {}

    done = 0
    for key, requested_at in reqs.items():
        entry = cache.get(key, {})
        if not _reprobe_requested(entry, requested_at):
            print(f"  • {names.get(key, key)} — 이미 처리된 요청, 건너뜀", flush=True)
            continue
        parsed = parsed_by_key.get(key)
        if not parsed:
            print(f"  • {key} — monitors.json에 없는 항목, 건너뜀", flush=True)
            continue
        probed = probe_schedule_period(parsed)
        if not probed:
            print(f"  • {names.get(key, key)} — 조회 실패, 기존 캐시 유지", flush=True)
            continue
        probed = _merge_probed_period(entry, probed)
        old_range = f"{entry.get('available_start')}~{entry.get('available_end')}"
        cache[key] = probed
        done += 1
        print(f"  • {names.get(key, key)} 운영 기간: {old_range} → "
              f"{probed.get('available_start')}~{probed.get('available_end')}", flush=True)

    if done:
        save_schedule_cache(cache)
        commit_schedule_cache()
    print(f"재탐색 완료: {done}건", flush=True)
    return done


def main():
    if "--reprobe" in sys.argv:
        run_reprobe_requests()
        return

    cfg = load_monitors()
    ntfy_topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    interval = _env_num("CHECK_INTERVAL_SEC", 30)
    loop_hours = _env_num("LOOP_HOURS", 5.5, float)
    monitors = cfg.get("monitors", [])

    active = [m for m in monitors if m.get("enabled", True)]
    if not active:
        print("활성화된 모니터링 항목 없음", flush=True)
        sys.exit(0)

    print(f"=== 모니터 시작 | 주기: {interval}초 | 최대: {loop_hours}시간 ===", flush=True)
    print_startup_info(active)

    cache = build_schedule_cache(monitors)
    if save_schedule_cache(cache):
        commit_schedule_cache()

    # job 재시작 시 이전 알림 상태 복원 (중복 알림 방지)
    alerted = load_alerted()
    sync_auto_book_state(monitors, alerted)

    for m in active:
        if not check_booking_accessible(m.get("url", "")):
            # 재시작 시점의 단발 체크는 오탐 가능성이 있으므로 바로 확정하지 않고
            # streak만 1로 시드한다. 다음 주기에 한 번 더 닫힘이 확인돼야 확정된다.
            mid = m.get("id", m.get("name", ""))
            alerted.setdefault(f"{mid}:url_close_streak", 1)
    end_time = time.time() + loop_hours * 3600
    iteration = 0

    while time.time() < end_time:
        iteration += 1
        try:
            cfg = load_monitors(from_github=True)
            monitors = cfg.get("monitors", [])
            ntfy_topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
        except Exception as exc:
            print(f"[경고] monitors.json 읽기 실패, 이전 설정 유지: {exc}", flush=True)

        remaining_min = (end_time - time.time()) / 60
        # 머리글은 예약만 해 둔다. 이 회차에 남길 게 하나도 없으면 머리글도 안 찍는다.
        set_round_header(f"--- [{iteration}회차] 남은 시간: {remaining_min:.1f}분 ---")
        # 별도 워크플로에서 돌고 있는 자동예약의 결과를 먼저 반영
        sync_auto_book_state(monitors, alerted)
        global _rate_limit_hits
        _rate_limit_hits = 0
        try:
            check_all(monitors, ntfy_topic, alerted)
        except Exception as exc:
            print(f"[오류] check_all 예외: {exc}", flush=True)

        if save_alerted(alerted):
            commit_alerted()

        # 여기까지 아무것도 안 찍혔으면 이 회차는 통째로 조용히 지나간다.
        # 다만 감시가 멈춘 것과 구분되도록 가끔 한 줄은 남긴다.
        log_round_tick(iteration, remaining_min)

        if _rate_limit_hits > 0 and interval < 120:
            interval = 120
            msg = f"[경고] API 속도 제한(429/403) 감지 → 확인 주기를 120초로 자동 조정"
            print(msg, flush=True)
            if ntfy_topic:
                send_ntfy(ntfy_topic, "⚠️ 모니터 속도 제한 감지", msg, "")

        remaining = end_time - time.time()
        if remaining > interval:
            time.sleep(interval)
        else:
            break

    print("=== 루프 종료 ===", flush=True)


if __name__ == "__main__":
    main()
