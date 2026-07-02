"""
네이버 예약 자리 모니터 — GitHub Actions 클라우드 버전
monitors.json 파일에서 설정 읽기 (enabled 필드로 항목별 ON/OFF)
환경변수: NTFY_TOPIC (선택, monitors.json 값 override)
          CHECK_INTERVAL_SEC, LOOP_HOURS

monitors.json 항목 선택 필드:
  booking_open_datetime  예약 오픈 일시 (ISO 형식, 예: "2026-06-01T20:00:00+09:00")
                         설정 시 해당 시각 이후 + 자리 있을 때만 알림 발송
"""

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

GITHUB_RAW_URL = "https://raw.githubusercontent.com/DuckOnDesk/naver-booking-monitor/main/monitors.json"
SCHEDULE_CACHE_FILE = Path(__file__).parent / "schedule_cache.json"

_rate_limit_hits = 0  # 현재 루프 회차 중 429/403 발생 횟수

KAKAO_API_URL = "https://booking.kakao.com/api/product/public/ticket/tickets/availableDates"
KAKAO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Referer": "https://booking.kakao.com/",
}


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
    hourlySchedule API로 시간대별 슬롯 조회. 이미 지난 시간대는 제외.
      times   : 예약 가능한 미래 시간대 목록 (HH:MM)
      total   : 미래 슬롯 수 (지난 슬롯 제외, 가용 여부 무관)
      queried : API 호출 성공 여부
    """
    KST = timezone(timedelta(hours=9))
    now_kst = datetime.now(KST)

    try:
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
                "query": (
                    "query hourlySchedule($scheduleParams: ScheduleParams) {"
                    "  schedule(input: $scheduleParams) {"
                    "    bizItemSchedule {"
                    "      hourly {"
                    "        unitStartTime unitBookingCount unitStock isUnitSaleDay __typename"
                    "      } __typename"
                    "    } __typename"
                    "  }"
                    "}"
                ),
            },
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            return {"times": [], "total": 0, "queried": False, "all_slots": []}

        hourly = data["data"]["schedule"]["bizItemSchedule"].get("hourly") or []

        future_slots = []
        for slot in hourly:
            if not slot.get("isUnitSaleDay"):
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

        return {"times": available_times, "total": len(future_slots), "queried": True, "all_slots": future_slots}

    except requests.HTTPError as e:
        print(f"  [오류] hourlySchedule API HTTP {e.response.status_code}", flush=True)
        if e.response.status_code in (429, 403):
            global _rate_limit_hits
            _rate_limit_hits += 1
        return {"times": [], "total": 0, "queried": False, "all_slots": []}
    except Exception:
        return {"times": [], "total": 0, "queried": False, "all_slots": []}


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


_CLOSED_URL_PATTERNS  = ["/error/"]
_CLOSED_TEXT_PATTERNS = [
    "운영하지 않는 예매 페이지",
    "판매 기간이 아닙니다",
    "판매기간이 아닙니다",
    "예약을 받고 있지 않습니다",
    "예약이 마감되었습니다",
    "더 이상 예약할 수 없습니다",
]

def _playwright_check(url: str) -> tuple[bool, str]:
    """(is_closed, reason) 반환. URL/텍스트 기반으로 예약창 닫힘 감지."""
    item_match = re.search(r"/items/\d+", url)
    item_path = item_match.group(0) if item_match else None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
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
            finally:
                browser.close()
    except Exception as exc:
        print(f"  [경고] playwright 확인 실패 → 열림으로 간주: {exc}", flush=True)
        return False, ""


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

    _pruned_dates: list[tuple[str, str]] = []

    for item in active:
        name = item.get("name", "?")
        url = item.get("url", "")

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
                print(f"[{now_str}] 🔒 {name} — 예약창 닫힘", flush=True)
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
            else:
                print(f"[{now_str}] ✅ {name} — 예약창 열림", flush=True)

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
                    print(f"[{now_str}] 🎉 {name} {ds} {stock}자리 (예약가능:{stock})", flush=True)
                    if ak not in alerted:
                        new_date_details.append(f"{ds} {stock}자리")
                        alerted[ak] = stock
                else:
                    alerted.pop(ak, None)
                    print(f"[{now_str}] ❌ {name} {ds} 매진 (예약가능:0)", flush=True)

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

        if not cache_entry:
            probed = probe_schedule_period(parsed)
            if probed:
                sched_cache[cache_key] = probed
                cache_entry = probed
                if save_schedule_cache(sched_cache):
                    commit_schedule_cache()
                print(f"[{now_str}] — {name} 운영 기간 최초 확인: {probed.get('available_start')}~{probed.get('available_end')}", flush=True)

        avail_start = cache_entry.get("available_start")
        avail_end   = cache_entry.get("available_end")

        item_id   = item.get("id", name)
        closed_key = f"{item_id}:url_closed"

        is_url_closed, closed_reason = _playwright_check(url)

        if is_url_closed:
            item_prefix = f"{item_id}:"
            for k in list(alerted.keys()):
                if k.startswith(item_prefix) and k != closed_key and not k.endswith(":closed"):
                    alerted.pop(k)
            alerted[closed_key] = 1
            print(f"[{now_str}] 🔒 {name} — 예약창 닫힘 ({closed_reason})", flush=True)
        else:
            if closed_key in alerted:
                alerted.pop(closed_key)
                item_prefix = f"{item_id}:"
                for k in list(alerted.keys()):
                    if k.startswith(item_prefix) and k.endswith(":closed"):
                        alerted.pop(k)
                print(f"[{now_str}] ✅ {name} — 예약창 열림 (방금 전환됨)", flush=True)
                if ntfy_topic:
                    send_ntfy(ntfy_topic, f"✅ {name} 예약창 열림", "예약창이 열렸습니다. 직접 확인해보세요!", url)
            else:
                print(f"[{now_str}] ✅ {name} — 예약창 열림", flush=True)

        result = check_availability(parsed["biz_id"], parsed["item_id"], parsed["service_id"], target_dates_only)
        if result is None:
            print(f"[{now_str}] {name} — API 실패", flush=True)
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
                print(f"[{now_str}] — {name} 전체 날짜 스캔 중...", flush=True)
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
                print(f"[{now_str}] — {name} 판매 중인 날짜 없음 (캐시 기간 내)", flush=True)
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
                print(f"[{now_str}] — {name} 운영 기간({avail_start}~{avail_end}) 외 날짜 {len(effective_dates)-len(trimmed)}개 제외", flush=True)
            effective_dates = trimmed

        for datekey in effective_dates:
            dow       = weekdays[date.fromisoformat(datekey).weekday()]
            date_str  = f"{datekey[5:]}({dow})"
            alert_key = f"{item_id}:{datekey}"
            time_range = target_time_map.get(datekey)

            if datekey < today_str:
                continue
            if datekey == today_str and time_range is not None:
                _, t_to = time_range
                if now_kst.strftime("%H:%M") > t_to:
                    continue

            d = days_map.get(datekey)

            if d is not None and d["hasBookableSlots"]:
                slot_info = fetch_slots(parsed["biz_id"], parsed["item_id"], parsed["service_id"], datekey)

                if time_range is not None and slot_info["queried"]:
                    t_from, t_to = time_range
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

                if slot_info["queried"] and slot_info["total"] == 0 and datekey == today_str:
                    alerted.pop(alert_key, None)
                    alerted.pop(f"{alert_key}:pre", None)
                    print(f"[{now_str}] ⏭ {name} {date_str} 오늘 남은 시간대 없음 (모두 지남)", flush=True)
                    continue

                if slot_info["queried"] and slot_info["total"] > 0 and not slot_info["times"]:
                    alerted.pop(alert_key, None)
                    alerted.pop(f"{alert_key}:pre", None)
                    r_stock   = slot_info.get("range_stock",   d["stock"])
                    r_booking = slot_info.get("range_booking", d["bookingCount"])
                    time_hint = f" [{t_from}~{t_to}]" if time_range is not None else ""
                    print(f"[{now_str}] ❌ {name} {date_str}{time_hint} 예약 가능 자리 없음 (재고:{r_stock} / 예약:{r_booking})", flush=True)
                    continue

                r_stock   = slot_info.get("range_stock",   d["stock"])
                r_booking = slot_info.get("range_booking", d["bookingCount"])
                available = r_stock - r_booking
                time_hint = f" [{t_from}~{t_to}]" if time_range is not None else ""

                ref_slots = slot_info.get("range_slots", slot_info.get("all_slots", []))
                per_slot = [
                    (s["unitStartTime"][11:16], s.get("unitStock", 0) - s.get("unitBookingCount", 0))
                    for s in ref_slots
                    if s.get("unitStock", 0) - s.get("unitBookingCount", 0) > 0
                ]
                stock_info = f"재고:{r_stock} / 예약:{r_booking}"

                if is_url_closed:
                    closed_alert_key = f"{alert_key}:closed"
                    prev_slots = alerted.get(closed_alert_key)
                    log_parts, increased = _format_slot_parts(per_slot, prev_slots)

                    print(f"[{now_str}] 🔒 {name} {date_str}{time_hint} {', '.join(log_parts)} ({stock_info}) - 예약창 닫힘", flush=True)

                    if available > 0 and (prev_slots is None or increased):
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

                    print(f"[{now_str}] 🎉 {name} {date_str}{time_hint} {', '.join(log_parts)} ({stock_info})", flush=True)

                    if prev_slots is None or increased:
                        if prev_slots is None:
                            title = f"🎉 {name} 예약 가능!"
                        else:
                            inc_str = ", ".join(f"{t}(+{d})" for t, d in increased)
                            title = f"🎉 {name} 자리 추가됨 - {inc_str}"
                        body = f"{date_str}{time_hint} " + " ".join(f"{t}({c})" for t, c in per_slot)
                        if ntfy_topic:
                            send_ntfy(ntfy_topic, title, body, url)
                    alerted[alert_key] = dict(per_slot)
                else:
                    alerted.pop(alert_key, None)
                    pre_key = f"{alert_key}:pre"
                    log_parts, _ = _format_slot_parts(per_slot, None)
                    print(f"[{now_str}] ⏳ {name} {date_str}{time_hint} {', '.join(log_parts)} ({stock_info}) · {window_reason}", flush=True)
                    if pre_key not in alerted:
                        if item.get("booking_open_datetime"):
                            open_dt = _parse_dt(item["booking_open_datetime"])
                            open_str = open_dt.strftime("%m/%d %H:%M") if open_dt else "?"
                            title = f"⏳ {name} 자리있음 ({open_str} 오픈)"
                            body = f"{date_str}{time_hint} " + " ".join(f"{t}({c})" for t, c in per_slot)
                        else:
                            title = f"⏳ {name} 자리 있음 (예약창 미오픈)"
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
                    print(f"[{now_str}] ❌ {name} {date_str}{time_hint} 매진 (재고 정보 없음)", flush=True)
                    if has_target_dates:
                        _pruned_dates.append((item_id, datekey))
                    continue

                print(f"[{now_str}] ❌ {name} {date_str}{time_hint} {_sold_out_label(r_stock, r_booking)}", flush=True)
                if has_target_dates and r_stock == 0:
                    _pruned_dates.append((item_id, datekey))

    if _pruned_dates:
        prune_dead_dates(_pruned_dates)


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

    return {
        "sale_start_date": result.get("sale_start_date"),
        "sale_end_date": result.get("sale_end_date"),
        "available_start": discovered[0] if discovered else None,
        "available_end": discovered[-1] if discovered else None,
        "checked_at": now_kst,
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
    try:
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "schedule_cache.json"], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", "chore: 팝업 예약 가능 기간 캐시 갱신"], check=True)
        subprocess.run(["git", "fetch", "origin"], check=True)
        subprocess.run(["git", "rebase", "origin/main"], check=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)
        print("  → schedule_cache.json 커밋/푸시 완료", flush=True)
    except Exception as exc:
        print(f"[경고] schedule_cache.json 커밋 실패: {exc}", flush=True)


def main():
    cfg = load_monitors()
    ntfy_topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    interval = int(os.environ.get("CHECK_INTERVAL_SEC", "30"))
    loop_hours = float(os.environ.get("LOOP_HOURS", "5.5"))
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

    alerted: dict[str, int] = {}

    for m in active:
        if not check_booking_accessible(m.get("url", "")):
            alerted[f"{m.get('id', m.get('name', ''))}:url_closed"] = 1
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
        print(f"--- [{iteration}회차] 남은 시간: {remaining_min:.1f}분 ---", flush=True)
        global _rate_limit_hits
        _rate_limit_hits = 0
        try:
            check_all(monitors, ntfy_topic, alerted)
        except Exception as exc:
            print(f"[오류] check_all 예외: {exc}", flush=True)

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
