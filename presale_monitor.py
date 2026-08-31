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
import subprocess
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

# "요즘 알림이 없다"가 진짜 신규가 없어서인지, 탐색이 깨진 건지 구분하기 위한 기준.
# 신규 발견은 보통 하루 2~3건 나온다 — 이 시간 동안 0건이면 탐색을 의심한다.
DISCOVERY_STALE_HOURS = 48
STALE_RENOTIFY_HOURS = 24   # 같은 경고를 다시 보내기까지의 최소 간격

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


def _recover_config_from_git() -> dict | None:
    """손상된(충돌 마커/깨진 JSON) presale_config.json을 git 이력의 최근 유효 버전으로 복구.
    tip이 깨져 있어도 과거 커밋에서 마지막 유효본을 찾아 파일을 다시 쓴다."""
    fname = CONFIG_FILE.name
    try:
        out = subprocess.run(
            ["git", "log", "--format=%H", "-n", "80", "--", fname],
            capture_output=True, text=True, cwd=str(BASE_DIR),
        ).stdout
    except Exception as e:
        print(f"  [복구 실패] git log 오류: {e}")
        return None
    for sha in out.split():
        try:
            blob = subprocess.run(
                ["git", "show", f"{sha}:{fname}"],
                capture_output=True, text=True, cwd=str(BASE_DIR), check=True,
            ).stdout
            cfg = json.loads(blob)
        except Exception:
            continue
        CONFIG_FILE.write_text(blob if blob.endswith("\n") else blob + "\n", encoding="utf-8")
        print(f"  [config 복구] 손상된 {fname}을 {sha[:7]} 버전으로 복원 (watched {len(cfg.get('watched_places', []))}개)")
        return cfg
    return None


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        return DEFAULT_CONFIG
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # autostash 충돌 마커 등으로 파일이 깨진 경우: 크래시 대신 이력에서 자가복구
        print("[경고] presale_config.json 파싱 실패 — git 이력에서 자가복구 시도")
        cfg = _recover_config_from_git()
        if cfg is None:
            print("[치명] config 자가복구 실패 — 기본 설정으로 진행(watched 유지 불가)")
            cfg = dict(DEFAULT_CONFIG)
    # 구버전 호환: "places" → "areas" 자동 마이그레이션
    if "places" in cfg and "areas" not in cfg:
        cfg["areas"] = DEFAULT_CONFIG["areas"]
        cfg.pop("places", None)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[설정 마이그레이션] areas 형식으로 업데이트됨")
    return cfg


def admission_name(item: dict) -> str:
    """항목의 admissionCondition.name (없거나 형태가 다르면 빈 문자열)."""
    ac = item.get("admissionCondition")
    return (ac.get("name") or "") if isinstance(ac, dict) else ""


def admission_label(item: dict) -> str:
    """admissionCondition 값 분포 집계용 라벨.

    네이버가 필드 이름·형태를 바꾸면 사전예약 항목이 통째로 0건이 되는데,
    그때 무엇으로 바뀌었는지 로그에 남기려고 별도 라벨을 붙인다.
    """
    ac = item.get("admissionCondition")
    if ac is None:
        return "(없음)"
    if isinstance(ac, dict):
        name = ac.get("name")
        if name:
            return str(name)
        return "(__ref)" if "__ref" in ac else "(이름없음)"
    return f"(비정상:{type(ac).__name__})"


