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

if not sys.stdout:
    sys.stdout = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
if not sys.stderr:
    sys.stderr = sys.stdout

KST = timezone(timedelta(hours=9))
LIST_URL = "https://pcmap.place.naver.com/popupstore/list"
PRESALE_KEY = "popupstore_label_prebook_and_walkin"

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


def fetch_presale_places(area: dict) -> list[dict]:
    params = {
        "query": area["query"],
        "x": area["x"], "y": area["y"],
        "clientX": area["x"], "clientY": area["y"],
        "display": "70",
        "ts": str(int(time.time() * 1000)),
        "locale": "ko",
        "mapUrl": f"https://map.naver.com/p/search/{area['query']}",
    }
    try:
        resp = SESSION.get(LIST_URL, params=params, timeout=15)
        resp.encoding = "utf-8"
    except Exception as e:
        print(f"  [요청 오류] {area['query']}: {e}")
        return []

    m = re.search(
        r'window\.__APOLLO_STATE__\s*=\s*(\{.+?\});\s*(?:</script>|window\.)',
        resp.text, re.DOTALL,
    )
    if not m:
        print(f"  [파싱 오류] Apollo state 없음: {area['query']} (status={resp.status_code})")
        return []

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  [JSON 오류] {e}")
        return []

    items = [v for k, v in data.items() if k.startswith("PopupstoreSearchBusinessItem:")]
    presale = [p for p in items if (p.get("admissionCondition") or {}).get("i18nKey") == PRESALE_KEY]

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
        "bookingOpenDatetime": None,  # check_once에서 config의 booking_open_datetimes로 채워짐
    }


def send_ntfy(topic: str, title: str, body: str, url: str) -> None:
    if not topic:
        return
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
        print("  → ntfy 전송 완료")
    except Exception as e:
        print(f"  [ntfy 오류] {e}")


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


def save_data(places: list[dict], config: dict) -> None:
    data = {
        "updated_at": datetime.now(KST).isoformat(),
        "disabled_places": config.get("disabled_places", []),
        "places": places,
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def check_once(config: dict, prev: dict) -> dict:
    now_str = datetime.now(KST).strftime("%H:%M:%S")
    disabled = set(str(x) for x in config.get("disabled_places", []))
    # 환경변수 우선 (GitHub Actions secret), 없으면 config 값 사용
    ntfy_topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic", "")

    raw: dict[str, dict] = {}
    for area in config.get("areas", []):
        for p in fetch_presale_places(area):
            pid = p.get("id")
            if pid and pid not in raw:
                raw[pid] = p

    current: dict[str, dict] = {pid: normalize(p) for pid, p in raw.items()}

    # 이전에 발견한 장소는 검색에 안 나와도 유지 (새 장소만 추가)
    for pid, place in prev.items():
        if pid not in current:
            current[pid] = place

    # config의 booking_open_datetimes를 각 장소에 병합
    bod = config.get("booking_open_datetimes", {})
    for pid, place in current.items():
        place["bookingOpenDatetime"] = bod.get(str(pid))

    for pid, place in current.items():
        name = place["name"]
        is_open = place["hasBooking"]
        was_open = prev.get(pid, {}).get("hasBooking", False)
        booking_url = place.get("bookingUrl") or ""
        dday = place.get("status") or ""

        if is_open and not was_open:
            print(f"[{now_str}] 🎉 {name} — 사전예약 오픈! {booking_url}")
            if pid not in disabled:
                msg = f"지금 바로 예약하세요! → {booking_url}"
                send_ntfy(ntfy_topic, f"🎉 {name} 사전예약 오픈!", msg, booking_url)
                send_toast(name, msg, booking_url)
        elif is_open:
            print(f"[{now_str}] ✅ {name} ({dday}) — 예약중")
        else:
            print(f"[{now_str}] ⏳ {name} ({dday}) — 대기중")

    save_data(list(current.values()), config)
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
    args = parser.parse_args()

    config = load_config()
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
            prev = check_once(config, prev)
        except Exception as e:
            print(f"[오류] {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
