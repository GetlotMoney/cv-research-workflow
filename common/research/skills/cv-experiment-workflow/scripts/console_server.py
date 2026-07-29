from __future__ import annotations

import argparse
import hmac
import json
import math
import queue
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

from workflow_core.intake import INTENTS
from workflow_core.project import is_link_or_reparse
from workflow_core.ui_service import (
    PROJECT_ACTIONS,
    console_project_action,
    console_state,
    console_write_action,
)


_MAX_BODY_BYTES = 64 * 1024
_MAX_CONTENT_LENGTH_DIGITS = len(str(_MAX_BODY_BYTES))
_MAX_JSON_INTEGER_DIGITS = 128
_MAX_JSON_DEPTH = 64
_MAX_CONCURRENT_REQUESTS = 8
_BUSY_DRAIN_BYTES = 8 * 1024
_REJECTION_WORKER_COUNT = 2
_REJECTION_QUEUE_SIZE = 128
_REJECTION_DEADLINE_SECONDS = 0.25
_ASSET_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
_WRITE_ACTIONS = {
    "create_project",
    "open_project",
    "save_research_brief",
    "create_domain_repo",
    "register_source",
    "save_idea",
    "revise_idea",
    "select_codebase",
    "create_task",
    "run_task_debug",
    "run_task_evidence",
    "seal_run_outputs",
    "confirm_run_evidence",
    "freeze_research_package",
}
_CSP = (
    "default-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'none'"
)


class _StrictJSONError(ValueError):
    pass


