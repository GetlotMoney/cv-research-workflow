from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from tests._helpers import ROOT, SCRIPTS


sys.path.insert(0, str(SCRIPTS))
import console_server  # noqa: E402
from console_server import create_server  # noqa: E402
from workflow_core.intake import INTENTS  # noqa: E402
from workflow_core.project import init_project  # noqa: E402
from workflow_core.ui_service import console_state  # noqa: E402


MAX_BODY_BYTES = 64 * 1024


def _tree_snapshot(root: Path) -> dict[str, tuple[str, str | None, int, int]]:
    snapshot: dict[str, tuple[str, str | None, int, int]] = {}
    for path in sorted([root, *root.rglob("*")], key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            snapshot[relative] = (
                "link",
                str(path.readlink()),
                info.st_size,
                info.st_mtime_ns,
            )
        elif path.is_dir():
            snapshot[relative] = (
                "directory",
                None,
                info.st_size,
                info.st_mtime_ns,
            )
        else:
            snapshot[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
                info.st_size,
                info.st_mtime_ns,
            )
    return snapshot


class ConsoleServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.project = root / "project"
        init_project(self.project, "本地控制台测试", layout="v2")
        self.assets = root / "assets"
        self.assets.mkdir()
        self.asset_payloads = {
            "index.html": b"<!doctype html><title>console</title>",
            "styles.css": b"body { color: #123; }",
            "app.js": b"globalThis.consoleReady = true;",
        }
        for name, payload in self.asset_payloads.items():
            (self.assets / name).write_bytes(payload)
        (self.assets / "secret.txt").write_text(
            "not public",
            encoding="utf-8",
        )
        self.server = create_server(
            self.project,
            assets_root=self.assets,
            request_timeout=0.4,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(
        self,
        method: str,
        target: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        port: int | None = None,
        timeout: float = 5,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        selected_port = self.port if port is None else port
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            selected_port,
            timeout=timeout,
        )
        try:
            connection.request(
                method,
                target,
                body=body,
                headers={} if headers is None else headers,
            )
            response = connection.getresponse()
            payload = response.read()
            return response.status, response.getheaders(), payload
        finally:
            connection.close()

    def _post(
        self,
        payload: object,
        *,
        origin: str | None = None,
        content_type: str = "application/json",
        port: int | None = None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        selected_port = self.port if port is None else port
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": content_type,
            "Origin": (
                f"http://127.0.0.1:{selected_port}"
                if origin is None
                else origin
            ),
        }
        return self._request(
            "POST",
            "/api/state",
            body=body,
            headers=headers,
            port=selected_port,
        )

    def _raw(
        self,
        request: bytes,
        *,
        shutdown_write: bool = True,
        port: int | None = None,
    ) -> bytes:
        selected_port = self.port if port is None else port
        with socket.create_connection(
            ("127.0.0.1", selected_port),
            timeout=2,
        ) as sock:
            sock.settimeout(2)
            sock.sendall(request)
            if shutdown_write:
                sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = sock.recv(65536)
                except (socket.timeout, ConnectionAbortedError, ConnectionResetError):
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)

    def _start_auxiliary_server(
        self,
        *,
        project: Path | None = None,
        request_timeout: float,
    ) -> console_server.ThreadingHTTPServer:
        server = create_server(
            self.project if project is None else project,
            assets_root=self.assets,
            request_timeout=request_timeout,
        )
        server.daemon_threads = False
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        thread.start()
        self.addCleanup(self._stop_auxiliary_server, server, thread)
        return server

    @staticmethod
    def _stop_auxiliary_server(
        server: console_server.ThreadingHTTPServer,
        thread: threading.Thread,
    ) -> None:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def _git(self, project: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(project), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def _bound_git_project(self, name: str) -> tuple[Path, Path]:
        project = Path(self.temporary.name) / name
        init_project(project, name, layout="v2")
        adapter_source = project / "workflow_adapter.py"
        adapter_source.write_bytes(
            (
                ROOT
                / "tests"
                / "fixtures"
                / "fake_cv_project"
                / "workflow_adapter.py"
            ).read_bytes()
        )
        self._git(project, "init")
        self._git(project, "config", "user.name", "Console Server Test")
        self._git(
            project,
            "config",
            "user.email",
            "console-server@example.com",
        )
        self._git(project, "add", ".")
        self._git(project, "commit", "-m", "fixture")
        commit = self._git(project, "rev-parse", "HEAD")
        adapter_path = project / ".experiment-workflow" / "adapter.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapter.update(
            {
                "status": "bound",
                "code_sources": [
                    {
                        "repo_url": f"local:{project.as_posix()}",
                        "commit": commit,
                        "relative_path": "workflow_adapter.py",
                    }
                ],
                "capabilities": {"workflow_adapter": True},
            }
        )
        adapter_path.write_text(
            json.dumps(adapter, ensure_ascii=False),
            encoding="utf-8",
        )
        return project, adapter_source

    def test_binds_only_to_ipv4_loopback_and_uses_a_temporary_port(self) -> None:
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        self.assertGreater(self.port, 0)

    def test_get_state_matches_the_shared_read_model(self) -> None:
        before = _tree_snapshot(self.project)
        status, headers, body = self._request("GET", "/api/state")

        self.assertEqual(200, status)
        self.assertEqual(
            console_state(self.project),
            json.loads(body.decode("utf-8")),
        )
        self.assertEqual(before, _tree_snapshot(self.project))
        self._assert_safe_headers(headers, "application/json; charset=utf-8")

    def test_post_state_accepts_every_shared_intent_without_writing(self) -> None:
        before = _tree_snapshot(self.project)
        for intent in sorted(INTENTS):
            with self.subTest(intent=intent):
                status, headers, body = self._post(
                    {"intent": intent, "provided": {}},
                )
                self.assertEqual(200, status)
                self.assertEqual(
                    console_state(self.project, intent, {}),
                    json.loads(body.decode("utf-8")),
                )
                self._assert_safe_headers(
                    headers,
                    "application/json; charset=utf-8",
                )
        self.assertEqual(before, _tree_snapshot(self.project))

    def test_static_assets_are_a_fixed_in_memory_allowlist(self) -> None:
        (self.assets / "index.html").write_bytes(b"changed after startup")
        routes = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        for route, (name, content_type) in routes.items():
            with self.subTest(route=route):
                status, headers, body = self._request("GET", route)
                self.assertEqual(200, status)
                self.assertEqual(self.asset_payloads[name], body)
                self._assert_safe_headers(headers, content_type)

        for target in (
            "/secret.txt",
            "/../secret.txt",
            "/%2e%2e/secret.txt",
            "/assets/console/index.html",
            "/C:/Windows/win.ini",
        ):
            with self.subTest(target=target):
                status, _, body = self._request("GET", target)
                self.assertEqual(404, status)
                self._assert_json_error(body, "not_found")

    def test_query_strings_cannot_choose_a_project_path_or_command(self) -> None:
        for target in (
            "/api/state?project=C%3A%5Csecret",
            "/api/state?path=..%2Fother",
            "/?command=git+push",
            "/index.html?assets_root=C%3A%5Csecret",
        ):
            with self.subTest(target=target):
                status, _, body = self._request("GET", target)
                self.assertEqual(400, status)
                self._assert_json_error(body, "query_not_allowed")

    def test_disallowed_methods_always_return_405_without_cors(self) -> None:
        for method in ("HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
            with self.subTest(method=method):
                status, headers, body = self._request(method, "/api/state")
                self.assertEqual(405, status)
                self.assertEqual("GET, POST", dict(headers).get("Allow"))
                self.assertNotIn("Access-Control-Allow-Origin", dict(headers))
                if method != "HEAD":
                    self._assert_json_error(body, "method_not_allowed")

    def test_host_must_match_the_current_ipv4_loopback_origin(self) -> None:
        for host in (
            "localhost",
            f"localhost:{self.port}",
            f"127.0.0.1:{self.port + 1}",
            f"[::1]:{self.port}",
        ):
            with self.subTest(host=host):
                status, _, body = self._request(
                    "GET",
                    "/api/state",
                    headers={"Host": host},
                )
                self.assertEqual(400, status)
                self._assert_json_error(body, "invalid_host")

        response = self._raw(
            (
                "GET /api/state HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])

    def test_origin_is_same_origin_when_present_and_required_for_post(self) -> None:
        status, _, body = self._request(
            "GET",
            "/api/state",
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(403, status)
        self._assert_json_error(body, "invalid_origin")

        status, _, body = self._request(
            "GET",
            "/api/state",
            headers={"Origin": self.origin},
        )
        self.assertEqual(200, status)
        self.assertEqual("valid", json.loads(body)["snapshot"]["status"])

        body_bytes = b'{"intent":"status","provided":{}}'
        status, _, body = self._request(
            "POST",
            "/api/state",
            body=body_bytes,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(403, status)
        self._assert_json_error(body, "origin_required")

        status, _, body = self._post(
            {"intent": "status", "provided": {}},
            origin="http://evil.example",
        )
        self.assertEqual(403, status)
        self._assert_json_error(body, "invalid_origin")

    def test_completed_post_early_errors_are_stable_over_real_tcp(
        self,
    ) -> None:
        expected_origin = f"http://127.0.0.1:{self.port}"
        max_body = b"{}" + (b" " * (MAX_BODY_BYTES - 2))
        cases = (
            (
                "invalid_host",
                "/api/state",
                {
                    "Content-Type": "application/json",
                    "Host": "evil.example",
                    "Origin": expected_origin,
                },
                b"{}",
                400,
                "invalid_host",
            ),
            (
                "missing_origin",
                "/api/state",
                {"Content-Type": "application/json"},
                b"{}",
                403,
                "origin_required",
            ),
            (
                "invalid_origin",
                "/api/state",
                {
                    "Content-Type": "application/json",
                    "Origin": "http://evil.example",
                },
                max_body,
                403,
                "invalid_origin",
            ),
            (
                "query",
                "/api/state?project=elsewhere",
                {
                    "Content-Type": "application/json",
                    "Origin": expected_origin,
                },
                b"{}",
                400,
                "query_not_allowed",
            ),
            (
                "not_found",
                "/api/other",
                {
                    "Content-Type": "application/json",
                    "Origin": expected_origin,
                },
                b"{}",
                404,
                "not_found",
            ),
            (
                "missing_content_type",
                "/api/state",
                {"Origin": expected_origin},
                b"{}",
                415,
                "content_type_required",
            ),
            (
                "unsupported_content_type",
                "/api/state",
                {
                    "Content-Type": "text/plain",
                    "Origin": expected_origin,
                },
                b"{}",
                415,
                "unsupported_media_type",
            ),
        )
        errors: list[str] = []
        responses: list[
            tuple[
                str,
                int,
                str,
                int,
                list[tuple[str, str]],
                bytes,
            ]
        ] = []
        for label, target, headers, request_body, status, code in cases:
            for attempt in range(30):
                try:
                    actual_status, actual_headers, response_body = (
                        self._request(
                            "POST",
                            target,
                            body=request_body,
                            headers=headers,
                            timeout=1,
                        )
                    )
                    responses.append(
                        (
                            label,
                            status,
                            code,
                            actual_status,
                            actual_headers,
                            response_body,
                        )
                    )
                except OSError as exc:
                    errors.append(
                        f"{label}:{attempt}:{type(exc).__name__}"
                    )

        self.assertEqual([], errors)
        self.assertEqual(len(cases) * 30, len(responses))
        for (
            label,
            expected_status,
            expected_code,
            status,
            headers,
            body,
        ) in responses:
            with self.subTest(path=label):
                self.assertEqual(expected_status, status)
                self._assert_json_error(body, expected_code)
                self._assert_safe_headers(
                    headers,
                    "application/json; charset=utf-8",
                )

    def test_post_requires_unambiguous_utf8_json_framing(self) -> None:
        valid = b'{"intent":"status","provided":{}}'
        cases = [
            (
                "missing_content_type",
                {
                    "Origin": self.origin,
                    "Content-Length": str(len(valid)),
                },
                415,
                "content_type_required",
            ),
            (
                "wrong_content_type",
                {
                    "Origin": self.origin,
                    "Content-Type": "text/plain",
                    "Content-Length": str(len(valid)),
                },
                415,
                "unsupported_media_type",
            ),
            (
                "wrong_charset",
                {
                    "Origin": self.origin,
                    "Content-Type": "application/json; charset=latin-1",
                    "Content-Length": str(len(valid)),
                },
                415,
                "unsupported_charset",
            ),
            (
                "missing_length",
                {
                    "Origin": self.origin,
                    "Content-Type": "application/json",
                },
                411,
                "length_required",
            ),
            (
                "invalid_length",
                {
                    "Origin": self.origin,
                    "Content-Type": "application/json",
                    "Content-Length": "-1",
                },
                400,
                "invalid_content_length",
            ),
        ]
        for name, headers, expected_status, error_code in cases:
            with self.subTest(name=name):
                request_lines = [
                    "POST /api/state HTTP/1.1",
                    f"Host: 127.0.0.1:{self.port}",
                    *(f"{key}: {value}" for key, value in headers.items()),
                    "Connection: close",
                    "",
                    "",
                ]
                response = self._raw("\r\n".join(request_lines).encode() + valid)
                self.assertIn(
                    f" {expected_status} ".encode(),
                    response.split(b"\r\n", 1)[0],
                )
                self.assertIn(f'"code":"{error_code}"'.encode(), response)

    def test_rejects_duplicate_length_content_type_and_transfer_encoding(self) -> None:
        valid = b'{"intent":"status","provided":{}}'
        base = (
            "POST /api/state HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Origin: {self.origin}\r\n"
        )
        requests = {
            "duplicate_length": (
                base
                + "Content-Type: application/json\r\n"
                + f"Content-Length: {len(valid)}\r\n"
                + f"Content-Length: {len(valid)}\r\n"
                + "Connection: close\r\n\r\n"
            ).encode() + valid,
            "duplicate_content_type": (
                base
                + "Content-Type: application/json\r\n"
                + "Content-Type: application/json\r\n"
                + f"Content-Length: {len(valid)}\r\n"
                + "Connection: close\r\n\r\n"
            ).encode() + valid,
            "transfer_encoding": (
                base
                + "Content-Type: application/json\r\n"
                + "Transfer-Encoding: chunked\r\n"
                + "Connection: close\r\n\r\n"
                + f"{len(valid):X}\r\n"
            ).encode() + valid + b"\r\n0\r\n\r\n",
        }
        for name, request in requests.items():
            with self.subTest(name=name):
                response = self._raw(request)
                self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
                self.assertIn(b'"error":', response)

    def test_invalid_ambiguous_and_oversized_framing_is_not_drained(
        self,
    ) -> None:
        server = self._start_auxiliary_server(request_timeout=2)
        port = server.server_address[1]
        base = (
            "POST /api/state HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
        )
        requests = {
            "invalid_length": (
                base + "Content-Length: -1\r\nConnection: close\r\n\r\n",
                400,
            ),
            "duplicate_length": (
                base
                + "Content-Length: 2\r\n"
                + "Content-Length: 2\r\n"
                + "Connection: close\r\n\r\n",
                400,
            ),
            "oversized_length": (
                base
                + f"Content-Length: {MAX_BODY_BYTES + 1}\r\n"
                + "Connection: close\r\n\r\n",
                413,
            ),
            "transfer_encoding": (
                base
                + "Transfer-Encoding: chunked\r\n"
                + "Connection: close\r\n\r\n",
                400,
            ),
        }
        for name, (raw_request, expected_status) in requests.items():
            with self.subTest(name=name):
                with socket.create_connection(
                    ("127.0.0.1", port),
                    timeout=1,
                ) as client:
                    client.settimeout(1)
                    started = time.monotonic()
                    client.sendall(raw_request.encode("ascii"))
                    chunks: list[bytes] = []
                    while b"\r\n" not in b"".join(chunks):
                        remaining = 0.5 - (time.monotonic() - started)
                        self.assertGreater(remaining, 0)
                        client.settimeout(remaining)
                        chunk = client.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    elapsed = time.monotonic() - started
                    response = b"".join(chunks)
                self.assertLess(elapsed, 0.5)
                self.assertIn(
                    f" {expected_status} ".encode("ascii"),
                    response.split(b"\r\n", 1)[0],
                )

    def test_rejects_bodies_over_64_kib_before_reading_them(self) -> None:
        response = self._raw(
            (
                "POST /api/state HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Origin: {self.origin}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {MAX_BODY_BYTES + 1}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        self.assertIn(b" 413 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b'"code":"body_too_large"', response)

    def test_rejects_incomplete_and_timed_out_request_bodies(self) -> None:
        incomplete = self._raw(
            (
                "POST /api/state HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Origin: {self.origin}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 100\r\n"
                "Connection: close\r\n\r\n"
                '{"intent"'
            ).encode("ascii")
        )
        self.assertIn(b" 400 ", incomplete.split(b"\r\n", 1)[0])
        self.assertIn(b'"code":"incomplete_body"', incomplete)

        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            sock.settimeout(2)
            sock.sendall(
                (
                    "POST /api/state HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.port}\r\n"
                    f"Origin: {self.origin}\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: 100\r\n"
                    "Connection: close\r\n\r\n"
                    '{"intent"'
                ).encode("ascii")
            )
            started = time.monotonic()
            response = sock.recv(65536)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.5)
        self.assertIn(b" 408 ", response.split(b"\r\n", 1)[0])

    def test_rejects_noncanonical_json_and_invalid_post_contracts(self) -> None:
        raw_json_cases = {
            "malformed": b"{",
            "invalid_utf8": b'{"intent":"status","provided":{"x":"\xff"}}',
            "duplicate_key": (
                b'{"intent":"status","intent":"continue","provided":{}}'
            ),
            "nan": b'{"intent":"status","provided":{"x":NaN}}',
            "infinity": b'{"intent":"status","provided":{"x":Infinity}}',
            "numeric_overflow": b'{"intent":"status","provided":{"x":1e999}}',
            "top_level_array": b"[]",
        }
        for name, body_bytes in raw_json_cases.items():
            with self.subTest(name=name):
                status, _, body = self._request(
                    "POST",
                    "/api/state",
                    body=body_bytes,
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "Origin": self.origin,
                    },
                )
                self.assertEqual(400, status)
                self._assert_json_error(body, "invalid_json")

        contract_cases = {
            "missing_intent": {"provided": {}},
            "missing_provided": {"intent": "status"},
            "extra_field": {
                "intent": "status",
                "provided": {},
                "project": "C:/secret",
            },
            "invalid_intent": {"intent": "train", "provided": {}},
            "non_object_provided": {"intent": "status", "provided": []},
        }
        for name, payload in contract_cases.items():
            with self.subTest(name=name):
                status, _, body = self._post(payload)
                self.assertEqual(400, status)
                self._assert_json_error(body, "invalid_request")

    def test_extreme_json_inputs_return_stable_400_without_tracebacks(
        self,
    ) -> None:
        huge_integer = "9" * 5000
        deep_value = "[" * 20000 + "0" + "]" * 20000
        bodies = {
            "huge_integer": (
                '{"intent":"status","provided":{"value":'
                + huge_integer
                + "}}"
            ).encode("ascii"),
            "deep_json": (
                '{"intent":"status","provided":{"value":'
                + deep_value
                + "}}"
            ).encode("ascii"),
        }
        for name, body_bytes in bodies.items():
            with self.subTest(name=name):
                request = (
                    "POST /api/state HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.port}\r\n"
                    f"Origin: {self.origin}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body_bytes)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii") + body_bytes
                errors = io.StringIO()
                with redirect_stderr(errors):
                    response = self._raw(request)
                self._assert_raw_json_error(
                    response,
                    400,
                    "invalid_json",
                )
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertNotIn(str(SCRIPTS), errors.getvalue())

        overlong_length = "9" * 5000
        errors = io.StringIO()
        with redirect_stderr(errors):
            response = self._raw(
                (
                    "POST /api/state HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.port}\r\n"
                    f"Origin: {self.origin}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {overlong_length}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii"),
                shutdown_write=False,
            )
        self._assert_raw_json_error(
            response,
            400,
            "invalid_content_length",
        )
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertNotIn(str(SCRIPTS), errors.getvalue())

    def test_request_input_has_an_absolute_header_and_body_deadline(
        self,
    ) -> None:
        server = self._start_auxiliary_server(request_timeout=0.2)
        port = server.server_address[1]
        origin = f"http://127.0.0.1:{port}"
        header_prefix = (
            "GET /api/state HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "X-Slow: "
        ).encode("ascii")
        elapsed, response = self._drip_until_response(
            port,
            header_prefix,
            drip=b"a",
        )
        self.assertLess(elapsed, 0.55)
        if response:
            self._assert_raw_json_error(response, 408, "request_timeout")

        body_prefix = (
            "POST /api/state HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Origin: {origin}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 1000\r\n"
            "Connection: close\r\n\r\n"
            '{"intent":"status","provided":{"value":"'
        ).encode("ascii")
        elapsed, response = self._drip_until_response(
            port,
            body_prefix,
            drip=b"a",
        )
        self.assertLess(elapsed, 0.55)
        if response:
            self._assert_raw_json_error(response, 408, "request_timeout")

    def test_completed_input_cancels_deadline_before_slow_state_read(
        self,
    ) -> None:
        server = self._start_auxiliary_server(request_timeout=0.1)
        port = server.server_address[1]

        def slow_state(*args: object, **kwargs: object) -> dict[str, bool]:
            del args, kwargs
            time.sleep(0.25)
            return {"slow_state_completed": True}

        with mock.patch.object(
            console_server,
            "console_state",
            side_effect=slow_state,
        ):
            status, _, body = self._request(
                "GET",
                "/api/state",
                port=port,
            )
            self.assertEqual(200, status)
            self.assertEqual(
                {"slow_state_completed": True},
                json.loads(body),
            )

            status, _, body = self._post(
                {"intent": "status", "provided": {}},
                port=port,
            )
            self.assertEqual(200, status)
            self.assertEqual(
                {"slow_state_completed": True},
                json.loads(body),
            )

    def test_concurrency_is_fixed_and_capacity_is_released(self) -> None:
        server = self._start_auxiliary_server(request_timeout=2)
        port = server.server_address[1]
        blockers: list[socket.socket] = []
        try:
            for _ in range(8):
                blocker = socket.create_connection(
                    ("127.0.0.1", port),
                    timeout=2,
                )
                blocker.sendall(
                    (
                        "GET /api/state HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        "X-Hold: "
                    ).encode("ascii")
                )
                blockers.append(blocker)
            time.sleep(0.1)

            for _ in range(4):
                response = self._raw(
                    (
                        "GET /api/state HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii"),
                    port=port,
                )
                self._assert_raw_json_error(
                    response,
                    503,
                    "server_busy",
                )
                self._assert_raw_safe_headers(response)
        finally:
            for blocker in blockers:
                blocker.close()

        deadline = time.monotonic() + 1.5
        recovered = b""
        while time.monotonic() < deadline:
            recovered = self._raw(
                (
                    "GET /api/state HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii"),
                port=port,
            )
            if b" 200 " in recovered.split(b"\r\n", 1)[0]:
                break
            time.sleep(0.02)
        self.assertIn(b" 200 ", recovered.split(b"\r\n", 1)[0])

        with mock.patch.object(
            console_server,
            "console_state",
            side_effect=RuntimeError("forced test failure"),
        ):
            for _ in range(12):
                response = self._raw(
                    (
                        "GET /api/state HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii"),
                    port=port,
                )
                self._assert_raw_json_error(
                    response,
                    500,
                    "internal_error",
                )

        response = self._raw(
            (
                "GET /api/state HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii"),
            port=port,
        )
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])

    def test_ready_overload_client_receives_fixed_safe_503(self) -> None:
        client, accepted = socket.socketpair()
        self.addCleanup(client.close)
        client.settimeout(1)
        client.sendall(
            b"GET /api/state HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Connection: close\r\n\r\n"
        )
        client.shutdown(socket.SHUT_WR)

        self.server._reject_busy(accepted)

        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        response = b"".join(chunks)
        self._assert_raw_json_error(response, 503, "server_busy")
        self._assert_raw_safe_headers(response)

    def test_real_tcp_overload_returns_safe_503_one_hundred_times(
        self,
    ) -> None:
        server = self._start_auxiliary_server(request_timeout=5)
        port = server.server_address[1]
        blockers = self._saturate_server(server)
        responses: list[tuple[int, list[tuple[str, str]], bytes]] = []
        errors: list[str] = []
        try:
            for attempt in range(100):
                try:
                    responses.append(
                        self._request(
                            "GET",
                            "/api/state",
                            port=port,
                            timeout=1,
                        )
                    )
                except OSError as exc:
                    errors.append(f"{attempt}:{type(exc).__name__}")
        finally:
            for blocker in blockers:
                blocker.close()

        self.assertEqual([], errors)
        self.assertEqual(100, len(responses))
        for status, headers, body in responses:
            self.assertEqual(503, status)
            self._assert_json_error(body, "server_busy")
            self._assert_safe_headers(
                headers,
                "application/json; charset=utf-8",
            )

    def test_real_tcp_overload_returns_safe_503_for_completed_post_bodies(
        self,
    ) -> None:
        server = self._start_auxiliary_server(request_timeout=5)
        port = server.server_address[1]
        blockers = self._saturate_server(server)
        bodies = {
            "small": b"{}",
            "max": b"{}" + (b" " * (MAX_BODY_BYTES - 2)),
        }
        errors: list[str] = []
        responses: list[
            tuple[str, int, list[tuple[str, str]], bytes]
        ] = []
        try:
            for label, request_body in bodies.items():
                for attempt in range(100):
                    try:
                        status, headers, response_body = self._request(
                            "POST",
                            "/api/state",
                            body=request_body,
                            headers={
                                "Content-Type": "application/json",
                                "Origin": f"http://127.0.0.1:{port}",
                            },
                            port=port,
                            timeout=1,
                        )
                        responses.append(
                            (label, status, headers, response_body)
                        )
                    except OSError as exc:
                        errors.append(
                            f"{label}:{attempt}:{type(exc).__name__}"
                        )
        finally:
            for blocker in blockers:
                blocker.close()

        self.assertEqual([], errors)
        self.assertEqual(200, len(responses))
        for label, status, headers, body in responses:
            with self.subTest(body=label):
                self.assertEqual(503, status)
                self._assert_json_error(body, "server_busy")
                self._assert_safe_headers(
                    headers,
                    "application/json; charset=utf-8",
                )

    def test_overload_rejection_resources_are_fixed_and_bounded(self) -> None:
        server = self._start_auxiliary_server(request_timeout=5)
        workers = getattr(server, "_rejection_workers", ())
        pending = getattr(server, "_rejection_queue", None)

        self.assertGreaterEqual(len(workers), 1)
        self.assertLessEqual(len(workers), 4)
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertGreaterEqual(pending.maxsize, 1)
        self.assertLessEqual(pending.maxsize, 128)

    def test_overloaded_slow_client_does_not_block_capacity_recovery(
        self,
    ) -> None:
        server = self._start_auxiliary_server(request_timeout=5)
        port = server.server_address[1]
        blockers = self._saturate_server(server)
        slow_socket, stop_slow, slow_thread = self._start_slow_drip(port)
        result: tuple[int, list[tuple[str, str]], bytes] | None = None
        elapsed = float("inf")
        try:
            time.sleep(0.1)
            blockers.pop().close()
            self._wait_for_free_capacity(server)
            started = time.monotonic()
            try:
                result = self._request(
                    "GET",
                    "/api/state",
                    port=port,
                    timeout=0.4,
                )
            except OSError:
                pass
            elapsed = time.monotonic() - started
        finally:
            stop_slow.set()
            slow_socket.close()
            slow_thread.join(timeout=1)
            for blocker in blockers:
                blocker.close()

        self.assertIsNotNone(
            result,
            "释放一个名额后，正常 GET 仍被过载慢连接堵住",
        )
        assert result is not None
        self.assertEqual(200, result[0])
        self.assertLess(elapsed, 0.4)

    def test_shutdown_is_not_blocked_by_an_overloaded_slow_client(self) -> None:
        server = self._start_auxiliary_server(request_timeout=5)
        port = server.server_address[1]
        blockers = self._saturate_server(server)
        slow_socket, stop_slow, slow_thread = self._start_slow_drip(port)
        shutdown_done = threading.Event()

        def shutdown_server() -> None:
            server.shutdown()
            shutdown_done.set()

        shutdown_thread = threading.Thread(
            target=shutdown_server,
            daemon=True,
        )
        try:
            time.sleep(0.1)
            started = time.monotonic()
            shutdown_thread.start()
            completed_quickly = shutdown_done.wait(0.4)
            elapsed = time.monotonic() - started
        finally:
            stop_slow.set()
            slow_socket.close()
            slow_thread.join(timeout=1)
            for blocker in blockers:
                blocker.close()
            shutdown_done.wait(2)
            shutdown_thread.join(timeout=1)

        self.assertTrue(
            completed_quickly,
            "shutdown 被过载慢连接拖住",
        )
        self.assertLess(elapsed, 0.4)

    def test_real_git_get_and_post_never_refresh_the_index(self) -> None:
        project, adapter_source = self._bound_git_project(
            "server-index-stability",
        )
        touched = adapter_source.stat().st_mtime_ns + 2_000_000_000
        os.utime(adapter_source, ns=(touched, touched))
        index = project / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        server = self._start_auxiliary_server(
            project=project,
            request_timeout=2,
        )
        port = server.server_address[1]

        status, _, get_body = self._request(
            "GET",
            "/api/state",
            port=port,
        )
        self.assertEqual(200, status)
        self.assertEqual(
            "pass",
            json.loads(get_body)["start"]["adapter"]["runtime"]["status"],
        )
        status, _, post_body = self._post(
            {"intent": "status", "provided": {}},
            port=port,
        )
        self.assertEqual(200, status)
        self.assertEqual(
            "pass",
            json.loads(post_body)["start"]["adapter"]["runtime"]["status"],
        )
        self.assertEqual(before, (index.read_bytes(), index.stat().st_mtime_ns))

    def test_internal_errors_are_generic_and_do_not_leak_paths_or_stacks(self) -> None:
        with mock.patch.object(
            console_server,
            "console_state",
            side_effect=RuntimeError("D:\\private\\project\\secret.json"),
        ):
            status, headers, body = self._request("GET", "/api/state")

        self.assertEqual(500, status)
        self._assert_json_error(body, "internal_error")
        decoded = body.decode("utf-8")
        self.assertNotIn("D:\\private", decoded)
        self.assertNotIn("Traceback", decoded)
        self._assert_safe_headers(headers, "application/json; charset=utf-8")

    def test_create_server_rejects_bad_startup_inputs_and_occupied_ports(
        self,
    ) -> None:
        missing = Path(self.temporary.name) / "missing"
        with self.assertRaises((FileNotFoundError, ValueError)):
            create_server(missing, assets_root=self.assets)

        file_project = Path(self.temporary.name) / "file-project"
        file_project.write_text("not a directory", encoding="utf-8")
        with self.assertRaises((NotADirectoryError, ValueError)):
            create_server(file_project, assets_root=self.assets)

        for timeout in (0, -1, float("inf"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    create_server(
                        self.project,
                        assets_root=self.assets,
                        request_timeout=timeout,
                    )

        for token in (
            "short",
            "abcdefghijklmnop\r\nX-Injected: yes",
            "abcdefghijklmnop!",
            "令牌abcdefghijklmnop",
        ):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    create_server(
                        self.project,
                        assets_root=self.assets,
                        write_token=token,
                    )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            occupied_port = occupied.getsockname()[1]
            with self.assertRaises(OSError):
                create_server(
                    self.project,
                    port=occupied_port,
                    assets_root=self.assets,
                )

    def test_cli_exposes_no_host_or_asset_override(self) -> None:
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            console_server.main(
                [
                    "--project",
                    str(self.project),
                    "--host",
                    "0.0.0.0",
                ]
            )
        self.assertEqual(2, error.exception.code)

        with (
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as error,
        ):
            console_server.main(
                [
                    "--project",
                    str(self.project),
                    "--assets-root",
                    str(self.assets),
                ]
            )
        self.assertEqual(2, error.exception.code)

    def _assert_json_error(self, body: bytes, code: str) -> None:
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual({"error"}, set(payload))
        self.assertEqual(code, payload["error"]["code"])
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertTrue(payload["error"]["message"])

    def _assert_raw_json_error(
        self,
        response: bytes,
        status: int,
        code: str,
    ) -> None:
        head, separator, body = response.partition(b"\r\n\r\n")
        self.assertEqual(b"\r\n\r\n", separator, response[:200])
        self.assertIn(
            f" {status} ".encode("ascii"),
            head.split(b"\r\n", 1)[0],
        )
        self._assert_json_error(body, code)

    def _assert_raw_safe_headers(self, response: bytes) -> None:
        head = response.partition(b"\r\n\r\n")[0].decode("iso-8859-1")
        headers = {
            line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
            for line in head.split("\r\n")[1:]
            if ":" in line
        }
        self.assertEqual(
            "application/json; charset=utf-8",
            headers.get("content-type"),
        )
        self.assertEqual("no-store", headers.get("cache-control"))
        self.assertEqual("nosniff", headers.get("x-content-type-options"))
        self.assertEqual("DENY", headers.get("x-frame-options"))
        self.assertEqual("close", headers.get("connection"))
        self.assertIn(
            "frame-ancestors 'none'",
            headers.get("content-security-policy", ""),
        )
        self.assertNotIn("access-control-allow-origin", headers)

    def _saturate_server(
        self,
        server: console_server.ThreadingHTTPServer,
    ) -> list[socket.socket]:
        port = server.server_address[1]
        blockers: list[socket.socket] = []
        for _ in range(8):
            blocker = socket.create_connection(
                ("127.0.0.1", port),
                timeout=2,
            )
            blocker.sendall(
                (
                    "GET /api/state HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    "X-Hold: "
                ).encode("ascii")
            )
            blockers.append(blocker)
        deadline = time.monotonic() + 1
        gate = server._request_gate
        while gate._value != 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(0, gate._value, "测试未能占满并发许可")
        return blockers

    def _start_slow_drip(
        self,
        port: int,
    ) -> tuple[socket.socket, threading.Event, threading.Thread]:
        slow_socket = socket.create_connection(
            ("127.0.0.1", port),
            timeout=2,
        )
        slow_socket.sendall(b"G")
        stop = threading.Event()

        def drip() -> None:
            while not stop.wait(0.02):
                try:
                    slow_socket.sendall(b"a")
                except OSError:
                    return

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        return slow_socket, stop, thread

    def _wait_for_free_capacity(
        self,
        server: console_server.ThreadingHTTPServer,
    ) -> None:
        deadline = time.monotonic() + 1
        gate = server._request_gate
        while gate._value == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertGreater(gate._value, 0, "关闭连接后许可没有释放")

    def _drip_until_response(
        self,
        port: int,
        prefix: bytes,
        *,
        drip: bytes,
    ) -> tuple[float, bytes]:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.settimeout(1)
            started = time.monotonic()
            sock.sendall(prefix)
            response = b""
            while time.monotonic() - started < 0.7:
                try:
                    sock.sendall(drip)
                except OSError:
                    pass
                readable, _, _ = select.select([sock], [], [], 0.03)
                if not readable:
                    continue
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = sock.recv(65536)
                    except (
                        socket.timeout,
                        ConnectionAbortedError,
                        ConnectionResetError,
                    ):
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                response = b"".join(chunks)
                break
            return time.monotonic() - started, response

    def _assert_safe_headers(
        self,
        headers: list[tuple[str, str]],
        content_type: str,
    ) -> None:
        values: dict[str, list[str]] = {}
        for name, value in headers:
            values.setdefault(name, []).append(value)
        self.assertEqual([content_type], values.get("Content-Type"))
        self.assertEqual(1, len(values.get("Content-Length", [])))
        self.assertEqual(["no-store"], values.get("Cache-Control"))
        self.assertEqual(["nosniff"], values.get("X-Content-Type-Options"))
        self.assertEqual(["DENY"], values.get("X-Frame-Options"))
        self.assertEqual(["close"], values.get("Connection"))
        self.assertIn("frame-ancestors 'none'", values["Content-Security-Policy"][0])
        self.assertNotIn("Access-Control-Allow-Origin", values)


if __name__ == "__main__":
    unittest.main()
