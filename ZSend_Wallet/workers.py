from __future__ import annotations

import os
import re
import time
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from PySide6.QtCore import QThread, Signal

from .common import (
    DATA_DIR,
    PARAMS_DIR,
    PARAMS_FILES,
    _NODE_MSGS,
    _is_z_addr,
    _sha256_file,
    find_node,
    launch_node,
    node_running,
)
from .debug_runtime import debug_exception, debug_log
from .rpc import BitcoinZRPC, RPCError
from .wallet_cache import WalletCache, btcz_to_zat

_PARAMS_DOWNLOAD_CHUNK = 1 << 20
_PARAMS_PROGRESS_MIN_BYTES = 4 << 20
_PARAMS_PROGRESS_MIN_SECONDS = 0.25
_REINDEX_PROGRESS_MAX_VALUE = 9500
_REINDEX_FINALIZING_VALUE = 10000
_REINDEX_LOG_TAIL_MAX_BYTES = 256 * 1024
_REINDEX_BLOCK_FILE_RE = re.compile(r"Reindexing block file blk(\d{5})\.dat", re.IGNORECASE)
_BOOTSTRAP_START_RE = re.compile(r"Importing bootstrap\.dat", re.IGNORECASE)
_BOOTSTRAP_PREALLOC_RE = re.compile(r"Pre-allocating up to position 0x[0-9a-f]+ in blk(\d{5})\.dat", re.IGNORECASE)
_BOOTSTRAP_LOADED_RE = re.compile(r"Loaded \d+ blocks from external file", re.IGNORECASE)
_BOOTSTRAP_NODE_START_MARKERS = ("BitcoinZ version",)
_NODE_START_MARKERS = (
    "BitcoinZ version",
    "init message:",
    "scheduler thread start",
    "dnsseed thread start",
    "net thread start",
)


def _clone_rpc(rpc: BitcoinZRPC) -> BitcoinZRPC:
    clone = getattr(rpc, "clone", None)
    return clone() if callable(clone) else rpc


def reindex_progress_value(current_index: int, max_index: int) -> int:
    if max_index <= 0:
        return 0
    current = max(0, min(int(current_index), int(max_index)))
    return max(0, min(_REINDEX_PROGRESS_MAX_VALUE, round((current / int(max_index)) * _REINDEX_PROGRESS_MAX_VALUE)))


def reindex_block_file_label(current_index: int, max_index: int) -> str:
    return f"Reindexing block file {max(0, int(current_index))} / {max(0, int(max_index))}"


def _chain_sync_percent(chain: dict) -> float:
    try:
        return max(0.0, min(100.0, float(chain.get("verificationprogress")) * 100.0))
    except Exception:
        pass
    try:
        blocks = int(chain.get("blocks", 0) or 0)
        headers = int(chain.get("headers", 0) or 0)
        if headers > 0:
            return max(0.0, min(100.0, (blocks / headers) * 100.0))
    except Exception:
        pass
    return 0.0


def max_reindex_blk_index(blocks_dir: Path | None = None) -> int:
    blocks_dir = blocks_dir or (DATA_DIR / "blocks")
    indexes: list[int] = []
    try:
        candidates = list(blocks_dir.glob("blk*.dat"))
    except OSError:
        return -1
    for path in candidates:
        m = re.fullmatch(r"blk(\d{5})\.dat", path.name, re.IGNORECASE)
        if not m:
            continue
        try:
            indexes.append(int(m.group(1)))
        except ValueError:
            pass
    return max(indexes) if indexes else -1


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _read_log_tail_after(log_path: Path, start_offset: int = 0, max_bytes: int = _REINDEX_LOG_TAIL_MAX_BYTES) -> list[str]:
    try:
        size = int(log_path.stat().st_size)
    except OSError:
        return []
    if size <= 0:
        return []
    if start_offset < 0 or start_offset > size:
        start_offset = 0
    read_start = max(start_offset, max(0, size - max_bytes))
    try:
        with open(log_path, "rb") as fh:
            fh.seek(read_start, os.SEEK_SET)
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    if read_start > start_offset:
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1:]
    return text.splitlines()


def reindex_progress_from_debug_log(
    log_path: Path | None = None,
    *,
    max_blk_index: int,
    start_offset: int = 0,
    include_finished: bool = True,
    reset_on_node_start: bool = False,
) -> dict | None:
    log_path = log_path or (DATA_DIR / "debug.log")
    lines = _read_log_tail_after(log_path, start_offset=start_offset)
    if not lines:
        return None

    last_index: int | None = None
    finished = False
    for line in lines:
        if reset_on_node_start and any(marker in line for marker in _NODE_START_MARKERS):
            last_index = None
            finished = False
            continue
        match = _REINDEX_BLOCK_FILE_RE.search(line)
        if match:
            last_index = int(match.group(1))
            finished = False
            continue
        if "Reindexing finished" in line:
            finished = True

    if finished:
        if not include_finished:
            return None
        if max_blk_index >= 0:
            return {
                "phase": "finalizing",
                "message": "Finalizing reindex",
                "bar_text": "Finalizing reindex",
                "bar_value": _REINDEX_FINALIZING_VALUE,
                "current_blk_index": max_blk_index,
                "max_blk_index": max_blk_index,
            }
        return {
            "phase": "finalizing",
            "message": "Finalizing reindex",
            "bar_text": "Finalizing reindex",
            "bar_value": _REINDEX_FINALIZING_VALUE,
        }

    if last_index is not None:
        if max_blk_index >= 0:
            current = max(0, min(last_index, max_blk_index))
            return {
                "phase": "reindex_files",
                "message": reindex_block_file_label(current, max_blk_index),
                "bar_text": reindex_block_file_label(current, max_blk_index),
                "bar_value": reindex_progress_value(current, max_blk_index),
                "current_blk_index": current,
                "max_blk_index": max_blk_index,
            }
        return {
            "phase": "reindex_files",
            "message": f"Reindexing block file {last_index}",
            "bar_text": "Reindexing block files",
            "bar_value": 0,
        }

    return None


def bootstrap_progress_from_debug_log(
    log_path: Path | None = None,
    *,
    start_offset: int = 0,
    reset_on_node_start: bool = True,
) -> dict | None:
    log_path = log_path or (DATA_DIR / "debug.log")
    lines = _read_log_tail_after(log_path, start_offset=start_offset)
    if not lines:
        return None

    active = False
    current_index: int | None = None
    for line in lines:
        if reset_on_node_start and any(marker in line for marker in _BOOTSTRAP_NODE_START_MARKERS):
            active = False
            current_index = None
            continue
        if _BOOTSTRAP_START_RE.search(line):
            active = True
            current_index = None
            continue
        if not active:
            continue
        match = _BOOTSTRAP_PREALLOC_RE.search(line)
        if match:
            try:
                current_index = int(match.group(1))
            except ValueError:
                current_index = None
            continue
        if _BOOTSTRAP_LOADED_RE.search(line):
            active = False
            current_index = None

    if not active:
        return None
    if current_index is not None:
        blk_name = f"blk{current_index:05d}.dat"
        return {
            "phase": "bootstrap_files",
            "message": "Bootstrap sync",
            "bar_text": blk_name,
            "bar_value": 0,
            "current_blk_index": current_index,
            "blk_name": blk_name,
        }
    return {
        "phase": "bootstrap_files",
        "message": "Bootstrap sync",
        "bar_text": "Bootstrap sync",
        "bar_value": 0,
    }