class _ConsoleHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    max_concurrent_requests = _MAX_CONCURRENT_REQUESTS

    def __init__(
        self,
        server_address: tuple[str, int],
        project: Path,
        assets: dict[str, tuple[bytes, str]],
        request_timeout: float,
        write_token: str,
        paperflow_entrypoint: str | None,
        instances_root: Path,
    ) -> None:
        self.project = project
        self.instances_root = instances_root
        self.assets = assets
        self.request_timeout = request_timeout
        self.write_token = write_token
        self.paperflow_entrypoint = paperflow_entrypoint
        self.project_lock = threading.RLock()
        self._request_gate = threading.BoundedSemaphore(
            self.max_concurrent_requests
        )
        self._rejection_queue: queue.Queue[socket.socket | None] = queue.Queue(
            maxsize=_REJECTION_QUEUE_SIZE
        )
        self._rejection_workers: list[threading.Thread] = []
        self._rejection_lock = threading.Lock()
        self._rejection_stopped = False
        super().__init__(server_address, _ConsoleRequestHandler)
        self._start_rejection_workers()

    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        if not self._request_gate.acquire(blocking=False):
            self._queue_rejection(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_gate.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_gate.release()

    def handle_error(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        del request, client_address

    def server_close(self) -> None:
        self._stop_rejection_workers()
        super().server_close()

    def _start_rejection_workers(self) -> None:
        for index in range(_REJECTION_WORKER_COUNT):
            worker = threading.Thread(
                target=self._rejection_worker,
                name=f"cv-console-reject-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._rejection_workers.append(worker)

    def _queue_rejection(self, request: socket.socket) -> None:
        with self._rejection_lock:
            if self._rejection_stopped:
                self.shutdown_request(request)
                return
            try:
                self._rejection_queue.put_nowait(request)
            except queue.Full:
                self.shutdown_request(request)

    def _rejection_worker(self) -> None:
        while True:
            request = self._rejection_queue.get()
            try:
                if request is None:
                    return
                self._reject_busy(request)
            finally:
                self._rejection_queue.task_done()

    def _stop_rejection_workers(self) -> None:
        with self._rejection_lock:
            if self._rejection_stopped:
                return
            self._rejection_stopped = True
        while True:
            try:
                request = self._rejection_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if request is not None:
                    self.shutdown_request(request)
            finally:
                self._rejection_queue.task_done()
        for _ in self._rejection_workers:
            self._rejection_queue.put_nowait(None)
        for worker in self._rejection_workers:
            worker.join(timeout=_REJECTION_DEADLINE_SECONDS + 0.5)

    def _reject_busy(self, request: socket.socket) -> None:
        try:
            deadline = time.monotonic() + min(
                _REJECTION_DEADLINE_SECONDS,
                self.request_timeout,
            )
            received = bytearray()
            while (
                b"\r\n\r\n" not in received
                and len(received) < _BUSY_DRAIN_BYTES
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                request.settimeout(remaining)
                chunk = request.recv(
                    min(1024, _BUSY_DRAIN_BYTES - len(received))
                )
                if not chunk:
                    break
                received.extend(chunk)
            if b"\r\n\r\n" not in received:
                return
            header_end = received.index(b"\r\n\r\n") + 4
            body_remaining = max(
                0,
                self._busy_request_body_length(received[:header_end])
                - (len(received) - header_end),
            )
            while body_remaining:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                request.settimeout(remaining)
                chunk = request.recv(min(8192, body_remaining))
                if not chunk:
                    return
                body_remaining -= len(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            request.settimeout(remaining)
            request.sendall(
                _raw_json_error_response(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "server_busy",
                    "本地控制台正在处理其他请求，请稍后重试。",
                    extra_headers={"Retry-After": "1"},
                )
            )
        except OSError:
            pass
        finally:
            self.shutdown_request(request)

    @staticmethod
    def _busy_request_body_length(header_block: bytes) -> int:
        content_lengths: list[bytes] = []
        for line in header_block.split(b"\r\n")[1:]:
            name, separator, value = line.partition(b":")
            if not separator:
                continue
            normalized_name = name.strip().lower()
            if normalized_name == b"transfer-encoding":
                return 0
            if normalized_name == b"content-length":
                content_lengths.append(value.strip())
        if len(content_lengths) != 1:
            return 0
        value = content_lengths[0]
        if (
            not value
            or len(value) > _MAX_CONTENT_LENGTH_DIGITS
            or not value.isdigit()
        ):
            return 0
        length = int(value)
        return length if length <= _MAX_BODY_BYTES else 0


class _ConsoleRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CVConsole"
    sys_version = ""

    @property
    def console_server(self) -> _ConsoleHTTPServer:
        return self.server  # type: ignore[return-value]

    def setup(self) -> None:
        super().setup()
        self._input_phase_lock = threading.Lock()
        self._input_phase_complete = False
        self._input_phase_expired = False
        self.connection.settimeout(self.console_server.request_timeout)
        self._input_timer = threading.Timer(
            self.console_server.request_timeout,
            self._expire_input_phase,
        )
        self._input_timer.daemon = True
        self._input_timer.start()

    def finish(self) -> None:
        self._finish_input_phase()
        super().finish()

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if not parsed:
            self._finish_input_phase()
            return False
        if self.command != "POST" and self._finish_input_phase():
            self._send_error(
                HTTPStatus.REQUEST_TIMEOUT,
                "request_timeout",
                "读取请求头超时。",
            )
            return False
        return True

    def version_string(self) -> str:
        return self.server_version

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        if self._input_deadline_expired():
            self._send_error(
                HTTPStatus.REQUEST_TIMEOUT,
                "request_timeout",
                "读取请求头超时。",
            )
            return
        self._send_error(
            code,
            "bad_request",
            "请求格式无效。",
        )

    def handle_expect_100(self) -> bool:
        self._send_error(
            HTTPStatus.EXPECTATION_FAILED,
            "expectation_not_supported",
            "不支持 Expect 请求头。",
        )
        return False

    def do_GET(self) -> None:
        if not self._validate_request(require_origin=False):
            return
        if self._has_query():
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "query_not_allowed",
                "此服务不接受查询参数。",
            )
            return
        if self.path == "/api/state":
            try:
                payload = _server_state(self.console_server)
                body = _json_bytes(payload)
            except Exception:
                self._send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "暂时无法读取项目状态。",
                )
                return
            self._send_bytes(
                HTTPStatus.OK,
                body,
                "application/json; charset=utf-8",
                extra_headers={"X-Console-Write-Token": self.console_server.write_token},
            )
            return
        asset = self.console_server.assets.get(self.path)
        if asset is None:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "请求的页面不存在。",
            )
            return
        body, content_type = asset
        self._send_bytes(HTTPStatus.OK, body, content_type)

    def do_POST(self) -> None:
        if not self._validate_request(require_origin=True):
            return
        if self._has_query():
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "query_not_allowed",
                "此服务不接受查询参数。",
            )
            return
        if self.path not in {"/api/state", "/api/actions"}:
            self._send_error(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "请求的接口不存在。",
            )
            return
        payload = self._read_json_body()
        if payload is None:
            return
        if self.path == "/api/state" and (
            not isinstance(payload, dict)
            or set(payload) != {"intent", "provided"}
            or payload.get("intent") not in INTENTS
            or not isinstance(payload.get("provided"), dict)
        ):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "请求只能包含有效的 intent 和 provided 对象。",
            )
            return
        if self.path == "/api/actions":
            if not self._valid_write_token():
                self._send_error(
                    HTTPStatus.FORBIDDEN,
                    "invalid_write_token",
                    "写入操作必须使用本次启动生成的令牌。",
                )
                return
            if (
                not isinstance(payload, dict)
                or set(payload) != {"action", "data"}
                or payload.get("action") not in _WRITE_ACTIONS
                or not isinstance(payload.get("data"), dict)
            ):
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_action",
                    "写入请求只能使用固定 action 和 data 对象。",
                )
                return
            try:
                with self.console_server.project_lock:
                    if payload["action"] in PROJECT_ACTIONS:
                        if (
                            console_state(
                                self.console_server.project
                            )["snapshot"]["status"]
                            != "valid"
                        ):
                            raise ValueError(
                                "只有有效科研项目才能新建或切换同级项目"
                            )
                        project, state = console_project_action(
                            self.console_server.instances_root,
                            payload["action"],
                            payload["data"],
                        )
                        self.console_server.project = project
                    else:
                        state = console_write_action(
                            self.console_server.project,
                            payload["action"],
                            payload["data"],
                            paperflow_entrypoint=(
                                self.console_server.paperflow_entrypoint
                            ),
                        )
                body = _json_bytes(state)
            except FileExistsError:
                self._send_error(
                    HTTPStatus.CONFLICT,
                    "action_conflict",
                    "目标已存在或当前记录与已有记录冲突；不会覆盖。",
                )
                return
            except (RuntimeError, NotImplementedError):
                self._send_error(
                    HTTPStatus.CONFLICT,
                    "action_unavailable",
                    "当前动作的受控生产能力不可用；没有执行替代命令。",
                )
                return
            except ValueError:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_action_data",
                    "写入动作缺少必填信息或不符合固定格式。",
                )
                return
            except (KeyError, TypeError):
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_action_data",
                    "写入动作的数据结构不完整。",
                )
                return
            except OSError:
                self._send_error(
                    HTTPStatus.CONFLICT,
                    "action_io_error",
                    "本机文件状态不允许完成这个动作；没有执行替代操作。",
                )
                return
            except Exception:
                self._send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "action_internal_error",
                    "固定动作未完成；服务端没有返回本机路径或错误堆栈。",
                )
                return
            self._send_bytes(HTTPStatus.OK, body, "application/json; charset=utf-8")
            return
        try:
            state = _server_state(
                self.console_server,
                payload["intent"],
                payload["provided"],
            )
            body = _json_bytes(state)
        except ValueError:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "提供的启动信息不符合工作流要求。",
            )
            return
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "暂时无法读取项目状态。",
            )
            return
        self._send_bytes(
            HTTPStatus.OK,
            body,
            "application/json; charset=utf-8",
            extra_headers={"X-Console-Write-Token": self.console_server.write_token},
        )

    def _valid_write_token(self) -> bool:
        tokens = self.headers.get_all("X-Console-Write-Token", [])
        return len(tokens) == 1 and hmac.compare_digest(
            tokens[0], self.console_server.write_token
        )

    def do_HEAD(self) -> None:
        self._method_not_allowed(write_body=False)

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def do_TRACE(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self, *, write_body: bool = True) -> None:
        self._send_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "此服务只允许 GET 和固定格式的本机 POST。",
            extra_headers={"Allow": "GET, POST"},
            write_body=write_body,
        )

    def _validate_request(self, *, require_origin: bool) -> bool:
        transfer_encoding = self.headers.get_all("Transfer-Encoding", [])
        if transfer_encoding:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "transfer_encoding_not_allowed",
                "不支持 Transfer-Encoding。",
            )
            return False

        content_lengths = self.headers.get_all("Content-Length", [])
        if len(content_lengths) > 1:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "ambiguous_content_length",
                "Content-Length 只能出现一次。",
            )
            return False

        expected_host = (
            f"127.0.0.1:{self.console_server.server_address[1]}"
        )
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] != expected_host:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_host",
                "Host 必须是当前本机控制台地址。",
            )
            return False

        origins = self.headers.get_all("Origin", [])
        expected_origin = f"http://{expected_host}"
        if require_origin and not origins:
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "origin_required",
                "POST 必须来自当前本机控制台页面。",
            )
            return False
        if len(origins) > 1 or (origins and origins[0] != expected_origin):
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "invalid_origin",
                "Origin 必须与当前本机控制台同源。",
            )
            return False
        return True

    def _has_query(self) -> bool:
        return "?" in self.path or "#" in self.path

    def _expire_input_phase(self) -> None:
        with self._input_phase_lock:
            if self._input_phase_complete:
                return
            self._input_phase_expired = True
            try:
                self.connection.shutdown(socket.SHUT_RD)
            except OSError:
                pass

    def _finish_input_phase(self) -> bool:
        lock = getattr(self, "_input_phase_lock", None)
        if lock is None:
            return False
        with lock:
            expired = self._input_phase_expired
            if not self._input_phase_complete:
                self._input_phase_complete = True
                self._input_timer.cancel()
            return expired

    def _input_deadline_expired(self) -> bool:
        lock = getattr(self, "_input_phase_lock", None)
        if lock is None:
            return False
        with lock:
            return self._input_phase_expired

    def _drain_early_post_body(self) -> None:
        if getattr(self, "command", None) != "POST":
            return
        lock = getattr(self, "_input_phase_lock", None)
        if lock is None:
            return
        with lock:
            if self._input_phase_complete or self._input_phase_expired:
                return
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        if headers.get_all("Expect", []):
            return
        if headers.get_all("Transfer-Encoding", []):
            return
        content_lengths = headers.get_all("Content-Length", [])
        if len(content_lengths) != 1:
            return
        value = content_lengths[0]
        if (
            not value.isascii()
            or not value.isdecimal()
            or len(value) > _MAX_CONTENT_LENGTH_DIGITS
        ):
            return
        length = int(value)
        if length > _MAX_BODY_BYTES:
            return
        try:
            self.rfile.read(length)
        except (TimeoutError, socket.timeout, OSError):
            pass
        finally:
            self._finish_input_phase()

    def _read_json_body(self) -> Any | None:
        content_types = self.headers.get_all("Content-Type", [])
        if not content_types:
            self._send_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "content_type_required",
                "POST 必须使用 application/json。",
            )
            return None
        if len(content_types) != 1:
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "ambiguous_content_type",
                "Content-Type 只能出现一次。",
            )
            return None
        media_type, charset_error = _json_content_type(content_types[0])
        if media_type is False:
            self._send_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "POST 只接受 application/json。",
            )
            return None
        if charset_error:
            self._send_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_charset",
                "JSON 只接受 UTF-8 编码。",
            )
            return None

        content_lengths = self.headers.get_all("Content-Length", [])
        if not content_lengths:
            self._send_error(
                HTTPStatus.LENGTH_REQUIRED,
                "length_required",
                "POST 必须提供 Content-Length。",
            )
            return None
        value = content_lengths[0]
        if (
            not value.isascii()
            or not value.isdecimal()
            or len(value) > _MAX_CONTENT_LENGTH_DIGITS
        ):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
                "Content-Length 无效。",
            )
            return None
        length = int(value)
        if length > _MAX_BODY_BYTES:
            self._send_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                "请求体不能超过 64 KiB。",
            )
            return None
        try:
            body = self.rfile.read(length)
        except (TimeoutError, socket.timeout, OSError):
            self._finish_input_phase()
            self._send_error(
                HTTPStatus.REQUEST_TIMEOUT,
                "request_timeout",
                "读取请求体超时。",
            )
            return None
        expired = self._finish_input_phase()
        if len(body) != length:
            if expired:
                self._send_error(
                    HTTPStatus.REQUEST_TIMEOUT,
                    "request_timeout",
                    "读取请求体超时。",
                )
                return None
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "incomplete_body",
                "请求体不完整。",
            )
            return None
        try:
            text = body.decode("utf-8", errors="strict")
            payload = json.loads(
                text,
                object_pairs_hook=_object_without_duplicates,
                parse_constant=_reject_json_constant,
                parse_float=_finite_float,
                parse_int=_bounded_int,
            )
            if not isinstance(payload, dict):
                raise _StrictJSONError("JSON 顶层必须是对象")
            _validate_json_depth(payload)
            return payload
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._send_error(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "请求体必须是严格的 UTF-8 JSON 对象。",
            )
            return None

    def _send_error(
        self,
        status: int,
        code: str,
        message: str,
        *,
        extra_headers: dict[str, str] | None = None,
        write_body: bool = True,
    ) -> None:
        body = _json_bytes({"error": {"code": code, "message": message}})
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            extra_headers=extra_headers,
            write_body=write_body,
        )

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
        write_body: bool = True,
    ) -> None:
        self._drain_early_post_body()
        self._finish_input_phase()
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Connection", "close")
            if extra_headers is not None:
                for name, value in extra_headers.items():
                    self.send_header(name, value)
            self.end_headers()
            if write_body:
                self.wfile.write(body)
        except OSError:
            pass


