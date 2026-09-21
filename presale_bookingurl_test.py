"""사전예약 알림 링크 회귀 테스트 — bookingUrl이 없는 팝업도 예약 링크를 찾아낸다.

2026-09-21 리베르 X 무신사 팝업에서 터진 문제:
지도 검색이 hasBooking=true·bookingBusinessId만 주고 bookingUrl을 null로 내려서
오픈 알림이 "지금 바로 예약하세요! → " 로 링크 없이 나갔다. 링크를 찾는 사이
예약이 마감돼 정작 예약을 못 했다. 게다가 /items/ URL이 없으면 오픈 예정 시각
조회도 통째로 건너뛰어서 열리기 전에 미리 알릴 수도 없었다.

네트워크 없이 돈다 (네이버 API 호출은 대체 함수로 교체).

확인 내용:
  - bookingBusinessId만 있어도 달력 API로 예약 타입을 찾아 URL을 복원한다
  - 타입을 못 찾으면 빈 문자열 (조용히 잘못된 URL을 만들지 않는다)
  - /items/ URL은 상품 목록 API로 먼저 찾고, 실패하면 HTML로 되돌아간다
  - 재조회 간격(30분) 안에는 같은 팝업을 다시 조회하지 않는다
  - 오픈 알림은 절대 링크 없이 나가지 않는다 (최후 수단: 지도 장소 페이지)

사용법: python presale_bookingurl_test.py
"""

import json
import sys
from datetime import datetime, timedelta

import presale_monitor as pm

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


KST = pm.KST
BIZ = "1736126"
PID = "2033835877"
NAME = "리베르 X 무신사 뷰티 스페이스1 팝업"


class Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def stub_get(routes, calls=None):
    """URL 접두사 → Resp 매핑으로 SESSION.get을 대체한다."""
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
    ym = datetime.now(KST).strftime("%Y-%m")

    print("1) resolve_business_booking_url — bookingBusinessId만으로 예약 URL 복원")
    pm._BIZ_URL_CACHE.clear()
    calls: list = []
    # 타입 12는 404, 타입 5가 달력을 돌려주는 상황
    pm.SESSION.get = stub_get({
        f"https://m.booking.naver.com/booking/5/bizes/{BIZ}/calendars/{ym}":
            Resp(payload={"calendars": []}),
    }, calls)
    got = pm.resolve_business_booking_url(BIZ)
    check(got == f"https://m.booking.naver.com/booking/5/bizes/{BIZ}",
          f"달력 API가 200을 주는 타입으로 URL 복원 ({got})")
    check(any("/booking/12/" in u for u in calls),
          "빈도 순으로 타입 후보를 훑는다 (12 먼저)")

    before = len(calls)
    pm.resolve_business_booking_url(BIZ)
    check(len(calls) == before, "같은 주기에 같은 업체를 다시 조회하지 않는다 (캐시)")

    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({})          # 전부 404
    check(pm.resolve_business_booking_url(BIZ) == "",
          "타입을 못 찾으면 빈 문자열 (엉뚱한 URL 안 만듦)")
    pm._BIZ_URL_CACHE.clear()
    check(pm.resolve_business_booking_url("") == "", "businessId가 없으면 빈 문자열")

    print()
    print("2) resolve_booking_item_url — 상품 목록 API 우선, HTML 폴백")
    base = f"https://m.booking.naver.com/booking/5/bizes/{BIZ}"
    pm.SESSION.get = stub_get({
        f"{base}/items": Resp(payload={"bizItems": [{"bizItemId": "8055123"}]}),
    })
    check(pm.resolve_booking_item_url(base) == f"{base}/items/8055123",
          "상품 목록 API에서 상품 id를 읽는다")

    pm.SESSION.get = stub_get({
        f"{base}/items": Resp(payload={"businessId": BIZ, "id": BIZ,
                                       "bizItems": [{"id": BIZ, "bizItemId": "8055777"}]}),
    })
    check(pm.resolve_booking_item_url(base) == f"{base}/items/8055777",
          "업체 id를 상품 id로 착각하지 않는다 (bizItemId 우선)")

    pm.SESSION.get = stub_get({
        f"{base}/items": Resp(status_code=404),
        base: Resp(text='<a href="/booking/5/bizes/x/items/8055999">예약</a>'),
    })
    check(pm.resolve_booking_item_url(base) == f"{base}/items/8055999",
          "API가 실패하면 예약 페이지 HTML에서 찾는다")

    pm.SESSION.get = stub_get({})
    check(pm.resolve_booking_item_url(base) == base,
          "둘 다 실패하면 원래 URL 그대로 (알림은 계속 나간다)")
    already = f"{base}/items/1"
    check(pm.resolve_booking_item_url(already) == already, "이미 /items/면 조회 생략")

    print()
    print("3) url_resolve_due — 재조회 간격 제한")
    check(pm.url_resolve_due({}, "bookingItemCheckedAt") is True, "조회 이력이 없으면 조회 대상")
    recent = (datetime.now(KST) - timedelta(minutes=5)).isoformat()
    check(pm.url_resolve_due({"bookingItemCheckedAt": recent}, "bookingItemCheckedAt") is False,
          "5분 전 조회했으면 건너뜀")
    old = (datetime.now(KST) - timedelta(hours=2)).isoformat()
    check(pm.url_resolve_due({"bookingItemCheckedAt": old}, "bookingItemCheckedAt") is True,
          "30분이 지나면 다시 조회")
    check(pm.url_resolve_due({"bookingUrlCheckedAt": "깨진값"}, "bookingUrlCheckedAt") is True,
          "값이 깨졌으면 조회 대상으로 취급")

    print()
    print("4) place_map_url — 예약 URL을 끝내 못 찾았을 때 쓸 대체 링크")
    check(pm.place_map_url(PID) == f"https://map.naver.com/p/entry/place/{PID}",
          "지도 장소 페이지 URL")
    check(pm.place_map_url("") == "", "장소 id가 없으면 빈 문자열")

    print()
    print("5) check_once — 리베르 상황 재현 (bookingUrl=null, businessId만 있음)")
    real = {name: getattr(pm, name) for name in
            ("fetch_presale_places", "fetch_bookable_setting", "fetch_sale_start_date",
             "load_prev_alerts", "load_seen_ids", "has_available_slots",
             "_queue_ntfy", "send_ntfy", "send_toast", "save_data", "CONFIG_FILE")}

    item_url = f"https://m.booking.naver.com/booking/5/bizes/{BIZ}/items/8055123"
    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({
        f"https://m.booking.naver.com/booking/5/bizes/{BIZ}/calendars/{ym}":
            Resp(payload={"calendars": []}),
        f"https://m.booking.naver.com/booking/5/bizes/{BIZ}/items":
            Resp(payload={"bizItems": [{"bizItemId": "8055123"}]}),
    })
    pm.fetch_presale_places = lambda area, stats=None: [{
        "id": PID, "name": NAME, "hasBooking": True,
        "bookingUrl": None, "bookingBusinessId": BIZ,
        "popupstoreInfo": {"admissionCondition": {"name": "사전예약&현장대기"},
                           "remainingDays": 2},
        "commonAddress": "서울 성동구",
    }]
    pm.fetch_bookable_setting = lambda u, b: {"isPaused": False, "isUseOpen": False,
                                              "openDateTime": None, "isOpened": True}
    pm.fetch_sale_start_date = lambda u, b: None
    pm.load_prev_alerts = lambda: []
    pm.load_seen_ids = lambda: {PID}
    pm.has_available_slots = lambda u, b: True
    pm._queue_ntfy = lambda *a, **k: None
    sent: list = []
    pm.send_ntfy = lambda topic, title, body, url: sent.append({"title": title, "body": body, "url": url})
    pm.send_toast = lambda *a, **k: None
    saved: dict = {}
    pm.save_data = lambda places, cfg, alerts=None, seen_ids=None, discovery_stats=None: \
        saved.update({"places": places, "alerts": alerts})
    pm.CONFIG_FILE = type("P", (), {"write_text": staticmethod(lambda *a, **k: None),
                                    "name": "presale_config.json"})()

    cfg = {"areas": [{"query": "성수 팝업", "x": "1", "y": "2", "address_filter": "성동구"}],
           "watched_places": [PID], "ntfy_topic": "t",
           "selection_page_url": "https://example.test/select"}
    prev = {PID: {"id": PID, "name": NAME, "hasBooking": False, "bookingUrl": None,
                  "bookingBusinessId": None, "bookingNotified": False,
                  "bookingOpenHistory": [], "district": "성동구"}}
    result = pm.check_once(cfg, prev)

    place = result.get(PID, {})
    check(place.get("bookingUrl") == item_url,
          f"예약 URL이 복원되고 /items/까지 올라감 ({place.get('bookingUrl')})")
    check(len(sent) == 1, f"오픈 알림 1건 발송 (실제 {len(sent)}건)")
    if sent:
        check(sent[0]["url"] == item_url, f"알림 클릭 링크가 예약 URL ({sent[0]['url']})")
        check(item_url in sent[0]["body"], "알림 본문에도 링크가 들어간다")
    alert = next((a for a in (saved.get("alerts") or [])
                  if a.get("type") == "booking_open" and a.get("place_id") == PID), None)
    check(alert is not None and alert.get("booking_url") == item_url,
          "알림함 기록에도 링크가 남는다")

    print()
    print("6) check_once — 예약 URL을 끝내 못 찾아도 링크 없는 알림은 안 나간다")
    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({})          # 달력·상품 API 전부 실패
    sent.clear()
    saved.clear()
    prev2 = {PID: dict(prev[PID])}
    result2 = pm.check_once(cfg, prev2)
    check(not (result2.get(PID, {}).get("bookingUrl") or ""),
          "예약 URL은 비어 있는 상태")
    check(len(sent) == 1, f"그래도 오픈 알림은 발송 (실제 {len(sent)}건)")
    if sent:
        check(sent[0]["url"] == pm.place_map_url(PID),
              f"링크가 지도 장소 페이지로 대체됨 ({sent[0]['url']})")
        check(sent[0]["body"].rstrip().endswith(pm.place_map_url(PID)),
              "본문이 '→ ' 로 끝나지 않는다 (링크 없는 알림 금지)")

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
