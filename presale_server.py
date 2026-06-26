"""
사전예약 모니터 웹 서버
presale_monitor.py 가 생성하는 presale_data.json 을 페이지로 서빙하고
알림 ON/OFF 토글 요청을 처리한다.

실행: python presale_server.py
접속: http://localhost:8765
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = 8765
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "presale_data.json"
CONFIG_FILE = BASE_DIR / "presale_config.json"
HTML_FILE = BASE_DIR / "presale.html"
SELECT_HTML_FILE = BASE_DIR / "presale_select.html"


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in ("/", "/presale.html"):
            if HTML_FILE.exists():
                self._send(200, "text/html; charset=utf-8", HTML_FILE.read_bytes())
            else:
                self._send(404, "text/plain", b"presale.html not found")

        elif path == "/presale_select.html":
            if SELECT_HTML_FILE.exists():
                self._send(200, "text/html; charset=utf-8", SELECT_HTML_FILE.read_bytes())
            else:
                self._send(404, "text/plain", b"presale_select.html not found")

        elif path == "/api/data":
            if DATA_FILE.exists():
                data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            else:
                data = {"places": [], "disabled_places": [], "updated_at": None}
            self._json(data)

        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/toggle":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            place_id = str(body.get("id", ""))
            watching = bool(body.get("watching", True))

            if not CONFIG_FILE.exists():
                self._json({"error": "config not found"}, 500)
                return

            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            watched = set(str(x) for x in config.get("watched_places", []))

            if watching:
                watched.add(place_id)
            else:
                watched.discard(place_id)

            config["watched_places"] = sorted(watched)
            CONFIG_FILE.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # presale_data.json 의 watched_places 도 즉시 반영
            if DATA_FILE.exists():
                data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
                data["watched_places"] = config["watched_places"]
                DATA_FILE.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            self._json({"ok": True, "watched_places": list(watched)})

        elif path == "/api/set-open-time":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            place_id = str(body.get("id", ""))
            dt = body.get("datetime")  # ISO 문자열 또는 null

            if not CONFIG_FILE.exists():
                self._json({"error": "config not found"}, 500)
                return

            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            bod = config.setdefault("booking_open_datetimes", {})
            if dt:
                bod[place_id] = dt
            else:
                bod.pop(place_id, None)

            CONFIG_FILE.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            if DATA_FILE.exists():
                data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
                for p in data.get("places", []):
                    if str(p.get("id")) == place_id:
                        p["bookingOpenDatetime"] = dt
                DATA_FILE.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            self._json({"ok": True})

        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass  # 콘솔 로그 억제


if __name__ == "__main__":
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"=== 사전예약 모니터 서버 시작 ===")
    print(f"브라우저에서 열기: http://localhost:{PORT}")
    print("Ctrl+C 로 종료")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버 종료")
