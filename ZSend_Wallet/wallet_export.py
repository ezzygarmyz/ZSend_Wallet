from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .common import cleanup_sensitive_dump_artifacts, wallet_version
from .rpc import BitcoinZRPC, RPCError


def _clone_rpc(rpc: BitcoinZRPC) -> BitcoinZRPC:
    clone = getattr(rpc, "clone", None)
    return clone() if callable(clone) else rpc


_EXPORT_DUMP_PREFIX = "zsendexport"
_LEGACY_EXPORT_DUMP_PREFIX = "ZSendWalletExport"
_IMPORT_DUMP_PREFIX = "zsend_import_"
_IMPORT_DUMP_SUFFIX = ".dump"

def _sanitize_dump_basename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value or "")
    return cleaned[:48] or _EXPORT_DUMP_PREFIX


def cleanup_wallet_dump_artifacts(export_dir: Path, data_dir: Path, secure_dir: Path | None = None) -> None:
    """Remove raw node dump files left by interrupted export/import flows."""
    candidates: list[Path] = []
    try:
        candidates.extend(Path(export_dir).glob(f"{_EXPORT_DUMP_PREFIX}*"))
        candidates.extend(Path(export_dir).glob(f"{_LEGACY_EXPORT_DUMP_PREFIX}*"))
    except OSError:
        pass
    try:
        candidates.extend(Path(data_dir).glob(f"{_IMPORT_DUMP_PREFIX}*{_IMPORT_DUMP_SUFFIX}"))
    except OSError:
        pass

    for path in candidates:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    cleanup_sensitive_dump_artifacts(root=secure_dir)


def _dump_entry_kind(secret: str, address: str) -> str:
    if address.startswith("zs") or address.startswith("zc"):
        return "shielded"
    if secret.startswith("secret-extended-key"):
        return "shielded"
    return "transparent"


def _parse_wallet_dump_text(dump_text: str) -> list[dict]:
    entries: list[dict] = []
    for raw_line in (dump_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        body, _, comment = line.partition("#")
        tokens = body.strip().split()
        if len(tokens) < 2:
            continue
        secret = tokens[0]
        created_at = tokens[1]
        meta: dict[str, str] = {}
        for token in tokens[2:]:
            if "=" in token:
                k, v = token.split("=", 1)
                meta[k.strip()] = v.strip()
        for token in comment.strip().split():
            if "=" in token:
                k, v = token.split("=", 1)
                meta[k.strip()] = v.strip()
        address = meta.get("addr") or meta.get("zaddr") or ""
        entries.append({
            "type": _dump_entry_kind(secret, address),
            "address": address,
            "secret": secret,
            "created_at": created_at,
            "label": meta.get("label", ""),
            "reserve": meta.get("reserve") == "1",
            "change": meta.get("change") == "1",
            "hdkeypath": meta.get("hdkeypath", ""),
            "seedfp": meta.get("seedfp", ""),
            "raw_line": raw_line,
        })
    return entries


def _sanitize_wallet_export_entries(entries: list[dict]) -> list[dict]:
    safe_entries: list[dict] = []
    for entry in entries:
        safe_entries.append({
            "type": entry.get("type", ""),
            "address": entry.get("address", ""),
            "created_at": entry.get("created_at", ""),
            "label": entry.get("label", ""),
            "reserve": bool(entry.get("reserve")),
            "change": bool(entry.get("change")),
            "hdkeypath": entry.get("hdkeypath", ""),
            "seedfp": entry.get("seedfp", ""),
        })
    return safe_entries


def _build_wallet_export_payload(dump_text: str) -> dict:
    now = datetime.now()
    entries = _parse_wallet_dump_text(dump_text)
    return {
        "format": "zsend_wallet_export_v1",
        "created_at": now.isoformat(timespec="seconds"),
        "created_unix": int(now.timestamp()),
        "export_method": "z_exportwallet",
        "wallet_version": wallet_version,
        "summary": {
            "entry_count": len(entries),
            "transparent_count": sum(1 for e in entries if e["type"] == "transparent"),
            "shielded_count": sum(1 for e in entries if e["type"] == "shielded"),
            "reserved_count": sum(1 for e in entries if e.get("reserve")),
        },
        "entries_meta": _sanitize_wallet_export_entries(entries),
        "node_dump_text": dump_text,
    }

class FullWalletExportWorker(QThread):
    done = Signal(dict)
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, export_dir: Path, dump_basename: str):
        super().__init__()
        self.rpc = _clone_rpc(rpc)
        self.export_dir = Path(export_dir)
        self.dump_basename = _sanitize_dump_basename(dump_basename)

    def run(self):
        dump_file: Path | None = None
        try:
            dump_path = self.rpc.z_ExportWallet(self.dump_basename)
            dump_file = Path(str(dump_path))
            if not dump_file.exists():
                alt = self.export_dir / self.dump_basename
                if alt.exists():
                    dump_file = alt
            dump_text = dump_file.read_text(encoding="utf-8", errors="replace")
            payload = _build_wallet_export_payload(dump_text)
            self.done.emit(payload)
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))
        finally:
            try:
                if dump_file is not None and dump_file.exists():
                    dump_file.unlink()
            except Exception:
                pass