class ParamsWorker(QThread):
    status   = Signal(str)
    progress = Signal(int, int)
    done     = Signal()
    failed   = Signal(str)

    def run(self):
        try:
            debug_log("ParamsWorker started", params_dir=str(PARAMS_DIR), file_count=len(PARAMS_FILES))
            PARAMS_DIR.mkdir(parents=True, exist_ok=True)
            self.status.emit("Checking ZcashParams")

            corrupt = []
            for idx, pf in enumerate(PARAMS_FILES, start=1):
                path = PARAMS_DIR / pf["name"]
                debug_log(
                    "Checking params file",
                    index=idx,
                    name=pf["name"],
                    path=str(path),
                    exists=path.exists(),
                )
                if path.exists():
                    self.status.emit(f"Verifying {pf['name']}")
                    size_mismatch = False
                    try:
                        actual_size = int(path.stat().st_size)
                        expected_size = int(pf["size"])
                        debug_log(
                            "Params file size",
                            name=pf["name"],
                            actual_size=actual_size,
                            expected_size=expected_size,
                        )
                        size_mismatch = actual_size != expected_size
                        if size_mismatch:
                            debug_log(
                                "Params size mismatch detected; deferring removal until hash check",
                                name=pf["name"],
                                actual_size=actual_size,
                                expected_size=expected_size,
                            )
                    except OSError as exc:
                        debug_exception(f"Failed to stat/unlink params file {pf['name']}", exc)
                        corrupt.append(pf["name"])
                        continue

                    if not path.exists():
                        debug_log("Params file disappeared before hashing", name=pf["name"])
                        corrupt.append(pf["name"])
                        continue

                    debug_log("Hashing params file", name=pf["name"])
                    try:
                        actual_sha = _sha256_file(path)
                    except FileNotFoundError as exc:
                        debug_exception(f"Params file missing during hashing {pf['name']}", exc)
                        corrupt.append(pf["name"])
                        continue
                    debug_log("Params file hash complete", name=pf["name"], sha256=actual_sha)
                    if actual_sha == pf["sha256"]:
                        if size_mismatch:
                            debug_log(
                                "Params file accepted despite size metadata mismatch because hash matched",
                                name=pf["name"],
                            )
                        continue

                    if actual_sha != pf["sha256"]:
                        corrupt.append(pf["name"])
                        debug_log("Params hash mismatch", name=pf["name"], expected_sha=pf["sha256"], actual_sha=actual_sha)
                        try:
                            path.unlink()
                        except OSError as exc:
                            debug_exception(f"Failed to remove corrupt params file {pf['name']}", exc)

            if corrupt:
                debug_log("Corrupt params removed", files=corrupt)
                self.status.emit(f"Corrupt files removed, re-downloading: {', '.join(corrupt)}")

            to_download = [pf for pf in PARAMS_FILES if not (PARAMS_DIR / pf["name"]).exists()]
            debug_log(
                "Params download plan prepared",
                download_count=len(to_download),
                download_names=[pf["name"] for pf in to_download],
            )

            if not to_download:
                debug_log("Params verification completed without downloads")
                self.status.emit("ZcashParams OK.")
                self.done.emit()
                return

            total = sum(pf["size"] for pf in to_download)
            existing_by_name: dict[str, int] = {}
            for pf in to_download:
                path = PARAMS_DIR / pf["name"]
                try:
                    existing_by_name[pf["name"]] = min(path.stat().st_size, int(pf["size"])) if path.exists() else 0
                except OSError:
                    existing_by_name[pf["name"]] = 0
            done_bytes = sum(existing_by_name.values())
            self.progress.emit(done_bytes, total)
            last_progress_bytes = done_bytes
            last_progress_time = time.monotonic()

            for pf in to_download:
                path    = PARAMS_DIR / pf["name"]
                attempt = 0

                while attempt < 2:
                    attempt += 1
                    existing = path.stat().st_size if path.exists() else 0
                    accounted_existing = min(existing, int(pf["size"]))
                    debug_log(
                        "Starting params download attempt",
                        name=pf["name"],
                        attempt=attempt,
                        path=str(path),
                        existing_bytes=existing,
                        url=pf["url"],
                    )
                    self.status.emit(f"Downloading {pf['name']}")
                    try:
                        headers = {"Range": f"bytes={existing}-"} if existing else {}
                        with requests.get(pf["url"], headers=headers, stream=True, timeout=30) as r:
                            debug_log(
                                "Params HTTP response",
                                name=pf["name"],
                                status_code=r.status_code,
                                content_length=r.headers.get("Content-Length"),
                                content_range=r.headers.get("Content-Range"),
                            )
                            if r.status_code not in (200, 206):
                                raise IOError(f"HTTP {r.status_code}")
                            mode = "ab" if existing and r.status_code == 206 else "wb"
                            if mode == "wb":
                                done_bytes = max(0, done_bytes - accounted_existing)
                                existing = 0
                            with open(path, mode) as f:
                                for chunk in r.iter_content(_PARAMS_DOWNLOAD_CHUNK):
                                    if not chunk:
                                        continue
                                    f.write(chunk)
                                    existing += len(chunk)
                                    done_bytes += len(chunk)
                                    now = time.monotonic()
                                    if (
                                        done_bytes - last_progress_bytes >= _PARAMS_PROGRESS_MIN_BYTES
                                        or now - last_progress_time >= _PARAMS_PROGRESS_MIN_SECONDS
                                        or done_bytes >= total
                                    ):
                                        self.progress.emit(min(done_bytes, total), total)
                                        last_progress_bytes = done_bytes
                                        last_progress_time = now
                                self.progress.emit(min(done_bytes, total), total)
                        debug_log("Params download finished", name=pf["name"], final_size=path.stat().st_size if path.exists() else None)
                    except Exception as e:
                        debug_exception(f"Params download attempt failed for {pf['name']}", e)
                        if attempt >= 2:
                            self.failed.emit(f"Failed to download {pf['name']}:\n{e}")
                            return

                    if not path.exists():
                        debug_log("Downloaded params file missing before verification", name=pf["name"])
                        if attempt >= 2:
                            self.failed.emit(f"Downloaded file disappeared before verification: {pf['name']}")
                            return
                        continue

                    self.status.emit(f"Verifying {pf['name']}")
                    debug_log("Verifying downloaded params hash", name=pf["name"])
                    try:
                        actual_sha = _sha256_file(path)
                    except FileNotFoundError as exc:
                        debug_exception(f"Downloaded params missing during verification {pf['name']}", exc)
                        actual_sha = ""
                    debug_log("Downloaded params hash complete", name=pf["name"], sha256=actual_sha)
                    if actual_sha == pf["sha256"]:
                        break
                    if attempt >= 2:
                        self.failed.emit(
                            f"File {pf['name']} is corrupted after download.\n"
                            "Please check your internet connection and try again."
                        )
                        return
                    if path.exists():
                        path.unlink()
                        debug_log("Removed downloaded params after hash mismatch", name=pf["name"])
                    self.status.emit(f"Hash mismatch for {pf['name']}, retrying")

            debug_log("ParamsWorker completed successfully")
            self.status.emit("ZcashParams OK.")
            self.done.emit()
        except Exception as e:
            debug_exception("ParamsWorker failed unexpectedly", e)
            self.failed.emit(f"ZcashParams check failed unexpectedly:\n{e}")


_NODE_READY_POLL_SECS = 3
_NODE_READY_MAX_COLD_WAIT_SECS = 360