def fetch_presale_places(area: dict, stats: dict | None = None) -> list[dict] | None:
    """지역 검색 결과의 사전예약 팝업 목록. 조회/파싱 실패 시 None (빈 결과 []와 구분).

    stats를 넘기면 이번 주기의 탐색 상태(성공/실패 지역 수, 팝업 후보 수,
    admissionCondition 값 분포)를 누적한다.
    """
    def bump(key: str, n: int = 1) -> None:
        if stats is not None:
            stats[key] = stats.get(key, 0) + n

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
        bump("areas_failed")
        return None

    m = re.search(
        r'window\.__APOLLO_STATE__\s*=\s*(\{.+?\});\s*(?:</script>|window\.)',
        resp.text, re.DOTALL,
    )
    if not m:
        print(f"  [파싱 오류] Apollo state 없음: {area['query']} (status={resp.status_code})")
        bump("areas_failed")
        return None

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  [JSON 오류] {e}")
        bump("areas_failed")
        return None

    bump("areas_ok")

    # 팝업 항목 후보 = admissionCondition 필드를 가진 엔트리
    candidates = [v for v in data.values()
                  if isinstance(v, dict) and "admissionCondition" in v]
    bump("candidate_items", len(candidates))
    if stats is not None:
        names = stats.setdefault("admission_names", {})
        for v in candidates:
            label = admission_label(v)
            names[label] = names.get(label, 0) + 1

    # 타입 prefix 무관하게 admissionCondition.name에 "사전예약" 포함된 항목만 수집
    presale = [v for v in candidates if PRESALE_NAME_FILTER in admission_name(v)]
    bump("presale_items", len(presale))

    # address_filter 설정 시 commonAddress로 필터링 (예: "성동구")
    addr_filter = area.get("address_filter", "").strip()
    if addr_filter:
        before = len(presale)
        presale = [p for p in presale if addr_filter in (p.get("commonAddress") or "")]
        filtered = before - len(presale)
        if filtered:
            print(f"  [필터] '{addr_filter}' 외 {filtered}개 제외")

    bump("after_district_filter", len(presale))
    return presale


def extract_district(common_address: str | None) -> str | None:
    """commonAddress('서울 성동구')에서 구 이름('성동구')만 추출."""
    if not common_address:
        return None
    for tok in common_address.split():
        if tok.endswith("구"):
            return tok
    return None


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
        "district": extract_district(p.get("commonAddress")),
        "bookingOpenDatetime": None,
        "bookingOpenHistory": [],  # 예약 오픈된 이력 (ISO datetime 목록)
        # 업체가 예약 관리에 지정한 오픈 예정 시각 (bookableSettingJson.openDateTime)
        # ISO 문자열 / "" = 조회했지만 없음 / None = 아직 조회 안 함
        "bookingOpenAuto": None,
        "bookingOpenAutoCheckedAt": None,
        "bookingPaused": False,
        "saleStartDate": None,  # 실제 판매 시작일 (네이버 API에서 자동 조회, ISO 문자열 또는 "" = 조회했지만 없음)
        "bookingNotified": False,  # 처음 오픈 알림을 보냈는지 (한 번 True가 되면 계속 유지 — 재오픈 알림은 안 보냄)
        "discoveredAt": None,  # 새 팝업으로 처음 발견된 시각 (ISO) — 관리 페이지 NEW 표시용
    }


