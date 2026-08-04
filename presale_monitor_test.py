"""사전예약 모니터 회귀 테스트 — 오픈 예정 시각 자동 감지 + 링크 직접 등록.

네트워크 없이 돈다 (네이버 API 호출은 대체 함수로 교체).

확인 내용:
  - bookableSettingJson.openDateTime을 오픈 예정 시각으로 읽는다
  - isUseOpen=false면 오픈 예정 시각으로 쓰지 않는다
  - 조회 결과를 캐싱해 매 주기 다시 부르지 않는다 (30분)
  - 이미 지난 오픈 시각은 확정으로 보고 더 조회하지 않는다
  - 조회 실패 시 기존 캐시를 지우지 않는다
  - 오픈 시각 우선순위: 직접 입력 > 업체 설정 > 판매 시작일
  - manual_places(링크 직접 등록)는 지도 검색 결과에 없어도 목록에서 유지된다

사용법: python presale_monitor_test.py
"""

import sys
from datetime import datetime, timedelta

import presale_monitor as pm

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


KST = pm.KST
URL = "https://booking.naver.com/booking/13/bizes/1707570/items/7913541"
FUTURE = (datetime.now(KST) + timedelta(days=3)).replace(microsecond=0)
PAST = (datetime.now(KST) - timedelta(days=3)).replace(microsecond=0)


def stub_setting(setting, calls):
    def _fetch(booking_url, biz_id):
        calls.append((booking_url, biz_id))
        return setting
    return _fetch


def place(**kw):
    p = {"id": "1", "name": "테스트", "bookingUrl": URL, "bookingBusinessId": "1707570",
         "bookingOpenAuto": None, "bookingOpenAutoCheckedAt": None, "saleStartDate": None}
    p.update(kw)
    return p