def create_server(
    project: Path | str,
    port: int = 0,
    *,
    assets_root: Path | str | None = None,
    request_timeout: float = 5,
    write_token: str | None = None,
    paperflow_entrypoint: str | None = None,
    instances_root: Path | str | None = None,
) -> ThreadingHTTPServer:
    """创建只监听本机回环地址、只接受固定动作的控制台服务。"""
    project_path = _existing_directory(project, "项目目录")
    instance_path = _existing_directory(
        project_path.parent if instances_root is None else instances_root,
        "科研实例根",
    )
    if (
        is_link_or_reparse(instance_path)
        or project_path.parent != instance_path
    ):
        raise ValueError("项目必须是科研实例根下的直接普通子目录")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port 必须是 0 到 65535 之间的整数")
    if (
        isinstance(request_timeout, bool)
        or not isinstance(request_timeout, (int, float))
        or not math.isfinite(request_timeout)
        or request_timeout <= 0
    ):
        raise ValueError("request_timeout 必须是有限的正数")
    if assets_root is None:
        asset_path = Path(__file__).resolve().parent.parent / "assets" / "console"
    else:
        asset_path = Path(assets_root)
    assets_directory = _existing_directory(asset_path, "页面资源目录")
    assets = _load_assets(assets_directory)
    token = secrets.token_urlsafe(32) if write_token is None else write_token
    if (
        not isinstance(token, str)
        or not 16 <= len(token) <= 256
        or not all(
            character.isascii()
            and (character.isalnum() or character in "_-")
            for character in token
        )
    ):
        raise ValueError(
            "write_token 必须是 16 到 256 位 ASCII 字母、数字、_ 或 -"
        )
    paperflow_url = _paperflow_entrypoint(paperflow_entrypoint)
    return _ConsoleHTTPServer(
        ("127.0.0.1", port),
        project_path,
        assets,
        float(request_timeout),
        token,
        paperflow_url,
        instance_path,
    )


