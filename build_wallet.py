from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from importlib import util as importlib_util
from pathlib import Path

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
LICENSE_DIR = PROJECT_ROOT / "license"
VERSION_INFO_PATH = PROJECT_ROOT / "_pyi_version_info.txt"
BUILD_MARKER_PATH = PROJECT_ROOT / "_zsend_build_mode.json"
LOCALES_DIR = PROJECT_ROOT / "ZSend_Wallet" / "locales"
APP_NAME = "ZSend_Wallet"
DEBUG_APP_NAME = f"{APP_NAME}_debug"
BITCOINZ_RELEASE_API = "https://api.github.com/repos/btcz/bitcoinz/releases/latest"
NODE_BINARIES = ("bitcoinzd.exe", "bitcoinz-cli.exe", "bitcoinz-tx.exe")
BUILD_REQUIREMENTS = (
    ("PyInstaller", "PyInstaller"),
    ("PySide6", "PySide6"),
    ("requests", "requests"),
    ("qrcode", "qrcode[pil]"),
    ("PIL", "Pillow"),
)
PYINSTALLER_EXCLUDES = ("numpy", "pygame")


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
    subprocess.run(cmd, cwd=cwd or PROJECT_ROOT, check=True)


def _ensure_build_requirements() -> None:
    missing: list[str] = []
    for module_name, package_name in BUILD_REQUIREMENTS:
        if importlib_util.find_spec(module_name) is None:
            missing.append(package_name)
    if not missing:
        return
    print("Installing missing build dependencies:")
    for package_name in missing:
        print(f"  - {package_name}")
    _run([sys.executable, "-m", "pip", "install", "--upgrade", *missing])


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


def _find_windows_node_asset(release: dict) -> dict:
    assets = release.get("assets") or []
    candidates = []
    for asset in assets:
        name = str(asset.get("name") or "")
        lower = name.lower()
        if lower.endswith(".zip") and "win64" in lower and "bitcoinz" in lower:
            candidates.append(asset)
    if not candidates:
        available = "\n".join(str(asset.get("name") or "") for asset in assets) or "(no assets)"
        raise RuntimeError("Could not find BitcoinZ win64 zip asset. Available assets:\n" + available)
    candidates.sort(key=lambda asset: str(asset.get("name") or ""))
    return candidates[0]


def _extract_node_binaries(zip_path: Path, destination: Path) -> None:
    _remove_path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    found: set[str] = set()
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
    missing = [filename for filename in NODE_BINARIES if filename not in found]
    if missing:
        raise RuntimeError(f"BitcoinZ node archive is missing required file(s): {', '.join(missing)}")


def prepare_node() -> dict[str, object]:
    TMP_DIR.mkdir(exist_ok=True)
    node_tmp = TMP_DIR / "bitcoinz_node"
    _remove_path(node_tmp)
    node_tmp.mkdir(parents=True, exist_ok=True)

    print("Checking latest BitcoinZ node release...")
    release = _download_json(BITCOINZ_RELEASE_API)
    asset = _find_windows_node_asset(release)
    asset_name = str(asset["name"])
    download_url = str(asset["browser_download_url"])
    zip_path = node_tmp / asset_name

    print(f"Downloading BitcoinZ node: {asset_name}")
    _download_file(download_url, zip_path)
    print("Extracting node binaries...")
    _extract_node_binaries(zip_path, NODE_DIR)

    metadata = {
        "release_name": str(release.get("name") or ""),
        "tag_name": str(release.get("tag_name") or ""),
        "asset_name": asset_name,
        "download_url": download_url,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "included_files": list(NODE_BINARIES),
    }
    (NODE_DIR / "bitcoinz_node_release.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _remove_path(node_tmp)
    return metadata


def _read_existing_node_metadata() -> dict[str, object] | None:
    metadata_path = NODE_DIR / "bitcoinz_node_release.json"
    if not metadata_path.exists():
        return None
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        raise FileNotFoundError(f"Required release input not found: {src}")
    shutil.copytree(src, dst)


def _zip_dir(src_dir: Path, zip_path: Path) -> None:
    _remove_path(zip_path)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file_path in sorted(src_dir.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(src_dir.parent))


def _safe_version_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "unknown"


def package_release(exe_path: Path, identity: dict[str, str], _node_metadata: dict[str, object] | None) -> Path:
    mode_suffix = "_debug" if identity["app_name"] == DEBUG_APP_NAME else ""
    package_name = f"ZSend_Wallet_{_safe_version_name(DISPLAY_VERSION)}{mode_suffix}"
    package_root = RELEASE_DIR / package_name
    _remove_path(package_root)
    package_root.mkdir(parents=True, exist_ok=True)

    shutil.copy2(exe_path, package_root / exe_path.name)
    _copy_tree(NODE_DIR, package_root / "node")
    _copy_tree(LICENSE_DIR, package_root / "license")

    RELEASE_DIR.mkdir(exist_ok=True)
    zip_path = RELEASE_DIR / f"{package_name}-win.zip"
    _zip_dir(package_root, zip_path)
    return zip_path


def build(debug: bool = False, skip_node: bool = False, skip_package: bool = False) -> int:
    _ensure_build_requirements()
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

    node_metadata = None
    if not skip_node:
        node_metadata = prepare_node()
    else:
        node_metadata = _read_existing_node_metadata()

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
        f"{ICON_PATH};icons",
        "--add-data",
        f"{LOCALES_DIR};locales",
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
        zip_path = package_release(final_exe_path, identity, node_metadata)
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
    return parser.parse_args()


if __name__ == "__main__":
    try:
        args = _parse_args()
        raise SystemExit(build(debug=args.debug, skip_node=args.skip_node, skip_package=args.skip_package))
    except Exception as exc:
        print(f"Build failed:\n{exc}", file=sys.stderr)
        raise SystemExit(1)
