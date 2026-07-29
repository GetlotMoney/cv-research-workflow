from __future__ import annotations

import argparse
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .api import CoreApi
from .service import CoreService


MAX_REQUEST_BYTES = 1024 * 1024


class CoreRequestHandler(BaseHTTPRequestHandler):
    server_version = "CVWF-Core/1.0"

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/":
            self._serve_page()
            return
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _serve_page(self) -> None:
        content = self.server.index_path.read_bytes()  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _dispatch(self, method: str) -> None:
        try:
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                raise PermissionError("只允许本机访问")
            token = self.headers.get("X-CVWF-Token", "")
            if not secrets.compare_digest(
                token,
                self.server.access_token,  # type: ignore[attr-defined]
            ):
                raise PermissionError("会话令牌无效")
            payload = self._payload() if method == "POST" else {}
            path = self.path.split("?", 1)[0]
            result = self.server.api.dispatch(  # type: ignore[attr-defined]
                method,
                path,
                payload,
            )
        except KeyError as error:
            self._json(404, {"status": "error", "message": str(error)})
        except PermissionError as error:
            self._json(403, {"status": "error", "message": str(error)})
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._json(400, {"status": "error", "message": str(error)})
        else:
            self._json(200, {"status": "ok", "result": result})

    def _payload(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("请求长度无效") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("请求正文大小无效")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求 JSON 顶层必须是对象")
        return payload

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        content = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            .encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def serve(
    *,
    config_path: Path,
    index_path: Path,
    token_file: Path,
    host: str,
    port: int,
) -> None:
    if host != "127.0.0.1":
        raise ValueError("统一入口只允许监听 127.0.0.1")
    service = CoreService.from_file(config_path)
    token_path = Path(token_file).absolute()
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
    else:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        token_path.write_text(token, encoding="utf-8")
    if len(token) < 32:
        raise ValueError("统一入口令牌太短")
    server = ThreadingHTTPServer((host, port), CoreRequestHandler)
    server.api = CoreApi(service)  # type: ignore[attr-defined]
    server.access_token = token  # type: ignore[attr-defined]
    server.index_path = Path(index_path).resolve(strict=True)  # type: ignore[attr-defined]
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="启动通用 CV 科研工作流")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18082)
    arguments = parser.parse_args()
    serve(
        config_path=arguments.config,
        index_path=arguments.index,
        token_file=arguments.token_file,
        host=arguments.host,
        port=arguments.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