def serve(
    project: Path | str,
    port: int = 0,
    *,
    request_timeout: float = 5,
    paperflow_entrypoint: str | None = None,
    instances_root: Path | str | None = None,
) -> None:
    """启动本地固定动作控制台，直到用户中断进程。"""
    with create_server(
        project,
        port,
        request_timeout=request_timeout,
        paperflow_entrypoint=paperflow_entrypoint,
        instances_root=instances_root,
    ) as server:
        actual_port = server.server_address[1]
        print(f"http://127.0.0.1:{actual_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 CV 工作流本地控制台")
    parser.add_argument("--project", required=True, help="现有项目目录")
    parser.add_argument(
        "--port",
        default=0,
        type=_port_argument,
        help="本机端口；0 表示自动选择空闲端口",
    )
    parser.add_argument(
        "--request-timeout",
        default=5.0,
        type=_positive_number_argument,
        help="单次请求读取超时秒数",
    )
    parser.add_argument(
        "--paperflow-entrypoint",
        help="本次统一启动生成的 127.0.0.1 PaperFlow 入口",
    )
    parser.add_argument(
        "--instances-root",
        help="只允许新建或打开其直接子目录中的科研项目",
    )
    args = parser.parse_args(argv)
    try:
        serve(
            args.project,
            args.port,
            request_timeout=args.request_timeout,
            paperflow_entrypoint=args.paperflow_entrypoint,
            instances_root=args.instances_root,
        )
    except (OSError, ValueError):
        parser.exit(2, "无法启动本地控制台，请检查项目目录、页面资源和端口。\n")
    return 0


