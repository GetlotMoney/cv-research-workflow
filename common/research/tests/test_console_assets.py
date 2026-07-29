from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

from tests._helpers import ROOT


ASSET_ROOT = ROOT / "skills" / "cv-experiment-workflow" / "assets" / "console"


class _DocumentProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.inline_scripts: list[str] = []
        self._inside_inline_script = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if tag == "script" and not attributes.get("src"):
            self._inside_inline_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside_inline_script = False

    def handle_data(self, data: str) -> None:
        if self._inside_inline_script and data.strip():
            self.inline_scripts.append(data)


class ConsoleAssetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
        cls.css = (ASSET_ROOT / "styles.css").read_text(encoding="utf-8")
        cls.javascript = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
        cls.document = _DocumentProbe()
        cls.document.feed(cls.html)

    def _tag(
        self,
        tag_name: str,
        *,
        identifier: str | None = None,
    ) -> dict[str, str | None]:
        for tag, attributes in self.document.tags:
            if tag == tag_name and (
                identifier is None or attributes.get("id") == identifier
            ):
                return attributes
        self.fail(f"缺少 <{tag_name}>#{identifier or ''}")

    def test_three_hash_views_have_clear_navigation_and_semantics(self) -> None:
        navigation_targets = {
            attributes.get("href")
            for tag, attributes in self.document.tags
            if tag == "a" and attributes.get("data-nav") == "view"
        }
        self.assertEqual(
            {"#start", "#relationships", "#confirm"},
            navigation_targets,
        )
        for identifier in ("start", "relationships", "confirm"):
            section = self._tag("section", identifier=identifier)
            self.assertEqual("view", section.get("data-view"))
            self.assertIsNotNone(section.get("aria-labelledby"))
        self.assertIn("开始与设置", self.html)
        self.assertIn("关系图", self.html)
        self.assertIn("执行确认", self.html)
        self.assertIn("hashchange", self.javascript)

    def test_start_view_exposes_route_and_required_input_feedback(self) -> None:
        form = self._tag("form", identifier="intake-form")
        self.assertEqual("/api/state", form.get("data-endpoint"))
        self.assertIn("novalidate", form)
        intent = self._tag("select", identifier="intent")
        self.assertEqual("intent", intent.get("name"))
        self.assertEqual("intent-help", intent.get("aria-describedby"))
        for identifier in (
            "route-help",
            "provided-fields",
            "required-fields",
            "intake-summary",
            "page-status",
            "page-error",
        ):
            self._tag("div", identifier=identifier)
        self.assertIn('aria-live="polite"', self.html)
        self.assertIn('aria-live="assertive"', self.html)
        self.assertIn('setAttribute("aria-required", "true")', self.javascript)
        self.assertNotRegex(self.javascript, r"\.required\s*=")

    def test_relationship_view_has_real_svg_and_accessible_list_fallback(
        self,
    ) -> None:
        graph = self._tag("svg", identifier="graph-canvas")
        self.assertEqual("img", graph.get("role"))
        self.assertTrue(graph.get("aria-label"))
        graph_list = self._tag("ul", identifier="graph-list")
        self.assertEqual("list", graph_list.get("role"))
        edge_list = self._tag("ul", identifier="graph-edge-list")
        self.assertEqual("list", edge_list.get("role"))
        self._tag("div", identifier="graph-empty")
        self.assertRegex(
            self.javascript,
            r"relationships\s*\.\s*graph|relationships\?\.\s*graph",
        )
        self.assertIn("createElementNS", self.javascript)
        self.assertIn(".nodes", self.javascript)
        self.assertIn(".edges", self.javascript)
        self.assertIn("`${edge.from} → ${edge.to}`", self.javascript)
        self.assertIn("edge.kind", self.javascript)

    def test_machine_status_values_are_translated_before_rendering(self) -> None:
        expected_labels = {
            "current": "当前版本可信",
            "trusted_previous": "已识别旧版本，可安全查看",
            "worktree_dirty": "代码仓库有尚未提交的改动",
            "registered_codebase_available": "已登记 Codebase，运行时再核对 Git",
            "pass": "通过",
            "block": "未通过",
            "not_checked": "未检查",
        }
        for machine_value, chinese_label in expected_labels.items():
            with self.subTest(machine_value=machine_value):
                self.assertRegex(
                    self.javascript,
                    (
                        rf"(?<![A-Za-z0-9_]){re.escape(machine_value)}"
                        rf"\s*:\s*[\"']{re.escape(chinese_label)}[\"']"
                    ),
                )
        self.assertIn("humanWorkflowTrust(workflow?.trust)", self.javascript)
        self.assertIn("humanAdapterState(adapter)", self.javascript)
        self.assertIn("humanConditionStatus(condition.status)", self.javascript)
        self.assertGreaterEqual(
            self.javascript.count("humanGraphStatus(node)"),
            3,
        )
        self.assertIn("未识别版本状态", self.javascript)
        self.assertIn("代码现场状态未知", self.javascript)
        self.assertIn("未识别检查状态", self.javascript)

    def test_graph_machine_kinds_are_translated_for_every_visible_surface(
        self,
    ) -> None:
        expected_kinds = {
            "adapter_repo": "代码仓库",
            "project_skill": "项目工作流入口",
            "workflow": "通用工作流",
            "ledger": "实验账本",
            "source": "资料来源",
            "idea": "研究想法",
            "template": "代码模板",
            "module": "实验模块",
            "task": "实验任务",
            "run": "实验运行",
            "evidence": "实验依据",
        }
        expected_edges = {
            "adapter_repo_supplies_project": "代码仓库提供项目代码",
            "project_uses_workflow": "项目使用工作流",
            "workflow_manages_ledger": "工作流管理实验账本",
            "source_supports_idea": "资料支持研究想法",
            "source_supports_template": "资料支持代码模板",
            "source_supports_module": "资料支持实验模块",
            "idea_defines_module": "研究想法定义实验模块",
            "template_hosts_module": "代码模板承载实验模块",
            "catalog_ref_feeds_task": "登记对象作为任务输入",
            "task_prerequisite": "前置任务",
            "prior_run_feeds_task": "已有运行作为任务输入",
            "task_produces_run": "任务产生实验运行",
            "run_has_evidence": "运行产生实验依据",
            "run_supports_evidence": "运行支持实验依据",
        }
        for machine_value, chinese_label in {
            **expected_kinds,
            **expected_edges,
        }.items():
            with self.subTest(machine_value=machine_value):
                self.assertRegex(
                    self.javascript,
                    rf"{machine_value}\s*:\s*[\"']{re.escape(chinese_label)}[\"']",
                )
        self.assertGreaterEqual(
            self.javascript.count("humanNodeKind(node.kind)"),
            3,
        )
        self.assertGreaterEqual(
            self.javascript.count("humanEdgeKind(edge.kind)"),
            2,
        )
        self.assertIn("未识别对象", self.javascript)
        self.assertIn("未识别关系", self.javascript)

    def test_execution_routes_back_to_the_fixed_action_area(self) -> None:
        action = self._tag("button", identifier="execution-action")
        self.assertNotIn("disabled", action)
        self.assertNotIn("aria-disabled", action)
        self.assertIn("本机受控操作", self.html)
        self.assertIn("不会删除、push 或发布", self.html)
        self._tag("ol", identifier="condition-list")
        self._tag("div", identifier="blocked-summary")

    def test_assets_follow_strict_csp_without_external_or_inline_resources(
        self,
    ) -> None:
        self.assertFalse(self.document.inline_scripts)
        self.assertNotIn("<style", self.html.lower())
        self.assertFalse(
            [
                attributes["style"]
                for _, attributes in self.document.tags
                if attributes.get("style")
            ]
        )
        resource_urls = [
            attributes[name]
            for tag, attributes in self.document.tags
            for name in ("href", "src")
            if name in attributes
            and attributes[name]
            and not (
                tag == "a"
                and str(attributes[name]).startswith("#")
            )
        ]
        self.assertEqual(["data:,", "/styles.css", "/app.js"], resource_urls)
        favicon = next(
            attributes
            for tag, attributes in self.document.tags
            if tag == "link" and attributes.get("rel") == "icon"
        )
        self.assertEqual("data:,", favicon.get("href"))
        for text in (self.html, self.css, self.javascript):
            self.assertNotRegex(text, r"https?://|//cdn\.|@import\s+url")
        self.assertNotRegex(self.css, r"#(?:[a-fA-F0-9]{2})?800080")

    def test_javascript_uses_fixed_state_and_write_endpoints_only(self) -> None:
        self.assertIn('const API_ENDPOINT = "/api/state"', self.javascript)
        self.assertRegex(
            self.javascript,
            r"fetch\(API_ENDPOINT(?:,\s*\{)?",
        )
        self.assertIn('method: "POST"', self.javascript)
        self.assertRegex(
            self.javascript,
            r"JSON\.stringify\(\{\s*intent,\s*provided\s*\}\)",
        )
        api_paths = set(re.findall(r'["\'](/api/[^"\']+)["\']', self.javascript))
        self.assertEqual({"/api/state", "/api/actions"}, api_paths)
        self.assertNotRegex(
            self.javascript,
            r'method\s*:\s*["\'](?:PUT|PATCH|DELETE)["\']',
        )
        self.assertNotRegex(
            self.javascript.lower(),
            r"/api/(?:run|train|task|git|push|commit|execute)",
        )
        self.assertIn("payload.error.message", self.javascript)
        self.assertNotRegex(
            self.javascript,
            r"\b(?:innerHTML|outerHTML|insertAdjacentHTML)\b",
        )

    def test_unified_entry_exposes_six_directions_and_the_research_chain(self) -> None:
        for value in ("DET", "CLS", "SEG", "INSTSEG", "SR", "GZSL"):
            with self.subTest(value=value):
                self.assertIn(value, self.html)
        for action in (
            "create_project",
            "open_project",
            "register_source",
            "seal_run_outputs",
            "confirm_run_evidence",
            "freeze_research_package",
        ):
            self.assertIn(f'value="{action}"', self.html)
        link = self._tag("a", identifier="paperflow-link")
        self.assertEqual("_blank", link.get("target"))
        self.assertEqual("noreferrer", link.get("rel"))
        self.assertIn("validatedPaperFlowUrl", self.javascript)
        self.assertIn(r"127\.0\.0\.1", self.javascript)
        self.assertIn(
            r"^http:\/\/127\.0\.0\.1:",
            self.javascript,
        )
        self.assertIn("project-instances", self.html)
        self.assertIn(
            (
                "Project → Research Brief → Idea → Codebase → Task → Run "
                "→ Evidence → Research Package → PaperFlow"
            ),
            self.html,
        )
        for value in (
            "Research Brief", "Idea", "Codebase", "Task", "Run",
            "Evidence", "Research Package", "PaperFlow",
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.html)

    def test_css_supports_keyboard_wide_narrow_and_reduced_motion(self) -> None:
        self.assertIn(":focus-visible", self.css)
        self.assertRegex(self.css, r"@media\s*\([^)]*max-width")
        self.assertIn("prefers-reduced-motion", self.css)
        self.assertIn("font-family", self.css)
        self.assertIn("background-image", self.css)

    def test_paperflow_url_runtime_accepts_every_valid_port_including_80(
        self,
    ) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("当前主机没有 Node.js")
        script_path = (
            ROOT
            / "skills"
            / "cv-experiment-workflow"
            / "assets"
            / "console"
            / "app.js"
        )
        probe = r"""
const fs = require("fs");
const vm = require("vm");
const context = {document: {addEventListener() {}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
const token = "abcdefghijklmnop";
const valid = [1, 80, 65535].map(
  (port) => vm.runInContext(
    `validatedPaperFlowUrl("http://127.0.0.1:${port}/#token=${token}")`,
    context,
  ),
);
const invalid = [
  "http://127.0.0.1:0/#token=abcdefghijklmnop",
  "http://127.0.0.1:65536/#token=abcdefghijklmnop",
  "http://localhost:80/#token=abcdefghijklmnop",
  "http://user@127.0.0.1:80/#token=abcdefghijklmnop",
  "http://127.0.0.1:80/?x=1#token=abcdefghijklmnop",
].map((value) => vm.runInContext(
  `validatedPaperFlowUrl(${JSON.stringify(value)})`,
  context,
));
if (valid.some((value) => value === null) || invalid.some((value) => value !== null)) {
  process.exit(1);
}
"""
        result = subprocess.run(
            [node, "-e", probe, str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
