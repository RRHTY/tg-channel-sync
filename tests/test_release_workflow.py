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
        self.assertIn('release_assets="$(gh release view', workflow)
        self.assertIn('done <<< "$release_assets"', workflow)
        self.assertNotIn("for asset in $(gh release view", workflow)
        self.assertNotIn("gh release edit \"$RELEASE_TAG\" --title \"$RELEASE_TAG\" --notes-file \"$notes\" --latest", workflow)
        self.assertNotIn("dist-release/*.zip", workflow)
        self.assertNotIn("pull_request_target", workflow)

        dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("dist-release/", dockerignore)

    def test_v053_docs_describe_native_downloads_without_legacy_packages(self):
        version = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        release_notes = (PROJECT_ROOT / "docs/releases/v0.5.3.md").read_text(encoding="utf-8")

        self.assertEqual(version, "v0.5.3")
        self.assertIn("tg-channel-sync-v0.5.3-windows-x64.zip", readme)
        self.assertIn("tg-channel-sync-v0.5.3-linux-x64.zip", readme)
        self.assertIn("cd tg-channel-sync-v0.5.3-linux-x64", readme)
        self.assertIn("Docker Compose", readme)
        self.assertNotIn("windows-x64-portable.zip", readme)
        self.assertNotIn("windows-x64-full.zip", readme)
        self.assertNotIn("build-portable.ps1", readme)
        self.assertIn("下载与你系统对应的压缩包", release_notes)
        self.assertIn("### 稳定性与正确性", release_notes)
        self.assertIn("### 界面与操作", release_notes)


if __name__ == "__main__":
    unittest.main()
