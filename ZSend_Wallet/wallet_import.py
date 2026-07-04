from __future__ import annotations

import os
import re
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .common import DATA_DIR
from .locales import tr
from .rpc import BitcoinZRPC, RPCError


def _clone_rpc(rpc: BitcoinZRPC) -> BitcoinZRPC:
    clone = getattr(rpc, "clone", None)
    return clone() if callable(clone) else rpc


class ImportKeyWorker(QThread):
    progress       = Signal(str)
    done           = Signal(str)
    error          = Signal(str)
    z_key_accepted = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, key: str, is_z: bool,
                 start_height: int = 0):
        super().__init__()
        self.rpc = _clone_rpc(rpc); self.key = key; self.is_z = is_z
        self.start_height = int(start_height)

    def run(self):
        try:
            if self.is_z:
                self.progress.emit(tr("dialogs.wallet_import.importing_shielded"))
                result = self.rpc.z_ImportKey(self.key, self.start_height, rescan="yes")
                addr   = (result or {}).get("address", "")
                self.z_key_accepted.emit(addr)
                self.done.emit(tr("dialogs.wallet_import.shielded_success"))
            else:
                self.progress.emit(tr("dialogs.wallet_import.importing_private_wait"))
                self.rpc.ImportPrivKey(self.key, rescan=True)
                self.done.emit(tr("dialogs.wallet_import.transparent_success"))
        except RPCError as e:
            self.error.emit(str(e))


class ImportPreflightWorker(QThread):
    done = Signal(object)
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, is_z: bool):
        super().__init__()
        self.rpc = _clone_rpc(rpc)
        self.is_z = bool(is_z)

    @staticmethod
    def _wallet_locked(wallet_info: dict) -> bool:
        if not isinstance(wallet_info, dict):
            return False
        if "unlocked_until" in wallet_info:
            try:
                return int(wallet_info.get("unlocked_until") or 0) <= int(time.time())
            except Exception:
                return True
        status = str(wallet_info.get("encryptionstatus", "")).lower()
        return "lock" in status and "unlock" not in status

    def run(self):
        try:
            chain = self.rpc.getBlockchainInfo()
            if isinstance(chain, dict) and chain.get("pruned"):
                self.done.emit({"pruned": True, "started_block": 0})
                return
            try:
                started_block = int((chain or {}).get("blocks", 0) or 0)
            except Exception:
                started_block = 0
            self.done.emit({"pruned": False, "started_block": started_block})
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))



class FullWalletImportWorker(QThread):
    done = Signal()
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, dump_path: Path):
        super().__init__()
        self.rpc = _clone_rpc(rpc)
        self.dump_path = Path(dump_path)

    def run(self):
        try:
            self.rpc.z_ImportWallet(str(self.dump_path))
            self.done.emit()
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))
        finally:
            try:
                if self.dump_path.exists():
                    self.dump_path.unlink()
            except OSError:
                pass


class LogTailWorker(QThread):
    progress = Signal(float, int)
    done     = Signal()

    _DONE_IDLE_SECS = 120

    def __init__(self):
        super().__init__()
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        log_path = DATA_DIR / "debug.log"
        try:
            last_pos = log_path.stat().st_size
        except OSError:
            last_pos = 0

        last_progress_time: float = 0.0
        seen_progress = False

        while not self._stop:
            time.sleep(1)
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(last_pos)
                    while True:
                        line = f.readline()
                        if not line:
                            last_pos = f.tell()
                            break
                        m_prog  = re.search(r"Progress=(\d+\.\d+)", line)
                        m_block = re.search(r"block[^\d]*(\d+)", line, re.IGNORECASE)
                        if m_prog:
                            frac  = float(m_prog.group(1))
                            block = int(m_block.group(1)) if m_block else 0
                            self.progress.emit(frac, block)
                            last_progress_time = time.time()
                            seen_progress = True
            except OSError:
                pass

            if (seen_progress
                    and last_progress_time > 0
                    and time.time() - last_progress_time > self._DONE_IDLE_SECS
                    and not self._stop):
                self.done.emit()
                return


def _read_log_tail_lines(log_path: Path, max_bytes: int = 256 * 1024) -> list[str]:
    try:
        file_size = log_path.stat().st_size
    except OSError:
        return []

    try:
        with open(log_path, "rb") as fh:
            if file_size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            chunk = fh.read()
    except OSError:
        return []

    text = chunk.decode("utf-8", errors="ignore")
    if file_size > max_bytes:
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1:]
    return text.splitlines()


def read_recent_wallet_rescan_state(max_age_sec: int = 180) -> dict | None:
    log_path = DATA_DIR / "debug.log"
    try:
        if not log_path.exists():
            return None
    except OSError:
        return None

    lines = _read_log_tail_lines(log_path)
    if not lines:
        return None
    progress = None
    block = None
    progress_idx = -1
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        m_prog = re.search(r"Progress=(\d+\.\d+)", line)
        if m_prog:
            progress = float(m_prog.group(1))
            m_block = re.search(r"block[^\d]*(\d+)", line, re.IGNORECASE)
            if m_block:
                block = int(m_block.group(1))
            progress_idx = idx
            break
    if progress is None:
        return None

    try:
        age_sec = max(0, time.time() - log_path.stat().st_mtime)
    except OSError:
        age_sec = max_age_sec + 1
    if age_sec > max_age_sec:
        return None

    recent_window = lines[max(0, progress_idx - 8):progress_idx + 8]
    recent_text = "\n".join(recent_window).lower()
    if "rescann" not in recent_text and "import" not in recent_text and "rescanning" not in recent_text:
        return None

    return {
        "progress": progress,
        "block": block or 0,
        "age_sec": int(age_sec),
    }
