"""속도 제한 감지·자동 백오프 회귀 테스트.

네트워크 없이 돈다 (requests는 가짜 응답 객체로 교체).

배경 — 2026-09-08 11:34~13:20 KST, 약 2시간 동안 전 항목이
"schedule API 요청 실패 — enhanced: HTTP 400 (필드 미지원 추정) /
 base: GraphQL errors (BookingAPITooManyRequests)"만 찍으며 감시가 통째로 비었다.
원인은 두 가지였다.
  - 네이버는 속도 제한을 HTTP 429가 아니라 200 본문의 errors[]에 담아 보내는데,
    _rate_limit_hits는 429/403일 때만 올라가 자동 백오프가 발동하지 않았다.
  - 첫 쿼리의 400을 무조건 "필드 미지원 추정"으로 단정해, 로그가 진짜 원인을 가렸다.

확인 내용:
  - 200 본문의 BookingAPITooManyRequests를 속도 제한으로 센다
  - 400 본문이 속도 제한이면 "필드 미지원"으로 단정하지 않고, 두 번째 쿼리도 접는다
  - 진짜 필드 미지원 400은 종전처럼 두 번째 쿼리로 재시도한다
  - hourlySchedule/business/캘린더 교차확인도 같은 판정을 쓴다
    (교차확인은 평소 꺼져 있어 요청 자체가 안 나간다 — CALENDAR_CROSSCHECK)
  - 백오프가 120 → 300으로 한 칸씩 올라가고, 정상 회차가 이어지면 되돌아온다

사용법: python check_booking_ratelimit_test.py
"""

import sys

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


class Resp:
    """requests.Response 대역. raise_for_status는 4xx/5xx에서 HTTPError를 던진다."""

    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if text or payload is None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = cb.requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


TOO_MANY = {"errors": [{"message": "BookingAPITooManyRequests"}]}
FIELD_400 = {"errors": [{"message": "Unknown field 'saleStartDate'"}]}


def with_responses(responses):
    """requests.post/get이 responses를 순서대로 돌려주게 만든다. 호출 횟수를 반환."""
    seq = list(responses)
    calls = []

    def fake(*a, **kw):
        calls.append(1)
        return seq.pop(0) if seq else Resp(200, {"data": {}})

    cb.requests.post = fake
    cb.requests.get = fake
    return calls


