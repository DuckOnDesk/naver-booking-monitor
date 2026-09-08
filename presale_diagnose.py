"""사전예약 탐색이 0건일 때 네이버 응답이 어떻게 바뀌었는지 들여다보는 진단 스크립트.

로컬/샌드박스에서는 pcmap.place.naver.com 접근이 막혀 있어 원인 파악이 안 된다.
GitHub Actions 러너(외부망 가능)에서 돌려 로그로 실제 응답 구조를 확인한다.

    python -u presale_diagnose.py            # 설정의 앞 2개 지역
    python -u presale_diagnose.py "성수 팝업"  # 특정 질의만
"""

import json
import re
import sys
import time
from collections import Counter

import presale_monitor as pm

CURRENT_RE = re.compile(
    r'window\.__APOLLO_STATE__\s*=\s*(\{.+?\});\s*(?:</script>|window\.)', re.DOTALL
)


def balanced_json(text: str, start: int) -> str | None:
    """text[start]의 '{'부터 짝이 맞는 '}'까지 잘라낸다 (문자열/이스케이프 인식)."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def show(label: str, value) -> None:
    print(f"  {label}: {value}")


def diagnose(area: dict) -> None:
    print(f"\n===== {area['query']} =====")
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
        resp = pm.SESSION.get(pm.LIST_URL, params=params, timeout=20)
        resp.encoding = "utf-8"
    except Exception as e:
        print(f"  [요청 실패] {e}")
        return

    html = resp.text
    show("status", resp.status_code)
    show("content-type", resp.headers.get("content-type"))
    show("length", len(html))
    show("final url", resp.url)

    for needle in ("__APOLLO_STATE__", "admissionCondition", "사전예약",
                   "popupstore", "PopupStore", "hasBooking", "bookingUrl"):
        show(f"raw contains {needle!r}", html.count(needle))

    m = CURRENT_RE.search(html)
    show("현재 정규식 매치", bool(m))
    if m:
        show("현재 정규식이 잡은 길이", len(m.group(1)))

    idx = html.find("__APOLLO_STATE__")
    if idx == -1:
        print("  [진단] Apollo state 자체가 응답에 없음 — 페이지 구조가 통째로 바뀐 듯")
        print("  --- 응답 앞 1500자 ---")
        print(html[:1500])
        return

    brace = html.find("{", idx)
    blob = balanced_json(html, brace) if brace != -1 else None
    show("괄호 매칭으로 잡은 길이", len(blob) if blob else None)

    for label, raw in (("현재 정규식", m.group(1) if m else None),
                       ("괄호 매칭", blob)):
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            show(f"{label} JSON 파싱", f"실패: {e}")
            continue
        print(f"\n  --- {label} 파싱 결과 ---")
        show("최상위 키 수", len(data))
        prefixes = Counter(str(k).split(":")[0] for k in data)
        show("타입 prefix 상위 15", prefixes.most_common(15))

        dict_vals = [(k, v) for k, v in data.items() if isinstance(v, dict)]
        show("dict 엔트리 수", len(dict_vals))
        show("admissionCondition 보유 엔트리",
             sum(1 for _, v in dict_vals if "admissionCondition" in v))

        # 장소처럼 보이는 엔트리(이름+주소 계열 필드 보유)의 필드 이름을 노출
        placeish = [(k, v) for k, v in dict_vals
                    if any(f in v for f in ("commonAddress", "roadAddress",
                                            "bookingUrl", "hasBooking",
                                            "operationStartDateTime"))]
        show("장소성 엔트리 수", len(placeish))
        for k, v in placeish[:3]:
            print(f"    [{k}] fields={sorted(v.keys())}")
            print(f"      sample={json.dumps(v, ensure_ascii=False)[:800]}")

        # 어디에도 안 걸리면 ROOT_QUERY가 뭘 들고 있는지라도 본다
        if not placeish:
            for k, v in dict_vals[:5]:
                print(f"    [{k}] fields={sorted(v.keys())[:40]}")
            root = data.get("ROOT_QUERY")
            if isinstance(root, dict):
                print(f"    ROOT_QUERY keys={sorted(root.keys())[:40]}")
                print(f"    ROOT_QUERY sample={json.dumps(root, ensure_ascii=False)[:1500]}")


def main() -> None:
    cfg = pm.load_config()
    areas = cfg.get("areas", [])
    if len(sys.argv) > 1 and sys.argv[1].strip():
        want = sys.argv[1].strip()
        areas = [a for a in areas if a["query"] == want] or [
            {"query": want, "x": "127.057", "y": "37.544"}
        ]
    else:
        areas = areas[:2]
    for area in areas:
        diagnose(area)


if __name__ == "__main__":
    main()