def _is_node_busy_message(code: int, message: str) -> bool:
    return code == -28 or any(k in message for k in _NODE_MSGS)


class NodeStartWorker(QThread):
    status = Signal(str)
    ready  = Signal()
    failed = Signal(str)

    def __init__(self, rpc: BitcoinZRPC):
        super().__init__(); self.rpc = _clone_rpc(rpc)

    def run(self):
        try:
            debug_log("NodeStartWorker started")
            self.status.emit("Checking node status")

            should_launch = True
            try:
                self.rpc.getBlockchainInfo()
                debug_log("Configured RPC responded before launch")
                self.status.emit("Node is ready!")
                self.ready.emit()
                return
            except RPCError as e:
                msg = str(e)
                debug_log("Initial configured RPC probe failed", code=e.code, rpc_message=msg)
                if e.code == 401:
                    self.failed.emit(
                        "HTTP 401: rpcuser/rpcpassword mismatch with bitcoinz.conf.\n"
                        "Delete bitcoinz.conf and restart the wallet to regenerate it."
                    )
                    return
                if e.code == 403:
                    self.failed.emit(
                        "HTTP 403: node is rejecting connections.\n"
                        "Add these lines to bitcoinz.conf:\n"
                        "  server=1\n"
                        "  rpcallowip=127.0.0.1\n"
                        "then restart the node."
                    )
                    return
                if _is_node_busy_message(e.code, msg):
                    should_launch = False
            except Exception as e:
                debug_exception("Initial configured RPC probe failed unexpectedly", e)

            binary = find_node()
            debug_log(
                "Node binary lookup",
                binary=str(binary) if binary else None,
                tasklist_hint=node_running(),
                should_launch=should_launch,
            )
            if binary and should_launch:
                self.status.emit(f"Starting {binary.name}")
                proc = launch_node(binary)
                debug_log("Node launch attempted", binary=str(binary), launched=proc is not None)
                time.sleep(5)
            elif not binary:
                self.status.emit("bitcoinzd.exe not found - waiting for manual start")

            attempt = 0
            cold_wait_started = time.monotonic()
            seen_node_busy = False
            while True:
                attempt += 1
                try:
                    debug_log("RPC readiness probe", attempt=attempt)
                    self.rpc.getBlockchainInfo()
                    debug_log("Node RPC responded successfully", attempt=attempt)
                    self.status.emit("Node is ready!")
                    self.ready.emit()
                    return
                except RPCError as e:
                    msg  = str(e)
                    code = e.code
                    node_busy = _is_node_busy_message(code, msg)
                    seen_node_busy = seen_node_busy or node_busy
                    debug_log(
                        "RPC readiness probe failed",
                        attempt=attempt,
                        code=code,
                        rpc_message=msg,
                        node_busy=node_busy,
                        seen_node_busy=seen_node_busy,
                    )

                    if code == 401:
                        self.failed.emit(
                            "HTTP 401: rpcuser/rpcpassword mismatch with bitcoinz.conf.\n"
                            "Delete bitcoinz.conf and restart the wallet to regenerate it."
                        )
                        return
                    if code == 403:
                        self.failed.emit(
                            "HTTP 403: node is rejecting connections.\n"
                            "Add these lines to bitcoinz.conf:\n"
                            "  server=1\n"
                            "  rpcallowip=127.0.0.1\n"
                            "then restart the node."
                        )
                        return

                    if node_busy:
                        display = next(
                            (v for k, v in _NODE_MSGS.items() if k in msg),
                            msg.splitlines()[0]
                        )
                        self.status.emit(display)
                    elif "Connection refused" in msg or "refused" in msg.lower():
                        self.status.emit(f"Waiting for node ({attempt * _NODE_READY_POLL_SECS}s)")
                    else:
                        self.status.emit(f"Waiting ({msg.splitlines()[0][:60]})")

                if not seen_node_busy:
                    cold_wait_elapsed = time.monotonic() - cold_wait_started
                    if cold_wait_elapsed >= _NODE_READY_MAX_COLD_WAIT_SECS:
                        debug_log(
                            "NodeStartWorker timed out waiting for cold node",
                            elapsed_seconds=round(cold_wait_elapsed, 1),
                        )
                        self.failed.emit(
                            "Node did not respond within 360 seconds.\n"
                            "Check that bitcoinzd.exe is present and bitcoinz.conf is correct.\n"
                            "Open Diagnostics for details."
                        )
                        return

                time.sleep(_NODE_READY_POLL_SECS)
        except Exception as e:
            debug_exception("NodeStartWorker failed unexpectedly", e)
            self.failed.emit(f"Node start failed unexpectedly:\n{e}")