def _paperflow_entrypoint(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("paperflow_entrypoint 必须是本机 URL")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("paperflow_entrypoint 端口无效") from exc
    token_prefix = "token="
    token = (
        parsed.fragment[len(token_prefix):]
        if parsed.fragment.startswith(token_prefix)
        else ""
    )
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65535
        or parsed.path != "/"
        or parsed.query
        or not 16 <= len(token) <= 256
        or not all(
            character.isascii()
            and (character.isalnum() or character in "_-")
            for character in token
        )
    ):
        raise ValueError("paperflow_entrypoint 必须是带会话令牌的本机入口")
    return f"http://127.0.0.1:{port}/#token={token}"


def _server_state(
    server: _ConsoleHTTPServer,
    intent: str = "status",
    provided: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with server.project_lock:
        return console_state(server.project, intent, provided)


def _existing_directory(value: Path | str, label: str) -> Path:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label}不存在") from exc
    if not resolved.is_dir():
        raise NotADirectoryError(f"{label}必须是目录")
    return resolved


def _load_assets(root: Path) -> dict[str, tuple[bytes, str]]:
    files: dict[str, tuple[bytes, str]] = {}
    loaded: dict[str, bytes] = {}
    for filename, _ in set(_ASSET_FILES.values()):
        asset = root / filename
        if not asset.is_file():
            raise FileNotFoundError("固定页面资源不完整")
        loaded[filename] = asset.read_bytes()
    for route, (filename, content_type) in _ASSET_FILES.items():
        files[route] = (loaded[filename], content_type)
    return files