def main() -> int:
    real_post, real_get = cb.requests.post, cb.requests.get
    quiet = cb.print
    cb.print = lambda *a, **kw: None
    try:
        print("1) 문구 판정")
        check(cb.looks_rate_limited("BookingAPITooManyRequests"), "BookingAPITooManyRequests")
        check(cb.looks_rate_limited("Too Many Requests"), "Too Many Requests (대소문자 무관)")
        check(cb.looks_rate_limited("rate limit exceeded"), "rate limit")
        check(not cb.looks_rate_limited("Unknown field 'saleStartDate'"), "필드 오류는 아님")
        check(not cb.looks_rate_limited("item 429 not found"), "본문 속 숫자 429는 안 잡는다")
        check(not cb.looks_rate_limited(""), "빈 문자열은 아님")

        print("2) 응답 판정 — 상태 코드와 본문 둘 다 본다")
        cb._rate_limit_hits = 0
        check(cb.rate_limited_response(Resp(429, {})) is not None, "HTTP 429")
        check(cb.rate_limited_response(Resp(403, {})) is not None, "HTTP 403")
        check(cb.rate_limited_response(Resp(400, TOO_MANY)) is not None, "HTTP 400 + 제한 본문")
        check(cb.rate_limited_response(Resp(400, FIELD_400)) is None, "HTTP 400 + 필드 오류는 아님")
        check(cb._rate_limit_hits == 3, f"잡힌 건수만 센다 (실제: {cb._rate_limit_hits})")

        print("3) schedule API — 200 본문의 제한을 센다 (이번 사고의 핵심)")
        cb._rate_limit_hits = 0
        calls = with_responses([Resp(200, TOO_MANY)])
        out = cb.check_availability("1", "2", 12, [])
        check(out is None, "조회 실패로 처리")
        check(cb._rate_limit_hits == 1, f"속도 제한 1건 계상 (실제: {cb._rate_limit_hits})")
        check(len(calls) == 1, f"막혔으면 두 번째 쿼리는 접는다 (실제 호출 {len(calls)}회)")

        print("4) schedule API — 400 본문이 제한이면 '필드 미지원'으로 단정하지 않는다")
        cb._rate_limit_hits = 0
        calls = with_responses([Resp(400, TOO_MANY)])
        cb.check_availability("1", "2", 12, [])
        check(cb._rate_limit_hits == 1, f"속도 제한으로 계상 (실제: {cb._rate_limit_hits})")
        check(len(calls) == 1, f"두 번째 쿼리도 접는다 (실제 호출 {len(calls)}회)")

        print("5) schedule API — 진짜 필드 미지원 400은 종전대로 재시도한다")
        cb._rate_limit_hits = 0
        good = {"data": {"schedule": {"bizItemSchedule": {
            "daily": {"summary": [{"dateKey": "2026-09-20", "stock": 3, "bookingCount": 0,
                                   "hasBookableSlots": True, "isSaleDay": True}]}}}}}
        calls = with_responses([Resp(400, FIELD_400), Resp(200, good)])
        out = cb.check_availability("1", "2", 12, [])
        check(out is not None and len(out["days"]) == 1, f"base 쿼리로 복구 (실제: {out})")
        check(len(calls) == 2, f"두 번 시도 (실제 호출 {len(calls)}회)")
        check(cb._rate_limit_hits == 0, "속도 제한으로 세지 않는다")

        print("6) hourlySchedule / business / 캘린더 교차확인도 같은 판정을 쓴다")
        cb._rate_limit_hits = 0
        with_responses([Resp(200, TOO_MANY)])
        check(cb.fetch_slots("1", "2", 12, "2026-09-20")["queried"] is False, "슬롯 조회 실패")
        with_responses([Resp(400, TOO_MANY)])
        cb.fetch_slots("1", "2", 12, "2026-09-20")
        with_responses([Resp(200, TOO_MANY)])
        cb.fetch_item_restrictions("1")
        # 캘린더 교차확인은 기본으로 꺼져 있다(CALENDAR_CROSSCHECK). 되살렸을 때도
        # 같은 판정을 쓰는지 봐야 하므로 이 확인 동안만 켠다.
        was_on = cb.CALENDAR_CROSSCHECK
        cb.CALENDAR_CROSSCHECK = True
        try:
            with_responses([Resp(429, {})])
            check(cb.fetch_calendar_day_status(12, "1", "2026-09-20") is None, "교차확인 판단 불가")
        finally:
            cb.CALENDAR_CROSSCHECK = was_on
        check(cb._rate_limit_hits == 4, f"네 경로 모두 계상 (실제: {cb._rate_limit_hits})")

        # 꺼져 있는 평소에는 요청 자체가 안 나가므로 계상도 없다.
        before = cb._rate_limit_hits
        with_responses([Resp(429, {})])
        check(cb.fetch_calendar_day_status(12, "1", "2026-09-20") is None,
              "꺼져 있으면 그대로 판단 불가")
        check(cb._rate_limit_hits == before,
              f"꺼져 있으면 요청도 계상도 없다 (실제: {cb._rate_limit_hits - before}건 증가)")

        print("7) 백오프 계단 — 올라갈 때와 내려올 때")
        check(cb.backoff_up(60) == 120, "60 → 120")
        check(cb.backoff_up(120) == 300, "120 → 300")
        check(cb.backoff_up(300) is None, "300이 상한")
        check(cb.backoff_down(300, 60) == 120, "300 → 120")
        check(cb.backoff_down(120, 60) == 60, "120 → 기준 주기")
        check(cb.backoff_down(300, 200) == 200, "기준 주기가 계단보다 크면 바로 복귀")

        print("8) 회복 시나리오 — 제한이 풀리면 원래 주기로 돌아온다")
        interval, base, clean = 60, 60, 0
        for hits in (1, 1, 0, 0, 0, 0, 0, 0):
            if hits:
                clean = 0
                stepped = cb.backoff_up(interval)
                if stepped:
                    interval = stepped
            elif interval > base:
                clean += 1
                if clean >= cb.RATE_LIMIT_RECOVER_ROUNDS:
                    interval, clean = cb.backoff_down(interval, base), 0
        check(interval == 60, f"제한 2회 → 300초, 정상 6회 → 60초 복귀 (실제: {interval})")
    finally:
        cb.requests.post, cb.requests.get = real_post, real_get
        cb.print = quiet

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
