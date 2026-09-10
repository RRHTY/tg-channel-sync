from __future__ import annotations

import argparse
from pathlib import Path

if __package__:
    from .build_release import artifact_basename, sha256_file, validate_release_archive
else:
    from build_release import artifact_basename, sha256_file, validate_release_archive


PLATFORMS = (("windows-x64", ".exe"), ("linux-x64", ""))


def prepare_release_assets(assets_dir: Path, tag: str, local_version: str) -> Path:
    normalized_tag = tag.strip()
    normalized_version = local_version.strip()
    if normalized_tag != normalized_version:
        raise RuntimeError(f"release tag {normalized_tag!r} does not match VERSION {normalized_version!r}")

    expected_archives = {
        f"{artifact_basename(normalized_version, platform_tag)}.zip" for platform_tag, _ in PLATFORMS
    }
    expected_sidecars = {
        f"{artifact_basename(normalized_version, platform_tag)}.sha256" for platform_tag, _ in PLATFORMS
    }
    actual_archives = {path.name for path in assets_dir.glob("*.zip")}
    actual_sidecars = {path.name for path in assets_dir.glob("*.sha256")}
    if actual_archives != expected_archives or actual_sidecars != expected_sidecars:
        raise RuntimeError(
            "unexpected release assets: "
            f"archives={sorted(actual_archives)!r}, checksums={sorted(actual_sidecars)!r}"
        )

    checksum_lines: list[str] = []
    for platform_tag, executable_suffix in PLATFORMS:
        basename = artifact_basename(normalized_version, platform_tag)
        archive = assets_dir / f"{basename}.zip"
        sidecar = assets_dir / f"{basename}.sha256"
        executable_name = f"{basename}{executable_suffix}"
        if not archive.is_file() or not sidecar.is_file():
            raise RuntimeError(f"missing release asset or checksum for {platform_tag}")

        validate_release_archive(archive, basename, executable_name)
        actual_digest = sha256_file(archive)
        sidecar_parts = sidecar.read_text(encoding="utf-8").strip().split()
        if sidecar_parts != [actual_digest, archive.name]:
            raise RuntimeError(f"checksum mismatch for {archive.name}")
        checksum_lines.append(f"{actual_digest}  {archive.name}")

    checksum_file = assets_dir / "SHA256SUMS.txt"
    checksum_file.write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    return checksum_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify native archives and create SHA256SUMS.txt.")
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version-file", type=Path, default=Path("VERSION"))
    args = parser.parse_args()

    version = args.version_file.read_text(encoding="utf-8").strip()
    checksum_file = prepare_release_assets(args.assets_dir.resolve(), args.tag, version)
    print(f"Verified release assets; checksums: {checksum_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
