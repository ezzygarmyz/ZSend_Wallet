from __future__ import annotations

import sys
import os
import csv
import re
import time
import socket
import subprocess
import json
import io
import binascii
import secrets
import hashlib
import traceback
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parent.parent
_VENDOR_QR = _APP_ROOT / "_vendor_qr"
if _VENDOR_QR.exists():
    sys.path.insert(0, str(_VENDOR_QR))

import requests
from requests.auth import HTTPBasicAuth
try:
    import qrcode
except Exception:
    qrcode = None

from .version import DISPLAY_VERSION
from .wallet_cache import WalletCache, btcz_to_zat, zat_to_float
from .helpers import fmt_btcz, is_shielded_address, current_platform

wallet_version = DISPLAY_VERSION
DEVELOPER_TIP_ADDRESS = "t1fjtqgoCboGToe6Mv68n5AvdDZaL6ZSend"


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = str(_APP_ROOT)
    return os.path.join(base_path, relative_path)


_SILENT_FLAGS: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QLabel,
    QLineEdit, QDoubleSpinBox, QSpinBox, QMessageBox, QHeaderView, QFrame,
    QDialog, QFormLayout, QDialogButtonBox, QComboBox,
    QProgressBar, QAbstractItemView, QTextEdit, QMenu,
    QStyledItemDelegate, QStyle, QStyleOptionComboBox, QScrollArea,
    QFileDialog, QTableView,
)
from PySide6.QtGui import QColor, QPainter, QFont, QAction, QIcon, QPixmap, QImage
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QRect, QEvent, QObject, QUrl, QAbstractTableModel, QModelIndex
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .debug_runtime import debug_exception, debug_log

if current_platform == "windows":
    DATA_DIR  = Path(os.environ.get("APPDATA", Path.home())) / "BitcoinZ"
    PARAMS_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ZcashParams"
elif current_platform == "linux":
    DATA_DIR  = Path.home() / ".bitcoinz"
    PARAMS_DIR = Path.home() / ".zcash-params"

NODE_EXECUTABLE = "bitcoinzd.exe" if current_platform == "windows" else "bitcoinzd"

WALLET_DIR = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)

CONF_PATH = DATA_DIR / "bitcoinz.conf"
EXPORT_DIR = DATA_DIR / "exports"
SENSITIVE_TEMP_DIR = Path(tempfile.gettempdir()) / "ZSendWallet" / "secure"
SENSITIVE_DUMP_PATTERNS = (
    "zsend_import_*.dump",
    "zsend_export_*.dump",
    "zsendexport*",
    "ZSendWalletExport*",
)

NODE_CANDIDATES = [
    WALLET_DIR / NODE_EXECUTABLE,
    WALLET_DIR / "node" / NODE_EXECUTABLE,
    WALLET_DIR / "bitcoinz" / NODE_EXECUTABLE,
]

PARAMS_FILES = [
    {
        "name":   "sapling-output.params",
        "url":    "https://github.com/zalpader/ZSend_Wallet/releases/download/ZcashParams/sapling-output.params",
        "sha256": "2f0ebbcbb9bb0bcffe95a397e7eba89c29eb4dde6191c339db88570e3f3fb0e4",
        "size":   3592860,
    },
    {
        "name":   "sapling-spend.params",
        "url":    "https://github.com/zalpader/ZSend_Wallet/releases/download/ZcashParams/sapling-spend.params",
        "sha256": "8e48ffd23abb3a5fd9c5589204f32d9c31285a04b78096ba40a79b75677efc13",
        "size":   47958396,
    },
    {
        "name":   "sprout-groth16.params",
        "url":    "https://github.com/zalpader/ZSend_Wallet/releases/download/ZcashParams/sprout-groth16.params",
        "sha256": "b685d700c60328498fbde589c8c7c484c722b788b265b72af448a5bf0ee55b50",
        "size":   725523612,
    },
    {
        "name":   "sprout-proving.key",
        "url":    "https://github.com/zalpader/ZSend_Wallet/releases/download/ZcashParams/sprout-proving.key",
        "sha256": "8bc20a7f013b2b58970cddd2e7ea028975c88ae7ceb9259a5344a16bc2c0eef7",
        "size":   910173851,
    },
    {
        "name":   "sprout-verifying.key",
        "url":    "https://github.com/zalpader/ZSend_Wallet/releases/download/ZcashParams/sprout-verifying.key",
        "sha256": "4bd498dae0aacfd8e98dc306338d017d9c08dd0918ead18172bd0aec2fc5df82",
        "size":   1449,
    },
]
PARAMS_TOTAL_SIZE = sum(f["size"] for f in PARAMS_FILES)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(n: int) -> str:
    return "".join(secrets.choice(_B58) for _ in range(n))


