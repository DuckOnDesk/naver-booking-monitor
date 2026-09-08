"""사전예약 탐색 파싱 회귀 테스트 — 네트워크 없이 돈다.

2026-09-03 08시경 팝업 조회가 통째로 0건이 됐다. 코드 변경은 없었고,
지역 조회는 34/34 "성공"인데 후보만 0 — 네이버 Apollo 캐시에서 장소
엔트리를 최상위 값에서만 찾던 게 원인이다.

확인 내용:
  - 현재 네이버 구조(PlaceListBusinessesItem.popupstoreInfo)에서 찾는다
  - 예전 구조(최상위 admissionCondition)도 계속 읽는다
  - 비정규화 캐시(ROOT_QUERY 아래 중첩 배열)에서도 찾는다
  - 장소가 아닌 엔트리(카테고리 등)는 후보에 넣지 않는다
  - 같은 장소가 여러 위치에 나와도 한 번만 센다
  - admissionCondition이 통째로 사라지면 문자열 매칭으로 버틴다
  - 후보 0건이면 48시간을 기다리지 않고 바로 경고한다
  - 후보 0건인 지역은 "빈 결과"가 아니라 조회 실패로 넘긴다
  - 한 주기에 무더기로 빠지면 추적 장소를 지우지 않는다
  - 탐색이 깨진 주기에는 watched_places를 정리하지 않는다
  - 탐색 경고가 관리 페이지 알림함(alerts)에도 남는다

사용법: python presale_discovery_test.py
"""

import json
import sys

import presale_monitor as pm

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


AREA = {"query": "성수 팝업", "x": "127.057", "y": "37.544"}


def live_popup(pid, name, admission="사전예약", i18n="popupstore_label_pre_book"):
    """2026-09-08 실제 응답에서 뜬 구조 (PlaceListBusinessesItem + popupstoreInfo)."""
    return {
        "__typename": "PlaceListBusinessesItem", "id": pid, "name": name,
        "category": "팝업스토어", "hasBooking": True,
        "bookingUrl": f"https://m.booking.naver.com/booking/12/bizes/{pid}",
        "bookingBusinessId": pid, "commonAddress": "서울 성동구",
        "roadAddress": "아차산로11길 7", "imageUrl": "https://example/i.jpg",
        "popupstoreInfo": {
            "__typename": "PlaceListBusinessesItemPopupstoreInfo",
            "operationStartDateTime": "26.07.28.",
            "operationEndDateTime": "26.10.18.",
            "remainingDays": None,
            "status": {"__typename": "PopupstoreSearchBusinessItemStatus",
                       "name": "진행중", "value": "3",
                       "i18nKey": "popupstore_status_ongoing"},
            "admissionCondition": None if admission is None else {
                "__typename": "PopupstoreSearchBusinessItemAdmissionCondition",
                "name": admission, "i18nKey": i18n},
            "mediaList": [],
        },
    }


def popup(pid, name, admission="사전예약"):
    p = {"id": pid, "name": name, "commonAddress": "서울 성동구",
         "operationStartDateTime": "2026-09-01T00:00:00", "hasBooking": True,
         "bookingUrl": f"https://booking.naver.com/{pid}", "__typename": "PopupStore"}
    p["admissionCondition"] = None if admission is None else {"name": admission}
    return p


def as_response(state: dict) -> str:
    """Apollo state를 담은 응답 HTML을 흉내낸다."""
    return ("<html><script>window.__APOLLO_STATE__ = "
            + json.dumps(state, ensure_ascii=False)
            + ";</script></html>")


class FakeResp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200
        self.encoding = "utf-8"


def stub_get(state):
    def _get(url, params=None, timeout=None):
        return FakeResp(as_response(state))
    return _get


def fetch(state, stats=None):
    real = pm.SESSION.get
    pm.SESSION.get = stub_get(state)
    try:
        return pm.fetch_presale_places(AREA, stats)
    finally:
        pm.SESSION.get = real


