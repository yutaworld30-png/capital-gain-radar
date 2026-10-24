from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work"))
from rollback_private_archive import select_target  # noqa: E402


class RollbackPrivateArchiveTests(unittest.TestCase):
    def test_only_verified_production_deployment_is_selected(self) -> None:
        target = {"id": "00000000-0000-4000-8000-000000000001", "short_id": "81983c8d",
                  "environment": "production", "latest_stage": {"status": "success"}}
        preview = {**target, "id": "00000000-0000-4000-8000-000000000002", "environment": "preview"}
        self.assertEqual(select_target([preview, target]), target)

    def test_missing_failed_or_ambiguous_target_is_rejected(self) -> None:
        target = {"id": "00000000-0000-4000-8000-000000000001", "short_id": "81983c8d",
                  "environment": "production", "latest_stage": {"status": "success"}}
        for deployments in ([], [{**target, "latest_stage": {"status": "failure"}}], [target, target]):
            with self.subTest(deployments=deployments), self.assertRaises(RuntimeError):
                select_target(deployments)


if __name__ == "__main__":
    unittest.main()
