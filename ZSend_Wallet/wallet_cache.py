import json
import os
import sqlite3
import threading
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from .helpers import current_platform


SCHEMA_VERSION = 4
BTCZ_ZAT = Decimal("100000000")


def default_cache_path() -> Path:
    if current_platform == "windows":
        data_dir  = Path(os.environ.get("APPDATA", Path.home())) / "BitcoinZ"
    elif current_platform == "linux":
        data_dir  = Path.home() / ".bitcoinz"

    return data_dir / "zsend_cache.sqlite"


def now_ts() -> int:
    return int(time.time())


def btcz_to_zat(value: Any) -> int:
    amount = Decimal(str(value or "0"))
    return int((amount * BTCZ_ZAT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def zat_to_btcz(value: int) -> str:
    amount = Decimal(int(value)) / BTCZ_ZAT
    return format(amount.normalize(), "f")


def zat_to_float(value: int | None) -> float:
    return float(Decimal(int(value or 0)) / BTCZ_ZAT)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def address_type(address: str) -> str:
    if address.startswith(("t1", "t3")):
        return "transparent"
    if address.startswith("zs"):
        return "sapling"
    if address.startswith("zc"):
        return "sprout"
    return "unknown"


def tx_timestamp(tx: dict[str, Any]) -> int:
    for key in ("blocktime", "time", "timereceived"):
        value = tx.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return 0


def total_balance_key(total_bal: dict[str, Any] | None) -> str:
    total_bal = total_bal or {}
    normalized = {
        key: btcz_to_zat(total_bal.get(key, 0))
        for key in ("transparent", "private", "total")
    }
    return json_dumps(normalized)


def wallet_activity_key(wallet_info: dict[str, Any] | None) -> str:
    wallet_info = wallet_info or {}
    normalized = {
        key: wallet_info.get(key)
        for key in ("txcount", "keypoolsize", "keypoolsize_hd_internal")
        if key in wallet_info
    }
    return json_dumps(normalized)


class WalletCache:
    """SQLite cache for ZSend UI data.

    The BitcoinZ node remains the source of truth. This database is only a local
    cache for fast startup, transaction history, operation tracking, and UI
    metadata.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    @classmethod
    def open_default(cls) -> "WalletCache":
        return cls(default_cache_path())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _configure(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")

    def _migrate(self) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            current_version = 0
            if row is not None:
                try:
                    ver_row = self._conn.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()
                    if ver_row is not None:
                        current_version = int(ver_row[0] or 0)
                except Exception:
                    current_version = 0
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wallet_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS addresses (
                    address TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    label TEXT,
                    imported INTEGER NOT NULL DEFAULT 0,
                    hidden INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER,
                    import_height INTEGER,
                    last_checked_block INTEGER,
                    balance_zat INTEGER NOT NULL DEFAULT 0,
                    confirmed_balance_zat INTEGER NOT NULL DEFAULT 0,
                    unconfirmed_balance_zat INTEGER NOT NULL DEFAULT 0,
                    spendable INTEGER NOT NULL DEFAULT 1,
                    source TEXT,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    txid TEXT NOT NULL,
                    entry_index INTEGER NOT NULL,
                    category TEXT,
                    address TEXT,
                    amount_zat INTEGER NOT NULL DEFAULT 0,
                    fee_zat INTEGER,
                    confirmations INTEGER NOT NULL DEFAULT 0,
                    blockhash TEXT,
                    blockheight INTEGER,
                    blockindex INTEGER,
                    timestamp INTEGER,
                    timereceived INTEGER,
                    status TEXT,
                    raw_json TEXT,
                    details_json TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (txid, entry_index)
                );

                CREATE TABLE IF NOT EXISTS operations (
                    opid TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    from_address TEXT,
                    to_address TEXT,
                    amount_zat INTEGER,
                    fee_zat INTEGER,
                    memo TEXT,
                    txid TEXT,
                    error TEXT,
                    result_json TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    last_error TEXT,
                    last_seen_block INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_addresses_balance
                    ON addresses(balance_zat DESC, address ASC);
                CREATE INDEX IF NOT EXISTS idx_transactions_time
                    ON transactions(timestamp DESC, txid ASC);
                CREATE INDEX IF NOT EXISTS idx_transactions_address
                    ON transactions(address);
                CREATE INDEX IF NOT EXISTS idx_transactions_confirmations
                    ON transactions(confirmations);
                CREATE INDEX IF NOT EXISTS idx_operations_status
                    ON operations(status);
                """
            )
            if current_version < 2:
                self._conn.executescript(
                    """
                    CREATE INDEX IF NOT EXISTS idx_transactions_blockhash
                        ON transactions(blockhash);
                    CREATE INDEX IF NOT EXISTS idx_transactions_status
                        ON transactions(status);
                    """
                )
                self._conn.execute(
                    """
                    INSERT INTO wallet_state(key, value, updated_at)
                    VALUES('schema_migrated_at', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (str(now_ts()), now_ts()),
                )
            if current_version < 3:
                self._conn.execute(
                    """
                    UPDATE transactions
                    SET status = CASE
                        WHEN confirmations < 0 THEN 'conflicted'
                        WHEN confirmations = 0 THEN 'pending'
                        ELSE 'confirmed'
                    END,
                    updated_at = ?
                    WHERE status IS NULL
                       OR status = ''
                       OR status NOT IN ('pending', 'confirmed', 'conflicted', 'stale', 'reorged')
                    """,
                    (now_ts(),),
                )
                self._conn.execute(
                    """
                    INSERT INTO wallet_state(key, value, updated_at)
                    VALUES('schema_normalized_at', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (str(now_ts()), now_ts()),
                )
            self._conn.executescript(
                """
                UPDATE transactions
                SET raw_json = NULL,
                    details_json = NULL
                WHERE raw_json IS NOT NULL
                   OR details_json IS NOT NULL;

                UPDATE operations
                SET memo = NULL,
                    error = NULL,
                    result_json = NULL
                WHERE memo IS NOT NULL
                   OR error IS NOT NULL
                   OR result_json IS NOT NULL;
                """
            )
            self._conn.execute(
                """
                INSERT INTO schema_meta(key, value)
                VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def _executemany(self, sql: str, rows: Iterable[tuple[Any, ...]]) -> sqlite3.Cursor:
        return self._conn.executemany(sql, rows)

    def set_state(self, key: str, value: Any) -> None:
        payload = value if isinstance(value, str) else json_dumps(value)
        with self._lock:
            self._execute(
                """
                INSERT INTO wallet_state(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, payload, now_ts()),
            )
            self._conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._execute(
                "SELECT value FROM wallet_state WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        value = row["value"]
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value

    def set_app_setting(self, key: str, value: Any) -> None:
        payload = value if isinstance(value, str) else json_dumps(value)
        with self._lock:
            self._execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, payload, now_ts()),
            )
            self._conn.commit()

    def get_app_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        value = row["value"]
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value

    def upsert_address(
        self,
        address: str,
        *,
        addr_type: str | None = None,
        label: str | None = None,
        imported: bool | None = None,
        hidden: bool | None = None,
        created_at: int | None = None,
        import_height: int | None = None,
        last_checked_block: int | None = None,
        balance_zat: int | None = None,
        confirmed_balance_zat: int | None = None,
        unconfirmed_balance_zat: int | None = None,
        spendable: bool | None = None,
        source: str | None = None,
        commit: bool = True,
    ) -> None:
        ts = now_ts()
        addr_type = addr_type or address_type(address)
        with self._lock:
            self._execute(
                """
                INSERT INTO addresses(
                    address, type, label, imported, hidden, created_at,
                    import_height, last_checked_block, balance_zat,
                    confirmed_balance_zat, unconfirmed_balance_zat,
                    spendable, source, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    type = excluded.type,
                    label = COALESCE(excluded.label, addresses.label),
                    imported = CASE WHEN ? THEN excluded.imported ELSE addresses.imported END,
                    hidden = CASE WHEN ? THEN excluded.hidden ELSE addresses.hidden END,
                    created_at = COALESCE(addresses.created_at, excluded.created_at),
                    import_height = COALESCE(excluded.import_height, addresses.import_height),
                    last_checked_block = COALESCE(excluded.last_checked_block, addresses.last_checked_block),
                    balance_zat = CASE WHEN ? THEN excluded.balance_zat ELSE addresses.balance_zat END,
                    confirmed_balance_zat = CASE WHEN ? THEN excluded.confirmed_balance_zat ELSE addresses.confirmed_balance_zat END,
                    unconfirmed_balance_zat = CASE WHEN ? THEN excluded.unconfirmed_balance_zat ELSE addresses.unconfirmed_balance_zat END,
                    spendable = CASE WHEN ? THEN excluded.spendable ELSE addresses.spendable END,
                    source = COALESCE(excluded.source, addresses.source),
                    updated_at = excluded.updated_at
                """,
                (
                    address,
                    addr_type,
                    label,
                    int(imported) if imported is not None else 0,
                    int(hidden) if hidden is not None else 0,
                    created_at,
                    import_height,
                    last_checked_block,
                    balance_zat if balance_zat is not None else 0,
                    confirmed_balance_zat if confirmed_balance_zat is not None else 0,
                    unconfirmed_balance_zat if unconfirmed_balance_zat is not None else 0,
                    int(spendable) if spendable is not None else 1,
                    source,
                    ts,
                    imported is not None,
                    hidden is not None,
                    balance_zat is not None,
                    confirmed_balance_zat is not None,
                    unconfirmed_balance_zat is not None,
                    spendable is not None,
                ),
            )
            if commit:
                self._conn.commit()

    def upsert_addresses(
        self,
        addresses: Iterable[str],
        *,
        addr_type: str | None = None,
        source: str = "node",
        last_checked_block: int | None = None,
    ) -> None:
        with self._lock:
            for address in addresses:
                self.upsert_address(
                    address,
                    addr_type=addr_type,
                    source=source,
                    last_checked_block=last_checked_block,
                    commit=False,
                )
            self._conn.commit()

    def update_address_balance(
        self,
        address: str,
        balance: Any,
        *,
        last_checked_block: int | None = None,
        confirmed_balance: Any | None = None,
        unconfirmed_balance: Any | None = None,
        commit: bool = True,
    ) -> None:
        balance_zat = btcz_to_zat(balance)
        confirmed_zat = btcz_to_zat(confirmed_balance) if confirmed_balance is not None else balance_zat
        unconfirmed_zat = btcz_to_zat(unconfirmed_balance) if unconfirmed_balance is not None else 0
        self.upsert_address(
            address,
            balance_zat=balance_zat,
            confirmed_balance_zat=confirmed_zat,
            unconfirmed_balance_zat=unconfirmed_zat,
            last_checked_block=last_checked_block,
            source="node",
            commit=commit,
        )

    def list_addresses(
        self,
        *,
        addr_type: str | None = None,
        include_hidden: bool = False,
        order: str = "balance",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if addr_type is not None:
            where.append("type = ?")
            params.append(addr_type)
        if not include_hidden:
            where.append("hidden = 0")
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        order_sql = "balance_zat DESC, address ASC" if order == "balance" else "address ASC"
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            params.append(limit)
        with self._lock:
            rows = self._execute(
                f"SELECT * FROM addresses {where_sql} ORDER BY {order_sql}{limit_sql}",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_address(self, address: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._execute("SELECT * FROM addresses WHERE address = ?", (address,)).fetchone()
        return row_to_dict(row)

    def update_address_metadata(
        self,
        address: str,
        *,
        label: str | None = None,
        hidden: bool | None = None,
    ) -> None:
        self.upsert_address(address, label=label, hidden=hidden, commit=True)

    def get_total_address_balance_zat(self, *, include_hidden: bool = False) -> int:
        where_sql = "" if include_hidden else "WHERE hidden = 0"
        with self._lock:
            row = self._execute(
                f"SELECT COALESCE(SUM(balance_zat), 0) AS total_zat FROM addresses {where_sql}"
            ).fetchone()
        return int((row or {"total_zat": 0})["total_zat"] or 0)

    def upsert_transaction(
        self,
        tx: dict[str, Any],
        *,
        entry_index: int = 0,
        raw: dict[str, Any] | None = None,
        details: dict[str, Any] | list[Any] | None = None,
        blockheight: int | None = None,
        commit: bool = True,
    ) -> None:
        txid = str(tx.get("txid", ""))
        if not txid:
            return
        fee = tx.get("fee")
        confirmations = int(tx.get("confirmations", 0) or 0)
        status = tx.get("status")
        if not status:
            if confirmations < 0:
                status = "conflicted"
            elif confirmations == 0:
                status = "pending"
            else:
                status = "confirmed"
        ts = now_ts()
        with self._lock:
            self._execute(
                """
                INSERT INTO transactions(
                    txid, entry_index, category, address, amount_zat, fee_zat,
                    confirmations, blockhash, blockheight, blockindex,
                    timestamp, timereceived, status, raw_json, details_json,
                    updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(txid, entry_index) DO UPDATE SET
                    category = excluded.category,
                    address = excluded.address,
                    amount_zat = excluded.amount_zat,
                    fee_zat = excluded.fee_zat,
                    confirmations = excluded.confirmations,
                    blockhash = excluded.blockhash,
                    blockheight = COALESCE(excluded.blockheight, transactions.blockheight),
                    blockindex = excluded.blockindex,
                    timestamp = excluded.timestamp,
                    timereceived = excluded.timereceived,
                    status = excluded.status,
                    raw_json = NULL,
                    details_json = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    txid,
                    entry_index,
                    tx.get("category"),
                    tx.get("address"),
                    btcz_to_zat(tx.get("amount", 0)),
                    btcz_to_zat(fee) if fee is not None else None,
                    confirmations,
                    tx.get("blockhash"),
                    blockheight,
                    tx.get("blockindex"),
                    tx_timestamp(tx),
                    int(tx.get("timereceived", 0) or 0) or None,
                    status,
                    None,
                    None,
                    ts,
                ),
            )
            if commit:
                self._conn.commit()

    def upsert_transactions(self, txs: Iterable[dict[str, Any]]) -> None:
        tx_list = list(txs)
        seen: dict[str, int] = {}
        with self._lock:
            for tx in tx_list:
                txid = str(tx.get("txid", ""))
                entry_index = seen.get(txid, 0)
                seen[txid] = entry_index + 1
                meta = tx.get("_cache_meta") or {}
                self.upsert_transaction(
                    tx,
                    entry_index=entry_index,
                    raw=meta.get("raw"),
                    details=meta.get("details"),
                    blockheight=meta.get("blockheight"),
                    commit=False,
                )
            for txid, count in seen.items():
                if txid:
                    self._execute(
                        "DELETE FROM transactions WHERE txid = ? AND entry_index >= ?",
                        (txid, count),
                    )
            self._conn.commit()

    def list_transactions(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        newest_first: bool = True,
    ) -> list[dict[str, Any]]:
        direction = "DESC" if newest_first else "ASC"
        with self._lock:
            rows = self._execute(
                f"""
                SELECT * FROM transactions
                ORDER BY timestamp {direction}, txid {direction}, entry_index ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_transaction_entries(self, txid: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._execute(
                """
                SELECT * FROM transactions
                WHERE txid = ?
                ORDER BY entry_index ASC
                """,
                (txid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_refresh_snapshot(self, *, tx_limit: int = 200) -> dict[str, Any]:
        info = self.get_state("last_info", {}) or {}
        chain = self.get_state("last_chain", {}) or {}
        wallet_info = self.get_state("last_wallet_info", {}) or {}
        total_bal = self.get_state("last_total_balance", {}) or {}
        addresses = self.list_addresses(include_hidden=False, order="balance")
        tx_rows = self.list_transactions(limit=tx_limit, newest_first=True)

        t_addrs: list[str] = []
        z_addrs: list[str] = []
        t_balances: dict[str, float] = {}
        z_balances: dict[str, float] = {}

        for row in addresses:
            addr = row["address"]
            bal = zat_to_float(row.get("balance_zat"))
            if row.get("type") == "transparent":
                t_addrs.append(addr)
                t_balances[addr] = bal
            else:
                z_addrs.append(addr)
                z_balances[addr] = bal

        txs: list[dict[str, Any]] = []
        for row in tx_rows:
            tx = {
                "txid": row["txid"],
                "category": row["category"] or "",
                "address": row["address"] or "",
                "amount": zat_to_float(row.get("amount_zat")),
                "confirmations": int(row.get("confirmations") or 0),
                "blockhash": row["blockhash"] or "",
                "blockindex": row["blockindex"],
                "blockheight": row["blockheight"],
                "time": row["timestamp"],
                "timereceived": row["timereceived"],
                "status": row["status"] or "",
            }
            if row["fee_zat"] is not None:
                tx["fee"] = zat_to_float(row["fee_zat"])
            txs.append(tx)

        return {
            "info": info,
            "chain": chain,
            "wallet_info": wallet_info,
            "t_addrs": t_addrs,
            "z_addrs": z_addrs,
            "t_balances": t_balances,
            "z_balances": z_balances,
            "total_bal": total_bal,
            "txs": txs,
            "tx_snapshot_complete": False,
            "cached": True,
            "last_refresh_at": self.get_state("last_refresh_at"),
            "reindexing": False,
        }

    def get_live_backed_snapshot(
        self,
        *,
        info: dict[str, Any],
        chain: dict[str, Any],
        total_bal: dict[str, Any],
        wallet_info: dict[str, Any] | None = None,
        tx_limit: int = 200,
    ) -> dict[str, Any]:
        data = self.get_refresh_snapshot(tx_limit=tx_limit)
        data["info"] = info
        data["chain"] = chain
        data["wallet_info"] = wallet_info or {}
        data["total_bal"] = total_bal
        data["cached"] = False
        data["cache_reused"] = True
        return data

    def has_cached_wallet_data(self) -> bool:
        with self._lock:
            row = self._execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM addresses) AS addr_count,
                    (SELECT COUNT(*) FROM transactions) AS tx_count,
                    (SELECT COUNT(*) FROM wallet_state) AS state_count
                """
            ).fetchone()
        if row is None:
            return False
        return any(int(row[key] or 0) > 0 for key in ("addr_count", "tx_count", "state_count"))

    def refresh_unchanged(
        self,
        *,
        block_height: int | None,
        total_bal: dict[str, Any],
        wallet_info: dict[str, Any] | None = None,
    ) -> bool:
        if block_height is None or not self.has_cached_wallet_data():
            return False
        if self.has_unsettled_wallet_state():
            return False
        stored_key = self.get_state("last_total_balance_key")
        if not isinstance(stored_key, str):
            stored_key = json_dumps(stored_key)
        stored_wallet_key = self.get_state("last_wallet_activity_key")
        current_wallet_key = wallet_activity_key(wallet_info)
        if current_wallet_key != "{}" and stored_wallet_key != current_wallet_key:
            return False
        return (
            self.get_state("last_block_height") == block_height
            and stored_key == total_balance_key(total_bal)
        )

    def has_unsettled_wallet_state(self) -> bool:
        with self._lock:
            row = self._execute(
                """
                SELECT 1
                FROM operations
                WHERE lower(status) IN ('submitted', 'queued', 'executing')
                LIMIT 1
                """
            ).fetchone()
            if row is not None:
                return True
            row = self._execute(
                """
                SELECT 1
                FROM transactions
                WHERE confirmations = 0
                  AND lower(COALESCE(status, '')) NOT IN (
                      'failed', 'expired', 'stale', 'reorged', 'conflicted'
                  )
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def upsert_operation(
        self,
        opid: str,
        *,
        op_type: str,
        status: str,
        from_address: str | None = None,
        to_address: str | None = None,
        amount: Any | None = None,
        fee: Any | None = None,
        memo: str | None = None,
        txid: str | None = None,
        error: str | None = None,
        result: Any | None = None,
    ) -> None:
        ts = now_ts()
        with self._lock:
            self._execute(
                """
                INSERT INTO operations(
                    opid, type, status, from_address, to_address, amount_zat,
                    fee_zat, memo, txid, error, result_json, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opid) DO UPDATE SET
                    type = excluded.type,
                    status = excluded.status,
                    from_address = COALESCE(excluded.from_address, operations.from_address),
                    to_address = COALESCE(excluded.to_address, operations.to_address),
                    amount_zat = COALESCE(excluded.amount_zat, operations.amount_zat),
                    fee_zat = COALESCE(excluded.fee_zat, operations.fee_zat),
                    memo = NULL,
                    txid = COALESCE(excluded.txid, operations.txid),
                    error = NULL,
                    result_json = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    opid,
                    op_type,
                    status,
                    from_address,
                    to_address,
                    btcz_to_zat(amount) if amount is not None else None,
                    btcz_to_zat(fee) if fee is not None else None,
                    None,
                    txid,
                    None,
                    None,
                    ts,
                    ts,
                ),
            )
            self._conn.commit()

    def update_operation_status(
        self,
        opid: str,
        status: str,
        *,
        txid: str | None = None,
        error: str | None = None,
        result: Any | None = None,
    ) -> None:
        with self._lock:
            self._execute(
                """
                UPDATE operations
                SET status = ?,
                    txid = COALESCE(?, txid),
                    error = NULL,
                    result_json = NULL,
                    updated_at = ?
                WHERE opid = ?
                """,
                (
                    status,
                    txid,
                    now_ts(),
                    opid,
                ),
            )
            self._conn.commit()

    def list_operations(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where_sql = ""
        if status is not None:
            where_sql = "WHERE status = ?"
            params.append(status)
        params.append(limit)
        with self._lock:
            rows = self._execute(
                f"""
                SELECT * FROM operations
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_transactions_stale(self, missing_txids: Iterable[str]) -> None:
        txids = sorted({str(txid) for txid in missing_txids if txid})
        if not txids:
            return
        placeholders = ",".join("?" for _ in txids)
        with self._lock:
            self._execute(
                f"""
                UPDATE transactions
                SET status = 'stale',
                    updated_at = ?
                WHERE txid IN ({placeholders})
                """,
                (now_ts(), *txids),
            )
            self._conn.commit()

    def clear_transactions_stale(self, txids: Iterable[str]) -> None:
        ids = sorted({str(txid) for txid in txids if txid})
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            self._execute(
                f"""
                UPDATE transactions
                SET status = CASE
                    WHEN confirmations < 0 THEN 'conflicted'
                    WHEN confirmations = 0 THEN 'pending'
                    ELSE 'confirmed'
                END,
                updated_at = ?
                WHERE txid IN ({placeholders}) AND status = 'stale'
                """,
                (now_ts(), *ids),
            )
            self._conn.commit()

    def update_transaction_reconcile(self, txid: str, *, status: str | None = None,
                                     confirmations: int | None = None,
                                     blockhash: str | None = None) -> None:
        if not txid:
            return
        with self._lock:
            self._execute(
                """
                UPDATE transactions
                SET status = COALESCE(?, status),
                    confirmations = COALESCE(?, confirmations),
                    blockhash = COALESCE(?, blockhash),
                    updated_at = ?
                WHERE txid = ?
                """,
                (status, confirmations, blockhash, now_ts(), txid),
            )
            self._conn.commit()

    def start_sync_job(self, job_type: str, *, last_seen_block: int | None = None) -> int:
        with self._lock:
            cur = self._execute(
                """
                INSERT INTO sync_jobs(type, status, started_at, last_seen_block)
                VALUES(?, 'running', ?, ?)
                """,
                (job_type, now_ts(), last_seen_block),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_sync_job(
        self,
        job_id: int,
        *,
        status: str = "success",
        last_error: str | None = None,
        last_seen_block: int | None = None,
    ) -> None:
        with self._lock:
            self._execute(
                """
                UPDATE sync_jobs
                SET status = ?,
                    finished_at = ?,
                    last_error = ?,
                    last_seen_block = COALESCE(?, last_seen_block)
                WHERE id = ?
                """,
                (status, now_ts(), last_error, last_seen_block, job_id),
            )
            self._conn.commit()

    def store_refresh_snapshot(self, data: dict[str, Any]) -> None:
        info = data.get("info") or {}
        chain = data.get("chain") or {}
        wallet_info = data.get("wallet_info") or {}
        block_height = None
        try:
            block_height = int(info.get("blocks"))
        except (TypeError, ValueError):
            pass

        t_addrs = list(data.get("t_addrs") or [])
        z_addrs = list(data.get("z_addrs") or [])
        t_balances = data.get("t_balances") or {}
        z_balances = data.get("z_balances") or {}
        total_bal = data.get("total_bal") or {}
        txs = data.get("txs") or []

        with self._lock:
            self.set_state("last_info", info)
            self.set_state("last_chain", chain)
            self.set_state("last_wallet_info", wallet_info)
            self.set_state("last_wallet_activity_key", wallet_activity_key(wallet_info))
            self.set_state("last_total_balance", total_bal)
            self.set_state("last_total_balance_key", total_balance_key(total_bal))
            self.set_state("last_refresh_at", now_ts())
            if block_height is not None:
                self.set_state("last_block_height", block_height)

            for address in t_addrs:
                self.upsert_address(
                    address,
                    addr_type="transparent",
                    source="node",
                    last_checked_block=block_height,
                    commit=False,
                )
                if address in t_balances:
                    self.update_address_balance(
                        address,
                        t_balances[address],
                        last_checked_block=block_height,
                        commit=False,
                    )
            for address in z_addrs:
                self.upsert_address(
                    address,
                    addr_type=address_type(address),
                    source="node",
                    last_checked_block=block_height,
                    commit=False,
                )
                if address in z_balances:
                    self.update_address_balance(
                        address,
                        z_balances[address],
                        last_checked_block=block_height,
                        commit=False,
                    )
            self.upsert_transactions(txs)
            self._conn.commit()

    def clear_runtime_cache(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                DELETE FROM wallet_state;
                DELETE FROM addresses;
                DELETE FROM transactions;
                DELETE FROM operations;
                DELETE FROM sync_jobs;
                """
            )
            self._conn.commit()

    def integrity_check(self) -> bool:
        with self._lock:
            row = self._execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")

    def backup_to(self, destination: Path | str) -> None:
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            backup_conn = sqlite3.connect(str(dest))
            try:
                self._conn.backup(backup_conn)
            finally:
                backup_conn.close()
