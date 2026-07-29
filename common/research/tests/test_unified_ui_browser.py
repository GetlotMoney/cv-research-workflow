from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from tests._helpers import SCRIPTS


sys.path.insert(0, str(SCRIPTS))
from console_server import create_server  # noqa: E402
from workflow_core.project import init_project  # noqa: E402

try:
    from playwright.sync_api import Page, Route, sync_playwright
except ImportError:  # pragma: no cover - exercised only on an unprepared host
    Page = Any  # type: ignore[assignment,misc]
    Route = Any  # type: ignore[assignment,misc]
    sync_playwright = None


_PAPERFLOW_TOKEN = "paperflow-browser-test-token-20260727"
_SCREENSHOT_OUTPUT_ENV = "CV_WORKFLOW_BROWSER_SCREENSHOT_OUT"


def _configured_screenshot_output(
    environment: Mapping[str, str],
) -> Path | None:
    raw_path = environment.get(_SCREENSHOT_OUTPUT_ENV, "").strip()
    if not raw_path:
        return None
    output = Path(raw_path)
    if not output.is_absolute():
        raise ValueError(f"{_SCREENSHOT_OUTPUT_ENV} 必须使用绝对路径")
    if not output.parent.is_dir():
        raise ValueError(f"截图目标目录不存在：{output.parent}")
    return output


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"截图不是有效 PNG：{path}")
    return (
        int.from_bytes(header[16:20], byteorder="big"),
        int.from_bytes(header[20:24], byteorder="big"),
    )