class MaintenanceRestartWorker(QThread):
    status = Signal(str)
    progress = Signal(object)
    done = Signal(str)
    error = Signal(str)

    _POLL_SECS = 3
    _STOP_WAIT_SECS = 300

    def __init__(self, rpc: BitcoinZRPC, mode: str):
        super().__init__()
        mode = str(mode or "").strip().lower()
        if mode not in {"rescan", "reindex"}:
            mode = "rescan"
        self.rpc = _clone_rpc(rpc)
        self.mode = mode
        self._stop_requested = False
        self._detach_requested = False
        self._debug_log_start_offset = 0
        self._max_blk_index = -1
        self._last_progress_key: tuple | None = None

    def _emit_progress(
        self,
        phase: str,
        message: str,
        *,
        bar_text: str | None = None,
        bar_value: int = 0,
        percent: float | None = None,
        current_blk_index: int | None = None,
        max_blk_index: int | None = None,
    ):
        payload = {
            "phase": phase,
            "message": message,
            "bar_text": bar_text or "",
            "bar_value": max(0, min(10000, int(bar_value or 0))),
        }
        if percent is not None:
            try:
                payload["percent"] = max(0.0, min(100.0, float(percent)))
            except Exception:
                payload["percent"] = 0.0
        if current_blk_index is not None:
            payload["current_blk_index"] = int(current_blk_index)
        if max_blk_index is not None:
            payload["max_blk_index"] = int(max_blk_index)
        key = (
            payload["phase"],
            payload["message"],
            payload["bar_text"],
            payload["bar_value"],
            payload.get("percent"),
            payload.get("current_blk_index"),
            payload.get("max_blk_index"),
        )
        if key == self._last_progress_key:
            return
        self._last_progress_key = key
        self.progress.emit(payload)

    def _read_reindex_progress_hint(self) -> dict | None:
        if self.mode != "reindex":
            return None
        return reindex_progress_from_debug_log(
            DATA_DIR / "debug.log",
            max_blk_index=self._max_blk_index,
            start_offset=self._debug_log_start_offset,
        )

    def _emit_reindex_progress_hint(self, hint: dict | None = None) -> bool:
        if self.mode != "reindex":
            return False
        hint = hint or self._read_reindex_progress_hint()
        if not hint:
            return False
        self._emit_progress(
            str(hint.get("phase", "reindex_files")),
            str(hint.get("message", "")),
            bar_text=str(hint.get("bar_text", "")),
            bar_value=int(hint.get("bar_value", 0) or 0),
            current_blk_index=hint.get("current_blk_index"),
            max_blk_index=hint.get("max_blk_index"),
        )
        return True

    def stop(self):
        self._stop_requested = True
        try:
            _clone_rpc(self.rpc).stopNode()
        except Exception:
            pass

    def detach(self):
        self._detach_requested = True

    def _sleep_poll(self, seconds: float):
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            if self._stop_requested:
                raise RuntimeError("Node maintenance cancelled")
            if self._detach_requested:
                raise RuntimeError("Node maintenance detached")
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _is_connection_refused(message: str) -> bool:
        msg = str(message or "").lower()
        return (
            "connection refused" in msg
            or "failed to establish a new connection" in msg
            or "actively refused" in msg
            or "max retries exceeded" in msg
        )

    def _status_from_rpc_error(self, e: RPCError) -> str:
        msg = str(e)
        if self._is_connection_refused(msg):
            return "Waiting for BitcoinZ node to start"
        return next((v for k, v in _NODE_MSGS.items() if k in msg), msg.splitlines()[0][:80])

    def _wait_for_rpc_down(self):
        deadline = time.monotonic() + self._STOP_WAIT_SECS
        while time.monotonic() < deadline:
            if self._stop_requested:
                raise RuntimeError("Node maintenance cancelled")
            rpc_down = False
            try:
                self.rpc.getBlockchainInfo()
            except RPCError as e:
                if "stopping" in str(e).lower():
                    rpc_down = True
                elif self._is_connection_refused(str(e)):
                    rpc_down = True
                else:
                    rpc_down = True
            except Exception:
                rpc_down = True
            if rpc_down and not node_running():
                return
            self.status.emit("Waiting for node to stop")
            time.sleep(1)

    def _wait_until_ready(self, proc=None):
        attempt = 0
        while True:
            if self._stop_requested:
                raise RuntimeError("Node maintenance cancelled")
            if self._detach_requested:
                raise RuntimeError("Node maintenance detached")
            if proc is not None and proc.poll() is not None and not node_running():
                raise RuntimeError(f"bitcoinzd.exe exited before RPC became available (exit code {proc.returncode})")
            reindex_hint = self._read_reindex_progress_hint()
            hint_emitted = self._emit_reindex_progress_hint(reindex_hint)
            attempt += 1
            chain = None
            try:
                chain = self.rpc.getBlockchainInfo() or {}
                if bool(chain.get("reindex")):
                    if not hint_emitted:
                        self._emit_progress("reindex_files", "Reindexing blockchain", bar_text="Reindexing block files", bar_value=0)
                        self.status.emit("Reindexing blockchain")
                    self._sleep_poll(self._POLL_SECS)
                    continue
                if self.mode == "reindex" and isinstance(reindex_hint, dict) and str(reindex_hint.get("phase", "")) == "reindex_files":
                    self._sleep_poll(self._POLL_SECS)
                    continue
                pct = _chain_sync_percent(chain)
                if bool(chain.get("initialblockdownload")) or (
                    chain.get("verificationprogress") is not None and pct < 99.9
                ):
                    self._emit_progress(
                        "syncing",
                        "Synchronizing blockchain",
                        bar_value=int(pct * 100),
                        percent=pct,
                    )
                    self.status.emit("Synchronizing blockchain")
                    self._sleep_poll(self._POLL_SECS)
                    continue
                # Probe a wallet RPC that does not touch the keypool. During
                # -rescan this usually returns a busy error until wallet scan
                # has finished, while getblockchaininfo can already be ready.
                self.rpc.z_getTotalBalance()
                return
            except RPCError as e:
                if e.code in (401, 403):
                    raise
                if self.mode == "reindex" and _is_reindex_err(e):
                    if isinstance(chain, dict):
                        pct = _chain_sync_percent(chain)
                        if chain.get("verificationprogress") is not None and pct < 99.9:
                            self._emit_progress(
                                "syncing",
                                "Synchronizing blockchain",
                                bar_value=int(pct * 100),
                                percent=pct,
                            )
                            self.status.emit("Synchronizing blockchain")
                            self._sleep_poll(self._POLL_SECS)
                            continue
                    if not self._emit_reindex_progress_hint():
                        self._emit_progress(
                            "finalizing",
                            "Finalizing reindex",
                            bar_text="Finalizing reindex",
                            bar_value=_REINDEX_FINALIZING_VALUE,
                        )
                    self._sleep_poll(self._POLL_SECS)
                    continue
                self.status.emit(self._status_from_rpc_error(e))
            except Exception:
                self.status.emit(f"Waiting for node ({attempt * self._POLL_SECS}s)")
            self._sleep_poll(self._POLL_SECS)

    def _recover_normal_node(self, binary, failed_label: str, failure: Exception):
        self.status.emit("Maintenance start failed; restoring normal node")
        self._emit_progress("recovering", "Maintenance start failed; restoring normal node")
        proc = launch_node(binary)
        if proc is None:
            raise RuntimeError(f"{failure}\n\nRecovery failed: bitcoinzd.exe could not be started normally.")
        self._wait_until_ready(proc)
        raise RuntimeError(f"{failed_label} did not start successfully:\n{failure}\n\nBitcoinZ node was restarted normally.")

    def run(self):
        try:
            label = f"-{self.mode}"
            self.status.emit("Stopping BitcoinZ node")
            self._emit_progress("stopping", "Stopping BitcoinZ node")
            try:
                self.rpc.stopNode()
            except Exception as exc:
                debug_exception("Maintenance stopNode failed or node was already down", exc)
            self._wait_for_rpc_down()

            binary = find_node()
            if binary is None:
                self.error.emit("bitcoinzd.exe not found")
                return

            if self.mode == "reindex":
                self._max_blk_index = max_reindex_blk_index()
                self._debug_log_start_offset = _file_size(DATA_DIR / "debug.log")
            self.status.emit(f"Starting BitcoinZ node with {label}")
            self._emit_progress("starting", f"Starting BitcoinZ node with {label}")
            proc = launch_node(binary, extra_args=[label])
            if proc is None:
                self._recover_normal_node(binary, label, RuntimeError(f"Failed to start bitcoinzd.exe with {label}"))

            self.status.emit(f"Waiting for {label} to finish")
            if self.mode == "reindex":
                self._emit_progress("reindex_files", "Reindexing blockchain", bar_text="Reindexing block files", bar_value=0)
            try:
                self._wait_until_ready(proc)
            except RuntimeError as e:
                if "cancelled" in str(e).lower() or "detached" in str(e).lower():
                    raise
                self._recover_normal_node(binary, label, e)
            self.done.emit(self.mode)
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            if "detached" in str(e).lower():
                debug_log("MaintenanceRestartWorker detached; node left running", mode=self.mode)
                return
            debug_exception("MaintenanceRestartWorker failed unexpectedly", e)
            self.error.emit(str(e))


_REINDEX_PHRASES = ("reindexing", "while reindexing", "disabled while", "reindex")


def _is_reindex_err(e: RPCError) -> bool:
    return any(p in str(e).lower() for p in _REINDEX_PHRASES)


