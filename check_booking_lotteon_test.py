"""롯데온 감시(type: "lotteon") 회귀 테스트.

네트워크 없이 돈다 (페이지 조회·ntfy는 대체 함수로 교체).

배경 — 롯데온에는 공개 예약 API가 없어 매 회차 chromium으로 페이지를 연다.
알림 판단이 전부 "이전 회차와 비교"라서, 상태 저장 규칙이 어긋나면 조용히
알림이 안 가거나(놓침) 매 회차 울린다(묻힘). 그 규칙만 좁게 검증한다.

확인 내용:
  - 처음 보는 항목은 기준 상태만 기록하고 알리지 않는다
    (전시샵에 이미 살 수 있는 상품이 수십 개면 등록하자마자 전부 알림이 된다)
  - 품절 → 구매 가능 전환에만 알림이 간다
  - 같은 상태가 이어지는 회차에는 알림이 없다 (alerted도 안 바뀐다 —
    바뀌면 회차마다 git 커밋·푸시가 돈다)
  - 구매 가능한 상품이 새로 올라오면 알린다
  - 목록에서 사라진 상품의 상태 키는 지운다 (다시 올라오면 신규로 잡히도록)
  - 페이지 조회 실패는 '변화 없음'이 아니다 — 상태 키를 손대지 않는다
    (0건으로 흘리면 다음 회차에 전부 '신규'로 잡혀 알림이 쏟아진다)
  - LOTTEON_INTERVAL_SEC 안에는 페이지를 다시 열지 않는다
  - alert_keywords 문구는 '새로 등장'할 때만 알린다
  - name_filter를 주면 그 상품만 본다

사용법: python check_booking_lotteon_test.py
"""

import sys

import check_booking as cb

fails: list = []


def check(cond, msg):
    print(f"    {'PASS' if cond else 'FAIL'} — {msg}", flush=True)
    if not cond:
        fails.append(msg)


URL = "https://www.lotteon.com/m/display/shop/seltDpShop/52978?ch_no=100279"

page: dict | None = None      # 대체 fetch_lotteon_page가 돌려줄 값
fetches: list = []            # 페이지를 실제로 연 횟수 기록
sent: list = []               # 나간 ntfy 알림


def fake_fetch(url):
    fetches.append(url)
    return page


def fake_ntfy(topic, title, body, url):
    sent.append((title, body, url))


def product(pid, name, text):
    return {"id": pid, "name": name, "text": text,
            "url": f"https://www.lotteon.com/p/product/{pid}"}


def make_page(*products, text=""):
    return {"products": list(products), "text": text, "final_url": URL}


def run(item, alerted, *, topic="t"):
    """한 회차를 돈다. 회차마다 스로틀을 풀어 '간격이 지난 상태'로 만든다."""
    cb._lotteon_checked_at.clear()
    sent.clear()
    cb.check_lotteon(item, item["id"], item["url"], topic, alerted, "12:00:00")


