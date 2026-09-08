"""사전예약 탐색 파싱 회귀 테스트 — 네트워크 없이 돈다.

2026-09-03 08시경 팝업 조회가 통째로 0건이 됐다. 코드 변경은 없었고,
지역 조회는 34/34 "성공"인데 후보만 0 — 네이버 Apollo 캐시에서 장소
엔트리를 최상위 값에서만 찾던 게 원인이다.

확인 내용:
  - 정규화 캐시(최상위 엔트리)에서 예전처럼 찾는다
  - 비정규화 캐시(ROOT_QUERY 아래 중첩 배열)에서도 찾는다
  - 장소가 아닌 엔트리(카테고리 등)는 후보에 넣지 않는다
  - 같은 장소가 여러 위치에 나와도 한 번만 센다
  - admissionCondition이 통째로 사라지면 문자열 매칭으로 버틴다
  - 후보 0건이면 48시간을 기다리지 않고 바로 경고한다

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
    print("1) 정규화 캐시 — 장소가 최상위 엔트리로 오는 (기존) 형태")
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

    print("\n7) 후보 0건이면 48시간을 기다리지 않고 바로 경고")
    queued = []
    real_queue = pm._queue_ntfy
    pm._queue_ntfy = lambda t, b, u=None, **kw: queued.append((t, b))
    try:
        broken = {"areas_total": 34, "areas_ok": 34, "areas_failed": 0,
                  "candidate_items": 0, "presale_items": 0, "after_district_filter": 0,
                  "tracked_places": 0, "new_places": 0, "admission_names": {},
                  "last_new_place_at": None, "stale_warned_at": None,
                  "structure_warned_at": None}
        st = dict(broken)
        pm.report_discovery(st, {}, "")
        check(len(queued) == 1, f"구조 변경 경고 발송 (실제 {len(queued)}건)")
        check(st.get("structure_warned_at"), "경고 발송 시각 기록")

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
