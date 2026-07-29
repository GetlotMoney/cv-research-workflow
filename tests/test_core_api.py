from __future__ import annotations

import unittest
from unittest import mock

from app.api import CoreApi


class CoreApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = mock.Mock()
        self.api = CoreApi(self.service)

    def test_research_routes_call_the_core_service(self) -> None:
        self.service.scan_framework.return_value = {"status": "scanned"}
        self.service.prepare_framework_draft.return_value = {"status": "draft"}
        self.service.create_repository.return_value = {"created": True}
        self.service.run_experiment.return_value = {"id": "run-001-seed-31"}

        scanned = self.api.dispatch(
            "POST",
            "/api/frameworks/scan",
            {"source_repository": "C:/external"},
        )
        drafted = self.api.dispatch(
            "POST",
            "/api/frameworks/prepare",
            {
                "source_repository": "C:/external",
                "slug": "external-gzsl",
                "component_map": {"data": ["data.py"]},
            },
        )
        created = self.api.dispatch(
            "POST",
            "/api/repositories/create",
            {"name": "test-gzsl", "direction": "gzsl"},
        )
        run = self.api.dispatch(
            "POST",
            "/api/runs/execute",
            {
                "repository": "test-gzsl",
                "experiment_id": "tune-001-lr",
                "worktree": "C:/repo/.worktrees/tune-001-lr",
                "config": {"device": "cuda"},
                "seed": 31,
                "purpose": "evidence",
            },
        )

        self.assertEqual("scanned", scanned["status"])
        self.assertEqual("draft", drafted["status"])
        self.assertEqual({"created": True}, created)
        self.assertEqual("run-001-seed-31", run["id"])
        self.service.scan_framework.assert_called_once_with("C:/external")
        self.service.prepare_framework_draft.assert_called_once()

    def test_paperflow_routes_are_not_in_research_phase(self) -> None:
        for route in ("/api/papers/create", "/api/papers/import-package"):
            with self.subTest(route=route):
                with self.assertRaises(KeyError):
                    self.api.dispatch("POST", route, {})

    def test_unknown_route_fails_closed(self) -> None:
        with self.assertRaises(KeyError):
            self.api.dispatch("POST", "/api/unknown", {})


if __name__ == "__main__":
    unittest.main()
