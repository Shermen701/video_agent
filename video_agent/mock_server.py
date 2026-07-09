from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

STATE = {
    "tasks": [],
    "statuses": {},
    "uploads": {},
}


def _default_task() -> dict:
    now = datetime.now(timezone.utc).astimezone()
    return {
        "id": "mock-task-001",
        "meeting_provider": "tencent_meeting",
        "title": "Mock Tencent Meeting",
        "start_time": (now + timedelta(minutes=1)).isoformat(),
        "end_time": (now + timedelta(minutes=10)).isoformat(),
        "upload_target": "mock",
        "credentials": {"account": "13117414114", "password": "P@ssw0rd."},
        "meeting": {"meeting_no": "601-146-411", "password": "123456", "extra": {}},
    }


class MockHandler(BaseHTTPRequestHandler):
    server_version = "VideoAgentMock/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._json(
                200,
                {
                    "service": "video-agent mock server",
                    "status": "ok",
                    "endpoints": {
                        "health": "GET /health",
                        "next_task": "GET /api/recording/tasks/next?agent_id=recorder-001",
                        "status": "POST /api/recording/tasks/{task_id}/status",
                        "upload_init": "POST /api/uploads/init",
                        "upload_part": "PUT /api/uploads/{upload_id}/parts/{part_no}",
                        "upload_complete": "POST /api/uploads/{upload_id}/complete",
                    },
                },
            )
            return
        if parsed.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if parsed.path == "/api/recording/tasks/next":
            parse_qs(parsed.query)
            if not STATE["tasks"]:
                STATE["tasks"].append(_default_task())
            task = STATE["tasks"].pop(0)
            self._json(200, {"task": task})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path.startswith("/api/recording/tasks/") and parsed.path.endswith("/status"):
            task_id = parsed.path.split("/")[4]
            STATE["statuses"].setdefault(task_id, []).append(body)
            self._json(200, {"ok": True})
            return
        if parsed.path == "/api/uploads/init":
            upload_id = str(uuid4())
            upload_dir = Path("mock_uploads") / upload_id
            upload_dir.mkdir(parents=True, exist_ok=True)
            STATE["uploads"][upload_id] = {"init": body, "dir": str(upload_dir), "parts": []}
            self._json(200, {"upload_id": upload_id})
            return
        if parsed.path.startswith("/api/uploads/") and parsed.path.endswith("/complete"):
            upload_id = parsed.path.split("/")[3]
            upload = STATE["uploads"].get(upload_id)
            if upload is None:
                self._json(404, {"error": "upload not found"})
                return
            self._json(200, {"ok": True, "parts": upload["parts"]})
            return
        self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/uploads/") and "/parts/" in parsed.path:
            parts = parsed.path.split("/")
            upload_id = parts[3]
            part_no = int(parts[5])
            upload = STATE["uploads"].get(upload_id)
            if upload is None:
                self._json(404, {"error": "upload not found"})
                return
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            part_path = Path(upload["dir"]) / f"part-{part_no:05d}"
            part_path.write_bytes(data)
            upload["parts"].append(part_no)
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size == 0:
            return {}
        return dict(json.loads(self.rfile.read(size).decode("utf-8")))

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f"mock server listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
