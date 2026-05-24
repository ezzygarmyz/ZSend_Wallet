from __future__ import annotations

import time
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PySide6.QtCore import QThread, Signal

from .common import (
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
from .wallet_cache import WalletCache

_PARAMS_DOWNLOAD_CHUNK = 1 << 20
_PARAMS_PROGRESS_MIN_BYTES = 4 << 20
_PARAMS_PROGRESS_MIN_SECONDS = 0.25


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


class NodeStartWorker(QThread):
    status = Signal(str)
    ready  = Signal()
    failed = Signal(str)

    def __init__(self, rpc: BitcoinZRPC):
        super().__init__(); self.rpc = rpc

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
                if e.code == -28 or any(k in msg for k in _NODE_MSGS):
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

            for attempt in range(120):
                try:
                    debug_log("RPC readiness probe", attempt=attempt + 1)
                    self.rpc.getBlockchainInfo()
                    debug_log("Node RPC responded successfully", attempt=attempt + 1)
                    self.status.emit("Node is ready!")
                    self.ready.emit()
                    return
                except RPCError as e:
                    msg  = str(e)
                    code = e.code
                    debug_log("RPC readiness probe failed", attempt=attempt + 1, code=code, rpc_message=msg)

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

                    if code == -28 or any(k in msg for k in _NODE_MSGS):
                        display = next(
                            (v for k, v in _NODE_MSGS.items() if k in msg),
                            msg.splitlines()[0]
                        )
                        self.status.emit(display)
                    elif "Connection refused" in msg or "refused" in msg.lower():
                        self.status.emit(f"Waiting for node ({attempt * 3}s)")
                    else:
                        self.status.emit(f"Waiting ({msg.splitlines()[0][:60]})")

                time.sleep(3)

            debug_log("NodeStartWorker timed out waiting for node")
            self.failed.emit(
                "Node did not respond within 360 seconds.\n"
                "Check that bitcoinzd.exe is present and bitcoinz.conf is correct.\n"
                "Open Diagnostics for details."
            )
        except Exception as e:
            debug_exception("NodeStartWorker failed unexpectedly", e)
            self.failed.emit(f"Node start failed unexpectedly:\n{e}")


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
        self.rpc = rpc
        self.cache = cache
        self.force_full = force_full

    @staticmethod
    def _snapshot_block(data: dict) -> int | None:
        try:
            return int((data.get("info") or {}).get("blocks"))
        except Exception:
            return None

    def _store_cache_snapshot(self, data: dict, job_type: str = "refresh") -> None:
        if self.cache is None:
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
        for tx in txs:
            meta = tx.setdefault("_cache_meta", {})
            blockhash = tx.get("blockhash")
            if blockhash in block_heights:
                tx["blockheight"] = block_heights[blockhash]
                meta["blockheight"] = block_heights[blockhash]
            txid = tx.get("txid")
            if txid and (not tx.get("address") or tx.get("category") == "send") and z_view_count < 20:
                try:
                    meta["details"] = self.rpc.z_viewTransaction(txid)
                    z_view_count += 1
                except Exception:
                    pass
        return txs

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
        data = {
            "info": info, "chain": chain,
            "t_addrs": list(t_addrs), "z_addrs": list(z_addrs),
            "t_balances": t_bal or {}, "z_balances": z_bal or {},
            "total_bal": total_bal or {}, "txs": [],
            "tx_snapshot_complete": False,
            "reindexing": True,
        }
        self._store_cache_snapshot(data, "refresh_reindexing")
        self.reindexing.emit(data)

    def run(self):
        try:
            self.step.emit("getblockchaininfo")
            try:
                chain = self.rpc.getBlockchainInfo()
            except Exception:
                chain = {}
            info = self._build_info_from_chain(chain)

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

            self.step.emit("wallet info")
            try:
                wallet_info = self.rpc.getWalletInfo()
                if not isinstance(wallet_info, dict):
                    wallet_info = {}
            except RPCError as e:
                if _is_reindex_err(e):
                    self._reindex_emit(info, chain, total_bal=total_bal)
                    return
                wallet_info = {}
            except Exception:
                wallet_info = {}

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
    done = Signal(object)
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC):
        super().__init__()
        self.host = rpc.host
        self.port = rpc.port
        self.user = rpc.user
        self.password = rpc.password

    def run(self):
        try:
            rpc = BitcoinZRPC(self.host, self.port, self.user, self.password)
            chain = rpc.getBlockchainInfo() or {}
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
            self.done.emit({"chain": chain, "peers": peers})
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))


class PollWorker(QThread):
    status_update = Signal(str)
    success       = Signal(str)
    failed        = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, opid: str):
        super().__init__()
        self.rpc   = rpc
        self.opid  = opid
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            try:
                res = self.rpc.z_getOperationStatus(self.opid)
                if res and isinstance(res, list) and res:
                    s = res[0].get("status", "")
                    if s == "success":
                        res2 = self.rpc.z_getOperationResult(self.opid)
                        txid = ""
                        if res2 and isinstance(res2, list) and res2:
                            txid = res2[0].get("result", {}).get("txid", "")
                        self.success.emit(txid or "?")
                        return
                    elif s == "failed":
                        err = res[0].get("error", {})
                        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                        self.failed.emit(msg)
                        return
                    else:
                        self.status_update.emit(f"Status: {s}")
                else:
                    self.status_update.emit("Waiting for confirmation")
            except RPCError as e:
                self.status_update.emit(f"Polling error: {e}")
            except Exception:
                pass
            for _ in range(8):
                if self._stop: return
                time.sleep(0.5)


class SendPreflightWorker(QThread):
    done = Signal(object)
    error = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, frm: str, to: str):
        super().__init__()
        self.rpc = rpc
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
        self.rpc = rpc
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
        self.rpc = rpc; self.frm = frm; self.to = to
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
        super().__init__(); self.rpc = rpc

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