def normalize_manual(entry: dict) -> dict:
    """config.manual_places 항목 → 장소 레코드.

    지도 검색(popupstore/list)에 안 나오고 예약 링크만 공개된 팝업을 직접 등록하기 위한 것.
    지도에서 오는 정보(이미지·주소·운영기간·D-day)는 없고, 예약 상태는 상품의
    bookableSettingJson으로 판단한다.
    """
    url = (entry.get("bookingUrl") or "").strip()
    parsed = parse_booking_item_url(url)
    return {
        "id": str(entry.get("id") or ""),
        "name": (entry.get("name") or "").strip(),
        "hasBooking": False,        # refresh_booking_open_auto 결과로 아래에서 결정
        "bookingUrl": url,
        "bookingBusinessId": (parsed or {}).get("biz_id") or entry.get("bookingBusinessId"),
        "operationStart": entry.get("operationStart"),
        "operationEnd": entry.get("operationEnd"),
        "remainingDays": None,
        "status": None,
        "admissionCondition": None,
        "imageUrl": None,
        "roadAddress": entry.get("roadAddress"),
        "commonAddress": entry.get("commonAddress"),
        "district": entry.get("district") or extract_district(entry.get("commonAddress")),
        "bookingOpenDatetime": None,
        "bookingOpenHistory": [],
        "bookingOpenAuto": None,
        "bookingOpenAutoCheckedAt": None,
        "bookingPaused": False,
        "bookingIsOpened": False,
        "saleStartDate": None,
        "bookingNotified": False,
        "discoveredAt": None,
        "isManual": True,           # 지도 검색에 없어도 목록에서 지우지 않는 표시
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


def parse_booking_item_url(url: str) -> dict | None:
    """.../booking/{service_id}/bizes/{biz_id}/items/{item_id} URL 파싱."""
    m = re.search(r"/booking/(\d+)/bizes/(\d+)/items/(\d+)", url or "")
    if not m:
        return None
    return {"service_id": int(m.group(1)), "biz_id": m.group(2), "item_id": m.group(3)}


def fetch_sale_start_date(booking_url: str, booking_business_id: str) -> str | None:
    """네이버 예약 GraphQL(schedule)로 실제 판매 시작일(saleStartDate) 조회.
    예약창(hasBooking)은 열려도 saleStartDate가 미래인 경우 실제 예약은 아직 불가하다.
    미지원/조회 실패 시 None (호출 측에서 '정보 없음'으로 취급, 항상 재조회하지 않도록 캐싱 권장)."""
    parsed = parse_booking_item_url(booking_url)
    if not parsed or not booking_business_id:
        return None
    now = datetime.now(KST)
    query = (
        "query schedule($scheduleParams: ScheduleParams) {"
        "  schedule(input: $scheduleParams) {"
        "    bizItemSchedule { saleStartDate __typename } __typename } }"
    )
    body = {
        "operationName": "schedule",
        "variables": {
            "scheduleParams": {
                "businessId": booking_business_id,
                "bizItemId": parsed["item_id"],
                "businessTypeId": parsed["service_id"],
                "startDateTime": now.strftime("%Y-%m-%dT00:00:00+09:00"),
                "endDateTime": (now + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59+09:00"),
            }
        },
        "query": query,
    }
    try:
        resp = requests.post(
            "https://m.booking.naver.com/graphql?opName=schedule",
            json=body,
            headers={"Content-Type": "application/json", "User-Agent": SESSION.headers["User-Agent"],
                     "Referer": "https://m.booking.naver.com/"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            return None
        return data["data"]["schedule"]["bizItemSchedule"].get("saleStartDate")
    except Exception:
        return None


def fetch_bookable_setting(booking_url: str, booking_business_id: str) -> dict | None:
    """상품의 예약 오픈 설정(bookableSettingJson) 조회.

      {"isPaused": false, "isUseOpen": true,
       "openDateTime": "2026-08-04T00:00:00+09:00", "isOpened": true}

    업체가 예약 관리에서 직접 지정한 오픈 예정 시각이라, 판매 시작일(saleStartDate)보다
    정확하다. saleStartDate는 이 상품처럼 null로 오는 경우가 많다.
    조회 실패 시 None."""
    parsed = parse_booking_item_url(booking_url)
    if not parsed or not booking_business_id:
        return None
    body = {
        "operationName": "bizItem",
        "variables": {"businessId": booking_business_id, "bizItemId": parsed["item_id"]},
        "query": ("query bizItem($businessId: String!, $bizItemId: String!) {"
                  "  bizItem(input: { businessId: $businessId, bizItemId: $bizItemId }) {"
                  "    name bookableSettingJson } }"),
    }
    try:
        resp = requests.post(
            "https://m.booking.naver.com/graphql?opName=bizItem",
            json=body,
            headers={"Content-Type": "application/json", "User-Agent": SESSION.headers["User-Agent"],
                     "Referer": "https://m.booking.naver.com/"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            return None
        item = (data.get("data") or {}).get("bizItem") or {}
        setting = item.get("bookableSettingJson")
        if not isinstance(setting, dict):
            return None
        return {**setting, "name": item.get("name")}
    except Exception:
        return None


def booking_open_from_setting(setting: dict | None) -> str | None:
    """bookableSettingJson에서 오픈 예정 시각(ISO) 추출. 오픈 예약을 안 쓰면 None."""
    if not setting or not setting.get("isUseOpen"):
        return None
    dt = setting.get("openDateTime")
    return dt if isinstance(dt, str) and dt else None


def refresh_booking_open_auto(place: dict) -> None:
    """place의 자동 감지 오픈 예정 시각(bookingOpenAuto)을 갱신·캐싱한다.

    캐시 규칙 — 매 주기 조회하지 않으면서도 업체가 나중에 시각을 바꾸면 따라가도록:
      - 한 번도 조회 안 함        → 조회
      - 마지막 조회가 30분 초과   → 아직 오픈 시각이 미래이거나 값이 없을 때만 재조회
      - 오픈 시각이 이미 지남     → 확정으로 보고 더는 조회하지 않음
    """
    booking_url = place.get("bookingUrl") or ""
    biz_id = place.get("bookingBusinessId") or ""
    if not biz_id or "/items/" not in booking_url:
        return

    cached = place.get("bookingOpenAuto")
    checked_at = place.get("bookingOpenAutoCheckedAt")
    now = datetime.now(KST)

    if cached is not None and checked_at:
        try:
            if (now - datetime.fromisoformat(checked_at)) < timedelta(minutes=30):
                return
        except Exception:
            pass
        if cached:
            try:
                if datetime.fromisoformat(cached) <= now:
                    return          # 이미 오픈함 — 값이 더 바뀔 일 없음
            except Exception:
                pass

    setting = fetch_bookable_setting(booking_url, biz_id)
    if setting is None:
        return                      # 조회 실패 — 기존 캐시 유지
    place["bookingOpenAuto"] = booking_open_from_setting(setting) or ""
    place["bookingOpenAutoCheckedAt"] = now.isoformat()
    place["bookingPaused"] = bool(setting.get("isPaused"))
    place["bookingIsOpened"] = bool(setting.get("isOpened"))
    if place.get("isManual") and not place.get("name") and setting.get("name"):
        place["name"] = setting["name"]


def resolve_sale_start(place: dict) -> "datetime | None":
    """실제 판매 시작 시각.
    우선순위: 수동 설정(booking_open_datetimes) > 업체 오픈 설정(bookableSettingJson)
              > 판매 시작일(saleStartDate)
    아직 자동 조회를 안 해봤으면 place를 직접 갱신해 캐싱한다(재조회 방지)."""
    manual = place.get("bookingOpenDatetime")
    if manual:
        try:
            return datetime.fromisoformat(manual)
        except Exception:
            pass

    refresh_booking_open_auto(place)
    auto = place.get("bookingOpenAuto")
    if auto:
        try:
            return datetime.fromisoformat(auto)
        except Exception:
            pass

    cached = place.get("saleStartDate")
    if cached is None:
        biz_id = place.get("bookingBusinessId") or ""
        booking_url = place.get("bookingUrl") or ""
        if biz_id and "/items/" in booking_url:
            fetched = fetch_sale_start_date(booking_url, biz_id)
            place["saleStartDate"] = fetched or ""
            cached = place["saleStartDate"]
        else:
            return None
    if not cached:
        return None
    try:
        return datetime.fromisoformat(cached)
    except Exception:
        return None


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


def load_discovery_stats() -> dict:
    """직전 주기의 탐색 상태 (마지막 신규 발견 시각·경고 발송 시각 유지용)."""
    try:
        if DATA_FILE.exists():
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            return data.get("discovery_stats") or {}
    except Exception:
        pass
    return {}


def hours_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return (datetime.now(KST) - dt).total_seconds() / 3600


def last_known_discovery(alerts: list[dict], places) -> str | None:
    """discovery_stats가 아직 없을 때 쓰는 마지막 신규 발견 시각 추정치."""
    cands = [a.get("ts") for a in alerts
             if a.get("type") == "new_popup" and a.get("ts")]
    cands += [p.get("discoveredAt") for p in places if p.get("discoveredAt")]
    return max(cands) if cands else None


def build_discovery_stats(fetch_stats: dict, areas_total: int, unique_ids: int,
                          places, new_popups: list[dict], prev_alerts: list[dict],
                          now_iso: str) -> dict:
    """이번 주기의 탐색 상태 — "신규 0건"이 진짜인지 판단할 근거를 남긴다."""
    prev_stats = load_discovery_stats()
    places = list(places)
    if new_popups:
        last_new = now_iso
    else:
        last_new = (prev_stats.get("last_new_place_at")
                    or last_known_discovery(prev_alerts, places))
    names = fetch_stats.get("admission_names", {})
    return {
        "checked_at": now_iso,
        "areas_total": areas_total,
        "areas_ok": fetch_stats.get("areas_ok", 0),
        "areas_failed": fetch_stats.get("areas_failed", 0),
        "candidate_items": fetch_stats.get("candidate_items", 0),
        "presale_items": fetch_stats.get("presale_items", 0),
        "after_district_filter": fetch_stats.get("after_district_filter", 0),
        "unique_place_ids": unique_ids,
        "tracked_places": len(places),
        "new_places": len(new_popups),
        "last_new_place_at": last_new,
        # 상위 8개만 — 네이버가 admissionCondition을 바꾸면 여기서 먼저 드러난다
        "admission_names": dict(sorted(names.items(), key=lambda kv: -kv[1])[:8]),
        "stale_warned_at": prev_stats.get("stale_warned_at"),
    }


def report_discovery(stats: dict, config: dict, sel_url: str) -> None:
    """주기마다 탐색 한 줄 요약. 신규가 오래 끊기면 경고 알림도 보낸다."""
    age = hours_since(stats.get("last_new_place_at"))
    age_txt = "기록 없음" if age is None else f"{age / 24:.1f}일 전"
    print(f"  [탐색] 지역 {stats['areas_ok']}/{stats['areas_total']} 성공"
          f" | 후보 {stats['candidate_items']}"
          f" → 사전예약 {stats['presale_items']}"
          f" → 지역필터 {stats['after_district_filter']}"
          f" | 추적 {stats['tracked_places']}개"
          f" | 이번 주기 신규 {stats['new_places']}건"
          f" | 마지막 신규 {age_txt}")
    if stats["areas_failed"]:
        print(f"  [탐색] 조회 실패 지역 {stats['areas_failed']}개")
    if stats["areas_ok"] and not stats["presale_items"]:
        dist = ", ".join(f"{k}={v}" for k, v in stats["admission_names"].items())
        print(f"  [탐색 경고] 사전예약 항목 0건 — admissionCondition 분포: {dist or '없음'}")

    limit = config.get("discovery_stale_hours", DISCOVERY_STALE_HOURS)
    if age is None or age < limit:
        return
    warned = hours_since(stats.get("stale_warned_at"))
    if warned is not None and warned < STALE_RENOTIFY_HOURS:
        return
    body = (f"{age / 24:.1f}일째 새 팝업이 한 건도 안 잡혔어요. "
            f"지역 {stats['areas_ok']}/{stats['areas_total']} 조회 성공, "
            f"사전예약 {stats['presale_items']}개 인식 중.")
    print(f"  [탐색 경고] {body}")
    _queue_ntfy("⚠️ 사전예약 탐색 점검 필요", body, sel_url)
    stats["stale_warned_at"] = datetime.now(KST).isoformat()


def save_data(places: list[dict], config: dict, alerts: list[dict] | None = None,
              seen_ids: set | None = None, discovery_stats: dict | None = None) -> None:
    data = {
        "updated_at": datetime.now(KST).isoformat(),
        "watched_places": config.get("watched_places", []),
        "booking_open_datetimes": config.get("booking_open_datetimes", {}),
        "manual_places": config.get("manual_places", []),
        "places": places,
        "alerts": (alerts or [])[-200:],  # 최근 200건만 유지
        "seen_place_ids": sorted(seen_ids or set()),
        "discovery_stats": discovery_stats if discovery_stats is not None else load_discovery_stats(),
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
    fetch_stats: dict = {}
    for area in config.get("areas", []):
        result = fetch_presale_places(area, fetch_stats)
        if result is None:
            fetch_failed = True
            continue
        for p in result:
            pid = p.get("id")
            if pid and pid not in raw:
                raw[pid] = p

    current: dict[str, dict] = {pid: normalize(p) for pid, p in raw.items()}

    # 링크로 직접 등록한 팝업 (지도 검색에 안 나오는 것) — 검색 결과와 무관하게 항상 유지
    for entry in config.get("manual_places", []):
        place = normalize_manual(entry)
        if not place["id"] or not place["bookingUrl"]:
            continue
        current[place["id"]] = place

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
        place["saleStartDate"] = prev.get(pid, {}).get("saleStartDate")
        place["bookingOpenAuto"] = prev.get(pid, {}).get("bookingOpenAuto")
        place["bookingOpenAutoCheckedAt"] = prev.get(pid, {}).get("bookingOpenAutoCheckedAt")
        place["bookingPaused"] = prev.get(pid, {}).get("bookingPaused", False)
        place["bookingIsOpened"] = prev.get(pid, {}).get("bookingIsOpened", False)
        place["bookingNotified"] = prev.get(pid, {}).get("bookingNotified", False)
        place["discoveredAt"] = prev.get(pid, {}).get("discoveredAt")

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

    # 예약 오픈 예정 시각 자동 감지 (업체가 예약 관리에 지정한 bookableSettingJson).
    # 오픈 전 팝업도 "오픈 정보"에 시각이 떠야 하므로 hasBooking과 무관하게 갱신한다.
    # 조회는 30분 캐시가 걸려 있어 매 주기 요청하지 않는다 (refresh_booking_open_auto).
    for pid, place in current.items():
        if place.get("isManual") or str(pid) in watched:
            refresh_booking_open_auto(place)
        if place.get("isManual"):
            # 지도 정보가 없으므로 예약 오픈 여부는 상품 설정으로만 판단
            place["hasBooking"] = bool(place.get("bookingIsOpened")) and not place.get("bookingPaused")
            if not place["name"]:
                place["name"] = "(이름 미확인)"

    for pid, place in current.items():
        name = place["name"]
        is_open = place["hasBooking"]
        was_open = prev.get(pid, {}).get("hasBooking", False)
        booking_url = place.get("bookingUrl") or ""
        dday = place.get("status") or ""

        if pid not in prev and str(pid) not in seen_ids:
            # 처음 보는 팝업 → git push 이후 알림 발송 (페이지 데이터가 업데이트된 뒤 수신되도록)
            place["discoveredAt"] = now_iso
            if is_open:
                place["bookingOpenHistory"].append(now_iso)
            if place.get("isManual"):
                # 사용자가 링크로 직접 등록한 팝업 — 본인이 방금 추가했으므로 발견 알림은 생략
                print(f"[{now_str}] ➕ {name} — 링크로 직접 등록됨")
            else:
                print(f"[{now_str}] 🆕 {name} — 새 팝업 발견!")
                _queue_ntfy(f"🆕 새 팝업 발견: {name}", "예약 선택 페이지에서 확인하세요", sel_url or booking_url)
            new_alerts.append({"type": "new_popup", "place_id": str(pid), "place_name": name,
                                "booking_url": booking_url, "ts": now_iso})
        elif pid not in prev:
            # 과거에 이미 발견 알림을 보낸 팝업이 검색에 다시 나타남 → 조용히 복원
            print(f"[{now_str}] ↩️ {name} — 재등장 (이미 발견한 팝업, 알림 생략)")
        elif is_open and not was_open:
            # 예약창(페이지) 자체가 열림 — 실제 판매 시작 여부는 아래에서 별도 확인
            place["bookingOpenHistory"].append(now_iso)
            new_alerts.append({"type": "booking_open", "place_id": str(pid), "place_name": name,
                                "booking_url": booking_url, "ts": now_iso})

        if not is_open:
            print(f"[{now_str}] ⏳ {name} ({dday}) — 대기중")
            continue

        # 예약창은 열려 있음 — 실제 판매 시작일(saleStartDate, 수동 설정 우선)을 확인해
        # 예약 창만 열리고 실제 예약은 아직 불가능한 경우("오픈" 오탐)를 걸러낸다.
        sale_start = resolve_sale_start(place)
        now_dt = datetime.now(KST)
        if sale_start and now_dt < sale_start:
            print(f"[{now_str}] ⏳ {name} — 예약창은 열렸지만 실제 판매 시작 전 "
                  f"({sale_start.strftime('%m/%d %H:%M')} 시작 예정)")
            continue

        if pid not in watched:
            print(f"[{now_str}] ✅ {name} ({dday}) — 예약중 (알림없음)")
            continue

        # presale_monitor는 "처음 오픈" 알림 전용이다. 닫힘 후 재오픈 감지·알림은
        # check_booking.py(monitors.json) 쪽에서 항목별로 다룬다 — 한 번 알림을
        # 보낸 장소는 bookingNotified를 계속 True로 유지해 다시 알리지 않는다.
        if place.get("bookingNotified"):
            print(f"[{now_str}] ✅ {name} ({dday}) — 예약중 (이미 알림 발송함, 생략)")
            continue

        biz_id = place.get("bookingBusinessId") or ""
        if not has_available_slots(booking_url, biz_id):
            print(f"[{now_str}] ✅ {name} ({dday}) — 예약중 (잔여 없음, 알림 생략)")
            continue

        print(f"[{now_str}] 🎉 {name} — 사전예약 오픈! {booking_url}")
        msg = f"지금 바로 예약하세요! → {booking_url}"
        send_ntfy(ntfy_topic, f"🎉 {name} 사전예약 오픈!", msg, booking_url)
        send_toast(name, msg, booking_url)
        place["lastBookingNotifiedAt"] = now_dt.isoformat()
        place["bookingNotified"] = True

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

    stats = build_discovery_stats(
        fetch_stats, len(config.get("areas", [])), len(raw), current.values(),
        [a for a in new_alerts if a["type"] == "new_popup"], prev_alerts, now_iso)
    report_discovery(stats, config, sel_url)

    seen_ids |= {str(pid) for pid in current}
    save_data(list(current.values()), config, prev_alerts + new_alerts, seen_ids, stats)
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
