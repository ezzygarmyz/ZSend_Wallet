from __future__ import annotations

import argparse
import os
import json
import re
import shutil
import stat
import tarfile
import platform
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from importlib import util as importlib_util
from pathlib import Path

from ZSend_Wallet.helpers import current_platform
from ZSend_Wallet.version import (
    COMMENTS,
    COMPANY_NAME,
    DISPLAY_VERSION,
    FILE_DESCRIPTION,
    FILE_VERSION,
    INTERNAL_NAME,
    LEGAL_COPYRIGHT,
    ORIGINAL_FILENAME,
    PRODUCT_NAME,
    PRODUCT_VERSION,
)

PROJECT_ROOT = Path(__file__).resolve().parent
APP_ENTRY = PROJECT_ROOT / "ZSend_Wallet.py"
ICON_PATH = PROJECT_ROOT / "ZSend_Wallet" / "icons" / "bitcoinz.ico"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
TMP_DIR = PROJECT_ROOT / "tmp"
RELEASE_DIR = PROJECT_ROOT / "release"
NODE_DIR = PROJECT_ROOT / "node"
LICENSE_DIR = PROJECT_ROOT / "LICENSE"
VERSION_INFO_PATH = PROJECT_ROOT / "_pyi_version_info.txt"
BUILD_MARKER_PATH = PROJECT_ROOT / "_zsend_build_mode.json"
LOCALES_DIR = PROJECT_ROOT / "ZSend_Wallet" / "locales"
BUILD_DEPS_DIR = PROJECT_ROOT / "tools" / "build_deps"
APP_NAME = "ZSend_Wallet"
DEBUG_APP_NAME = f"{APP_NAME}_debug"
BITCOINZ_RELEASE_API = "https://api.github.com/repos/btcz/bitcoinz/releases/latest"
NODE_BINARIES = (
    ("bitcoinzd.exe", "bitcoinz-cli.exe", "bitcoinz-tx.exe")
    if current_platform == "windows"
    else ("bitcoinzd", "bitcoinz-cli", "bitcoinz-tx")
)
BUILD_REQUIREMENTS = (
    ("PyInstaller", "PyInstaller", "PyInstaller", "6.14.2"),
    ("PySide6", "PySide6", "PySide6", "6.9.1"),
    ("requests", "requests", "requests", "2.32.4"),
    ("qrcode", "qrcode", "qrcode[pil]", "8.2"),
    ("PIL", "Pillow", "Pillow", "11.3.0"),
)
TEST_REQUIREMENTS = (
    ("pytest", "pytest", "pytest", "9.0.3"),
)
DEV_REQUIREMENTS = TEST_REQUIREMENTS + BUILD_REQUIREMENTS
PYINSTALLER_EXCLUDES = ("numpy", "pygame")


def _prepend_build_deps_path() -> None:
    if not BUILD_DEPS_DIR.exists():
        return
    deps_path = str(BUILD_DEPS_DIR)
    if deps_path not in sys.path:
        sys.path.insert(0, deps_path)


_prepend_build_deps_path()


def _build_identity(debug: bool) -> dict[str, str]:
    if not debug:
        return {
            "app_name": APP_NAME,
            "internal_name": INTERNAL_NAME,
            "original_filename": ORIGINAL_FILENAME,
            "product_name": PRODUCT_NAME,
            "file_description": FILE_DESCRIPTION,
            "display_version": DISPLAY_VERSION,
            "comments": COMMENTS,
        }
    return {
        "app_name": DEBUG_APP_NAME,
        "internal_name": f"{INTERNAL_NAME}_debug",
        "original_filename": f"{DEBUG_APP_NAME}.exe",
        "product_name": f"{PRODUCT_NAME} Debug",
        "file_description": f"{FILE_DESCRIPTION} Debug",
        "display_version": f"{DISPLAY_VERSION} Debug",
        "comments": f"{COMMENTS} (Debug build)",
    }


def _remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _remove_pycache_dirs(root: Path) -> None:
    for path in root.rglob("__pycache__"):
        _remove_path(path)


def _run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print(" ".join(f'"{part}"' if " " in part else part for part in cmd))
    env = os.environ.copy()
    if BUILD_DEPS_DIR.exists():
        existing_pythonpath = env.get("PYTHONPATH", "")
        paths = [str(BUILD_DEPS_DIR)]
        if existing_pythonpath:
            paths.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(paths)
    subprocess.run(cmd, cwd=cwd or PROJECT_ROOT, check=True, env=env)


