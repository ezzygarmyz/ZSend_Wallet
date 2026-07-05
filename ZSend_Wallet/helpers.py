from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from PySide6.QtWidgets import QLabel, QPushButton


_PLATFORMS = {
    "darwin": "macOS",
    "linux": "linux",
    "freebsd": "freeBSD",
    "win32": "windows",
}

def get_current_platform() -> str | None:
    return _PLATFORMS.get(sys.platform)

current_platform = get_current_platform()


_BTCZ_DISPLAY_QUANT = Decimal("0.00000001")


def is_shielded_address(addr: str) -> bool:
    return str(addr or "").startswith(("zs", "zc"))


def _is_z_addr(addr: str) -> bool:
    return is_shielded_address(addr)


def _fmt_addr(addr: str) -> str:
    addr = str(addr or "")
    if _is_z_addr(addr):
        return "\U0001f6e1 " + addr[:25] + "..." + addr[-25:]
    return addr


def _sort_addr_items(items: list, mode: str) -> list:
    if mode == "name_desc":
        return sorted(items, key=lambda x: x[0].lower(), reverse=True)
    if mode == "name_asc":
        return sorted(items, key=lambda x: x[0].lower())
    if mode == "balance_asc":
        non_zero = sorted([(a, b) for a, b in items if b > 0], key=lambda x: (x[1], x[0].lower()))
        zero = sorted([(a, b) for a, b in items if b <= 0], key=lambda x: x[0].lower())
        return zero + non_zero
    non_zero = sorted([(a, b) for a, b in items if b > 0], key=lambda x: (x[1], x[0].lower()), reverse=True)
    zero = sorted([(a, b) for a, b in items if b <= 0], key=lambda x: x[0].lower())
    return non_zero + zero


def fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def fmt_btcz(v: float) -> str:
    try:
        amount = Decimal(str(v)).quantize(_BTCZ_DISPLAY_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    if not amount.is_finite() or amount == 0:
        return "0"
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def tx_status_code(tx: dict) -> str:
    status = str((tx or {}).get("status", "") or "").strip().lower()
    try:
        confirms = int((tx or {}).get("confirmations", 0) or 0)
    except (TypeError, ValueError):
        confirms = 0
    if status in {"failed", "expired"}:
        return status
    if status == "conflicted" or confirms < 0:
        return "conflicted"
    if status == "reorged":
        return "reorged"
    if status == "stale":
        return "stale"
    if status == "pending" or confirms == 0:
        return "pending"
    if confirms < 6:
        return "confirming"
    return "confirmed"


def safe_set_text(widget_ref: "QPushButton | QLabel", text: str) -> None:
    try:
        widget_ref.setText(text)
    except RuntimeError:
        pass