def main() -> int:
    real_fetch = pm.fetch_bookable_setting

    print("1) booking_open_from_setting — 오픈 예정 시각 추출")
    check(pm.booking_open_from_setting(
        {"isUseOpen": True, "openDateTime": "2026-08-04T00:00:00+09:00"}
    ) == "2026-08-04T00:00:00+09:00", "isUseOpen=true면 openDateTime 사용")
    check(pm.booking_open_from_setting(
        {"isUseOpen": False, "openDateTime": "2026-08-04T00:00:00+09:00"}) is None,
        "isUseOpen=false면 무시")
    check(pm.booking_open_from_setting({"isUseOpen": True, "openDateTime": None}) is None,
          "openDateTime이 없으면 None")
    check(pm.booking_open_from_setting(None) is None, "설정 자체가 없으면 None")

    print("2) refresh_booking_open_auto — 조회·캐싱")
    calls = []
    pm.fetch_bookable_setting = stub_setting(
        {"isPaused": False, "isUseOpen": True, "openDateTime": FUTURE.isoformat(), "isOpened": True}, calls)
    p = place()
    pm.refresh_booking_open_auto(p)
    check(p["bookingOpenAuto"] == FUTURE.isoformat(), f"오픈 시각 저장 ({p['bookingOpenAuto']})")
    check(p["bookingIsOpened"] is True and p["bookingPaused"] is False, "isOpened·isPaused 저장")
    pm.refresh_booking_open_auto(p)
    check(len(calls) == 1, f"30분 안에는 재조회 안 함 (호출 {len(calls)}회)")

    p["bookingOpenAutoCheckedAt"] = (datetime.now(KST) - timedelta(hours=2)).isoformat()
    pm.refresh_booking_open_auto(p)
    check(len(calls) == 2, "30분이 지나고 오픈 전이면 재조회")

    print("3) 이미 지난 오픈 시각은 재조회하지 않음")
    calls = []
    pm.fetch_bookable_setting = stub_setting(
        {"isPaused": False, "isUseOpen": True, "openDateTime": PAST.isoformat(), "isOpened": True}, calls)
    p = place(bookingOpenAuto=PAST.isoformat(),
              bookingOpenAutoCheckedAt=(datetime.now(KST) - timedelta(hours=5)).isoformat())
    pm.refresh_booking_open_auto(p)
    check(not calls, "오픈 시각이 지났으면 조회 생략")

    print("4) 조회 실패 시 기존 캐시 유지")
    pm.fetch_bookable_setting = lambda u, b: None
    p = place(bookingOpenAuto=FUTURE.isoformat(),
              bookingOpenAutoCheckedAt=(datetime.now(KST) - timedelta(hours=5)).isoformat())
    pm.refresh_booking_open_auto(p)
    check(p["bookingOpenAuto"] == FUTURE.isoformat(), "실패해도 값을 지우지 않는다")

    print("5) /items/ URL이 아니면 조회하지 않음")
    calls = []
    pm.fetch_bookable_setting = stub_setting({"isUseOpen": True, "openDateTime": FUTURE.isoformat()}, calls)
    p = place(bookingUrl="https://m.booking.naver.com/booking/13/bizes/1707570")
    pm.refresh_booking_open_auto(p)
    check(not calls and p["bookingOpenAuto"] is None, "상품 URL이 없으면 조회 생략")

    print("6) resolve_sale_start — 우선순위")
    manual = (datetime.now(KST) + timedelta(days=1)).replace(microsecond=0)
    pm.fetch_bookable_setting = stub_setting(
        {"isUseOpen": True, "openDateTime": FUTURE.isoformat(), "isOpened": True}, [])
    p = place(bookingOpenDatetime=manual.isoformat(), saleStartDate=PAST.isoformat())
    check(pm.resolve_sale_start(p) == manual, "직접 입력이 최우선")

    p = place(saleStartDate=PAST.isoformat())
    check(pm.resolve_sale_start(p) == FUTURE, "직접 입력이 없으면 업체 설정(bookableSettingJson)")

    pm.fetch_bookable_setting = lambda u, b: {"isUseOpen": False, "openDateTime": None}
    p = place(saleStartDate=FUTURE.isoformat())
    check(pm.resolve_sale_start(p) == FUTURE, "업체 설정이 없으면 판매 시작일")

    print("7) normalize_manual — 링크 직접 등록")
    m = pm.normalize_manual({"id": "manual-1707570-7913541", "name": "직접등록", "bookingUrl": URL,
                             "district": "성동구"})
    check(m["isManual"] is True, "isManual 표시")
    check(m["bookingBusinessId"] == "1707570", f"URL에서 businessId 추출 ({m['bookingBusinessId']})")
    check(m["district"] == "성동구" and m["hasBooking"] is False, "지역 유지, 초기 hasBooking=False")

    print("8) check_once — 지도 검색에 없어도 manual_places는 유지")
    pm.fetch_presale_places = lambda area: []          # 검색 결과 없음
    pm.fetch_bookable_setting = lambda u, b: {"isPaused": False, "isUseOpen": True,
                                              "openDateTime": FUTURE.isoformat(), "isOpened": True}
    pm.load_prev_alerts = lambda: []
    pm.load_seen_ids = lambda: set()
    pm.has_available_slots = lambda u, b: True
    pm._queue_ntfy = lambda *a, **k: None
    pm.send_ntfy = lambda *a, **k: None
    pm.send_toast = lambda *a, **k: None
    saved = {}
    pm.save_data = lambda places, cfg, alerts=None, seen_ids=None: saved.update(
        {"places": places, "config": cfg})
    pm.CONFIG_FILE = type("P", (), {"write_text": staticmethod(lambda *a, **k: None),
                                    "name": "presale_config.json"})()

    cfg = {"areas": [{"query": "성수 팝업", "x": "1", "y": "2"}], "watched_places": [],
           "manual_places": [{"id": "manual-1707570-7913541", "name": "링크등록팝업",
                              "bookingUrl": URL}]}
    result = pm.check_once(cfg, {})
    check("manual-1707570-7913541" in result, f"검색 결과가 비어도 유지됨 (키: {list(result)})")
    mp = result.get("manual-1707570-7913541", {})
    check(mp.get("bookingOpenAuto") == FUTURE.isoformat(), "오픈 예정 시각 자동 감지됨")
    check(mp.get("hasBooking") is True, "isOpened=true면 예약 오픈으로 판정")
    check("manual-1707570-7913541" in [str(x) for x in cfg.get("watched_places", [])],
          "watched_places에 자동 추가")

    pm.fetch_bookable_setting = real_fetch

    print()
    if fails:
        print(f"실패 {len(fails)}건:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("전체 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
