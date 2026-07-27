from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
sys.path.insert(0, str(WORK))

from prepare_local_private import (  # noqa: E402
    LocalPrivatePreparationError,
    load_existing_local_candidates,
)


class ExistingLocalCandidateTests(unittest.TestCase):
    def test_valid_existing_cache_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps({"candidates": [1]}), encoding="utf-8")
            with patch(
                "prepare_local_private.validate_local_candidates",
                return_value=[],
            ):
                payload = load_existing_local_candidates(path)
        self.assertEqual(payload["candidates"], [1])

    def test_invalid_existing_cache_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text(json.dumps({"candidates": []}), encoding="utf-8")
            with patch(
                "prepare_local_private.validate_local_candidates",
                return_value=["invalid cache"],
            ):
                with self.assertRaises(LocalPrivatePreparationError):
                    load_existing_local_candidates(path)


if __name__ == "__main__":
    unittest.main()
