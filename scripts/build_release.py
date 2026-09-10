from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path


APP_NAME = "tg-channel-sync"
FORBIDDEN_ARCHIVE_NAMES = {"config.json", "data", "temp", ".codestable"}


def detect_platform_tag(system: str, machine: str, pointer_bits: int) -> str:
    normalized_machine = machine.strip().lower()
    if system not in {"win32", "linux"}:
        raise RuntimeError(f"unsupported platform: {system}")
    if pointer_bits != 64 or normalized_machine not in {"amd64", "x86_64"}:
        raise RuntimeError(
            f"release builds require 64-bit x64 Python; got {machine or 'unknown'} / {pointer_bits}-bit"
        )
    return "windows-x64" if system == "win32" else "linux-x64"


def artifact_basename(version: str, platform_tag: str) -> str:
    normalized_version = version.strip()
    if not normalized_version:
        raise RuntimeError("VERSION is empty")
    return f"{APP_NAME}-{normalized_version}-{platform_tag}"


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def create_release_archive(binary: Path, archive: Path, basename: str) -> None:
    binary = binary.resolve()
    archive = archive.resolve()
    if not binary.is_file() or binary.stat().st_size == 0:
        raise RuntimeError(f"built binary is missing or empty: {binary}")

    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        package.writestr(_zip_info(f"{basename}/", 0o40755), b"")
        binary_info = _zip_info(f"{basename}/{binary.name}", 0o100755)
        with binary.open("rb") as source, package.open(binary_info, "w") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)


def validate_release_archive(archive: Path, basename: str, executable_name: str) -> dict[str, int]:
    expected = [f"{basename}/", f"{basename}/{executable_name}"]
    with zipfile.ZipFile(archive) as package:
        actual = package.namelist()
        if actual != expected:
            raise RuntimeError(f"unexpected archive entries: {actual!r}; expected {expected!r}")

        for entry in actual:
            parts = {part.lower() for part in Path(entry).parts}
            forbidden = parts & FORBIDDEN_ARCHIVE_NAMES
            if forbidden:
                raise RuntimeError(f"forbidden archive entry: {entry}")

        binary_info = package.getinfo(expected[1])
        if binary_info.file_size <= 0:
            raise RuntimeError("archived binary is empty")
        if not (binary_info.external_attr >> 16) & 0o111:
            raise RuntimeError("archived binary is not marked executable")
        return {"binary_size": binary_info.file_size, "archive_entries": len(actual)}


def parse_bundle_smoke_output(output: str, expected_version: str) -> dict[str, object]:
    marker = "BUNDLE_SMOKE_OK "
    report_line = next((line for line in output.splitlines() if line.startswith(marker)), "")
    if not report_line:
        raise RuntimeError(f"bundle smoke did not report success: {output!r}")
    try:
        report = json.loads(report_line[len(marker) :])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bundle smoke returned invalid JSON: {report_line!r}") from exc
    if report.get("version") != expected_version:
        raise RuntimeError(
            f"bundle smoke reported version {report.get('version')!r}; expected {expected_version!r}"
        )
    return report


def run_archive_smoke(
    archive: Path,
    build_root: Path,
    basename: str,
    executable_name: str,
    expected_version: str,
) -> str:
    smoke_root = build_root / "smoke"
    with zipfile.ZipFile(archive) as package:
        package.extractall(smoke_root)
        binary_info = package.getinfo(f"{basename}/{executable_name}")

    executable = smoke_root / basename / executable_name
    executable.chmod((binary_info.external_attr >> 16) & 0o777)
    completed = subprocess.run(
        [str(executable), "--bundle-smoke"],
        cwd=executable.parent,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = completed.stdout.strip()
    parse_bundle_smoke_output(output, expected_version)

    runtime_root = executable.parent
    required = (
        runtime_root / "config.json",
        runtime_root / "data",
        runtime_root / "temp",
        runtime_root / "data" / "logs",
        runtime_root / "data" / "sessions",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError(f"bundle smoke did not create runtime files: {missing}")
    return output


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(project_root: Path, output_dir: Path) -> tuple[Path, Path]:
    version = (project_root / "VERSION").read_text(encoding="utf-8").strip()
    platform_tag = detect_platform_tag(sys.platform, platform.machine(), struct.calcsize("P") * 8)
    basename = artifact_basename(version, platform_tag)
    executable_name = f"{basename}.exe" if sys.platform == "win32" else basename

    temp_root = project_root / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    build_root = temp_root / f"release-build-{uuid.uuid4().hex}"
    pyinstaller_dist = build_root / "dist"
    pyinstaller_work = build_root / "work"
    archive = output_dir / f"{basename}.zip"
    checksum = output_dir / f"{basename}.sha256"

    environment = os.environ.copy()
    environment["TG_SYNC_BINARY_NAME"] = basename
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(pyinstaller_dist),
        "--workpath",
        str(pyinstaller_work),
        str(project_root / "tg-channel-sync.spec"),
    ]

    try:
        subprocess.run(command, cwd=project_root, env=environment, check=True)
        built_binary = pyinstaller_dist / executable_name
        create_release_archive(built_binary, archive, basename)
        result = validate_release_archive(archive, basename, executable_name)
        smoke_output = run_archive_smoke(archive, build_root, basename, executable_name, version)
        digest = sha256_file(archive)
        output_dir.mkdir(parents=True, exist_ok=True)
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    finally:
        if build_root.exists() and build_root.is_relative_to(temp_root):
            shutil.rmtree(build_root, ignore_errors=True)

    print(f"Archive: {archive}")
    print(f"Checksum: {checksum}")
    print(f"SHA-256: {digest}")
    print(f"Binary size: {result['binary_size']}")
    print(smoke_output)
    return archive, checksum


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one native tg-channel-sync release archive.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Release output directory (default: <project>/dist-release)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    output_dir = (args.output_dir or project_root / "dist-release").resolve()
    build_release(project_root, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