class RefreshWorker(QThread):
    done       = Signal(object)
    error      = Signal(str)
    reindexing = Signal(object)
    step       = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, cache: WalletCache | None = None,
                 force_full: bool = False):
        super().__init__()
        self.rpc = _clone_rpc(rpc)
        self.cache = cache
        self.force_full = force_full
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        self.requestInterruption()

    def _should_stop(self) -> bool:
        return bool(self._stop_requested or self.isInterruptionRequested())

    @staticmethod
    def _snapshot_block(data: dict) -> int | None:
        try:
            return int((data.get("info") or {}).get("blocks"))
        except Exception:
            return None

    @staticmethod
    def _tx_entry_key(tx: dict) -> tuple[str, str, str, str, str, str, str]:
        try:
            amount = f"{float(tx.get('amount', 0) or 0):.8f}"
        except Exception:
            amount = "0.00000000"
        return (
            str(tx.get("txid", "") or ""),
            str(tx.get("category", "") or ""),
            str(tx.get("address", "") or ""),
            amount,
            str(tx.get("outindex", tx.get("output", "")) if tx.get("outindex", tx.get("output", "")) is not None else ""),
            str(tx.get("jsindex", "") if tx.get("jsindex", "") is not None else ""),
            str(tx.get("jsoutindex", "") if tx.get("jsoutindex", "") is not None else ""),
        )

    @staticmethod
    def _shielded_item_amount(item: dict) -> float:
        for key in ("valueZat", "amountZat"):
            value = item.get(key)
            if value not in (None, ""):
                try:
                    return int(value) / 100_000_000
                except Exception:
                    pass
        for key in ("value", "amount"):
            value = item.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except Exception:
                    pass
        return 0.0

    @staticmethod
    def _status_from_confirmations(confirmations: int) -> str:
        if confirmations < 0:
            return "conflicted"
        if confirmations == 0:
            return "pending"
        return "confirmed"

    def _node_tx_metadata(self, txid: str) -> dict:
        try:
            source = self.rpc.getTransaction(txid) or {}
        except Exception:
            try:
                source = self.rpc.getRawTransaction(txid) or {}
            except Exception:
                source = {}

        try:
            confirmations = int(source.get("confirmations", 0) or 0)
        except Exception:
            confirmations = 0
        blockhash = source.get("blockhash") or ""
        blockheight = source.get("height", source.get("blockheight"))
        if blockheight is None and blockhash:
            try:
                block = self.rpc.getBlock(blockhash)
                if isinstance(block, dict) and block.get("height") is not None:
                    blockheight = int(block.get("height"))
            except Exception:
                pass
        return {
            "confirmations": confirmations,
            "blockhash": blockhash,
            "blockheight": blockheight,
            "blockindex": source.get("blockindex"),
            "time": source.get("time") or source.get("blocktime"),
            "blocktime": source.get("blocktime"),
            "timereceived": source.get("timereceived") or source.get("time") or source.get("blocktime"),
            "status": self._status_from_confirmations(confirmations),
            "_cache_meta": {"blockheight": blockheight},
        }

    def _shielded_receive_entry(
        self,
        txid: str,
        address: str,
        amount: float,
        *,
        note: dict | None = None,
        metadata: dict | None = None,
        details: dict | None = None,
    ) -> dict | None:
        txid = str(txid or "").strip()
        address = str(address or "").strip()
        try:
            amount = float(amount)
        except Exception:
            amount = 0.0
        if not txid or not address or amount <= 0:
            return None

        meta = dict(metadata or {})
        cache_meta = dict(meta.pop("_cache_meta", {}) or {})
        if details is not None:
            cache_meta["details"] = details
        entry = {
            "txid": txid,
            "category": "receive",
            "address": address,
            "amount": amount,
            **meta,
            "_cache_meta": cache_meta,
            "_synthetic": "shielded_receive",
        }
        if isinstance(note, dict):
            for key in ("memoStr", "memo", "outindex", "output", "jsindex", "jsoutindex", "change"):
                if key in note:
                    entry[key] = note.get(key)
            entry["_shielded_note"] = note
        return entry

    def _shielded_receive_entries_from_view(self, tx: dict, details: dict | None) -> list[dict]:
        if not isinstance(details, dict):
            return []
        txid = str(details.get("txid") or tx.get("txid", "") or "").strip()
        if not txid:
            return []
        metadata = {
            "confirmations": int(tx.get("confirmations", 0) or 0),
            "blockhash": tx.get("blockhash", ""),
            "blockheight": tx.get("blockheight"),
            "blockindex": tx.get("blockindex"),
            "time": tx.get("time") or tx.get("blocktime"),
            "blocktime": tx.get("blocktime"),
            "timereceived": tx.get("timereceived") or tx.get("time") or tx.get("blocktime"),
        }
        metadata["status"] = self._status_from_confirmations(int(metadata["confirmations"] or 0))
        metadata["_cache_meta"] = dict((tx.get("_cache_meta") or {}), details=details)

        entries: list[dict] = []
        for output in details.get("outputs", []) or []:
            if output.get("outgoing") is True:
                continue
            entry = self._shielded_receive_entry(
                txid,
                str(output.get("address", "") or ""),
                self._shielded_item_amount(output),
                note=output,
                metadata=metadata,
                details=details,
            )
            if entry is not None:
                entries.append(entry)
        return entries

    def _store_cache_snapshot(self, data: dict, job_type: str = "refresh") -> None:
        if self.cache is None:
            return
        if self._should_stop():
            return
        job_id = None
        try:
            job_id = self.cache.start_sync_job(
                job_type,
                last_seen_block=self._snapshot_block(data),
            )
            self.cache.store_refresh_snapshot(data)
            self.cache.finish_sync_job(
                job_id,
                status="success",
                last_seen_block=self._snapshot_block(data),
            )
        except Exception as e:
            try:
                if job_id is not None:
                    self.cache.finish_sync_job(
                        job_id,
                        status="failed",
                        last_error=str(e),
                        last_seen_block=self._snapshot_block(data),
                    )
                self.cache.set_state("last_cache_error", str(e))
            except Exception:
                pass

    def _run_reconciliation(self, data: dict) -> None:
        if self.cache is None:
            return
        try:
            cached_sum_zat = self.cache.get_total_address_balance_zat(include_hidden=False)
            total_zat = btcz_to_zat((data.get("total_bal") or {}).get("total", 0))
            delta = abs(cached_sum_zat - total_zat)
            self.cache.set_state("last_reconcile_delta_zat", delta)
            self.cache.set_state("last_reconciled_at", int(time.time()))

            live_txs = [tx for tx in (data.get("txs") or []) if tx.get("txid")]
            live_map = {str(tx.get("txid")): tx for tx in live_txs}
            live_txids = set(live_map)
            if live_txids:
                self.cache.clear_transactions_stale(live_txids)

            cached_rows = self.cache.list_transactions(limit=500, newest_first=True)
            cached_txids = {str(row.get("txid", "")) for row in cached_rows if row.get("txid")}
            for row in cached_rows:
                if self._should_stop():
                    return
                txid = str(row.get("txid", "") or "")
                if not txid or txid not in live_map:
                    continue
                live_tx = live_map[txid]
                live_confirms = int(live_tx.get("confirmations", 0) or 0)
                live_blockhash = str(live_tx.get("blockhash", "") or "")
                cached_blockhash = str(row.get("blockhash", "") or "")
                status = None
                if live_confirms < 0:
                    status = "conflicted"
                elif cached_blockhash and live_blockhash and cached_blockhash != live_blockhash:
                    status = "reorged"
                elif live_confirms == 0:
                    status = "pending"
                elif live_confirms > 0:
                    status = "confirmed"
                if status is not None:
                    self.cache.update_transaction_reconcile(
                        txid,
                        status=status,
                        confirmations=live_confirms,
                        blockhash=live_blockhash or None,
                    )

            if live_txids and data.get("tx_snapshot_complete"):
                stale_txids = cached_txids - live_txids
                if stale_txids:
                    self.cache.mark_transactions_stale(stale_txids)
        except Exception:
            pass

    def _enrich_transactions(self, txs: list) -> list:
        if not txs:
            return txs
        block_heights: dict[str, int] = {}
        for blockhash in {tx.get("blockhash") for tx in txs if tx.get("blockhash")}:
            try:
                block = self.rpc.getBlock(blockhash)
                if isinstance(block, dict) and block.get("height") is not None:
                    block_heights[blockhash] = int(block.get("height"))
            except Exception:
                pass

        z_view_count = 0
        enriched: list[dict] = []
        seen_entries = {self._tx_entry_key(tx) for tx in txs}
        for tx in txs:
            meta = tx.setdefault("_cache_meta", {})
            blockhash = tx.get("blockhash")
            if blockhash in block_heights:
                tx["blockheight"] = block_heights[blockhash]
                meta["blockheight"] = block_heights[blockhash]
            txid = tx.get("txid")
            details = None
            if txid and (not tx.get("address") or tx.get("category") == "send") and z_view_count < 20:
                try:
                    details = self.rpc.z_viewTransaction(txid)
                    meta["details"] = details
                    z_view_count += 1
                except Exception:
                    pass
            enriched.append(tx)
            for shielded_entry in self._shielded_receive_entries_from_view(tx, details):
                key = self._tx_entry_key(shielded_entry)
                if key in seen_entries:
                    continue
                seen_entries.add(key)
                enriched.append(shielded_entry)
        return enriched

    def _merge_shielded_received_transactions(self, txs: list, z_addrs: list[str]) -> list:
        if not z_addrs:
            return txs
        rows = list(txs or [])
        seen_entries = {self._tx_entry_key(tx) for tx in rows}
        metadata_cache: dict[str, dict] = {}
        details_cache: dict[str, dict | None] = {}

        for zaddr in z_addrs:
            try:
                notes = self.rpc.z_listReceivedByAddress(zaddr, 0) or []
            except Exception:
                continue
            for note in notes:
                if not isinstance(note, dict):
                    continue
                txid = str(note.get("txid", "") or "").strip()
                if not txid:
                    continue
                if txid not in metadata_cache:
                    metadata_cache[txid] = self._node_tx_metadata(txid)
                if txid not in details_cache:
                    try:
                        details_cache[txid] = self.rpc.z_viewTransaction(txid) or {}
                    except Exception:
                        details_cache[txid] = None
                entry = self._shielded_receive_entry(
                    txid,
                    str(note.get("address") or zaddr),
                    self._shielded_item_amount(note),
                    note=note,
                    metadata=metadata_cache[txid],
                    details=details_cache[txid],
                )
                if entry is None:
                    continue
                key = self._tx_entry_key(entry)
                if key in seen_entries:
                    continue
                seen_entries.add(key)
                rows.append(entry)
        return rows

    def _operation_transaction_from_node(self, op: dict) -> dict | None:
        txid = str(op.get("txid", "") or "").strip()
        if not txid:
            return None
        raw = None
        details = None
        try:
            raw = self.rpc.getRawTransaction(txid)
        except Exception:
            try:
                raw = self.rpc.getTransaction(txid)
            except Exception:
                raw = None
        try:
            details = self.rpc.z_viewTransaction(txid)
        except Exception:
            details = None

        source = raw if isinstance(raw, dict) else details if isinstance(details, dict) else {}
        if not source:
            return None
        try:
            confirmations = int(source.get("confirmations", 0) or 0)
        except Exception:
            confirmations = 0
        blockhash = source.get("blockhash") or ""
        blockheight = source.get("height", source.get("blockheight"))
        if blockheight is None and blockhash:
            try:
                block = self.rpc.getBlock(blockhash)
                if isinstance(block, dict) and block.get("height") is not None:
                    blockheight = int(block.get("height"))
            except Exception:
                pass
        status = "conflicted" if confirmations < 0 else "pending" if confirmations == 0 else "confirmed"
        tx = {
            "txid": txid,
            "category": "",
            "address": "",
            "amount": 0.0,
            "confirmations": confirmations,
            "blockhash": blockhash,
            "blockheight": blockheight,
            "blockindex": source.get("blockindex"),
            "time": source.get("time") or source.get("blocktime") or op.get("created_at"),
            "blocktime": source.get("blocktime"),
            "timereceived": source.get("timereceived") or op.get("created_at"),
            "created_at": op.get("created_at"),
            "status": status,
            "_cache_meta": {
                "raw": raw,
                "details": details,
                "blockheight": blockheight,
            },
        }
        return tx

    def _merge_operation_transactions(self, txs: list) -> list:
        if self.cache is None:
            return txs
        rows = list(txs or [])
        existing_txids = {str(tx.get("txid", "") or "").strip() for tx in rows if tx.get("txid")}
        try:
            operations = self.cache.list_operations(status="success", limit=50)
        except Exception:
            return rows
        for op in operations:
            txid = str(op.get("txid", "") or "").strip()
            if not txid or txid in existing_txids:
                continue
            tx = self._operation_transaction_from_node(op)
            if tx is None:
                continue
            rows.append(tx)
            existing_txids.add(txid)
        return rows

    def _fetch_transactions(self) -> list:
        if not self.force_full:
            return self.rpc.listTransactions(200, 0)
        txs: list = []
        page_size = 200
        max_rows = 2000
        offset = 0
        while offset < max_rows:
            page = self.rpc.listTransactions(page_size, offset)
            if not page:
                break
            txs.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return txs

    def _build_info_from_chain(self, chain: dict) -> dict:
        info = {
            "blocks": chain.get("blocks", 0),
            "headers": chain.get("headers", chain.get("blocks", 0)),
            "difficulty": chain.get("difficulty", 0),
        }
        try:
            info["connections"] = self.rpc.getConnectionCount()
        except Exception:
            info["connections"] = "-"
        try:
            net = self.rpc.getNetworkInfo()
            if isinstance(net, dict):
                info["version"] = net.get("version", "")
                info["subversion"] = net.get("subversion", "")
                info["protocolversion"] = net.get("protocolversion", "")
                info["connections"] = net.get("connections", info["connections"])
        except Exception:
            pass
        return info

    def _reindex_emit(self, info, chain, t_addrs=(), z_addrs=(),
                      t_bal=None, z_bal=None, total_bal=None):
        reindex_progress = reindex_progress_from_debug_log(
            DATA_DIR / "debug.log",
            max_blk_index=max_reindex_blk_index(),
            include_finished=False,
            reset_on_node_start=True,
        )
        bootstrap_progress = bootstrap_progress_from_debug_log(
            DATA_DIR / "debug.log",
            reset_on_node_start=True,
        )
        data = {
            "info": info, "chain": chain,
            "t_addrs": list(t_addrs), "z_addrs": list(z_addrs),
            "t_balances": t_bal or {}, "z_balances": z_bal or {},
            "total_bal": total_bal or {}, "txs": [],
            "tx_snapshot_complete": False,
            "reindexing": True,
            "reindex_progress": reindex_progress,
            "bootstrap_progress": bootstrap_progress,
        }
        self._store_cache_snapshot(data, "refresh_reindexing")
        self.reindexing.emit(data)

    def run(self):
        try:
            if self._should_stop():
                return
            self.step.emit("getblockchaininfo")
            try:
                chain = self.rpc.getBlockchainInfo()
            except RPCError as e:
                if _is_reindex_err(e):
                    chain = {}
                else:
                    raise
            except Exception:
                raise
            info = self._build_info_from_chain(chain)
            if self._should_stop():
                return

            if chain.get("reindex", False) or chain.get("initialblockdownload", False):
                self._reindex_emit(info, chain)
                return

            self.step.emit("total balance")
            try:
                total_bal = self.rpc.z_getTotalBalance()
            except RPCError as e:
                if _is_reindex_err(e):
                    self._reindex_emit(info, chain)
                    return
                raise

            # Do not poll getwalletinfo from the 30s refresh loop.
            # BitcoinZ getwalletinfo calls GetOldestKeyPoolTime(), which
            # reserves and returns a key only to inspect the keypool. That is
            # harmless for wallet state, but it spams debug.log with
            # "keypool reserve/return" on every UI refresh.
            wallet_info = {}
            if self._should_stop():
                return

            if self.cache is not None and not self.force_full:
                block_height = self._snapshot_block({"info": info})
                try:
                    if self.cache.refresh_unchanged(
                        block_height=block_height,
                        total_bal=total_bal,
                        wallet_info=wallet_info,
                    ):
                        data = self.cache.get_live_backed_snapshot(
                            info=info,
                            chain=chain,
                            total_bal=total_bal,
                            wallet_info=wallet_info,
                            tx_limit=200,
                        )
                        self._store_cache_snapshot(data, "refresh_cached_reuse")
                        if self._should_stop():
                            return
                        self.done.emit(data)
                        return
                except Exception:
                    pass

            self.step.emit("t-addresses")
            try:
                t_addrs = self.rpc.ListAddresses()
            except RPCError as e:
                if _is_reindex_err(e):
                    self._reindex_emit(info, chain)
                    return
                raise

            self.step.emit("z-addresses")
            try:
                z_addrs = self.rpc.z_listAddresses()
            except RPCError as e:
                if _is_reindex_err(e):
                    self._reindex_emit(info, chain, t_addrs)
                    return
                raise

            self.step.emit("transactions")
            try:
                txs = self._fetch_transactions()
                tx_snapshot_complete = (len(txs) < 200) if not self.force_full else (len(txs) < 2000)
                txs = self._enrich_transactions(txs)
                txs = self._merge_operation_transactions(txs)
                txs = self._merge_shielded_received_transactions(txs, z_addrs)
            except RPCError as e:
                if _is_reindex_err(e):
                    self._reindex_emit(info, chain, t_addrs, z_addrs, total_bal=total_bal)
                    return
                raise

            self.step.emit("address balances")

            worker_local = threading.local()

            def _balance_rpc() -> BitcoinZRPC:
                rpc = getattr(worker_local, "rpc", None)
                if rpc is None:
                    rpc = BitcoinZRPC(self.rpc.host, self.rpc.port, self.rpc.user, self.rpc.password)
                    worker_local.rpc = rpc
                return rpc

            def _fetch_bal(addr):
                try:
                    return addr, _balance_rpc().z_getBalance(addr)
                except RPCError as e:
                    return addr, e
                except Exception:
                    return addr, 0.0

            all_addrs = list(t_addrs) + list(z_addrs)
            t_bal: dict = {}
            z_bal: dict = {}
            bal_error: str = ""
            if all_addrs:
                with ThreadPoolExecutor(max_workers=min(6, len(all_addrs))) as ex:
                    futures = {ex.submit(_fetch_bal, a): a for a in all_addrs}
                    for fut in as_completed(futures):
                        addr, bal = fut.result()
                        if isinstance(bal, RPCError):
                            if not bal_error and bal.code in (401, 403):
                                bal_error = str(bal)
                            bal = 0.0
                        if addr in t_addrs:
                            t_bal[addr] = bal
                        else:
                            z_bal[addr] = bal
            if bal_error:
                self.error.emit(f"Balance fetch error: {bal_error}")
                return

            data = {
                "info": info, "chain": chain,
                "wallet_info": wallet_info,
                "t_addrs": t_addrs, "z_addrs": z_addrs,
                "t_balances": t_bal, "z_balances": z_bal,
                "total_bal": total_bal, "txs": txs,
                "tx_snapshot_complete": tx_snapshot_complete,
                "reindexing": False,
            }
            self._store_cache_snapshot(data)
            self._run_reconciliation(data)
            if self._should_stop():
                return
            self.done.emit(data)

        except RPCError as e:
            if self.cache is not None:
                try:
                    job_id = self.cache.start_sync_job("refresh")
                    self.cache.finish_sync_job(job_id, status="failed", last_error=str(e))
                    self.cache.set_state("last_refresh_error", str(e))
                except Exception:
                    pass
            self.error.emit(str(e))
        except Exception:
            msg = traceback.format_exc()
            if self.cache is not None:
                try:
                    job_id = self.cache.start_sync_job("refresh")
                    self.cache.finish_sync_job(job_id, status="failed", last_error=msg)
                    self.cache.set_state("last_refresh_error", msg)
                except Exception:
                    pass
            self.error.emit(msg)


