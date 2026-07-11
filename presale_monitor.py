"""
네이버 팝업스토어 사전예약 오픈 감지 모니터 v2
pcmap.place.naver.com/popupstore/list API 사용

presale_config.json 설정:
{
  "ntfy_topic": "naver-booking-alert",
  "check_interval_seconds": 60,
  "areas": [
    {"query": "성수 팝업", "x": "127.057", "y": "37.544"}
  ],
  "disabled_places": []
}
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "presale_config.json"
DATA_FILE = BASE_DIR / "presale_data.json"
LOG_FILE = BASE_DIR / "presale_monitor.log"
PENDING_NTFY_FILE = BASE_DIR / "pending_ntfy.json"

if not sys.stdout:
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if not sys.stderr:
    sys.stderr = sys.stdout

KST = timezone(timedelta(hours=9))
LIST_URL = "https://pcmap.place.naver.com/popupstore/list"
PRESALE_NAME_FILTER = "사전예약"  # admissionCondition.name에 포함되는 키워드로 필터

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://map.naver.com/",
})

DEFAULT_CONFIG = {
    "ntfy_topic": "naver-booking-alert",
    "check_interval_seconds": 60,
    "areas": [
        {"query": "성수 팝업", "x": "127.057", "y": "37.544"}
    ],
    "disabled_places": [],
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return DEFAULT_CONFIG
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    # 구버전 호환: "places" → "areas" 자동 마이그레이션
    if "places" in cfg and "areas" not in cfg:
        cfg["areas"] = DEFAULT_CONFIG["areas"]
        cfg.pop("places", None)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[설정 마이그레이션] areas 형식으로 업데이트됨")
    return cfg


def fetch_presale_places(area: dict) -> list[dict] | None:
    """지역 검색 결과의 사전예약 팝업 목록. 조회/파싱 실패 시 None (빈 결과 []와 구분)."""
    params = {
        "query": area["query"],
        "x": area["x"], "y": area["y"],
        "clientX": area["x"], "clientY": area["y"],
        "display": "100",
        "ts": str(int(time.time() * 1000)),
        "locale": "ko",
        "mapUrl": f"https://map.naver.com/p/search/{area['query']}",
    }
    try:
        resp = SESSION.get(LIST_URL, params=params, timeout=15)
        resp.encoding = "utf-8"
    except Exception as e:
        print(f"  [요청 오류] {area['query']}: {e}")
        return None

    m = re.search(
        r'window\.__APOLLO_STATE__\s*=\s*(\{.+?\});\s*(?:</script>|window\.)',
        resp.text, re.DOTALL,
    )
    if not m:
        print(f"  [파싱 오류] Apollo state 없음: {area['query']} (status={resp.status_code})")
        return None

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  [JSON 오류] {e}")
        return None

    # 타입 prefix 무관하게 admissionCondition.name에 "사전예약" 포함된 항목만 수집
    presale = [
        v for v in data.values()
        if isinstance(v, dict)
        and PRESALE_NAME_FILTER in ((v.get("admissionCondition") or {}).get("name") or "")
    ]

    # address_filter 설정 시 commonAddress로 필터링 (예: "성동구")
    addr_filter = area.get("address_filter", "").strip()
    if addr_filter:
        before = len(presale)
        presale = [p for p in presale if addr_filter in (p.get("commonAddress") or "")]
        filtered = before - len(presale)
        if filtered:
            print(f"  [필터] '{addr_filter}' 외 {filtered}개 제외")

    return presale


def normalize(p: dict) -> dict:
    status = p.get("status") or {}
    admission = p.get("admissionCondition") or {}
    status_name = status.get("name") if isinstance(status, dict) else None
    remaining = p.get("remainingDays")
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "hasBooking": p.get("hasBooking", False),
        "bookingUrl": p.get("bookingUrl"),
        "bookingBusinessId": p.get("bookingBusinessId"),
        "operationStart": p.get("operationStartDateTime"),
        "operationEnd": p.get("operationEndDateTime"),
        "remainingDays": remaining,
        "status": status_name or (f"D-{remaining}" if remaining is not None else None),
        "admissionCondition": admission.get("name") if isinstance(admission, dict) else None,
        "imageUrl": p.get("imageUrl"),
        "roadAddress": p.get("roadAddress"),
        "commonAddress": p.get("commonAddress"),
        "bookingOpenDatetime": None,
        "bookingOpenHistory": [],  # 예약 오픈된 이력 (ISO datetime 목록)
    }


def resolve_booking_item_url(booking_url: str) -> str:
    """/search URL에서 /items/{id} 직접 예약 URL 자동 조회"""
    if not booking_url or "/items/" in booking_url:
        return booking_url
    m = re.search(r"(https://m\.booking\.naver\.com/booking/(\d+)/bizes/(\d+))", booking_url)
    if not m:
        return booking_url
    base_url = m.group(1)
    try:
        resp = SESSION.get(booking_url, timeout=15, allow_redirects=True)
        ids = re.findall(r'''["'/]items/(\d+)''', resp.text)
        if ids:
            print(f"  [아이템 URL 발견] /items/{ids[0]}")
            return f"{base_url}/items/{ids[0]}"
    except Exception as e:
        print(f"  [아이템 URL 조회 실패] {e}")
    return booking_url


def has_available_slots(booking_url: str, booking_business_id: str) -> bool:
    """네이버 예약 슬롯 잔여 여부 확인.
    - 슬롯 있음 확인 → True (알림 전송)
    - 슬롯 없음 확인 → False (알림 생략)
    - API 오류/구조 미확인 → True (기본: 알림 전송)
    """
    if not booking_url or not booking_business_id:
        return True
    m_type = re.search(r'/booking/(\d+)/bizes/', booking_url)
    if not m_type:
        return True
    booking_type = m_type.group(1)
    now = datetime.now(KST)

    confirmed_no_slot = 0
    checked_months = 0

    for month_offset in (0, 1):
        d = datetime(now.year, now.month, 1, tzinfo=KST)
        if month_offset:
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        ym = d.strftime("%Y-%m")
        cal_url = f"https://m.booking.naver.com/booking/{booking_type}/bizes/{booking_business_id}/calendars/{ym}"
        try:
            resp = SESSION.get(cal_url, timeout=8)
            if resp.status_code != 200:
                continue
            data = resp.json()
            checked_months += 1

            # list 형태: [{"date": "...", "status": "AVAILABLE"/"FULL"/...}, ...]
            calendars = data.get("calendars") or data.get("data")
            if isinstance(calendars, list):
                for day in calendars:
                    st = (day.get("status") or "").upper()
                    if st in ("AVAILABLE", "A") or day.get("available") or day.get("bookable"):
                        return True
                confirmed_no_slot += 1
            # dict 형태: {"YYYY-MM-DD": {"status": ...}, ...}
            elif isinstance(calendars, dict):
                for day in calendars.values():
                    if isinstance(day, dict):
                        st = (day.get("status") or "").upper()
                        if st in ("AVAILABLE", "A") or day.get("available"):
                            return True
                confirmed_no_slot += 1
        except Exception:
            pass

    # 2달 모두 확인됐고 슬롯 없음 → False
    if checked_months >= 2 and confirmed_no_slot >= 2:
        return False
    # 확인 불충분 → 기본 알림
    return True


def send_ntfy(topic: str, title: str, body: str, url: str) -> None:
    if not topic:
        return
    for attempt in range(3):
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
            print(f"  → ntfy 전송 완료 (시도 {attempt + 1})")
            return
        except Exception as e:
            print(f"  [ntfy 오류 {attempt + 1}/3] {e}")
            if attempt < 2:
                time.sleep(3)
    print("  [ntfy] 3회 시도 모두 실패")


def _queue_ntfy(title: str, body: str, url: str) -> None:
    """새 팝업 알림을 파일에 큐잉. GitHub Actions 워크플로우가 git push 후 발송한다."""
    try:
        items = json.loads(PENDING_NTFY_FILE.read_text(encoding="utf-8")) if PENDING_NTFY_FILE.exists() else []
    except Exception:
        items = []
    items.append({"title": title, "body": body, "url": url})
    PENDING_NTFY_FILE.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def flush_pending_ntfy(topic: str) -> None:
    """큐에 쌓인 알림을 즉시 발송하고 파일 삭제 (로컬 루프 모드 전용)."""
    if not PENDING_NTFY_FILE.exists():
        return
    try:
        items = json.loads(PENDING_NTFY_FILE.read_text(encoding="utf-8"))
        PENDING_NTFY_FILE.unlink()
    except Exception:
        return
    for item in items:
        send_ntfy(topic, item["title"], item["body"], item["url"])


def send_toast(name: str, body: str, url: str) -> None:
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id="네이버 예약 모니터",
            title=f"🎉 {name} 사전예약 오픈됐어요!",
            msg=body,
            launch=url,
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception as e:
        print(f"  [토스트 오류] {e}")


def load_prev_alerts() -> list[dict]:
    try:
        if DATA_FILE.exists():
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return data.get("alerts", [])
    except Exception:
        pass
    return []


def load_seen_ids() -> set[str]:
    """지금까지 '새 팝업 발견' 알림을 보낸 장소 ID 영구 목록."""
    try:
        if DATA_FILE.exists():
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return set(str(x) for x in data.get("seen_place_ids", []))
    except Exception:
        pass
    return set()


def save_data(places: list[dict], config: dict, alerts: list[dict] | None = None,
              seen_ids: set | None = None) -> None:
    data = {
        "updated_at": datetime.now(KST).isoformat(),
        "watched_places": config.get("watched_places", []),
        "booking_open_datetimes": config.get("booking_open_datetimes", {}),
        "places": places,
        "alerts": (alerts or [])[-200:],  # 최근 200건만 유지
        "seen_place_ids": sorted(seen_ids or set()),
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def check_once(config: dict, prev: dict) -> dict:
    now_str = datetime.now(KST).strftime("%H:%M:%S")
    watched = set(str(x) for x in config.get("watched_places", []))
    ntfy_topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic", "")
    sel_url = config.get("selection_page_url", "")
    prev_alerts = load_prev_alerts()
    new_alerts: list[dict] = []

    # 이미 발견 알림을 보낸 팝업 ID (영구 저장) — 재등장해도 중복 알림 방지
    seen_ids = load_seen_ids()
    seen_ids |= {str(pid) for pid in prev}  # 마이그레이션: 기존 데이터의 장소는 이미 본 것으로 간주

    raw: dict[str, dict] = {}
    fetch_failed = False
    for area in config.get("areas", []):
        result = fetch_presale_places(area)
        if result is None:
            fetch_failed = True
            continue
        for p in result:
            pid = p.get("id")
            if pid and pid not in raw:
                raw[pid] = p

    current: dict[str, dict] = {pid: normalize(p) for pid, p in raw.items()}

    if fetch_failed:
        # 일부 지역 조회 실패 → 검색에서 빠진 장소를 종료로 오판해 삭제하지 않고 유지
        carried = 0
        for pid, place in prev.items():
            if pid not in current:
                current[pid] = dict(place)
                carried += 1
        if carried:
            print(f"  [경고] 일부 지역 조회 실패 — 기존 장소 {carried}개 유지 (삭제 보류)")

    # 지도 검색에 나오지 않는 장소는 종료된 팝업으로 간주하고 제거 (carryover 안 함)
    removed = [pid for pid in prev if pid not in current]
    for pid in removed:
        print(f"  [검색 제외] {prev[pid].get('name', pid)} ({pid}) — 검색 결과에 없어 제거")

    # watched_places 등 config에서도 검색에 없는 장소 정리
    stale_watched = [pid for pid in watched if pid not in current]
    if stale_watched:
        config["watched_places"] = sorted(pid for pid in watched if pid in current)
        for pid in stale_watched:
            for key in ("booking_direct_urls", "booking_open_datetimes"):
                config.get(key, {}).pop(str(pid), None)
        CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  [설정 정리] 검색에 없는 watched_places {stale_watched} 제거됨")
        watched = set(config["watched_places"])

    # config의 booking_open_datetimes와 이전 예약오픈 이력 병합
    bod = config.get("booking_open_datetimes", {})
    direct_urls = config.get("booking_direct_urls", {})
    now_iso = datetime.now(KST).isoformat()
    for pid, place in current.items():
        place["bookingOpenDatetime"] = bod.get(str(pid))
        place["bookingOpenHistory"] = list(prev.get(pid, {}).get("bookingOpenHistory", []))
        place["lastBookingNotifiedAt"] = prev.get(pid, {}).get("lastBookingNotifiedAt")

        # 예약 URL 결정 (우선순위: config 수동 > 이전 /items/ URL > API URL > 이전 URL)
        prev_url = prev.get(pid, {}).get("bookingUrl") or ""
        curr_url = place.get("bookingUrl") or ""
        if str(pid) in direct_urls:
            place["bookingUrl"] = direct_urls[str(pid)]
        elif "/items/" in prev_url and "/items/" not in curr_url:
            place["bookingUrl"] = prev_url  # 이전에 발견한 더 구체적인 URL 유지
        elif not curr_url and prev_url:
            place["bookingUrl"] = prev_url

        if not place.get("bookingBusinessId") and prev.get(pid, {}).get("bookingBusinessId"):
            place["bookingBusinessId"] = prev[pid]["bookingBusinessId"]

    # 예약 중인 팝업의 /items/ URL 자동 조회 (수동 설정 없고 아직 /items/ 없는 경우)
    for pid, place in current.items():
        url = place.get("bookingUrl") or ""
        if (place.get("hasBooking") and url and "/items/" not in url
                and str(pid) not in direct_urls):
            direct = resolve_booking_item_url(url)
            if direct != url:
                place["bookingUrl"] = direct

    for pid, place in current.items():
        name = place["name"]
        is_open = place["hasBooking"]
        was_open = prev.get(pid, {}).get("hasBooking", False)
        booking_url = place.get("bookingUrl") or ""
        dday = place.get("status") or ""

        if pid not in prev and str(pid) not in seen_ids:
            # 처음 보는 팝업 → git push 이후 알림 발송 (페이지 데이터가 업데이트된 뒤 수신되도록)
            if is_open:
                place["bookingOpenHistory"].append(now_iso)
                place["lastBookingNotifiedAt"] = now_iso  # 새 팝업 발견 알림이 오픈 알림을 겸함
            print(f"[{now_str}] 🆕 {name} — 새 팝업 발견!")
            _queue_ntfy(f"🆕 새 팝업 발견: {name}", "예약 선택 페이지에서 확인하세요", sel_url or booking_url)
            new_alerts.append({"type": "new_popup", "place_id": str(pid), "place_name": name,
                                "booking_url": booking_url, "ts": now_iso})
        elif pid not in prev:
            # 과거에 이미 발견 알림을 보낸 팝업이 검색에 다시 나타남 → 조용히 복원
            print(f"[{now_str}] ↩️ {name} — 재등장 (이미 발견한 팝업, 알림 생략)")
        elif is_open and not was_open:
            # 예약 오픈됨 → 이력 추가, 감시 중인 경우만 알림
            place["bookingOpenHistory"].append(now_iso)
            new_alerts.append({"type": "booking_open", "place_id": str(pid), "place_name": name,
                                "booking_url": booking_url, "ts": now_iso})
            if pid in watched:
                # 24시간 내 이미 알림을 보낸 경우 중복 발송 방지 (API 오락가락 및 Actions 재시작 대응)
                last_notif_at = prev.get(pid, {}).get("lastBookingNotifiedAt")
                within_24h = False
                if last_notif_at:
                    try:
                        last_dt = datetime.fromisoformat(last_notif_at)
                        within_24h = (datetime.now(KST) - last_dt).total_seconds() < 24 * 3600
                    except Exception:
                        pass
                if within_24h:
                    print(f"[{now_str}] 🎉 {name} — 사전예약 오픈 (24시간 내 알림 이미 발송, 생략)")
                else:
                    biz_id = place.get("bookingBusinessId") or ""
                    slots_ok = has_available_slots(booking_url, biz_id)
                    if slots_ok:
                        print(f"[{now_str}] 🎉 {name} — 사전예약 오픈! {booking_url}")
                        msg = f"지금 바로 예약하세요! → {booking_url}"
                        send_ntfy(ntfy_topic, f"🎉 {name} 사전예약 오픈!", msg, booking_url)
                        send_toast(name, msg, booking_url)
                        place["lastBookingNotifiedAt"] = datetime.now(KST).isoformat()
                    else:
                        print(f"[{now_str}] 🎉 {name} — 사전예약 오픈 (잔여 없음, 알림 생략)")
            else:
                print(f"[{now_str}] 🎉 {name} — 사전예약 오픈 (알림없음)")
        elif is_open:
            print(f"[{now_str}] ✅ {name} ({dday}) — 예약중")
        else:
            print(f"[{now_str}] ⏳ {name} ({dday}) — 대기중")

    # 새로 발견된 팝업 자동으로 watched_places에 추가
    new_pids = [pid for pid in current if pid not in prev]
    if new_pids:
        watched_list = list(config.get("watched_places", []))
        watched_set = set(str(x) for x in watched_list)
        to_add = [str(pid) for pid in new_pids if str(pid) not in watched_set]
        if to_add:
            config["watched_places"] = sorted(watched_set | set(to_add))
            CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"  [자동 추가] watched_places에 {to_add} 추가됨")

    # 운영 기간이 지난 팝업 자동 정리 (YY.MM.DD. 형식 파싱)
    today = datetime.now(KST).date()
    expired_pids = []
    for pid, place in list(current.items()):
        end = place.get("operationEnd") or ""
        m = re.match(r"^(\d{2})\.(\d{2})\.(\d{2})", end)
        if m:
            try:
                end_date = datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                if end_date < today:
                    expired_pids.append(pid)
            except ValueError:
                pass

    if expired_pids:
        ws = set(str(x) for x in config.get("watched_places", []))
        cfg_changed = False
        for pid in expired_pids:
            name = current.pop(pid, {}).get("name", pid)
            print(f"  [만료 정리] {name} ({pid}) — 운영 종료")
            if str(pid) in ws:
                ws.discard(str(pid))
                cfg_changed = True
            for key in ("booking_direct_urls", "booking_open_datetimes"):
                config.get(key, {}).pop(str(pid), None)
        if cfg_changed:
            config["watched_places"] = sorted(ws)
        if cfg_changed or expired_pids:
            CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"  [만료 정리] {len(expired_pids)}개 제거됨")

    seen_ids |= {str(pid) for pid in current}
    save_data(list(current.values()), config, prev_alerts + new_alerts, seen_ids)
    return current


def load_prev_state() -> dict:
    """재시작 시 이전 상태 복원 → 이미 오픈된 장소에 중복 알림 방지"""
    try:
        if DATA_FILE.exists():
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return {p["id"]: p for p in data.get("places", [])}
    except Exception:
        pass
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="한 번만 체크하고 종료 (GitHub Actions용)")
    parser.add_argument("--flush-ntfy", action="store_true", help="큐에 쌓인 ntfy 알림만 발송하고 종료 (GitHub Actions용)")
    args = parser.parse_args()

    config = load_config()

    if args.flush_ntfy:
        topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic", "")
        flush_pending_ntfy(topic)
        return
    if not config.get("areas"):
        print("모니터링할 지역이 없습니다. presale_config.json 의 areas 를 설정하세요.")
        sys.exit(0)

    prev: dict = load_prev_state()

    if args.once:
        # GitHub Actions: 한 번 체크하고 종료
        try:
            check_once(config, prev)
        except Exception as e:
            print(f"[오류] {e}")
        return

    # 로컬 루프 모드
    interval = config.get("check_interval_seconds", 60)
    print(f"=== 사전예약 오픈 감지 시작 | 주기: {interval}초 ===")
    for a in config["areas"]:
        print(f"  • {a['query']}")

    while True:
        try:
            config = load_config()
            ntfy_topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic", "")
            prev = check_once(config, prev)
            flush_pending_ntfy(ntfy_topic)  # 로컬 루프: push 없으므로 즉시 발송
        except Exception as e:
            print(f"[오류] {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