def main() -> int:
    cb.fetch_lotteon_page = fake_fetch
    cb.send_ntfy = fake_ntfy
    cb.LOG_DEDUP = False

    item = {"id": "lo1", "name": "롯데온 팝업", "url": URL, "type": "lotteon"}

    print("\n[1] 처음 보는 항목 — 기준 상태만 기록")
    global page
    alerted: dict = {}
    page = make_page(product("LM1", "굿즈 A", "굿즈 A 12,000원 구매하기"),
                     product("LM2", "굿즈 B", "굿즈 B 20,000원 품절"))
    run(item, alerted)
    check(not sent, "첫 회차에는 알림이 나가지 않는다")
    check(alerted.get("lo1:lo_seen") == 1, "기준 상태 기록 표시가 남는다")
    check(alerted.get("lo1:lo:LM1") == "1", "구매 가능 상품 상태 저장")
    check(alerted.get("lo1:lo:LM2") == "0:품절", "품절 상품은 사유까지 저장")

    print("\n[2] 같은 상태가 이어지는 회차")
    before = dict(alerted)
    run(item, alerted)
    check(not sent, "알림 없음")
    check(alerted == before, "alerted가 바뀌지 않는다 (불필요한 커밋 방지)")

    print("\n[3] 품절 → 구매 가능 전환")
    page = make_page(product("LM1", "굿즈 A", "굿즈 A 12,000원 구매하기"),
                     product("LM2", "굿즈 B", "굿즈 B 20,000원 구매하기"))
    run(item, alerted)
    check(len(sent) == 1, "알림 1건")
    check(sent and "굿즈 B" in sent[0][1], "전환된 상품만 알린다")
    check(sent and "굿즈 A" not in sent[0][1], "계속 살 수 있던 상품은 다시 알리지 않는다")
    check(sent and sent[0][2].endswith("LM2"), "1건이면 그 상품 링크로 보낸다")
    check(alerted.get("lo1:lo:LM2") == "1", "전환 후 상태 갱신")

    print("\n[4] 전환 다음 회차")
    run(item, alerted)
    check(not sent, "같은 상태를 다시 알리지 않는다")

    print("\n[5] 구매 가능한 상품이 새로 올라옴")
    page = make_page(product("LM1", "굿즈 A", "굿즈 A 12,000원 구매하기"),
                     product("LM2", "굿즈 B", "굿즈 B 20,000원 구매하기"),
                     product("LM3", "예약권", "예약권 0원 예약하기"))
    run(item, alerted)
    check(len(sent) == 1 and "예약권" in sent[0][1], "새 상품을 알린다")
    check(sent and "(신규)" in sent[0][1], "신규 등록임을 표시한다")

    print("\n[6] 상품이 목록에서 사라짐")
    page = make_page(product("LM1", "굿즈 A", "굿즈 A 12,000원 구매하기"))
    run(item, alerted)
    check("lo1:lo:LM3" not in alerted and "lo1:lo:LM2" not in alerted,
          "사라진 상품의 상태 키를 지운다")
    check(not sent, "사라진 것 자체는 알리지 않는다")
    page = make_page(product("LM1", "굿즈 A", "굿즈 A 12,000원 구매하기"),
                     product("LM3", "예약권", "예약권 0원 예약하기"))
    run(item, alerted)
    check(len(sent) == 1 and "예약권" in sent[0][1], "돌아온 상품은 신규로 다시 알린다")

    print("\n[7] 페이지 조회 실패")
    before = dict(alerted)
    page = None
    run(item, alerted)
    check(alerted == before, "상태 키를 손대지 않는다")
    check(not sent, "알림 없음")
    page = make_page(product("LM1", "굿즈 A", "굿즈 A 12,000원 구매하기"),
                     product("LM3", "예약권", "예약권 0원 예약하기"))
    run(item, alerted)
    check(not sent, "복구된 회차에 이전 상품이 '신규'로 다시 알려지지 않는다")

    print("\n[8] 확인 간격(LOTTEON_INTERVAL_SEC)")
    cb._lotteon_checked_at.clear()
    fetches.clear()
    sent.clear()
    cb.check_lotteon(item, item["id"], item["url"], "t", alerted, "12:00:00")
    cb.check_lotteon(item, item["id"], item["url"], "t", alerted, "12:01:00")
    check(len(fetches) == 1, "간격 안에는 페이지를 다시 열지 않는다")

    print("\n[9] alert_keywords — 문구 등장")
    kw_item = {"id": "lo2", "name": "예약 오픈 감시", "url": URL, "type": "lotteon",
               "lotteon": {"alert_keywords": ["예약하기"]}}
    alerted = {}
    page = make_page(text="롯데온 팝업 안내 오픈 예정")
    run(kw_item, alerted)
    check(not sent, "첫 회차는 기준만 기록")
    page = make_page(text="롯데온 팝업 안내 예약하기")
    run(kw_item, alerted)
    check(len(sent) == 1 and "예약하기" in sent[0][1], "문구가 새로 등장하면 알린다")
    run(kw_item, alerted)
    check(not sent, "이미 떠 있는 문구는 다시 알리지 않는다")
    page = make_page(text="롯데온 팝업 안내 오픈 예정")
    run(kw_item, alerted)
    check("lo2:lo_kw:예약하기" not in alerted, "문구가 사라지면 키를 지운다")
    page = make_page(text="롯데온 팝업 안내 예약하기")
    run(kw_item, alerted)
    check(len(sent) == 1, "다시 나타나면 또 알린다")

    print("\n[10] name_filter")
    f_item = {"id": "lo3", "name": "필터", "url": URL, "type": "lotteon",
              "lotteon": {"name_filter": ["예약"]}}
    alerted = {}
    page = make_page(product("LM1", "굿즈 A", "굿즈 A 12,000원 품절"),
                     product("LM3", "예약권", "예약권 0원 품절"))
    run(f_item, alerted)
    check("lo3:lo:LM1" not in alerted and "lo3:lo:LM3" in alerted,
          "이름이 걸린 상품만 상태로 남는다")
    page = make_page(product("LM1", "굿즈 A", "굿즈 A 12,000원 구매하기"),
                     product("LM3", "예약권", "예약권 0원 구매하기"))
    run(f_item, alerted)
    check(len(sent) == 1 and "예약권" in sent[0][1], "걸린 상품의 전환만 알린다")

    print("\n[11] 구매 불가 판정 문구")
    for text, expect in [("굿즈 A 구매하기", True), ("굿즈 A 품절", False),
                         ("굿즈 A SOLD OUT", False), ("굿즈 A 오픈 예정", False),
                         ("굿즈 A 판매종료", False), ("굿즈 A 응모하기", True)]:
        got, _ = cb.lotteon_product_status(text, cb.LOTTEON_SOLD_OUT_TEXTS)
        check(got is expect, f"{text!r} → {'구매 가능' if expect else '불가'}")
    got, _ = cb.lotteon_product_status("굿즈 A 재고없음", ("재고없음",))
    check(got is False, "sold_out_texts를 항목별로 덮어쓸 수 있다")

    print("\n[12] URL 파싱")
    check(cb.parse_lotteon_url(URL) == "52978", "전시샵 번호 추출")
    check(cb.parse_lotteon_url("https://m.booking.naver.com/booking/12/bizes/1/items/2") is None,
          "롯데온이 아닌 URL은 None")

    print(f"\n=== 실패 {len(fails)}건 ===", flush=True)
    for f in fails:
        print(f"  - {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
