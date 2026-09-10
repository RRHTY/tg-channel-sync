import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_release import (
    artifact_basename,
    create_release_archive,
    detect_platform_tag,
    validate_release_archive,
)
from scripts.prepare_release import prepare_release_assets


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReleaseBuildTests(unittest.TestCase):
    def test_detects_supported_x64_platforms(self):
        self.assertEqual(detect_platform_tag("win32", "AMD64", 64), "windows-x64")
        self.assertEqual(detect_platform_tag("linux", "x86_64", 64), "linux-x64")

    def test_rejects_unsupported_platform_or_architecture(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported platform"):
            detect_platform_tag("darwin", "arm64", 64)
        with self.assertRaisesRegex(RuntimeError, "64-bit x64"):
            detect_platform_tag("linux", "aarch64", 64)

    def test_artifact_name_includes_version_and_platform(self):
        self.assertEqual(
            artifact_basename("v0.5.2", "windows-x64"),
            "tg-channel-sync-v0.5.2-windows-x64",
        )

    def test_archive_contains_only_named_directory_and_binary(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
            root = Path(temp_dir)
            binary = root / "tg-channel-sync-v0.5.2-linux-x64"
            binary.write_bytes(b"binary")
            archive = root / "tg-channel-sync-v0.5.2-linux-x64.zip"

            create_release_archive(binary, archive, "tg-channel-sync-v0.5.2-linux-x64")
            result = validate_release_archive(
                archive,
                "tg-channel-sync-v0.5.2-linux-x64",
                "tg-channel-sync-v0.5.2-linux-x64",
            )

            self.assertEqual(result["binary_size"], 6)
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(
                    package.namelist(),
                    [
                        "tg-channel-sync-v0.5.2-linux-x64/",
                        "tg-channel-sync-v0.5.2-linux-x64/tg-channel-sync-v0.5.2-linux-x64",
                    ],
                )
                mode = package.getinfo(package.namelist()[1]).external_attr >> 16
                self.assertTrue(mode & 0o111)

    def test_archive_validation_rejects_extra_files(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
            archive = Path(temp_dir) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("release/app", b"binary")
                package.writestr("release/config.json", b"{}")

            with self.assertRaisesRegex(RuntimeError, "unexpected archive entries"):
                validate_release_archive(archive, "release", "app")

    def test_prepare_release_verifies_both_platforms_and_writes_combined_checksums(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
            root = Path(temp_dir)
            expected_names = []
            for platform_tag, suffix in (("windows-x64", ".exe"), ("linux-x64", "")):
                basename = artifact_basename("v0.5.2", platform_tag)
                binary = root / f"{basename}{suffix}"
                binary.write_bytes(platform_tag.encode())
                archive = root / f"{basename}.zip"
                create_release_archive(binary, archive, basename)
                digest = __import__("hashlib").sha256(archive.read_bytes()).hexdigest()
                (root / f"{basename}.sha256").write_text(
                    f"{digest}  {archive.name}\n",
                    encoding="utf-8",
                )
                expected_names.append(archive.name)

            checksum_file = prepare_release_assets(root, "v0.5.2", "v0.5.2")

            self.assertEqual(checksum_file.name, "SHA256SUMS.txt")
            contents = checksum_file.read_text(encoding="utf-8")
            for name in expected_names:
                self.assertIn(name, contents)

    def test_prepare_release_rejects_tag_version_mismatch(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "temp") as temp_dir:
            with self.assertRaisesRegex(RuntimeError, "does not match VERSION"):
                prepare_release_assets(Path(temp_dir), "v0.5.2", "v0.5.1")


if __name__ == "__main__":
    unittest.main()