def _build_requirement_spec(requirement: tuple[str, str, str, str]) -> str:
    _module_name, _distribution_name, install_name, version = requirement
    return f"{install_name}=={version}"


def requirement_specs(requirements: tuple[tuple[str, str, str, str], ...]) -> list[str]:
    return [_build_requirement_spec(requirement) for requirement in requirements]


def dev_requirement_specs() -> list[str]:
    return requirement_specs(DEV_REQUIREMENTS)


def _distribution_version_from_build_deps(distribution_name: str) -> str | None:
    if not BUILD_DEPS_DIR.exists():
        return None
    try:
        distributions = importlib_metadata.distributions(path=[str(BUILD_DEPS_DIR)])
        for distribution in distributions:
            if distribution.metadata["Name"].lower().replace("_", "-") == distribution_name.lower().replace("_", "-"):
                return distribution.version
    except Exception:
        return None
    return None


def _find_unsatisfied_requirements(
    requirements: tuple[tuple[str, str, str, str], ...],
) -> list[tuple[str, str]]:
    unsatisfied: list[tuple[str, str]] = []
    _prepend_build_deps_path()
    for module_name, distribution_name, install_name, expected_version in requirements:
        spec = f"{install_name}=={expected_version}"
        if importlib_util.find_spec(module_name) is None:
            unsatisfied.append((spec, "missing"))
            continue
        actual_version = _distribution_version_from_build_deps(distribution_name)
        if actual_version is None:
            try:
                actual_version = importlib_metadata.version(distribution_name)
            except importlib_metadata.PackageNotFoundError:
                actual_version = "unknown"
        if actual_version != expected_version:
            unsatisfied.append((spec, f"installed {actual_version}"))
    return unsatisfied


def _find_unsatisfied_build_requirements() -> list[tuple[str, str]]:
    return _find_unsatisfied_requirements(BUILD_REQUIREMENTS)


def _ensure_build_requirements(*, install: bool = False) -> None:
    unsatisfied = _find_unsatisfied_build_requirements()
    if not unsatisfied:
        return

    exact_specs = [spec for spec, _reason in unsatisfied]
    if install:
        _remove_path(BUILD_DEPS_DIR)
        BUILD_DEPS_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Installing exact build dependencies into {BUILD_DEPS_DIR}:")
        for spec, reason in unsatisfied:
            print(f"  - {spec} ({reason})")
        _run([
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(BUILD_DEPS_DIR),
            *exact_specs,
        ])
        _prepend_build_deps_path()
        return

    details = "\n".join(f"  - {spec} ({reason})" for spec, reason in unsatisfied)
    install_cmd = " ".join([sys.executable, str(Path(__file__).name), "--install-build-deps"])
    raise RuntimeError(
        "Build dependencies are missing or not pinned to the expected versions:\n"
        f"{details}\n\n"
        "Install the exact local build dependencies first:\n"
        f"{install_cmd}\n\n"
        f"Dependencies will be installed into {BUILD_DEPS_DIR}, not into global Python."
    )


def _check_inputs() -> None:
    missing: list[str] = []
    if not APP_ENTRY.exists():
        missing.append(str(APP_ENTRY))
    if not ICON_PATH.exists():
        missing.append(str(ICON_PATH))
    if not LOCALES_DIR.exists():
        missing.append(str(LOCALES_DIR))
    if not LICENSE_DIR.exists():
        missing.append(str(LICENSE_DIR))
    if missing:
        raise FileNotFoundError("Missing required build input(s):\n" + "\n".join(missing))