_ADDNODES = [
    "152.53.180.255:1989",
    "51.222.50.26:1989",
    "109.199.122.150:1989",
]

_RPC_DEFAULT_PORT = 1979
_RPC_DEFAULT_HOST = "127.0.0.1"

_RUNNING_WORKERS: set = set()


def _mask_secret(value: str) -> str:
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}***{value[-2:]} (len={len(value)})"


def _current_user_sid() -> str:
    if os.name != "nt":
        return ""
    try:
        out = subprocess.check_output(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            stderr=subprocess.DEVNULL,
            creationflags=_SILENT_FLAGS,
            timeout=5,
        ).decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(out)))
        if rows and len(rows[0]) >= 2:
            return rows[0][1].strip()
    except Exception:
        return ""
    return ""


def _harden_windows_acl(path: Path) -> bool:
    if os.name != "nt":
        return True
    sid = _current_user_sid()
    if not sid:
        return False
    try:
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{sid}:F", "/c"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_SILENT_FLAGS,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def ensure_sensitive_temp_dir(root: Path | None = None) -> Path:
    temp_dir = Path(root) if root is not None else SENSITIVE_TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _harden_windows_acl(temp_dir)
    else:
        try:
            os.chmod(temp_dir, 0o700)
        except OSError:
            pass
    return temp_dir


def create_sensitive_text_file(prefix: str, suffix: str, text: str, *, root: Path | None = None) -> Path:
    temp_dir = ensure_sensitive_temp_dir(root)
    for _ in range(16):
        path = temp_dir / f"{prefix}{secrets.token_hex(16)}{suffix}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(str(path), flags, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            if os.name == "nt":
                _harden_windows_acl(path)
            else:
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            return path
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                path.unlink()
            except OSError:
                pass
            raise
    raise FileExistsError("Could not create a unique sensitive temporary file")


def cleanup_sensitive_dump_artifacts(*, root: Path | None = None, extra_dirs: list[Path] | tuple[Path, ...] | None = None) -> None:
    dirs: list[Path] = [Path(root) if root is not None else SENSITIVE_TEMP_DIR]
    dirs.extend(Path(folder) for folder in (extra_dirs or ()))
    seen: set[Path] = set()
    for folder in dirs:
        try:
            folder = folder.resolve()
        except OSError:
            folder = Path(folder)
        if folder in seen or not folder.exists() or not folder.is_dir():
            continue
        seen.add(folder)
        for pattern in SENSITIVE_DUMP_PATTERNS:
            try:
                matches = list(folder.glob(pattern))
            except OSError:
                continue
            for path in matches:
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass


def _norm_conf_path(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(str(path)))))


def _track(worker: "QThread") -> "QThread":
    _RUNNING_WORKERS.add(worker)
    worker.finished.connect(lambda: _RUNNING_WORKERS.discard(worker))
    return worker


_SINGLE_INSTANCE_KEY = "BitcoinZWallet_SingleInstance_v1"


def _is_z_addr(addr: str) -> bool:
    return is_shielded_address(addr)


def read_conf(path: Path = CONF_PATH) -> dict:
    result: dict = {}
    if not path.exists():
        return result
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return result


def read_conf_values(path: Path, key_name: str) -> list[str]:
    values: list[str] = []
    wanted = str(key_name or "").strip().lower()
    if not wanted or not path.exists():
        return values
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip().lower() == wanted:
                    values.append(value.strip())
    except OSError:
        pass
    return values


