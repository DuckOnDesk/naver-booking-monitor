"""예약 링크 회귀 테스트 — 알림에 반드시 예약 링크가 걸리도록.

배경 (2026-09-17 ~ 09-23에 확인된 실제 사고):
지도 검색(popupstore/list)은 같은 팝업이라도 bookingUrl을 줄 때와 안 줄 때가 있다.
2026-09-17 09:20에 팝업 6개가 검색에서 잠깐 빠졌고, 돌아왔을 때 4개가 링크를 잃었다.
장소 기록이 통째로 지워졌다 다시 만들어지면서 "이전 URL 유지"가 끊겼기 때문이다.
그 뒤 링크 보유율이 100% → 61%까지 내려갔고, 오픈 알림이 예약 페이지가 아니라
지도 장소 페이지로 연결됐다 (루나·THE AGE20'S 등).

예전에는 businessId로 예약 타입(/booking/{type}/)을 찍어 맞히려 했는데, 그 확인에
쓰던 달력 API가 이미 죽어 있어서(2026-09-08부터 JSON 대신 HTML 반환) 37번 시도해
37번 실패했다. 지금은 장소 상세 페이지에서 예약 링크를 그대로 찾아온다 —
businessId가 일치하는 링크만 채택하므로 타입을 추측할 필요가 없다.

네트워크 없이 돈다 (네이버 호출은 대체 함수로 교체).

확인 내용:
  - 장소 상세 페이지 HTML에서 예약 URL을 뽑는다 (JSON 이스케이프 포함)
  - businessId가 맞는 링크만 쓴다 (남의 업체 링크를 잘못 걸지 않는다)
  - /items/까지 있는 링크를 우선한다
  - 한 번 확인한 링크는 영구 보관되고 빈 값으로 덮이지 않는다
  - 오픈 알림은 예약 링크를 달고 나간다. 끝내 못 찾을 때만 지도 링크로 떨어진다

사용법: python presale_bookingurl_test.py
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
PID = "2029141346"          # 루나 클라우드 레이어 팝업스토어
BIZ = "1737133"
NAME = "루나 클라우드 레이어 팝업스토어"
BOOK_URL = f"https://m.booking.naver.com/booking/12/bizes/{BIZ}"


class Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text
        self.encoding = "utf-8"


def stub_get(routes, calls=None):
    def _get(url, **kw):
        if calls is not None:
            calls.append(url)
        for prefix, resp in routes.items():
            if url.startswith(prefix):
                return resp() if callable(resp) else resp
        return Resp(status_code=404)
    return _get


def main() -> int:
    real_get = pm.SESSION.get

    print("1) extract_booking_urls — 페이지에서 예약 URL 뽑기")
    html = f'<a href="https://m.booking.naver.com/booking/12/bizes/{BIZ}">예약</a>'
    check(pm.extract_booking_urls(html) == [(BIZ, BOOK_URL)], "평범한 링크")

    escaped = '{"bookingUrl":"https:\\/\\/m.booking.naver.com\\/booking\\/6\\/bizes\\/999\\/items\\/77"}'
    got = pm.extract_booking_urls(escaped)
    check(got == [("999", "https://m.booking.naver.com/booking/6/bizes/999/items/77")],
          f"JSON 이스케이프(\\/)된 링크도 찾는다 ({got})")

    dup = html + html + f'<a href="https://booking.naver.com/booking/13/bizes/555">x</a>'
    got = pm.extract_booking_urls(dup)
    check(len(got) == 2 and got[0][0] == BIZ, f"중복 제거, m. 없는 주소도 인식 ({got})")
    check(pm.extract_booking_urls("예약 링크 없음") == [], "없으면 빈 목록")

    print()
    print("2) pick_booking_url — businessId가 맞는 것만")
    cands = [("999", "https://m.booking.naver.com/booking/5/bizes/999"), (BIZ, BOOK_URL)]
    check(pm.pick_booking_url(cands, BIZ) == BOOK_URL, "businessId 일치 항목 선택")
    check(pm.pick_booking_url([("999", "https://m.booking.naver.com/booking/5/bizes/999")], BIZ) == "",
          "businessId가 다르면 쓰지 않는다 (엉뚱한 업체 링크 방지)")
    with_item = [(BIZ, BOOK_URL), (BIZ, BOOK_URL + "/items/8080")]
    check(pm.pick_booking_url(with_item, BIZ) == BOOK_URL + "/items/8080",
          "/items/까지 있는 링크를 우선")
    check(pm.pick_booking_url(cands, "") == cands[0][1], "businessId를 모르면 첫 후보")
    check(pm.pick_booking_url([], BIZ) == "", "후보가 없으면 빈 문자열")

    print()
    print("3) fetch_place_booking_url — 장소 상세 페이지 조회")
    pm._PLACE_URL_CACHE.clear()
    calls: list = []
    pm.SESSION.get = stub_get({
        f"https://m.place.naver.com/place/{PID}/home": Resp(text=html),
    }, calls)
    check(pm.fetch_place_booking_url(PID, BIZ) == BOOK_URL, "상세 페이지에서 예약 URL 발견")
    check(any("pcmap.place.naver.com" in c for c in calls), "후보 주소를 순서대로 시도")

    before = len(calls)
    pm.fetch_place_booking_url(PID, BIZ)
    check(len(calls) == before, "같은 주기에 같은 장소를 다시 조회하지 않는다 (캐시)")

    pm._PLACE_URL_CACHE.clear()
    pm.SESSION.get = stub_get({})
    check(pm.fetch_place_booking_url(PID, BIZ) == "", "페이지를 못 열면 빈 문자열")
    pm._PLACE_URL_CACHE.clear()
    pm.SESSION.get = stub_get({
        f"https://pcmap.place.naver.com/place/{PID}/home": Resp(text="예약 링크 없음"),
    })
    check(pm.fetch_place_booking_url(PID, BIZ) == "", "페이지에 예약 링크가 없으면 빈 문자열")

    print()
    print("4) remember_booking_url — 한 번 확인한 링크는 영구 보관")
    hist: dict = {}
    pm.remember_booking_url(hist, PID, BOOK_URL)
    check(hist[PID] == BOOK_URL, "링크 저장")
    pm.remember_booking_url(hist, PID, "")
    check(hist[PID] == BOOK_URL, "빈 값으로 덮어쓰지 않는다 (예약창이 닫혀도 유지)")
    pm.remember_booking_url(hist, PID, BOOK_URL + "/items/8080")
    check(hist[PID] == BOOK_URL + "/items/8080", "더 구체적인 링크면 갱신")
    pm.remember_booking_url(hist, PID, BOOK_URL)
    check(hist[PID] == BOOK_URL + "/items/8080", "덜 구체적인 링크로는 되돌리지 않는다")

    print()
    print("5) check_once — 루나 상황 재현 (지도 검색엔 링크 없음, 상세 페이지엔 있음)")
    real = {name: getattr(pm, name) for name in (
        "fetch_presale_places", "fetch_bookable_setting", "fetch_sale_start_date",
        "load_prev_alerts", "load_seen_ids", "has_available_slots",
        "load_place_memory", "load_auto_added_ids", "load_watch_missing",
        "load_booking_url_history", "resolve_booking_item_url",
        "_queue_ntfy", "send_ntfy", "send_toast", "save_data", "CONFIG_FILE")}

    pm._PLACE_URL_CACHE.clear()
    pm.SESSION.get = stub_get({
        f"https://pcmap.place.naver.com/place/{PID}/home": Resp(text=html),
    })
    pm.fetch_presale_places = lambda area, stats=None: [{
        "id": PID, "name": NAME, "hasBooking": True,
        "bookingUrl": None, "bookingBusinessId": BIZ,          # ← 지도 검색이 링크를 안 줌
        "popupstoreInfo": {"admissionCondition": {"name": "사전예약"}, "remainingDays": 8},
        "commonAddress": "서울 성동구",
    }]
    pm.fetch_bookable_setting = lambda u, b: {"isPaused": False, "isUseOpen": False,
                                              "openDateTime": None, "isOpened": True}
    pm.fetch_sale_start_date = lambda u, b: None
    pm.resolve_booking_item_url = lambda u: u          # /items/ 조회는 별도 테스트
    pm.load_prev_alerts = lambda: []
    pm.load_seen_ids = lambda: {PID}
    pm.has_available_slots = lambda u, b: True
    pm.load_place_memory = lambda: {}
    pm.load_auto_added_ids = lambda: set()
    pm.load_watch_missing = lambda: {}
    saved_hist: dict = {}
    pm.load_booking_url_history = lambda: dict(saved_hist)
    pm._queue_ntfy = lambda *a, **k: None
    sent: list = []
    pm.send_ntfy = lambda topic, title, body, url: sent.append({"body": body, "url": url})
    pm.send_toast = lambda *a, **k: None
    saved: dict = {}

    def _save(places, cfg, alerts=None, seen_ids=None, discovery_stats=None,
              place_memory=None, auto_added_ids=None, watch_missing=None, url_history=None):
        saved.update({"places": places, "alerts": alerts})
        if url_history is not None:
            saved_hist.clear()
            saved_hist.update(url_history)
    pm.save_data = _save
    pm.CONFIG_FILE = type("P", (), {"write_text": staticmethod(lambda *a, **k: None),
                                    "name": "presale_config.json"})()

    cfg = {"areas": [{"query": "성수 팝업", "x": "1", "y": "2", "address_filter": "성동구"}],
           "watched_places": [PID], "ntfy_topic": "t",
           "selection_page_url": "https://example.test/select"}
    prev = {PID: {"id": PID, "name": NAME, "hasBooking": False, "bookingUrl": None,
                  "bookingBusinessId": BIZ, "bookingNotified": False,
                  "bookingOpenHistory": [], "district": "성동구"}}
    result = pm.check_once(cfg, prev)

    check(result[PID].get("bookingUrl") == BOOK_URL,
          f"상세 페이지에서 예약 URL을 채워 넣음 ({result[PID].get('bookingUrl')})")
    check(len(sent) == 1, f"오픈 알림 1건 (실제 {len(sent)}건)")
    if sent:
        check(sent[0]["url"] == BOOK_URL, f"알림 링크가 예약 페이지 ({sent[0]['url']})")
        check("map.naver.com" not in sent[0]["url"], "지도 링크로 떨어지지 않는다")
    check(saved_hist.get(PID) == BOOK_URL, "확인한 링크가 영구 보관됨")

    print()
    print("6) check_once — 지도·상세 페이지 모두 링크가 없어도 보관본으로 복구")
    pm._PLACE_URL_CACHE.clear()
    pm.SESSION.get = stub_get({})              # 상세 페이지도 실패
    sent.clear()
    prev2 = {PID: dict(prev[PID], hasBooking=False, bookingUrl=None)}
    result2 = pm.check_once(cfg, prev2)
    check(result2[PID].get("bookingUrl") == BOOK_URL,
          f"영구 보관본에서 링크 복구 ({result2[PID].get('bookingUrl')})")
    if sent:
        check(sent[0]["url"] == BOOK_URL, "알림도 예약 링크로 나간다")

    print()
    print("7) check_once — 링크를 끝내 못 찾으면 지도 링크로 (링크 없는 알림 금지)")
    pm._PLACE_URL_CACHE.clear()
    pm.SESSION.get = stub_get({})
    saved_hist.clear()
    sent.clear()
    prev3 = {PID: dict(prev[PID])}
    result3 = pm.check_once(cfg, prev3)
    check(not (result3[PID].get("bookingUrl") or ""), "예약 URL은 비어 있는 상태")
    check(len(sent) == 1 and sent[0]["url"] == pm.place_map_url(PID),
          f"지도 장소 페이지로 대체 ({sent[0]['url'] if sent else None})")
    check(sent and not sent[0]["body"].rstrip().endswith("→"),
          "본문이 '→' 로 끝나지 않는다")

    for name, fn in real.items():
        setattr(pm, name, fn)
    pm.SESSION.get = real_get

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