def _version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in str(version).split(".") if p.strip()]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _write_version_file(path: Path, identity: dict[str, str]) -> None:
    file_ver = _version_tuple(FILE_VERSION)
    product_ver = _version_tuple(PRODUCT_VERSION)
    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={file_ver},
    prodvers={product_ver},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '{COMPANY_NAME}'),
          StringStruct('FileDescription', '{identity["file_description"]}'),
          StringStruct('FileVersion', '{identity["display_version"]}'),
          StringStruct('InternalName', '{identity["internal_name"]}'),
          StringStruct('OriginalFilename', '{identity["original_filename"]}'),
          StringStruct('ProductName', '{identity["product_name"]}'),
          StringStruct('ProductVersion', '{identity["display_version"]}'),
          StringStruct('Comments', '{identity["comments"]}'),
          StringStruct('LegalCopyright', '{LEGAL_COPYRIGHT}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path.write_text(content, encoding="utf-8")


def _write_build_marker(path: Path, identity: dict[str, str]) -> None:
    payload = {
        "build_kind": "builder-debug",
        "debug_logging": True,
        "product_name": identity["product_name"],
        "display_version": identity["display_version"],
        "output_name": identity["original_filename"],
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "builder": str(Path(__file__).name),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _download_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ZSend-Wallet-Builder",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_file(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "ZSend-Wallet-Builder"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out, length=1024 * 1024)


def _find_node_asset(release: dict) -> dict:
    assets = release.get("assets") or []
    candidates = []
    machine = platform.machine().lower()
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        
        if current_platform == "windows":
            if name.endswith(".zip") and "win64" in name and "bitcoinz" in name:
                candidates.append(asset)

        elif current_platform == "linux":
            is_linux_archive = name.endswith(".tar.gz") or name.endswith(".tar.xz")
            is_x86_64 = machine in ("x86_64", "amd64") and "x86_64" in name and "gnu" in name
            is_aarch64 = machine in ("aarch64", "arm64") and (
                "aarch64" in name or "arm64" in name
            ) and "gnu" in name
            if is_linux_archive and "bitcoinz" in name and (is_x86_64 or is_aarch64):
                candidates.append(asset)

    if not candidates:
        available = "\n".join(str(asset.get("name") or "") for asset in assets) or "(no assets)"
        raise RuntimeError(
            f"No BitcoinZ asset found for platform={current_platform}\n{available}"
        )
    candidates.sort(key=lambda asset: str(asset.get("name") or ""))
    return candidates[0]


def _extract_node_binaries(zip_path: Path, destination: Path) -> None:
    _remove_path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    found: set[str] = set()
    suffix = "".join(zip_path.suffixes)

    def _make_executable(path: Path) -> None:
        if os.name != "nt":
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if suffix.endswith(".zip"):
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                filename = Path(member.filename).name
                if filename not in NODE_BINARIES:
                    continue
                target = destination / filename
                with archive.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

                found.add(filename)

    else:
        with tarfile.open(zip_path, "r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                filename = Path(member.name).name
                if filename not in NODE_BINARIES:
                    continue
                target = destination / filename
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                with extracted as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                _make_executable(target)

                found.add(filename)
    missing = [filename for filename in NODE_BINARIES if filename not in found]
    if missing:
        raise RuntimeError(f"BitcoinZ node archive is missing required file(s): {', '.join(missing)}")


def prepare_node() -> dict[str, object]:
    TMP_DIR.mkdir(exist_ok=True)
    node_tmp = TMP_DIR / "bitcoinz_node"
    _remove_path(node_tmp)
    node_tmp.mkdir(parents=True, exist_ok=True)

    try:
        print("Checking latest BitcoinZ node release...")
        release = _download_json(BITCOINZ_RELEASE_API)
        asset = _find_node_asset(release)
        asset_name = str(asset["name"])
        download_url = str(asset["browser_download_url"])
        zip_path = node_tmp / asset_name

        print(f"Downloading BitcoinZ node: {asset_name}")
        _download_file(download_url, zip_path)
        print("Extracting node binaries...")
        _extract_node_binaries(zip_path, NODE_DIR)

        return {
            "release_name": str(release.get("name") or ""),
            "tag_name": str(release.get("tag_name") or ""),
            "asset_name": asset_name,
            "download_url": download_url,
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "included_files": list(NODE_BINARIES),
        }
    finally:
        _remove_path(node_tmp)


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Required release input not found: {src}")
    shutil.copytree(src, dst)


def _copy_node_binaries(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Required release input not found: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for filename in NODE_BINARIES:
        source_file = src / filename
        if not source_file.exists():
            missing.append(filename)
            continue
        shutil.copy2(source_file, dst / filename)
    if missing:
        raise FileNotFoundError("Missing required node binary file(s): " + ", ".join(missing))


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    _remove_path(zip_path)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file_path in sorted(src_dir.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(src_dir.parent))


def _safe_version_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "unknown"


def package_release(exe_path: Path, identity: dict[str, str]) -> Path:
    mode_suffix = "_debug" if identity["app_name"] == DEBUG_APP_NAME else ""
    package_name = f"ZSend_Wallet_{_safe_version_name(DISPLAY_VERSION)}{mode_suffix}"
    package_root = RELEASE_DIR / package_name
    _remove_path(package_root)
    package_root.mkdir(parents=True, exist_ok=True)

    shutil.copy2(exe_path, package_root / exe_path.name)
    _copy_node_binaries(NODE_DIR, package_root / "node")
    _copy_tree(LICENSE_DIR, package_root / "license")

    RELEASE_DIR.mkdir(exist_ok=True)
    zip_path = RELEASE_DIR / f"{package_name}-win.zip"
    _zip_dir(package_root, zip_path)
    return zip_path


def build(
    debug: bool = False,
    skip_node: bool = False,
    skip_package: bool = False,
    install_build_deps: bool = False,
) -> int:
    _ensure_build_requirements(install=install_build_deps)
    _check_inputs()
    identity = _build_identity(debug)
    final_exe_path = PROJECT_ROOT / identity["original_filename"]
    spec_path = PROJECT_ROOT / f"{identity['app_name']}.spec"
    alt_spec_paths = [
        PROJECT_ROOT / f"{APP_NAME}.spec",
        PROJECT_ROOT / f"{DEBUG_APP_NAME}.spec",
    ]

    DIST_DIR.mkdir(exist_ok=True)
    _remove_path(BUILD_DIR)
    _remove_path(VERSION_INFO_PATH)
    _remove_path(BUILD_MARKER_PATH)
    _remove_path(DIST_DIR)
    _remove_path(PROJECT_ROOT / f"{APP_NAME}.exe")
    _remove_path(PROJECT_ROOT / f"{DEBUG_APP_NAME}.exe")
    for extra_spec in alt_spec_paths:
        _remove_path(extra_spec)
    _write_version_file(VERSION_INFO_PATH, identity)
    if debug:
        _write_build_marker(BUILD_MARKER_PATH, identity)

    if not skip_node:
        prepare_node()

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        identity["app_name"],
        "--distpath",
        str(DIST_DIR),
        "--icon",
        str(ICON_PATH),
        "--version-file",
        str(VERSION_INFO_PATH),
        "--add-data",
        f"{ICON_PATH}:icons",
        "--add-data",
        f"{LOCALES_DIR}:locales",
        "--paths",
        str(PROJECT_ROOT),
        "--hidden-import",
        "qrcode",
        "--hidden-import",
        "PIL",
        str(APP_ENTRY),
    ]
    for module_name in PYINSTALLER_EXCLUDES:
        cmd.extend(["--exclude-module", module_name])
    if debug:
        cmd.extend([
            "--add-data",
            f"{BUILD_MARKER_PATH};.",
        ])

    mode_name = "debug" if debug else "release"
    print(f"Building ZSend Wallet ({mode_name})...")

    try:
        _run(cmd)
    finally:
        _remove_path(BUILD_DIR)
        _remove_path(spec_path)
        for extra_spec in alt_spec_paths:
            if extra_spec != spec_path:
                _remove_path(extra_spec)
        _remove_path(VERSION_INFO_PATH)
        _remove_path(BUILD_MARKER_PATH)
        _remove_pycache_dirs(PROJECT_ROOT)

    exe_path = DIST_DIR / identity["original_filename"]
    if not exe_path.exists():
        raise FileNotFoundError(f"Build finished but binary was not found: {exe_path}")
    shutil.move(str(exe_path), str(final_exe_path))
    _remove_path(DIST_DIR)

    print(f"\nBuild complete:\n{final_exe_path}")
    if not skip_package:
        zip_path = package_release(final_exe_path, identity)
        print(f"Release package:\n{zip_path}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build ZSend Wallet with PyInstaller.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Build a developer binary with paranoid startup debug logging enabled.",
    )
    parser.add_argument(
        "--skip-node",
        action="store_true",
        help="Do not download/update bundled BitcoinZ node binaries.",
    )
    parser.add_argument(
        "--skip-package",
        action="store_true",
        help="Build only the executable and skip release zip packaging.",
    )
    parser.add_argument(
        "--install-build-deps",
        action="store_true",
        help="Install exact pinned build dependencies before building.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        args = _parse_args()
        raise SystemExit(
            build(
                debug=args.debug,
                skip_node=args.skip_node,
                skip_package=args.skip_package,
                install_build_deps=args.install_build_deps,
            )
        )
    except Exception as exc:
        print(f"Build failed:\n{exc}", file=sys.stderr)
        raise SystemExit(1)