def _json_content_type(value: str) -> tuple[bool, bool]:
    parts = [part.strip() for part in value.split(";")]
    if not parts or parts[0].lower() != "application/json":
        return False, False
    charset_seen = False
    for parameter in parts[1:]:
        if "=" not in parameter:
            return False, False
        name, raw_value = parameter.split("=", 1)
        if name.strip().lower() != "charset" or charset_seen:
            return False, False
        charset_seen = True
        charset = raw_value.strip().strip('"').lower()
        if charset != "utf-8":
            return True, True
    return True, False


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError("JSON 对象存在重复字段")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _StrictJSONError(f"JSON 常量无效：{value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _StrictJSONError("JSON 数字必须有限")
    return parsed


def _bounded_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise _StrictJSONError("JSON 整数位数过多")
    try:
        return int(value)
    except ValueError as exc:
        raise _StrictJSONError("JSON 整数无效") from exc


def _validate_json_depth(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            raise _StrictJSONError("JSON 嵌套层数过多")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _raw_json_error_response(
    status: HTTPStatus,
    code: str,
    message: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    body = _json_bytes({"error": {"code": code, "message": message}})
    headers = [
        f"HTTP/1.1 {status.value} {status.phrase}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
        "X-Content-Type-Options: nosniff",
        f"Content-Security-Policy: {_CSP}",
        "X-Frame-Options: DENY",
        "Referrer-Policy: no-referrer",
        "Connection: close",
    ]
    if extra_headers is not None:
        headers.extend(f"{name}: {value}" for name, value in extra_headers.items())
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("端口必须是整数") from exc
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 0 到 65535 之间")
    return port


def _positive_number_argument(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("超时必须是数字") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("超时必须是有限的正数")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
