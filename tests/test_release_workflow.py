import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_release_workflow_has_native_matrix_and_gated_release(self):
        workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

        self.assertIn("windows-latest", workflow)
        self.assertIn("ubuntu-22.04", workflow)
        self.assertIn("python scripts/build_release.py", workflow)
        self.assertIn("python scripts/prepare_release.py", workflow)
        self.assertIn("needs: build", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("gh release create", workflow)
        self.assertNotIn("pull_request_target", workflow)


if __name__ == "__main__":
    unittest.main()