class _PaperFlowTestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = (
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<title>PaperFlow 测试入口</title>"
            "<body><main id='paperflow-test'>PaperFlow 本机测试入口</main></body>"
            "</html>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _installed_windows_browser() -> Path | None:
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    program_files_x86 = Path(
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    )
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    )
    candidates = (
        program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        local_app_data / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
        program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _start_server(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    return thread


def _stop_server(
    server: ThreadingHTTPServer,
    thread: threading.Thread,
) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


def _submit_action(
    page: Page,
    action: str,
    data: dict[str, object],
    expected_text: str,
) -> None:
    page.locator("#write-action").select_option(action)
    page.locator("#write-action-data").fill(
        json.dumps(data, ensure_ascii=False, indent=2)
    )
    previous_result = page.locator("#write-action-result").text_content()
    page.locator("#write-action-submit").click()
    page.wait_for_function(
        """previous => {
          const result = document.getElementById("write-action-result");
          const button = document.getElementById("write-action-submit");
          return Boolean(
            result
              && button
              && !button.disabled
              && result.textContent !== previous
          );
        }""",
        arg=previous_result,
        timeout=60_000,
    )
    actual_result = page.locator("#write-action-result").text_content() or ""
    if expected_text not in actual_result:
        raise AssertionError(
            f"动作 {action} 没有得到预期页面结果："
            f"expected={expected_text!r}, actual={actual_result!r}"
        )


class UnifiedUiBrowserTests(unittest.TestCase):
    def test_project_screenshot_export_is_explicit_and_absolute(self) -> None:
        self.assertIsNone(_configured_screenshot_output({}))
        self.assertIsNone(
            _configured_screenshot_output({_SCREENSHOT_OUTPUT_ENV: "  "})
        )
        with self.assertRaisesRegex(ValueError, "必须使用绝对路径"):
            _configured_screenshot_output(
                {_SCREENSHOT_OUTPUT_ENV: "artifacts/final.png"}
            )
        absolute_output = Path.cwd() / "final-ui.png"
        self.assertEqual(
            absolute_output,
            _configured_screenshot_output(
                {_SCREENSHOT_OUTPUT_ENV: str(absolute_output)}
            ),
        )

    def test_real_browser_runs_the_safe_unified_entry_flow(self) -> None:
        if sys.platform != "win32":
            self.skipTest("这条验收只验证当前 Windows 主流程")
        self.assertIsNotNone(
            sync_playwright,
            "Windows 浏览器验收需要本机已有的 Python Playwright，测试不会联网安装",
        )
        browser_executable = _installed_windows_browser()
        self.assertIsNotNone(
            browser_executable,
            "没有找到已安装的 Edge 或 Chrome，不能把真实浏览器验收标成通过",
        )
        project_screenshot_output = _configured_screenshot_output(os.environ)

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            instances_root = temporary_root / "科研项目"
            instances_root.mkdir()
            initial_project = instances_root / "起始项目"
            init_project(initial_project, "浏览器起始项目", layout="v2")

            paperflow_server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                _PaperFlowTestHandler,
            )
            paperflow_thread = _start_server(paperflow_server)
            self.addCleanup(
                _stop_server,
                paperflow_server,
                paperflow_thread,
            )
            paperflow_url = (
                f"http://127.0.0.1:{paperflow_server.server_address[1]}/"
                f"#token={_PAPERFLOW_TOKEN}"
            )

            console_server = create_server(
                initial_project,
                write_token="unified-browser-write-token",
                paperflow_entrypoint=paperflow_url,
                instances_root=instances_root,
            )
            console_thread = _start_server(console_server)
            self.addCleanup(
                _stop_server,
                console_server,
                console_thread,
            )
            console_url = (
                f"http://127.0.0.1:{console_server.server_address[1]}/"
            )

            action_requests: list[str] = []
            external_requests: list[str] = []
            page_errors: list[str] = []
            console_errors: list[str] = []
            screenshot_root = temporary_root / "browser-artifacts"
            screenshot_root.mkdir()
            screenshot_path = screenshot_root / "unified-entry.png"

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=str(browser_executable),
                    headless=True,
                    args=[
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-default-apps",
                        "--no-default-browser-check",
                        "--no-first-run",
                    ],
                )
                try:
                    context = browser.new_context(
                        locale="zh-CN",
                        viewport={"width": 1440, "height": 1000},
                    )

                    def route_local_only(route: Route) -> None:
                        target = urlsplit(route.request.url)
                        if target.scheme == "data" or target.hostname == "127.0.0.1":
                            route.continue_()
                            return
                        external_requests.append(route.request.url)
                        route.abort()

                    context.route("**/*", route_local_only)
                    page = context.new_page()
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.on(
                        "console",
                        lambda message: (
                            console_errors.append(message.text)
                            if message.type == "error"
                            else None
                        ),
                    )

                    def remember_action(request: Any) -> None:
                        if urlsplit(request.url).path != "/api/actions":
                            return
                        payload = request.post_data_json
                        if isinstance(payload, dict):
                            action_requests.append(str(payload.get("action")))

                    page.on("request", remember_action)
                    page.goto(console_url, wait_until="domcontentloaded")
                    page.wait_for_function(
                        """() => {
                          const root = document.getElementById("app-root");
                          const button = document.getElementById("write-action-submit");
                          return root?.getAttribute("aria-busy") === "false"
                            && Boolean(button && !button.disabled);
                        }""",
                        timeout=30_000,
                    )

                    self.assertEqual(
                        ["DET", "CLS", "SEG", "INSTSEG", "SR", "GZSL"],
                        page.locator(
                            "[aria-label='六个科研方向'] span"
                        ).all_text_contents(),
                    )
                    self.assertEqual(
                        "浏览器起始项目",
                        page.locator("#project-name").text_content(),
                    )
                    action_result = page.locator("#write-action-result")
                    original_action_result = action_result.text_content()
                    action_result.evaluate(
                        """(node, value) => {
                          node.textContent = value;
                        }""",
                        "sha256:" + "a" * 1_000,
                    )
                    for viewport in (
                        {"width": 1280, "height": 900},
                        {"width": 900, "height": 900},
                        {"width": 390, "height": 844},
                    ):
                        with self.subTest(viewport=viewport):
                            page.set_viewport_size(viewport)
                            layout = page.evaluate(
                                """() => {
                                  const rect = selector => {
                                    const bounds = document
                                      .querySelector(selector)
                                      ?.getBoundingClientRect();
                                    return bounds
                                      ? {
                                          left: bounds.left,
                                          right: bounds.right,
                                          top: bounds.top,
                                          bottom: bounds.bottom,
                                        }
                                      : null;
                                  };
                                  return {
                                    viewportWidth: window.innerWidth,
                                    documentWidth:
                                      document.documentElement.scrollWidth,
                                    navigation: Array.from(
                                      document.querySelectorAll(
                                        ".view-nav [data-nav='view']",
                                      ),
                                    ).map(node => {
                                      const bounds =
                                        node.getBoundingClientRect();
                                      return {
                                        left: bounds.left,
                                        right: bounds.right,
                                      };
                                    }),
                                    project: rect(".project-strip"),
                                    intake: rect(".intake-panel"),
                                    actionResult: {
                                      clientWidth: document.querySelector(
                                        "#write-action-result",
                                      ).clientWidth,
                                      scrollWidth: document.querySelector(
                                        "#write-action-result",
                                      ).scrollWidth,
                                    },
                                  };
                                }"""
                            )
                            self.assertLessEqual(
                                layout["documentWidth"],
                                layout["viewportWidth"],
                                f"页面在 {viewport['width']}px 下发生横向裁切",
                            )
                            self.assertEqual(3, len(layout["navigation"]))
                            for bounds in layout["navigation"]:
                                self.assertGreaterEqual(bounds["left"], 0)
                                self.assertLessEqual(
                                    bounds["right"],
                                    layout["viewportWidth"],
                                )
                            self.assertLessEqual(
                                layout["actionResult"]["scrollWidth"],
                                layout["actionResult"]["clientWidth"],
                                "固定动作返回的长路径或标识也必须在面板内换行",
                            )
                            if viewport["width"] == 1280:
                                self.assertLessEqual(
                                    layout["project"]["right"],
                                    layout["intake"]["left"],
                                    "1280px 下应保留完整的左右双栏",
                                )
                            else:
                                self.assertLessEqual(
                                    layout["project"]["bottom"],
                                    layout["intake"]["top"],
                                    "窄屏应切成单栏，避免内容被截断",
                                )
                    action_result.evaluate(
                        """(node, value) => {
                          node.textContent = value;
                        }""",
                        original_action_result,
                    )
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    options = page.locator("#write-action option").evaluate_all(
                        "nodes => nodes.map(node => node.value)"
                    )
                    for required_action in (
                        "create_project",
                        "open_project",
                        "create_domain_repo",
                    ):
                        self.assertIn(required_action, options)
                    self.assertTrue(page.locator("#paperflow-link").is_hidden())

                    created_directory = "浏览器新项目"
                    created_display_name = "分类浏览器测试项目"
                    _submit_action(
                        page,
                        "create_project",
                        {
                            "directory_name": created_directory,
                            "display_name": created_display_name,
                        },
                        f"已新建并打开科研项目 {created_display_name}",
                    )
                    page.wait_for_function(
                        """name =>
                          document.getElementById("project-name")?.textContent === name
                        """,
                        arg=created_display_name,
                    )
                    created_project = instances_root / created_directory
                    self.assertTrue(
                        (created_project / ".experiment-workflow").is_dir()
                    )

                    _submit_action(
                        page,
                        "create_domain_repo",
                        {"direction": "cls", "name": "browser-cls"},
                        "已创建并登记 CB-0001",
                    )
                    repository = created_project / "repositories" / "browser-cls"
                    self.assertTrue((repository / ".git").is_dir())
                    self.assertTrue((repository / "domain-pack.json").is_file())
                    self.assertEqual(
                        created_display_name,
                        page.locator("#project-name").text_content(),
                    )

                    page.locator("#write-action").select_option("select_codebase")
                    selected_example = json.loads(
                        page.locator("#write-action-data").input_value()
                    )
                    self.assertEqual("CB-0001", selected_example["codebase_id"])

                    _submit_action(
                        page,
                        "open_project",
                        {"directory_name": initial_project.name},
                        f"已打开科研项目 {initial_project.name}",
                    )
                    page.wait_for_function(
                        """() =>
                          document.getElementById("project-name")?.textContent
                            === "浏览器起始项目"
                        """
                    )

                    self.assertTrue(page.locator("#paperflow-link").is_hidden())
                    page.evaluate(
                        """url => showPaperFlowLink({
                          status: "ready",
                          entrypoint: url,
                        })""",
                        paperflow_url,
                    )
                    paperflow_link = page.locator("#paperflow-link")
                    self.assertTrue(paperflow_link.is_visible())
                    self.assertEqual(
                        paperflow_url,
                        paperflow_link.get_attribute("href"),
                    )
                    self.assertEqual("_blank", paperflow_link.get_attribute("target"))
                    self.assertIn(
                        "noreferrer",
                        (paperflow_link.get_attribute("rel") or "").split(),
                    )

                    with page.expect_popup(timeout=10_000) as popup_info:
                        paperflow_link.click()
                    popup = popup_info.value
                    popup.wait_for_load_state("domcontentloaded")
                    self.assertEqual(paperflow_url, popup.url)
                    self.assertEqual(
                        "PaperFlow 本机测试入口",
                        popup.locator("#paperflow-test").text_content(),
                    )
                    popup.close()

                    page.evaluate(
                        """() => showPaperFlowLink({
                          status: "ready",
                          entrypoint: "https://example.com/#token=not-local",
                        })"""
                    )
                    self.assertTrue(paperflow_link.is_hidden())
                    self.assertIsNone(paperflow_link.get_attribute("href"))
                    page.evaluate(
                        """url => showPaperFlowLink({
                          status: "ready",
                          entrypoint: url,
                        })""",
                        paperflow_url,
                    )
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    self.assertGreater(screenshot_path.stat().st_size, 1_000)
                    if project_screenshot_output is not None:
                        page.set_viewport_size({"width": 1280, "height": 1000})
                        page.evaluate("() => window.scrollTo(0, 0)")
                        screenshot_layout = page.evaluate(
                            """() => {
                              const project = document
                                .querySelector(".project-strip")
                                .getBoundingClientRect();
                              const intake = document
                                .querySelector(".intake-panel")
                                .getBoundingClientRect();
                              return {
                                viewportWidth: window.innerWidth,
                                documentWidth:
                                  document.documentElement.scrollWidth,
                                navigation: Array.from(
                                  document.querySelectorAll(
                                    ".view-nav [data-nav='view']",
                                  ),
                                ).map(node => {
                                  const bounds = node.getBoundingClientRect();
                                  return {
                                    left: bounds.left,
                                    right: bounds.right,
                                  };
                                }),
                                projectRight: project.right,
                                intakeLeft: intake.left,
                              };
                            }"""
                        )
                        self.assertEqual(
                            1280,
                            screenshot_layout["viewportWidth"],
                        )
                        self.assertLessEqual(
                            screenshot_layout["documentWidth"],
                            screenshot_layout["viewportWidth"],
                        )
                        self.assertEqual(
                            3,
                            len(screenshot_layout["navigation"]),
                        )
                        for bounds in screenshot_layout["navigation"]:
                            self.assertGreaterEqual(bounds["left"], 0)
                            self.assertLessEqual(
                                bounds["right"],
                                screenshot_layout["viewportWidth"],
                            )
                        self.assertLessEqual(
                            screenshot_layout["projectRight"],
                            screenshot_layout["intakeLeft"],
                        )
                        page.screenshot(
                            path=str(project_screenshot_output),
                            full_page=True,
                            animations="disabled",
                        )
                        self.assertGreater(
                            project_screenshot_output.stat().st_size,
                            1_000,
                        )
                        screenshot_width, screenshot_height = _png_dimensions(
                            project_screenshot_output
                        )
                        self.assertEqual(1280, screenshot_width)
                        self.assertGreater(screenshot_height, 1_000)

                    context.close()
                finally:
                    browser.close()

            self.assertEqual(
                ["create_project", "create_domain_repo", "open_project"],
                action_requests,
            )
            self.assertEqual([], external_requests)
            self.assertEqual([], page_errors)
            self.assertEqual([], console_errors)
            self.assertFalse(
                any((created_project / ".experiment-workflow" / "runs").iterdir())
            )
            self.assertFalse(
                (created_project / "deliverables" / "paperflow").exists()
            )


if __name__ == "__main__":
    unittest.main()