def set_conf_value(path: Path, key_name: str, value: str) -> bool:
    wanted = str(key_name or "").strip().lower()
    if not wanted or not path.exists():
        return False

    desired = f"{key_name}={value}"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    seen = False
    changed = False
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            cleaned.append(line)
            continue
        key, _, current_value = stripped.partition("=")
        if key.strip().lower() != wanted:
            cleaned.append(line)
            continue
        if seen:
            changed = True
            continue
        seen = True
        if current_value.strip() == str(value):
            cleaned.append(line)
        else:
            cleaned.append(desired)
            changed = True

    if not changed:
        return False

    try:
        path.write_text("\n".join(cleaned).rstrip() + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def append_conf_lines(path: Path, lines: list[str]) -> None:
    clean_lines = [line for line in lines if str(line).strip()]
    if not clean_lines:
        return

    prefix = ""
    try:
        if path.exists() and path.stat().st_size > 0:
            with open(path, "rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    prefix = "\n"
    except OSError:
        prefix = "\n"

    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + "\n".join(clean_lines) + "\n")


def normalize_addnode_spacing(path: Path) -> None:
    if not path.exists():
        return

    def is_addnode(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and not stripped.startswith("#") and stripped.lower().startswith("addnode=")

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return

    changed = False
    cleaned: list[str] = []
    for index, line in enumerate(lines):
        if line.strip():
            cleaned.append(line)
            continue

        prev_line = next((candidate for candidate in reversed(cleaned) if candidate.strip()), "")
        next_line = next((candidate for candidate in lines[index + 1 :] if candidate.strip()), "")
        if is_addnode(prev_line) and is_addnode(next_line):
            changed = True
            continue

        cleaned.append(line)

    if not changed:
        return

    try:
        path.write_text("\n".join(cleaned).rstrip() + "\n", encoding="utf-8")
    except OSError:
        pass


def ensure_conf(path: Path = CONF_PATH) -> dict:
    debug_log("ensure_conf called", path=str(path), exists=path.exists())
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_conf(path)

    if not path.exists():
        rpcuser = _b58(16)
        rpcpassword = _b58(32)
        export_dir = ensure_sensitive_temp_dir()
        lines = [
            f"rpcuser={rpcuser}",
            f"rpcpassword={rpcpassword}",
            "sendchangeback=1",
            f"exportdir={export_dir.as_posix()}",
        ]
        for node in _ADDNODES:
            lines.append(f"addnode={node}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass
    else:
        to_add: list[str] = []
        if not existing.get("rpcuser"):
            to_add.append(f"rpcuser={_b58(16)}")
        if not existing.get("rpcpassword"):
            to_add.append(f"rpcpassword={_b58(32)}")
        if not existing.get("sendchangeback"):
            to_add.append("sendchangeback=1")
        elif existing.get("sendchangeback") != "1":
            set_conf_value(path, "sendchangeback", "1")
        existing_addnodes = {node.lower() for node in read_conf_values(path, "addnode")}
        for node in _ADDNODES:
            if node.lower() not in existing_addnodes:
                to_add.append(f"addnode={node}")
        if to_add:
            try:
                append_conf_lines(path, to_add)
            except OSError:
                pass

    normalize_addnode_spacing(path)
    cfg = read_conf(path)
    debug_log(
        "ensure_conf finished",
        path=str(path),
        rpcuser=_mask_secret(cfg.get("rpcuser", "")),
        rpcpassword=_mask_secret(cfg.get("rpcpassword", "")),
        exportdir_configured=bool(cfg.get("exportdir", "")),
        keys=sorted(cfg.keys()),
    )
    return cfg


def ensure_exportdir(path: Path = CONF_PATH, secure_dir: Path | None = None) -> tuple[Path, bool]:
    cfg = ensure_conf(path)
    export_path = ensure_sensitive_temp_dir(secure_dir)
    export_val = cfg.get("exportdir", "").strip()
    changed = False
    if not export_val:
        changed = True
        try:
            append_conf_lines(path, [f"exportdir={export_path.as_posix()}"])
        except OSError:
            pass
    elif _norm_conf_path(export_val) != _norm_conf_path(export_path):
        changed = set_conf_value(path, "exportdir", export_path.as_posix()) or changed
    debug_log("ensure_exportdir finished", exportdir_secure=True, changed=changed)
    return export_path, changed


def load_rpc_cfg() -> dict:
    c = ensure_conf()
    return {
        "host":       c.get("rpcbind", _RPC_DEFAULT_HOST),
        "port":       int(c.get("rpcport", _RPC_DEFAULT_PORT)),
        "user":       c.get("rpcuser",     ""),
        "password":   c.get("rpcpassword", ""),
        "exportdir":  c.get("exportdir", ""),
        "conf_path":  str(CONF_PATH),
        "conf_found": CONF_PATH.exists(),
    }


def find_node():
    debug_log("Searching for node binary", candidates=[str(p) for p in NODE_CANDIDATES])
    for p in NODE_CANDIDATES:
        if p.exists():
            debug_log("Found node binary", path=str(p))
            return p
    debug_log("Node binary not found")
    return None


def node_running() -> bool:
    try:
        out = subprocess.check_output(
            ["tasklist", "/fo", "csv", "/nh"],
            stderr=subprocess.DEVNULL,
            creationflags=_SILENT_FLAGS,
            timeout=5,
        ).decode("utf-8", errors="replace").lower()
        running = "bitcoinzd" in out or "bitcoinz-qt" in out
        debug_log("node_running probe complete", running=running)
        return running
    except Exception as exc:
        debug_exception("node_running probe failed", exc)
        return False


def launch_node(binary: Path, extra_args: list[str] | tuple[str, ...] | None = None):
    try:
        export_dir, _ = ensure_exportdir()
        args = [str(binary), f"-conf={CONF_PATH}", f"-exportdir={export_dir}"]
        for arg in extra_args or ():
            arg = str(arg or "").strip()
            if arg:
                args.append(arg)
        debug_log("Launching node process", args=args)
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_SILENT_FLAGS,
        )
        debug_log("Node launch started", pid=getattr(proc, "pid", None))
        return proc
    except Exception as exc:
        debug_exception("launch_node failed", exc)
        return None



def tx_ts(tx: dict) -> int:
    for f in ("blocktime", "time", "timereceived", "created_at"):
        v = tx.get(f)
        if v:
            try: return int(v)
            except Exception: pass
    return 0


def tx_fingerprint(txs: list) -> str:
    rows = []
    for tx in txs or []:
        rows.append("|".join([
            str(tx.get("txid", "")),
            str(tx.get("category", "")),
            str(tx.get("address", "")),
            fmt_btcz(float(tx.get("amount", 0) or 0)),
            fmt_btcz(float(tx.get("fee", 0) or 0)),
            str(tx.get("confirmations", "")),
            str(tx.get("blockhash", "")),
            str(tx.get("blockheight", "")),
            str(tx.get("blockindex", "")),
            str(tx_ts(tx)),
            str(tx.get("status", "")),
        ]))
    return "\n".join(rows)


def _sanitize_dump_basename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "")
    return cleaned[:48] or "zsendexport"


def _representative_tx_address(entries: list[dict]) -> str:
    def addr_of(entry: dict) -> str:
        return str(entry.get("address") or "")

    receives = [entry for entry in entries if entry.get("category") == "receive" and addr_of(entry)]
    if receives:
        synthetic_receives = [entry for entry in receives if entry.get("_synthetic") == "own_operation_receive"]
        shielded_receives = [entry for entry in receives if _is_z_addr(addr_of(entry))]
        for bucket in (synthetic_receives, shielded_receives, receives):
            if bucket:
                return addr_of(bucket[0])

    sends = [entry for entry in entries if entry.get("category") == "send" and addr_of(entry)]
    if sends:
        return addr_of(sends[0])

    for entry in entries:
        if addr_of(entry):
            return addr_of(entry)
    return ""


def group_tx_rows(txs: list) -> list:
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for tx in txs or []:
        txid = tx.get("txid", "")
        key = txid or f"__row_{len(order)}"
        if key not in grouped:
            grouped[key] = dict(tx)
            grouped[key]["_entries"] = [tx]
            order.append(key)
            continue
        g = grouped[key]
        g["_entries"].append(tx)
        try:
            g["amount"] = float(g.get("amount", 0) or 0) + float(tx.get("amount", 0) or 0)
        except Exception:
            pass
        if not g.get("address") and tx.get("address"):
            g["address"] = tx.get("address")
        if not g.get("category") and tx.get("category"):
            g["category"] = tx.get("category")
        if tx_ts(tx) > tx_ts(g):
            for field in ("time", "blocktime", "timereceived"):
                if tx.get(field):
                    g[field] = tx.get(field)
        try:
            g["confirmations"] = min(
                int(g.get("confirmations", 0) or 0),
                int(tx.get("confirmations", 0) or 0),
            )
        except Exception:
            pass
        if tx.get("fee") is not None:
            g["fee"] = tx.get("fee")
        for field in ("blockhash", "blockheight", "blockindex", "status"):
            if tx.get(field) not in (None, ""):
                g[field] = tx.get(field)
    rows = [grouped[k] for k in order]
    for row in rows:
        entries = row.get("_entries") or []
        representative = _representative_tx_address(entries)
        if representative:
            row["address"] = representative
        has_send = any(entry.get("category") == "send" for entry in entries)
        has_receive = any(entry.get("category") == "receive" for entry in entries)
        if has_send and has_receive and row.get("fee") is not None:
            try:
                row["amount"] = float(row.get("amount", 0) or 0) + float(row.get("fee", 0) or 0)
            except Exception:
                pass
    return rows


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_NODE_MSGS = {
    "Loading block index":        "Loading block index",
    "Activating best chain":      "Activating best chain",
    "Rewinding blocks if needed": "Rewinding blocks",
    "Loading wallet":             "Loading wallet",
    "Rescanning":                 "Rescanning blockchain",
    "Verifying blocks":           "Verifying blocks",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_REINDEX_PHRASES = ("reindexing", "while reindexing", "disabled while", "reindex")


def _is_reindex_err(e) -> bool:
    return any(p in str(e).lower() for p in _REINDEX_PHRASES)
