from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    data: Any


class HttpClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout_seconds: int = 20,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.default_headers = default_headers or {}

    def get(self, path: str, headers: dict[str, str] | None = None) -> HttpResponse:
        return self._request("GET", path, extra_headers=headers)

    def post(self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> HttpResponse:
        return self._request("POST", path, body, extra_headers=headers)

    def put_bytes(self, path: str, data: bytes, headers: dict[str, str] | None = None) -> HttpResponse:
        return self._request("PUT", path, raw_body=data, extra_headers=headers)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        headers = {"Accept": "application/json", **self.default_headers}
        if self.token:
            headers["Authorization"] = self.token
        if extra_headers:
            headers.update(extra_headers)
        payload = raw_body
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=payload, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                return HttpResponse(resp.status, _decode_response(resp.read()))
        except error.HTTPError as exc:
            return HttpResponse(exc.code, _decode_response(exc.read()))


def _decode_response(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
