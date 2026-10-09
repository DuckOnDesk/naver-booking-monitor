"""예약 링크 회귀 테스트 — 알림에 반드시 예약 링크가 걸리도록.

배경 (2026-09-17 ~ 09-28에 실제로 확인된 것):
지도 검색(popupstore/list)은 같은 팝업이라도 bookingUrl을 줄 때와 안 줄 때가 있다.
2026-09-17 09:20에 팝업 6개가 검색에서 잠깐 빠졌고, 돌아왔을 때 4개가 링크를 잃었다.
장소 기록이 통째로 지워졌다 다시 만들어지면서 "이전 URL 유지"가 끊긴 탓이다.
그 뒤 링크 보유율이 100% → 61%까지 내려가, 오픈 알림이 예약 페이지가 아니라 지도
장소 페이지로 연결됐다 (루나·THE AGE20'S 등).

실패했던 접근 두 가지 (같은 실수를 반복하지 않도록 남긴다):
  1) 달력 API(/calendars/{ym})로 예약 타입을 찍어 맞히기 — 그 엔드포인트는
     2026-09-08부터 JSON 대신 HTML을 반환한다. 실행 로그상 37번 시도해 37번 실패.
  2) 장소 상세 페이지 HTML 긁기 — 2026-09-28 확인 결과 상세 페이지 소스와 네트워크
     요청 어디에도 booking.naver.com 주소가 없다. 예약 버튼은 네이버 내부 페이지
     (pcmap.place.naver.com/popupstore/{id}/booking)로 이동한다. 원리상 불가능.

지금 방식: bookingBusinessId로 URL을 그냥 만든다. 네이버가 예약 서비스 타입을
서버에서 정규화해 주기 때문이다 — biz 1737133에 타입 5·6·12·13을 각각 넣으면 전부
.../booking/12/bizes/1737133 으로 리다이렉트된다 (2026-09-28 확인). 타입을 맞힐
필요가 없다.

네트워크 없이 돈다 (네이버 호출은 대체 함수로 교체).

확인 내용:
  - businessId로 만든 URL이 리다이렉트 최종 주소(= 올바른 타입)로 정리된다
  - 404(예약 없는 업체)면 링크를 걸지 않는다
  - 엉뚱한 곳으로 리다이렉트되면 쓰지 않는다
  - 요청이 실패해도 만든 URL을 쓴다 (리다이렉트가 동작하므로 예약 페이지에 닿는다)
  - 한 번 확인한 링크는 영구 보관되고 빈 값으로 덮이지 않는다
  - 오픈 알림은 예약 링크를 달고 나간다. 끝내 못 찾을 때만 지도 링크로 떨어진다
  - 예약 업체가 바뀌면(스킨앤랩 2026-10-08) 예전 업체의 /items/ 링크를 버린다
  - 저장한 상품이 지워지거나 닫히고 새 상품이 열리면 새 상품 링크로 바꾼다

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
BOOK_URL = f"https://m.booking.naver.com/booking/12/bizes/{BIZ}"   # 타입 12가 정규 주소


class Resp:
    def __init__(self, status_code=200, text="", url=""):
        self.status_code = status_code
        self.text = text
        self.url = url
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

    print("1) resolve_booking_url_from_biz — businessId로 예약 URL 만들기")
    pm._BIZ_URL_CACHE.clear()
    calls: list = []
    # 타입 12로 요청 → 네이버가 정규화한 최종 주소를 그대로 채택
    pm.SESSION.get = stub_get({
        BOOK_URL: Resp(text="예약", url=BOOK_URL),
    }, calls)
    check(pm.resolve_booking_url_from_biz(BIZ) == BOOK_URL, "최종 주소 채택")
    check(calls and calls[0] == BOOK_URL, f"타입 12로 요청 ({calls[0] if calls else None})")

    before = len(calls)
    pm.resolve_booking_url_from_biz(BIZ)
    check(len(calls) == before, "같은 주기에 같은 업체를 다시 확인하지 않는다 (캐시)")

    # 다른 타입으로 리다이렉트되면 그 타입을 따른다
    pm._BIZ_URL_CACHE.clear()
    real_type = f"https://m.booking.naver.com/booking/6/bizes/{BIZ}"
    pm.SESSION.get = stub_get({BOOK_URL: Resp(text="예약", url=real_type + "?x=1")})
    check(pm.resolve_booking_url_from_biz(BIZ) == real_type,
          f"리다이렉트된 타입을 따르고 쿼리스트링은 버린다 ({real_type})")

    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({BOOK_URL: Resp(status_code=404, url=BOOK_URL)})
    check(pm.resolve_booking_url_from_biz(BIZ) == "", "404(예약 없는 업체)면 링크를 걸지 않는다")

    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({BOOK_URL: Resp(text="로그인",
                                              url="https://nid.naver.com/login")})
    check(pm.resolve_booking_url_from_biz(BIZ) == "",
          "엉뚱한 곳으로 보내면 쓰지 않는다 (잘못된 링크 방지)")

    def _boom(url, **kw):
        raise RuntimeError("네트워크 끊김")
    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = _boom
    check(pm.resolve_booking_url_from_biz(BIZ) == BOOK_URL,
          "요청이 실패해도 만든 URL을 쓴다 (리다이렉트로 예약 페이지에 닿는다)")
    pm._BIZ_URL_CACHE.clear()
    check(pm.resolve_booking_url_from_biz("") == "", "businessId가 없으면 빈 문자열")

    print()
    print("2) fetch_biz_items / resolve_booking_item_url — 상품 목록 GraphQL")
    posts: list = []

    class PResp:
        def __init__(self, payload, status=200):
            self._p = payload
            self.status_code = status
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return self._p

    # 2026-09-28 루나 팝업 실제 응답 형태
    luna = {"data": {"bizItems": [
        {"bizItemId": "8056468", "name": "루나 클라우드 레이어 팝업스토어",
         "stock": 0, "isClosedBooking": False, "bookableSettingJson": {},
         "__typename": "BizItem"},
    ]}}
    real_post = pm.requests.post
    pm.requests.post = lambda url, **kw: (posts.append((url, kw.get("json"))), PResp(luna))[1]

    items = pm.fetch_biz_items(BIZ)
    check(len(items) == 1 and items[0]["bizItemId"] == "8056468",
          f"상품 목록 파싱 ({[i.get('bizItemId') for i in items]})")
    url, body = posts[0]
    check(url == "https://m.booking.naver.com/graphql?opName=bizItems",
          f"확인된 엔드포인트로 요청 ({url})")
    check(body["operationName"] == "bizItems"
          and body["variables"]["input"]["businessId"] == BIZ
          and body["variables"]["input"]["lang"] == "ko",
          f"확인된 요청 형태 (input={body['variables']['input']})")

    check(pm.resolve_booking_item_url(BOOK_URL) == BOOK_URL + "/items/8056468",
          "예약 URL이 /items/ 까지 올라간다")
    check(pm.resolve_booking_item_url(BOOK_URL + "/items/1") == BOOK_URL + "/items/1",
          "이미 /items/면 조회 생략")

    # 닫힌 상품은 건너뛰고 열린 상품을 고른다
    multi = {"data": {"bizItems": [
        {"bizItemId": "111", "name": "마감", "isClosedBooking": True},
        {"bizItemId": "222", "name": "예약 가능", "isClosedBooking": False},
    ]}}
    pm.requests.post = lambda url, **kw: PResp(multi)
    check(pm.resolve_booking_item_url(BOOK_URL) == BOOK_URL + "/items/222",
          "닫히지 않은 상품을 고른다")

    allclosed = {"data": {"bizItems": [
        {"bizItemId": "111", "name": "마감", "isClosedBooking": True},
    ]}}
    pm.requests.post = lambda url, **kw: PResp(allclosed)
    check(pm.resolve_booking_item_url(BOOK_URL) == BOOK_URL + "/items/111",
          "전부 닫혔으면 첫 상품이라도 쓴다")

    pm.requests.post = lambda url, **kw: PResp({"errors": [{"message": "bad"}]})
    check(pm.resolve_booking_item_url(BOOK_URL) == BOOK_URL,
          "GraphQL 오류면 원래 URL 유지 (알림은 계속 나간다)")

    def _boom_post(url, **kw):
        raise RuntimeError("네트워크 끊김")
    pm.requests.post = _boom_post
    check(pm.resolve_booking_item_url(BOOK_URL) == BOOK_URL, "요청 실패해도 원래 URL 유지")
    check(pm.fetch_biz_items("") == [], "businessId가 없으면 빈 목록")
    pm.requests.post = real_post

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
    other = "https://m.booking.naver.com/booking/12/bizes/9999999"
    pm.remember_booking_url(hist, PID, other)
    check(hist[PID] == other, "업체가 바뀐 링크면 덜 구체적이어도 갱신")

    print()
    print("5) check_once — 루나 상황 재현 (지도 검색이 링크를 안 줌)")
    real = {name: getattr(pm, name) for name in (
        "fetch_presale_places", "fetch_bookable_setting", "fetch_sale_start_date",
        "load_prev_alerts", "load_seen_ids", "has_available_slots",
        "load_place_memory", "load_auto_added_ids", "load_watch_missing",
        "load_booking_url_history", "resolve_booking_item_url", "fetch_biz_items",
        "_queue_ntfy", "send_ntfy", "send_toast", "save_data", "CONFIG_FILE")}

    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({BOOK_URL: Resp(text="예약", url=BOOK_URL)})
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
    pm.fetch_biz_items = lambda biz: []                 # 상품 유효성 확인은 8·9번에서
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
          f"businessId로 예약 URL을 채워 넣음 ({result[PID].get('bookingUrl')})")
    check(len(sent) == 1, f"오픈 알림 1건 (실제 {len(sent)}건)")
    if sent:
        check(sent[0]["url"] == BOOK_URL, f"알림 링크가 예약 페이지 ({sent[0]['url']})")
        check("map.naver.com" not in sent[0]["url"], "지도 링크로 떨어지지 않는다")
    check(saved_hist.get(PID) == BOOK_URL, "확인한 링크가 영구 보관됨")

    print()
    print("6) check_once — 예약 URL을 못 만들어도 보관본으로 복구")
    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({BOOK_URL: Resp(status_code=404, url=BOOK_URL)})
    sent.clear()
    prev2 = {PID: dict(prev[PID], hasBooking=False, bookingUrl=None)}
    result2 = pm.check_once(cfg, prev2)
    check(result2[PID].get("bookingUrl") == BOOK_URL,
          f"영구 보관본에서 링크 복구 ({result2[PID].get('bookingUrl')})")
    if sent:
        check(sent[0]["url"] == BOOK_URL, "알림도 예약 링크로 나간다")

    print()
    print("7) check_once — 링크를 끝내 못 찾으면 지도 링크로 (링크 없는 알림 금지)")
    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({BOOK_URL: Resp(status_code=404, url=BOOK_URL)})
    saved_hist.clear()
    sent.clear()
    prev3 = {PID: dict(prev[PID])}
    result3 = pm.check_once(cfg, prev3)
    check(not (result3[PID].get("bookingUrl") or ""), "예약 URL은 비어 있는 상태")
    check(len(sent) == 1 and sent[0]["url"] == pm.place_map_url(PID),
          f"지도 장소 페이지로 대체 ({sent[0]['url'] if sent else None})")
    check(sent and not sent[0]["body"].rstrip().endswith("→"),
          "본문이 '→' 로 끝나지 않는다")

    print()
    print("8) check_once — 예약 업체가 바뀌면 예전 /items/ 링크를 버린다 (스킨앤랩 사례)")
    # 2026-10-08: 지도 검색의 bookingBusinessId가 1739573 → 1750599로 바뀌었는데
    # 예전 /items/ 링크를 계속 유지해서 닫힌 옛 예약 페이지로 연결됐다
    OLD_URL = "https://m.booking.naver.com/booking/13/bizes/1739573/items/8067644"
    NEW_BIZ = "1750599"
    NEW_BASE = f"https://m.booking.naver.com/booking/12/bizes/{NEW_BIZ}"
    pm._BIZ_URL_CACHE.clear()
    pm.SESSION.get = stub_get({NEW_BASE: Resp(text="예약", url=NEW_BASE)})
    pm.resolve_booking_item_url = lambda u: u + "/items/8120001" if "/items/" not in u else u
    pm.fetch_presale_places = lambda area, stats=None: [{
        "id": PID, "name": NAME, "hasBooking": True,
        "bookingUrl": None, "bookingBusinessId": NEW_BIZ,
        "popupstoreInfo": {"admissionCondition": {"name": "사전예약"}, "remainingDays": 8},
        "commonAddress": "서울 성동구",
    }]
    saved_hist.clear(); saved_hist[PID] = OLD_URL
    sent.clear()
    prev4 = {PID: {"id": PID, "name": NAME, "hasBooking": True, "bookingUrl": OLD_URL,
                   "bookingBusinessId": NEW_BIZ, "bookingNotified": True,
                   "bookingOpenAuto": "2026-09-18T18:00:00+09:00", "bookingIsOpened": True,
                   "bookingOpenAutoCheckedAt": "2026-09-28T11:04:49+09:00",
                   "bookingItemCheckedAt": "2026-09-28T11:04:39+09:00",
                   "bookingOpenHistory": [], "district": "성동구"}}
    result4 = pm.check_once(cfg, prev4)
    got = result4[PID].get("bookingUrl")
    check(got == NEW_BASE + "/items/8120001", f"새 업체 링크로 교체 ({got})")
    check(saved_hist.get(PID) == got, f"영구 보관본도 교체 ({saved_hist.get(PID)})")
    check(len(sent) == 1, f"새 회차 오픈 알림 1건 (실제 {len(sent)}건)")

    # 다음 주기: 그대로 유지 (옛 링크로 되돌아가지 않음)
    sent.clear()
    result5 = pm.check_once(cfg, result4)
    check(result5[PID].get("bookingUrl") == got, "다음 주기에도 새 링크 유지")
    check(not sent, "알림 재발송 없음")

    print()
    print("9) verify_item_url / check_once — 상품이 바뀌면 새 상품 링크로 (맥캘란 사례)")
    ITEM_URL = BOOK_URL + "/items/8109086"
    pm.fetch_biz_items = lambda biz: [{"bizItemId": "8109086", "isClosedBooking": True},
                                      {"bizItemId": "8130000", "isClosedBooking": False}]
    check(pm.verify_item_url(ITEM_URL) == BOOK_URL + "/items/8130000", "닫힌 상품 → 열린 새 상품")
    pm.fetch_biz_items = lambda biz: [{"bizItemId": "8130000", "isClosedBooking": False}]
    check(pm.verify_item_url(ITEM_URL) == BOOK_URL + "/items/8130000", "목록에서 사라진 상품 → 새 상품")
    pm.fetch_biz_items = lambda biz: [{"bizItemId": "8109086", "isClosedBooking": False},
                                      {"bizItemId": "8130000", "isClosedBooking": False}]
    check(pm.verify_item_url(ITEM_URL) is None, "기존 상품이 열려 있으면 그대로")
    pm.fetch_biz_items = lambda biz: [{"bizItemId": "8109086", "isClosedBooking": True}]
    check(pm.verify_item_url(ITEM_URL) is None, "다른 상품이 없으면 그대로 (그냥 마감)")
    pm.fetch_biz_items = lambda biz: []
    check(pm.verify_item_url(ITEM_URL) is None, "목록 조회 실패면 판단 안 함")

    pm.fetch_biz_items = lambda biz: [{"bizItemId": "8130000", "isClosedBooking": False}]
    pm.fetch_presale_places = lambda area, stats=None: [{
        "id": PID, "name": NAME, "hasBooking": True, "bookingUrl": None, "bookingBusinessId": BIZ,
        "popupstoreInfo": {"admissionCondition": {"name": "사전예약"}, "remainingDays": 8},
        "commonAddress": "서울 성동구",
    }]
    saved_hist.clear(); saved_hist[PID] = ITEM_URL
    sent.clear()
    prev6 = {PID: {"id": PID, "name": NAME, "hasBooking": True, "bookingUrl": ITEM_URL,
                   "bookingBusinessId": BIZ, "bookingNotified": True,
                   "bookingItemVerifiedAt": (datetime.now(KST) - timedelta(hours=3)).isoformat(),
                   "bookingOpenHistory": [], "district": "성동구"}}
    result6 = pm.check_once(cfg, prev6)
    check(result6[PID].get("bookingUrl") == BOOK_URL + "/items/8130000",
          f"새 상품 링크로 교체 ({result6[PID].get('bookingUrl')})")
    check(saved_hist.get(PID) == BOOK_URL + "/items/8130000", "영구 보관본도 교체")

    calls = []
    pm.fetch_biz_items = lambda biz: calls.append(biz) or []
    pm.check_once(cfg, result6)
    check(not calls, "방금 확인했으면 2시간 동안 다시 조회 안 함")

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
