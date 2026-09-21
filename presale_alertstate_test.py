"""알림 상태 저장 회귀 테스트 — 이미 보낸 알림이 다시 나가지 않도록.

2026-09-21에 확인된 문제:
지도 검색에서 팝업이 한 주기 빠졌다가 돌아오면 presale_data.json의 장소 기록이
통째로 사라졌다 새로 만들어졌다. 그 바람에
  (1) bookingNotified(알림 발송 기록)가 False로 초기화돼 이미 보낸 오픈 알림이
      다시 나갔고 (03:56 사라짐 → 04:01 복귀 → 04:07 재발송, 3개 팝업),
  (2) 돌아온 팝업이 "새 팝업"으로 보여 watched_places에 자동으로 다시 추가됐다
      (03:56에 6개 제거 → 04:01에 같은 6개 재추가). 사용자가 알림을 끈 팝업도
      이 경로로 다시 켜진다.

네트워크 없이 돈다 (네이버 API 호출은 대체 함수로 교체).

확인 내용:
  - 검색에서 사라졌다 돌아와도 알림 발송 기록이 유지된다 (재발송 없음)
  - 발견 시각·오픈 이력·예약 URL도 함께 복원된다
  - 한 번 자동 추가한 팝업은 다시 자동 추가하지 않는다 (사용자가 끈 것 존중)
  - watched_places 정리는 24시간 넘게 안 보일 때만 한다
  - place_memory는 오래된 항목을 정리한다

사용법: python presale_alertstate_test.py
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
PID = "2034884894"
NAME = "라리가웹툰 팝업스토어"
URL = "https://m.booking.naver.com/booking/12/bizes/1738783/items/8060001"


def raw_place(has_booking=True, pid=PID, name=NAME):
    return {"id": pid, "name": name, "hasBooking": has_booking,
            "bookingUrl": URL, "bookingBusinessId": "1738783",
            "popupstoreInfo": {"admissionCondition": {"name": "사전예약"}, "remainingDays": 3},
            "commonAddress": "서울 성동구"}


class Harness:
    """check_once를 네트워크·파일 없이 돌리기 위한 대체 구현 묶음."""

    def __init__(self):
        self.saved: dict = {}
        self.sent: list = []
        self.cfg_writes: list = []
        self.memory: dict = {}
        self.auto_added: set = set()
        self.watch_missing: dict = {}
        self._real = {name: getattr(pm, name) for name in (
            "fetch_presale_places", "fetch_bookable_setting", "fetch_sale_start_date",
            "load_prev_alerts", "load_seen_ids", "has_available_slots",
            "load_place_memory", "load_auto_added_ids", "load_watch_missing",
            "_queue_ntfy", "send_ntfy", "send_toast", "save_data", "CONFIG_FILE")}

    def install(self, places, seen_ids):
        self.sent.clear()       # 단계마다 이번 주기 발송만 센다
        pm.fetch_presale_places = lambda area, stats=None: list(places)
        pm.fetch_bookable_setting = lambda u, b: {"isPaused": False, "isUseOpen": False,
                                                  "openDateTime": None, "isOpened": True}
        pm.fetch_sale_start_date = lambda u, b: None
        pm.load_prev_alerts = lambda: []
        pm.load_seen_ids = lambda: set(seen_ids)
        pm.has_available_slots = lambda u, b: True
        pm.load_place_memory = lambda: dict(self.memory)
        pm.load_auto_added_ids = lambda: set(self.auto_added)
        pm.load_watch_missing = lambda: dict(self.watch_missing)
        pm._queue_ntfy = lambda *a, **k: None
        pm.send_ntfy = lambda topic, title, body, url: self.sent.append(title)
        pm.send_toast = lambda *a, **k: None

        def _save(places_, cfg, alerts=None, seen_ids=None, discovery_stats=None,
                  place_memory=None, auto_added_ids=None, watch_missing=None):
            self.saved = {"places": places_, "alerts": alerts}
            if place_memory is not None:
                self.memory = place_memory
            if auto_added_ids is not None:
                self.auto_added = set(auto_added_ids)
            if watch_missing is not None:
                self.watch_missing = dict(watch_missing)
        pm.save_data = _save

        harness = self
        pm.CONFIG_FILE = type("P", (), {
            "write_text": staticmethod(lambda *a, **k: harness.cfg_writes.append(1)),
            "name": "presale_config.json"})()

    def restore(self):
        for name, fn in self._real.items():
            setattr(pm, name, fn)


def main() -> int:
    h = Harness()
    base_cfg = {"areas": [{"query": "성수 팝업", "x": "1", "y": "2", "address_filter": "성동구"}],
                "ntfy_topic": "t", "selection_page_url": "https://example.test/select"}

    print("1) 검색에서 사라졌다 돌아와도 알림을 다시 보내지 않는다")
    h.install([raw_place()], seen_ids={PID})
    cfg = dict(base_cfg, watched_places=[PID])
    # 1주기: 예약 오픈 감지 → 알림 1건
    prev = {PID: {"id": PID, "name": NAME, "hasBooking": False, "bookingUrl": URL,
                  "bookingBusinessId": "1738783", "bookingNotified": False,
                  "bookingOpenHistory": [], "discoveredAt": "2026-09-17T08:00:00+09:00"}}
    cur = pm.check_once(cfg, prev)
    check(len(h.sent) == 1, f"오픈 알림 1건 발송 (실제 {len(h.sent)}건)")
    check(cur[PID]["bookingNotified"] is True, "발송 기록 저장됨")
    check(PID not in h.memory,
          "목록에 있는 동안은 기억에 중복 저장하지 않음 (데이터 파일 비대화 방지)")

    # 2주기: 검색에서 빠짐 → 장소 기록이 사라진다
    h.install([], seen_ids={PID})
    cur2 = pm.check_once(cfg, cur)
    check(PID not in cur2, "검색에 없으면 장소 목록에서 빠짐")
    check(PID in h.memory and h.memory[PID]["bookingNotified"] is True,
          "빠져도 영구 기억에는 발송 기록이 남는다")

    # 3주기: 다시 나타남 → prev에는 없지만 기억에서 복원되어야 한다
    h.install([raw_place()], seen_ids={PID})
    cur3 = pm.check_once(cfg, cur2)
    check(cur3.get(PID, {}).get("bookingNotified") is True,
          "재등장해도 발송 기록 유지 (재발송 안 함)")
    check(len(h.sent) == 0, f"알림 재발송 없음 (실제 {len(h.sent)}건)")
    check(cur3[PID].get("discoveredAt") == "2026-09-17T08:00:00+09:00",
          "발견 시각도 복원됨 (NEW 배지가 다시 붙지 않음)")
    check(cur3[PID].get("bookingUrl") == URL, "예약 URL도 복원됨")
    open_alerts = [a for a in (h.saved.get("alerts") or []) if a.get("type") == "booking_open"]
    check(not open_alerts, f"알림함에도 중복 기록 없음 (실제 {len(open_alerts)}건)")

    print()
    print("2) 사용자가 끈 팝업은 자동으로 다시 켜지지 않는다")
    # 사용자가 watched_places에서 뺀 상태에서, 팝업이 빠졌다 돌아온다
    h.auto_added = {PID}
    h.install([raw_place()], seen_ids={PID})
    cfg_off = dict(base_cfg, watched_places=[])
    h.cfg_writes.clear()
    cur4 = pm.check_once(cfg_off, {})
    check(str(PID) not in [str(x) for x in cfg_off.get("watched_places", [])],
          f"watched_places에 다시 추가되지 않음 (현재 {cfg_off.get('watched_places')})")
    check(len(h.sent) == 0, "감시 대상이 아니므로 알림도 없음")

    # 반대로 정말 처음 보는 팝업은 자동 추가된다
    h.auto_added = set()
    h.install([raw_place(pid="9999999999", name="새 팝업")], seen_ids=set())
    cfg_new = dict(base_cfg, watched_places=[])
    pm.check_once(cfg_new, {})
    check("9999999999" in [str(x) for x in cfg_new.get("watched_places", [])],
          f"처음 보는 팝업은 자동 추가 (현재 {cfg_new.get('watched_places')})")
    check("9999999999" in h.auto_added, "자동 추가 이력이 기록됨")

    print()
    print("3) watched_places 정리는 24시간 넘게 안 보일 때만")
    h.install([], seen_ids={PID})
    h.watch_missing = {}
    cfg_clean = dict(base_cfg, watched_places=[PID])
    pm.check_once(cfg_clean, {PID: {"id": PID, "name": NAME, "hasBooking": False}})
    check([str(x) for x in cfg_clean.get("watched_places", [])] == [PID],
          f"한 주기 안 보인다고 지우지 않음 (현재 {cfg_clean.get('watched_places')})")
    check(PID in h.watch_missing, "안 보이기 시작한 시각을 기록해 둔다")

    h.install([], seen_ids={PID})
    h.watch_missing = {PID: (datetime.now(KST) - timedelta(hours=30)).isoformat()}
    cfg_clean2 = dict(base_cfg, watched_places=[PID])
    pm.check_once(cfg_clean2, {PID: {"id": PID, "name": NAME, "hasBooking": False}})
    check([str(x) for x in cfg_clean2.get("watched_places", [])] == [],
          f"30시간째 안 보이면 정리 (현재 {cfg_clean2.get('watched_places')})")

    h.restore()

    print()
    print("4) update_place_memory — 오래된 기억 정리")
    mem = {"old": {"bookingNotified": True,
                   "savedAt": (datetime.now(KST) - timedelta(days=40)).isoformat()},
           "recent": {"bookingNotified": True,
                      "savedAt": (datetime.now(KST) - timedelta(days=2)).isoformat()}}
    now_iso = datetime.now(KST).isoformat()
    out = pm.update_place_memory(dict(mem), {}, {}, now_iso)
    check("old" not in out, "30일 넘은 기억은 정리")
    check("recent" in out, "최근 기억은 유지")
    gone = {PID: {"name": NAME, "bookingNotified": True, "hasBooking": True}}
    out2 = pm.update_place_memory({}, gone, {}, now_iso)
    check(out2[PID]["bookingNotified"] is True and out2[PID]["savedAt"] == now_iso,
          "검색에서 빠진 팝업은 기억에 담긴다")
    out3 = pm.update_place_memory({PID: {"bookingNotified": True, "savedAt": now_iso}},
                                  gone, {PID: {"name": NAME}}, now_iso)
    check(PID not in out3, "목록에 남아 있는 팝업은 중복 저장하지 않는다")

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