def main() -> int:
    print("0) 현재 네이버 구조 — popupstoreInfo 안의 admissionCondition")
    live = {
        "ROOT_QUERY": {"__typename": "Query"},
        "PlaceListBusinessesItem:2020855313":
            live_popup("2020855313", "STORY A 성수", "사전예약",
                       "popupstore_label_pre_book"),
        "PlaceListBusinessesItem:2033013073":
            live_popup("2033013073", "하겐다즈 팝업", "사전예약&현장대기",
                       "popupstore_label_prebook_and_walkin"),
        "PlaceListBusinessesItem:2049944406":
            live_popup("2049944406", "온그리디언츠 라운지", "현장대기",
                       "popupstore_label_walkin"),
    }
    stats = {}
    got = fetch(live, stats)
    check(sorted(p["id"] for p in got) == ["2020855313", "2033013073"],
          f"사전예약·사전예약&현장대기만 뽑는다 (실제 {[p['id'] for p in got]})")
    check(stats["candidate_items"] == 3, f"후보 3건 (실제 {stats.get('candidate_items')})")
    check(not stats.get("fallback_matched"), "예비 매칭 없이 정식 경로로 인식")
    check(sorted(stats["admission_names"]) == ["사전예약", "사전예약&현장대기", "현장대기"],
          f"입장 조건 분포 집계 (실제 {stats['admission_names']})")

    norm = pm.normalize(live["PlaceListBusinessesItem:2020855313"])
    check(norm["operationStart"] == "26.07.28." and norm["operationEnd"] == "26.10.18.",
          f"운영 기간을 popupstoreInfo에서 읽는다 (실제 {norm['operationStart']}~{norm['operationEnd']})")
    check(norm["status"] == "진행중", f"상태를 popupstoreInfo에서 읽는다 (실제 {norm['status']})")
    check(norm["admissionCondition"] == "사전예약", "입장 조건 정규화")
    check(norm["district"] == "성동구", "구 추출")

    # 한글 표기가 바뀌어도 i18nKey로 버틴다
    stats = {}
    renamed = {"ROOT_QUERY": {}, "a": live_popup("9", "표기변경 팝업", "선예약",
                                                 "popupstore_label_pre_book")}
    got = fetch(renamed, stats)
    check([p["id"] for p in got] == ["9"], "표기가 '선예약'으로 바뀌어도 i18nKey로 인식")

    print("\n1) 예전 구조 — 장소가 최상위 엔트리로 오는 형태도 계속 읽는다")
    normalized = {
        "ROOT_QUERY": {"__typename": "Query",
                       "popupStores": [{"__ref": "PopupStore:1"}, {"__ref": "PopupStore:2"}]},
        "PopupStore:1": popup("1", "사전예약 팝업", "사전예약"),
        "PopupStore:2": popup("2", "현장대기 팝업", "현장대기"),
    }
    stats = {}
    got = fetch(normalized, stats)
    check([p["id"] for p in got] == ["1"], "사전예약 팝업만 뽑는다")
    check(stats["candidate_items"] == 2, f"후보 2건 (실제 {stats.get('candidate_items')})")
    check(stats["areas_ok"] == 1 and not stats.get("areas_failed"), "지역 조회 성공으로 집계")

    print("\n2) 비정규화 캐시 — 장소가 ROOT_QUERY 아래 배열로 중첩된 형태")
    nested = {
        "ROOT_QUERY": {
            "__typename": "Query",
            'popupStores({"display":100})': {
                "__typename": "PopupStoreResult",
                "total": 2,
                "items": [popup("10", "중첩 사전예약", "사전예약&현장대기"),
                          popup("11", "중첩 현장대기", "현장대기")],
            },
        },
    }
    stats = {}
    got = fetch(nested, stats)
    check([p["id"] for p in got] == ["10"], "중첩돼 있어도 사전예약 팝업을 찾는다")
    check(stats["candidate_items"] == 2, f"후보 2건 (실제 {stats.get('candidate_items')})")

    print("\n3) 장소가 아닌 엔트리는 후보에서 제외")
    noisy = dict(nested)
    noisy = {
        "ROOT_QUERY": {"__typename": "Query",
                       "items": [popup("20", "진짜 팝업", "사전예약"),
                                 {"id": "c1", "name": "카페", "__typename": "Category"},
                                 {"id": "r1", "name": "성동구", "__typename": "Region"}]},
    }
    stats = {}
    got = fetch(noisy, stats)
    check(stats["candidate_items"] == 1, f"카테고리·지역은 후보 아님 (실제 {stats.get('candidate_items')})")
    check([p["id"] for p in got] == ["20"], "팝업만 남는다")

    print("\n4) 같은 장소가 여러 위치에 있어도 한 번만")
    dup_place = popup("30", "중복 팝업", "사전예약")
    dup = {
        "PopupStore:30": dup_place,
        "ROOT_QUERY": {"__typename": "Query", "items": [dup_place],
                       "recommend": {"items": [dup_place]}},
    }
    stats = {}
    got = fetch(dup, stats)
    check(len(got) == 1 and stats["candidate_items"] == 1,
          f"중복 제거 (후보 {stats.get('candidate_items')}, 결과 {len(got)})")

    print("\n5) admissionCondition이 사라지면 문자열 매칭으로 버틴다")
    renamed_a = popup("40", "이름바뀐 팝업", "사전예약")
    renamed_a.pop("admissionCondition")
    renamed_a["entryCondition"] = {"name": "사전예약"}
    renamed_b = popup("41", "현장 팝업", "현장대기")
    renamed_b.pop("admissionCondition")
    renamed_b["entryCondition"] = {"name": "현장대기"}
    stats = {}
    got = fetch({"ROOT_QUERY": {"items": [renamed_a, renamed_b]}}, stats)
    check([p["id"] for p in got] == ["40"], "필드명이 바뀌어도 사전예약 팝업을 찾는다")
    check(stats.get("fallback_matched") == 1, "예비 매칭 건수를 기록")

    print("\n6) admissionCondition이 살아 있으면 예비 매칭을 쓰지 않는다")
    # 설명글에 '사전예약'이 들어 있지만 입장 조건은 현장대기인 팝업
    decoy = popup("50", "현장대기 팝업", "현장대기")
    decoy["description"] = "사전예약 없이 현장에서 바로 입장하세요"
    stats = {}
    got = fetch({"PopupStore:50": decoy}, stats)
    check(got == [], "입장 조건이 멀쩡하면 설명글은 보지 않는다")
    check(not stats.get("fallback_matched"), "예비 매칭 미사용")

    print("\n7) 후보 0건인 지역은 빈 결과가 아니라 조회 실패로 넘긴다")
    stats = {}
    got = fetch({"ROOT_QUERY": {"__typename": "Query", "items": []}}, stats)
    check(got is None, "빈 결과([])가 아니라 None — 기존 장소를 종료로 오판하지 않게")
    check(stats.get("areas_empty") == 1, "팝업 0건 지역으로 집계")
    check(not stats.get("areas_ok"), "성공으로 세지 않는다 — '34/34 성공'으로 가려지지 않게")

    print("\n8) 한 주기에 무더기로 빠지면 추적 장소를 지우지 않는다")
    prev = {str(i): pm.normalize(popup(str(i), f"팝업{i}", "사전예약"))
            for i in range(1, 11)}
    cfg = {"areas": [AREA], "watched_places": ["1", "2", "3"], "ntfy_topic": ""}

    def run_check(state, config, prev_places):
        real = pm.SESSION.get
        real_save = pm.save_data
        real_queue = pm._queue_ntfy
        saved = {}
        pm.SESSION.get = stub_get(state)
        pm.save_data = lambda places, *a, **kw: saved.update({"places": places})
        pm._queue_ntfy = lambda *a, **kw: None
        try:
            out = pm.check_once(config, prev_places)
        finally:
            pm.SESSION.get = real
            pm.save_data = real_save
            pm._queue_ntfy = real_queue
        return out

    # 구조가 바뀌어 팝업이 하나도 안 잡히는 응답
    broken_state = {"ROOT_QUERY": {"__typename": "Query", "items": []}}
    out = run_check(broken_state, dict(cfg), prev)
    check(len(out) == 10, f"조회가 깨져도 10개 유지 (실제 {len(out)})")

    # 팝업 1개만 검색에 남은 응답 — 9/10이 빠지므로 삭제 보류
    one_left = {"ROOT_QUERY": {"items": [popup("1", "팝업1", "사전예약")]}}
    out = run_check(one_left, dict(cfg), prev)
    check(len(out) == 10, f"9/10이 빠지면 삭제 보류 (실제 {len(out)})")

    # 1개만 끝난 정상적인 경우 — 지운다
    nine_left = {"ROOT_QUERY": {"items": [popup(str(i), f"팝업{i}", "사전예약")
                                          for i in range(1, 10)]}}
    out = run_check(nine_left, dict(cfg), prev)
    check(len(out) == 9 and "10" not in out,
          f"1개만 빠지면 정상 제거 (실제 {len(out)}개)")

    print("\n9) 탐색이 깨진 주기에는 watched_places를 건드리지 않는다")
    cfg_w = dict(cfg, watched_places=["1", "2", "3"])
    run_check(broken_state, cfg_w, prev)
    check(cfg_w["watched_places"] == ["1", "2", "3"],
          f"감시 목록 유지 (실제 {cfg_w['watched_places']})")

    print("\n10) 후보 0건이면 48시간을 기다리지 않고 바로 경고")
    queued = []
    real_queue = pm._queue_ntfy
    pm._queue_ntfy = lambda t, b, u=None, **kw: queued.append((t, b))
    try:
        broken = {"areas_total": 34, "areas_ok": 0, "areas_failed": 0, "areas_empty": 34,
                  "candidate_items": 0, "presale_items": 0, "after_district_filter": 0,
                  "tracked_places": 0, "new_places": 0, "admission_names": {},
                  "last_new_place_at": None, "stale_warned_at": None,
                  "structure_warned_at": None}
        st = dict(broken)
        panel = []
        pm.report_discovery(st, {}, "", panel)
        check(len(queued) == 1, f"구조 변경 경고 발송 (실제 {len(queued)}건)")
        check(st.get("structure_warned_at"), "경고 발송 시각 기록")
        check([a["type"] for a in panel] == ["discovery_broken"],
              f"알림함에도 남는다 (실제 {[a.get('type') for a in panel]})")

        pm.report_discovery(dict(st), {}, "")
        check(len(queued) == 1, "24시간 안에는 같은 경고 재발송 안 함")

        healthy = dict(broken, candidate_items=831, presale_items=163,
                       tracked_places=46, last_new_place_at=None)
        pm.report_discovery(dict(healthy), {}, "")
        check(len(queued) == 1, "후보가 잡히면 구조 경고 없음")
    finally:
        pm._queue_ntfy = real_queue

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