class StatusWorker(QThread):
    _TX_PROBE_LIMIT = 8
    done = Signal(object)
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, txids: list[str] | tuple[str, ...] | None = None):
        super().__init__()
        self.host = rpc.host
        self.port = rpc.port
        self.user = rpc.user
        self.password = rpc.password
        self._stop_requested = False
        seen: set[str] = set()
        self.txids: list[str] = []
        for txid in txids or []:
            txid = str(txid or "").strip()
            if not txid or txid in seen:
                continue
            seen.add(txid)
            self.txids.append(txid)
            if len(self.txids) >= self._TX_PROBE_LIMIT:
                break

    def stop(self):
        self._stop_requested = True
        self.requestInterruption()

    def _should_stop(self) -> bool:
        return bool(self._stop_requested or self.isInterruptionRequested())

    @staticmethod
    def _status_from_confirmations(confirmations: int) -> str:
        if confirmations < 0:
            return "conflicted"
        if confirmations == 0:
            return "pending"
        return "confirmed"

    def _probe_transaction(self, rpc: BitcoinZRPC, txid: str) -> dict | None:
        source: dict = {}
        full: dict = {}
        raw: dict = {}
        try:
            full = rpc.getTransaction(txid) or {}
            if isinstance(full, dict):
                source.update(full)
        except Exception:
            pass
        if not source or not source.get("blockhash") or source.get("height") is None:
            try:
                raw = rpc.getRawTransaction(txid) or {}
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        source.setdefault(key, value)
            except Exception:
                pass
        if not source:
            return None
        try:
            confirmations = int(source.get("confirmations", 0) or 0)
        except Exception:
            confirmations = 0
        update = {
            "txid": txid,
            "confirmations": confirmations,
            "status": self._status_from_confirmations(confirmations),
        }
        field_map = {
            "blockhash": "blockhash",
            "height": "blockheight",
            "blockheight": "blockheight",
            "blockindex": "blockindex",
            "time": "time",
            "blocktime": "blocktime",
            "timereceived": "timereceived",
            "fee": "fee",
        }
        for source_key, target_key in field_map.items():
            value = source.get(source_key)
            if value not in (None, ""):
                update[target_key] = value
        return update

    def run(self):
        try:
            if self._should_stop():
                return
            rpc = BitcoinZRPC(self.host, self.port, self.user, self.password)
            chain = rpc.getBlockchainInfo() or {}
            if self._should_stop():
                return
            peers = "-"
            try:
                net = rpc.getNetworkInfo()
                if isinstance(net, dict):
                    peers = net.get("connections", peers)
            except Exception:
                try:
                    peers = rpc.getConnectionCount()
                except Exception:
                    pass
            reindex_progress = reindex_progress_from_debug_log(
                DATA_DIR / "debug.log",
                max_blk_index=max_reindex_blk_index(),
                start_offset=0,
                include_finished=bool(chain.get("reindex")),
                reset_on_node_start=True,
            )
            bootstrap_progress = bootstrap_progress_from_debug_log(
                DATA_DIR / "debug.log",
                reset_on_node_start=True,
            )
            tx_updates = []
            for txid in self.txids:
                if self._should_stop():
                    return
                update = self._probe_transaction(rpc, txid)
                if update:
                    tx_updates.append(update)
            if self._should_stop():
                return
            self.done.emit({
                "chain": chain,
                "peers": peers,
                "tx_updates": tx_updates,
                "reindex_progress": reindex_progress,
                "bootstrap_progress": bootstrap_progress,
            })
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))


