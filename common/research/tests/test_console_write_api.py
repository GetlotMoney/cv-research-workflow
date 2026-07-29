from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests._helpers import SCRIPTS

import sys

sys.path.insert(0, str(SCRIPTS))
from console_server import _paperflow_entrypoint, create_server  # noqa: E402
from workflow_core.project import init_project  # noqa: E402
from tests.test_research_brief import research_brief_manifest


class ConsoleWriteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "项目"
        init_project(self.project, "写入接口测试", layout="v2")
        self.server = create_server(
            self.project,
            write_token="test-write-token",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.port = self.server.server_address[1]

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(self, payload: object, token: str | None = None) -> tuple[int, dict[str, object]]:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
        }
        if token is not None:
            headers["X-Console-Write-Token"] = token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            connection.request("POST", "/api/actions", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_write_actions_require_the_startup_token_and_use_a_fixed_schema(self) -> None:
        status, payload = self._post({"action": "save_research_brief", "data": {}})
        self.assertEqual(403, status)
        self.assertEqual("invalid_write_token", payload["error"]["code"])

        status, payload = self._post({"action": "shell", "data": {}}, "test-write-token")
        self.assertEqual(400, status)
        self.assertEqual("invalid_action", payload["error"]["code"])

    def test_action_failures_return_fixed_errors_instead_of_dropping_connection(
        self,
    ) -> None:
        request = {
            "action": "save_research_brief",
            "data": research_brief_manifest(),
        }
        with mock.patch(
            "console_server.console_write_action",
            side_effect=PermissionError("private local path"),
        ):
            status, payload = self._post(request, "test-write-token")
        self.assertEqual(409, status)
        self.assertEqual("action_io_error", payload["error"]["code"])
        self.assertNotIn("private local path", json.dumps(payload))

        with mock.patch(
            "console_server.console_write_action",
            side_effect=Exception("private local stack"),
        ):
            status, payload = self._post(request, "test-write-token")
        self.assertEqual(500, status)
        self.assertEqual("action_internal_error", payload["error"]["code"])
        self.assertNotIn("private local stack", json.dumps(payload))

    def test_paperflow_entrypoint_only_accepts_tokenized_loopback_url(self) -> None:
        valid = "http://127.0.0.1:8766/#token=abcdefghijklmnop"
        self.assertEqual(valid, _paperflow_entrypoint(valid))
        invalid = [
            "http://127.0.0.1:0/#token=abcdefghijklmnop",
            "http://localhost:8766/#token=abcdefghijklmnop",
            "http://user@127.0.0.1:8766/#token=abcdefghijklmnop",
            "http://127.0.0.1:8766/?query=1#token=abcdefghijklmnop",
            "http://127.0.0.1:8766/work#token=abcdefghijklmnop",
            "http://127.0.0.1:8766/#token=short",
            "http://127.0.0.1:8766/#token=abcdefghijklmnop!",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _paperflow_entrypoint(value)

    def test_save_research_brief_writes_only_the_fixed_project_brief(self) -> None:
        data = research_brief_manifest()
        status, payload = self._post(
            {"action": "save_research_brief", "data": data},
            "test-write-token",
        )
        self.assertEqual(200, status)
        self.assertEqual("saved", payload["status"])
        self.assertEqual("BRIEF-0001", payload["result"]["brief_id"])
        brief = json.loads(
            (
                self.project
                / ".experiment-workflow"
                / "paper-packages"
                / "briefs"
                / "BRIEF-0001.json"
            ).read_text(encoding="utf-8")
        )
        for key, value in data.items():
            self.assertEqual(value, brief[key])
        self.assertFalse((self.project / "RESEARCH_BRIEF.json").exists())

    def test_save_research_brief_creates_a_new_revision_without_overwrite(self) -> None:
        original = research_brief_manifest()
        changed = {**original, "background": "不应覆盖"}
        status, first = self._post(
            {"action": "save_research_brief", "data": original},
            "test-write-token",
        )
        self.assertEqual(200, status)
        first_path = (
            self.project
            / ".experiment-workflow"
            / "paper-packages"
            / "briefs"
            / "BRIEF-0001.json"
        )
        first_bytes = first_path.read_bytes()

        status, payload = self._post(
            {"action": "save_research_brief", "data": changed},
            "test-write-token",
        )

        self.assertEqual(200, status)
        self.assertEqual("BRIEF-0002", payload["result"]["brief_id"])
        self.assertEqual(first_bytes, first_path.read_bytes())
        second = json.loads(
            (
                self.project
                / ".experiment-workflow"
                / "paper-packages"
                / "briefs"
                / "BRIEF-0002.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual("不应覆盖", second["background"])
        self.assertEqual("BRIEF-0001", first["result"]["brief_id"])

    def test_create_domain_repo_is_limited_to_the_project_repositories_directory(self) -> None:
        status, payload = self._post(
            {"action": "create_domain_repo", "data": {"direction": "cls", "name": "demo-cls"}},
            "test-write-token",
        )
        self.assertEqual(200, status)
        self.assertEqual("created", payload["status"])
        repository = self.project / "repositories" / "demo-cls"
        self.assertTrue(repository.is_dir())
        self.assertEqual("CB-0001", payload["result"]["codebase"]["id"])
        runtime_condition = next(
            item
            for item in payload["state"]["execution"]["conditions"]
            if item["id"] == "adapter_runtime"
        )
        self.assertEqual(
            {
                "id": "adapter_runtime",
                "status": "pass",
                "reason": "registered_codebase_available",
            },
            runtime_condition,
        )
        self.assertNotIn(
            "adapter_runtime",
            payload["state"]["execution"]["blocked_by"],
        )
        self.assertIn(
            "codebase:CB-0001",
            {
                node["id"]
                for node in payload["state"]["relationships"]["nodes"]
            },
        )
        status, payload = self._post(
            {"action": "create_domain_repo", "data": {"direction": "cls", "name": "demo-cls"}},
            "test-write-token",
        )
        self.assertEqual(409, status)
        self.assertEqual("action_conflict", payload["error"]["code"])

    def test_select_codebase_and_create_structured_task_use_real_records(self) -> None:
        status, created = self._post(
            {"action": "create_domain_repo", "data": {"direction": "cls", "name": "task-cls"}},
            "test-write-token",
        )
        self.assertEqual(200, status)
        codebase_id = created["result"]["codebase"]["id"]
        status, selected = self._post(
            {"action": "select_codebase", "data": {"codebase_id": codebase_id}},
            "test-write-token",
        )
        self.assertEqual(200, status)
        self.assertEqual("selected", selected["status"])
        binding = {
            key: selected["result"][key]
            for key in ("codebase_id", "branch", "commit", "tag")
        }
        task = {
            "owner_request": "跑一个受限调试任务",
            "route": "tune",
            "target_refs": ["PACK-CLS"],
            "route_inputs": {
                "config": {"mode": "synthetic_smoke"},
                "debug_required": True,
                "changes_code_behavior": False,
                "primary_metric": "top1_accuracy",
                "code_binding": binding,
            },
            "budget": {"max_runs": 1}, "stop_condition": {"type": "max_runs", "value": 1},
        }
        status, payload = self._post(
            {"action": "create_task", "data": task}, "test-write-token",
        )
        self.assertEqual(200, status)
        self.assertEqual("created", payload["status"])
        self.assertEqual("TASK-0001", payload["result"]["id"])

    def test_project_actions_create_and_open_only_direct_sibling_projects(
        self,
    ) -> None:
        status, created = self._post(
            {
                "action": "create_project",
                "data": {
                    "directory_name": "目标检测项目",
                    "display_name": "目标检测研究",
                },
            },
            "test-write-token",
        )
        self.assertEqual(200, status)
        self.assertEqual("created", created["status"])
        self.assertEqual("目标检测研究", created["result"]["name"])
        new_project = self.project.parent / "目标检测项目"
        self.assertTrue((new_project / ".experiment-workflow").is_dir())
        self.assertEqual(
            "目标检测研究",
            created["state"]["start"]["project"]["name"],
        )

        status, opened = self._post(
            {
                "action": "open_project",
                "data": {"directory_name": self.project.name},
            },
            "test-write-token",
        )
        self.assertEqual(200, status)
        self.assertEqual("opened", opened["status"])
        self.assertEqual(
            "写入接口测试",
            opened["state"]["start"]["project"]["name"],
        )

        before = sorted(path.name for path in self.project.parent.iterdir())
        for invalid in ("../escape", "CON", "bad:name", "tail."):
            with self.subTest(invalid=invalid):
                status, payload = self._post(
                    {
                        "action": "create_project",
                        "data": {
                            "directory_name": invalid,
                            "display_name": "拒绝",
                        },
                    },
                    "test-write-token",
                )
                self.assertEqual(400, status)
                self.assertEqual(
                    "invalid_action_data",
                    payload["error"]["code"],
                )
                self.assertEqual(
                    before,
                    sorted(path.name for path in self.project.parent.iterdir()),
                )

    def test_register_source_then_save_idea_uses_real_catalog_records(self) -> None:
        paper = self.project.parent / "reference.pdf"
        paper.write_bytes(b"%PDF-1.4\nfixed test source\n")
        digest = "sha256:" + hashlib.sha256(paper.read_bytes()).hexdigest()
        source_data = {
            "source_path": str(paper.resolve()),
            "kind": "paper",
            "identity": "test-paper",
            "locator": str(paper.resolve()),
            "revision": "v1",
            "commit": None,
            "digest": digest,
            "license": "test-only",
        }
        status, source = self._post(
            {"action": "register_source", "data": source_data},
            "test-write-token",
        )
        self.assertEqual(200, status)
        self.assertEqual("SRC-0001", source["result"]["id"])
        self.assertNotIn("locator", source["result"])
        idea_data = {
            "status": "ready",
            "problem": "检测小目标时容易漏检。",
            "mechanism": "加入多尺度特征融合。",
            "falsifiable_hypothesis": "若小目标召回率没有改善，则假设不成立。",
            "source_refs": ["SRC-0001"],
            "evidence_refs": ["SRC-0001"],
            "source_links": [
                {
                    "source_ref": "SRC-0001",
                    "locator": "page:1",
                    "supports_field": "problem",
                    "claim": "该来源说明小目标检测仍存在困难。",
                }
            ],
        }
        status, idea = self._post(
            {"action": "save_idea", "data": idea_data},
            "test-write-token",
        )
        self.assertEqual(200, status)
        self.assertEqual("IDEA-0001", idea["result"]["id"])


if __name__ == "__main__":
    unittest.main()