class PollWorker(QThread):
    status_update = Signal(str)
    success       = Signal(str)
    failed        = Signal(str)
    cancelled     = Signal(str)
    unknown       = Signal(str)

    TOTAL_TIMEOUT_SECONDS = 20 * 60
    EMPTY_STATUS_LIMIT = 5
    RPC_ERROR_LIMIT = 10

    def __init__(
        self,
        rpc: BitcoinZRPC,
        opid: str,
        *,
        timeout_seconds: float | None = None,
        empty_status_limit: int | None = None,
        rpc_error_limit: int | None = None,
        sleep_func=None,
    ):
        super().__init__()
        self.rpc   = _clone_rpc(rpc)
        self.opid  = opid
        self._stop = False
        self.timeout_seconds = (
            self.TOTAL_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
        )
        self.empty_status_limit = (
            self.EMPTY_STATUS_LIMIT if empty_status_limit is None else int(empty_status_limit)
        )
        self.rpc_error_limit = (
            self.RPC_ERROR_LIMIT if rpc_error_limit is None else int(rpc_error_limit)
        )
        self._sleep_func = sleep_func or time.sleep

    def stop(self):
        self._stop = True

    @staticmethod
    def _extract_txid(item) -> str:
        if not isinstance(item, dict):
            return ""
        result = item.get("result")
        if isinstance(result, dict):
            txid = result.get("txid") or result.get("txId")
            return str(txid or "").strip()
        if isinstance(result, str) and len(result.strip()) == 64:
            return result.strip()
        return ""

    def _cleanup_finished_result(self):
        try:
            res = self.rpc.z_getOperationResult(self.opid)
        except Exception:
            return []
        return res if isinstance(res, list) else []

    @staticmethod
    def _poll_interval(elapsed: float) -> float:
        if elapsed < 30:
            return 2.0
        if elapsed < 180:
            return 5.0
        if elapsed < 600:
            return 10.0
        return 15.0

    def _sleep_interruptible(self, seconds: float):
        remaining = max(0.0, float(seconds))
        while remaining > 0 and not self._stop:
            chunk = min(0.5, remaining)
            self._sleep_func(chunk)
            remaining -= chunk

    def run(self):
        started_at = time.monotonic()
        empty_status_count = 0
        rpc_error_count = 0
        while not self._stop:
            elapsed = time.monotonic() - started_at
            try:
                res = self.rpc.z_getOperationStatus(self.opid)
                if res and isinstance(res, list) and res:
                    empty_status_count = 0
                    rpc_error_count = 0
                    item = res[0] if isinstance(res[0], dict) else {}
                    s = str(item.get("status", "") or "").strip().lower()
                    if s == "success":
                        txid = self._extract_txid(item)
                        if not txid:
                            for done_item in self._cleanup_finished_result():
                                txid = self._extract_txid(done_item)
                                if txid:
                                    break
                        else:
                            self._cleanup_finished_result()
                        if txid:
                            self.success.emit(txid)
                        else:
                            self.unknown.emit("Send operation completed, but the node did not return a transaction id.")
                        return
                    if s == "failed":
                        err = item.get("error", {})
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        self.failed.emit(msg)
                        return
                    if s == "cancelled":
                        self.cancelled.emit("Send operation was cancelled by the node.")
                        return
                    if s in {"queued", "executing"}:
                        self.status_update.emit(s)
                    else:
                        self.unknown.emit(f"Unexpected send operation status: {s or 'empty'}")
                        return
                else:
                    empty_status_count += 1
                    self.status_update.emit("queued")
                    if empty_status_count >= self.empty_status_limit:
                        self.unknown.emit("Send operation id is no longer visible in the node.")
                        return
            except RPCError as e:
                rpc_error_count += 1
                self.status_update.emit("executing")
                if rpc_error_count >= self.rpc_error_limit:
                    self.unknown.emit(f"Could not poll send operation status: {e}")
                    return
            except Exception as e:
                rpc_error_count += 1
                debug_exception("PollWorker.run", e)
                self.status_update.emit("executing")
                if rpc_error_count >= self.rpc_error_limit:
                    self.unknown.emit("Could not poll send operation status.")
                    return
            if self.timeout_seconds >= 0 and (time.monotonic() - started_at) >= self.timeout_seconds:
                self.unknown.emit("Send operation polling timed out.")
                return
            self._sleep_interruptible(self._poll_interval(elapsed))


class SendPreflightWorker(QThread):
    done = Signal(object)
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, frm: str, to: str):
        super().__init__()
        self.rpc = _clone_rpc(rpc)
        self.frm = frm
        self.to = to

    def run(self):
        try:
            if _is_z_addr(self.to):
                validation = self.rpc.z_validateAddress(self.to)
            else:
                validation = self.rpc.validateAddress(self.to)
            if not isinstance(validation, dict) or not validation.get("isvalid", False):
                self.done.emit({"valid": False, "balance": 0.0})
                return
            balance = round(float(self.rpc.z_getBalance(self.frm, 1)), 8)
            self.done.emit({"valid": True, "balance": balance})
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))


class NewAddressWorker(QThread):
    done = Signal(str)
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, shielded: bool):
        super().__init__()
        self.rpc = _clone_rpc(rpc)
        self.shielded = bool(shielded)

    def run(self):
        try:
            addr = self.rpc.z_getNewAddress() if self.shielded else self.rpc.getNewAddress()
            self.done.emit(addr)
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))


class SendWorker(QThread):
    done  = Signal(str)
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, frm: str, to: str,
                 amount: float, fee: float, memo: str = ""):
        super().__init__()
        self.rpc = _clone_rpc(rpc); self.frm = frm; self.to = to
        self.amount = amount; self.fee = fee; self.memo = memo

    def run(self):
        try:
            if self.memo:
                opid = self.rpc.SendMemo(self.frm, self.to, self.amount, self.fee, self.memo)
            else:
                opid = self.rpc.z_sendMany(self.frm, self.to, self.amount, self.fee)
            self.done.emit(opid)
        except RPCError as e:
            self.error.emit(str(e))



class ShutdownWorker(QThread):
    status = Signal(str)
    done   = Signal()

    def __init__(self, rpc: BitcoinZRPC):
        super().__init__(); self.rpc = _clone_rpc(rpc)

    def run(self):
        self.status.emit("Sending stop command to node")
        try:
            self.rpc.stopNode()
        except RPCError:
            pass
        except Exception:
            pass
        self.status.emit("Node stop command sent")
        self.done.emit()
