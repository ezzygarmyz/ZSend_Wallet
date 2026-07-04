from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .common import _is_z_addr, _track
from .dialogs import _DraggableDialog, _msg_critical, _msg_warning
from .helpers import fmt_btcz
from .locales import tr
from .rpc import RPCError
from .wallet_cache import btcz_to_zat, zat_to_float

MIN_NODE_FEE_ZAT = btcz_to_zat(0.00001)
MAX_NODE_FEE_ZAT = btcz_to_zat(0.1)
from .workers import PollWorker, SendPreflightWorker, SendWorker


def _show_err(window, title: str, available: str, required: str, note: str):
    dlg_err = _DraggableDialog(window)
    dlg_err.setMinimumWidth(340)
    ev = QVBoxLayout(dlg_err)
    ev.setContentsMargins(24, 22, 24, 22)
    ev.setSpacing(10)
    t_err = QLabel(title)
    t_err.setStyleSheet("font-size:14px;font-weight:700;color:#f85149;")
    t_err.setAlignment(Qt.AlignmentFlag.AlignCenter)
    ev.addWidget(t_err)
    div_e = QFrame()
    div_e.setFixedHeight(1)
    div_e.setStyleSheet("background:#30363d;")
    ev.addWidget(div_e)

    def _erow(lbl_text, val_text, val_color="#e6edf3"):
        h = QHBoxLayout()
        h.setSpacing(8)
        l = QLabel(lbl_text)
        l.setStyleSheet("color:#8b949e;font-size:12px;min-width:80px;")
        h.addWidget(l)
        vl = QLabel(val_text)
        vl.setStyleSheet(f"font-size:12px;font-weight:600;color:{val_color};")
        h.addWidget(vl, 1)
        ev.addLayout(h)

    if available:
        _erow(tr("dialogs.send_flow.available"), available, "#238636")
    if required:
        _erow(tr("dialogs.send_flow.required"), required, "#f85149")
    div_e2 = QFrame()
    div_e2.setFixedHeight(1)
    div_e2.setStyleSheet("background:#30363d;")
    ev.addWidget(div_e2)
    n = QLabel(note)
    n.setStyleSheet("color:#8b949e;font-size:11px;")
    n.setWordWrap(True)
    n.setAlignment(Qt.AlignmentFlag.AlignCenter)
    ev.addWidget(n)
    btn_ok = QPushButton(tr("common.buttons.ok"))
    btn_ok.setObjectName("primary")
    btn_ok.setMinimumWidth(90)
    btn_ok.clicked.connect(dlg_err.accept)
    hb = QHBoxLayout()
    hb.addStretch()
    hb.addWidget(btn_ok)
    hb.addStretch()
    ev.addLayout(hb)
    dlg_err.exec()


def do_send(window) -> None:
    frm = window.combo_from.currentData()
    to = window.e_to.text().strip()
    amt = round(window.spin_amt.value(), 8)
    fee = round(window.spin_fee.value(), 8)
    memo = window.e_memo.text().strip()
    if not frm:
        _msg_warning(window, tr("dialogs.send_flow.missing_title"), tr("dialogs.send_flow.select_from"))
        return

    if str(frm).strip() == str(to).strip():
        _show_err(window, tr("dialogs.send_flow.same_address_title"), "", "", tr("dialogs.send_flow.same_address_note"))
        window._update_send_btn()
        return

    cached_balance_zat = btcz_to_zat(getattr(window, "_addr_balances", {}).get(frm, 0.0))
    if cached_balance_zat <= 0:
        _show_err(window, tr("dialogs.send_flow.empty_address_title"), "", "", tr("dialogs.send_flow.empty_address_note"))
        window._update_send_btn()
        return

    if frm in getattr(window, "_busy_addresses", set()):
        _show_err(window, tr("dialogs.send_flow.address_busy_title"), "", "", tr("dialogs.send_flow.address_busy_note"))
        return

    if memo and not _is_z_addr(to):
        _show_err(window, tr("dialogs.send_flow.invalid_memo"), "", "", tr("dialogs.send_flow.invalid_memo_note"))
        return

    if len(memo.encode("utf-8")) > 512:
        _show_err(
            window,
            tr("dialogs.send_flow.memo_too_large"),
            "",
            tr("dialogs.send_flow.memo_too_large_required"),
            tr("dialogs.send_flow.memo_too_large_note"),
        )
        return

    ctx = {"from": frm, "to": to, "amount": amt, "fee": fee, "memo": memo}
    window.btn_send.setEnabled(False)
    w = SendPreflightWorker(window.rpc, frm, to)
    window._threads.append(w)
    w.finished.connect(lambda: window._threads.remove(w) if w in window._threads else None)
    w.done.connect(lambda result: _send_preflight_done(window, ctx, result))
    w.error.connect(lambda msg: _send_preflight_error(window, msg))
    _track(w).start()


def _send_preflight_error(window, msg: str) -> None:
    window._update_send_btn()
    _show_err(window, tr("dialogs.send_flow.address_check_failed"), "", "", msg)


def _send_preflight_done(window, ctx: dict, result: dict) -> None:
    if not result.get("valid"):
        window._update_send_btn()
        _show_err(window, tr("dialogs.send_flow.invalid_address"), "", "", tr("dialogs.send_flow.invalid_address_note"))
        return
    bal = round(float(result.get("balance", 0) or 0), 8)
    window._addr_balances[ctx["from"]] = bal
    _confirm_and_send(window, ctx, bal)


def _confirm_and_send(window, ctx: dict, bal: float) -> None:
    frm = ctx["from"]
    to = ctx["to"]
    memo = ctx["memo"]
    amt = ctx["amount"]
    fee = ctx["fee"]

    bal_zat = btcz_to_zat(bal)
    amt_zat = btcz_to_zat(amt)
    fee_zat = btcz_to_zat(fee)
    amt = zat_to_float(amt_zat)
    fee = zat_to_float(fee_zat)

    if fee_zat > MAX_NODE_FEE_ZAT:
        window._update_send_btn()
        _show_err(
            window,
            tr("dialogs.send_flow.fee_too_large"),
            f"{fmt_btcz(zat_to_float(fee_zat))} BTCZ",
            f"{fmt_btcz(zat_to_float(MAX_NODE_FEE_ZAT))} BTCZ",
            tr("dialogs.send_flow.fee_above_node_limit"),
        )
        return
    if fee_zat < MIN_NODE_FEE_ZAT:
        window._update_send_btn()
        _show_err(
            window,
            tr("dialogs.send_flow.fee_too_small"),
            f"{fmt_btcz(zat_to_float(fee_zat))} BTCZ",
            f"{fmt_btcz(zat_to_float(MIN_NODE_FEE_ZAT))} BTCZ",
            tr("dialogs.send_flow.fee_below_node_minimum"),
        )
        return
    if fee_zat >= bal_zat:
        window._update_send_btn()
        _show_err(window, tr("dialogs.send_flow.fee_too_large"), f"{fmt_btcz(zat_to_float(bal_zat))} BTCZ", f"{fmt_btcz(zat_to_float(fee_zat))} BTCZ", tr("dialogs.send_flow.fee_less_than_balance"))
        return
    if amt_zat <= 0:
        window._update_send_btn()
        _show_err(window, tr("dialogs.send_flow.invalid_amount"), "", "", tr("dialogs.send_flow.invalid_amount_note"))
        return
    if fee_zat >= amt_zat:
        window._update_send_btn()
        _show_err(window, tr("dialogs.send_flow.fee_too_large"), f"{fmt_btcz(zat_to_float(amt_zat))} BTCZ (amount)", f"{fmt_btcz(zat_to_float(fee_zat))} BTCZ (fee)", tr("dialogs.send_flow.fee_less_than_amount"))
        return
    if amt_zat + fee_zat > bal_zat:
        window._update_send_btn()
        _show_err(
            window,
            tr("dialogs.send_flow.insufficient_funds"),
            f"{fmt_btcz(zat_to_float(bal_zat))} BTCZ",
            f"{fmt_btcz(zat_to_float(amt_zat + fee_zat))} BTCZ",
            tr("dialogs.send_flow.insufficient_funds_note"),
        )
        return

    dlg = _DraggableDialog(window)
    dlg.setMinimumWidth(520)
    root = QVBoxLayout(dlg)
    root.setContentsMargins(16, 18, 16, 16)
    root.setSpacing(0)

    title = QLabel(tr("dialogs.send_flow.confirm_title"))
    title.setStyleSheet("font-size:15px;font-weight:700;color:#e6edf3;")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    root.addWidget(title)
    root.addSpacing(14)

    def _add_row(label: str, value: str, value_style: str = "color:#e6edf3;"):
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(label)
        lbl.setStyleSheet("color:#8b949e;font-size:12px;min-width:52px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(lbl)
        val = QLabel(value)
        val.setStyleSheet(f"font-size:12px;font-weight:600;{value_style}")
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(val, 1)
        root.addLayout(row)
        root.addSpacing(5)

    _add_row(tr("dialogs.send_flow.row_amount"), f"{fmt_btcz(amt)} BTCZ", "color:#e6edf3;")
    _add_row(tr("dialogs.send_flow.row_fee"), f"{fmt_btcz(fee)} BTCZ", "color:#8b949e;")
    div = QFrame()
    div.setFixedHeight(1)
    div.setStyleSheet("background:#30363d;")
    root.addWidget(div)
    root.addSpacing(5)
    _add_row(tr("dialogs.send_flow.row_total"), f"{fmt_btcz(amt + fee)} BTCZ", "color:#f7a32c;font-size:13px;")
    root.addSpacing(5)
    div2 = QFrame()
    div2.setFixedHeight(1)
    div2.setStyleSheet("background:#30363d;")
    root.addWidget(div2)
    root.addSpacing(8)
    _add_row(tr("dialogs.send_flow.row_from"), frm, "color:#8b949e;font-family:Consolas,'Courier New',monospace;font-size:12px;")
    _add_row(tr("dialogs.send_flow.row_to"), to, "color:#58a6ff;font-family:Consolas,'Courier New',monospace;font-size:12px;")
    if memo:
        _add_row(tr("dialogs.send_flow.row_memo"), memo, "color:#8b949e;")

    root.addSpacing(14)
    btn_row = QHBoxLayout()
    btn_row.setSpacing(10)
    btn_row.addStretch()
    btn_cancel = QPushButton(tr("dialogs.send_flow.cancel"))
    btn_cancel.setMinimumWidth(100)
    btn_row.addWidget(btn_cancel)
    btn_confirm = QPushButton(tr("dialogs.send_flow.send"))
    btn_confirm.setObjectName("primary")
    btn_confirm.setMinimumWidth(100)
    btn_row.addWidget(btn_confirm)
    btn_row.addStretch()
    root.addLayout(btn_row)

    dlg._confirmed = False
    btn_confirm.clicked.connect(lambda: (setattr(dlg, "_confirmed", True), dlg.accept()))
    btn_cancel.clicked.connect(dlg.reject)
    if not dlg.exec() or not dlg._confirmed:
        window._update_send_btn()
        return

    window._pending_send = {
        "from": frm,
        "to": to,
        "amount": amt,
        "fee": fee,
        "memo": memo,
    }
    window._active_opid = "submitting"
    window._update_send_btn()
    w = SendWorker(window.rpc, frm, to, amt, fee, memo)
    window._threads.append(w)
    w.finished.connect(lambda: window._threads.remove(w) if w in window._threads else None)
    w.done.connect(lambda opid: send_ok(window, opid))
    w.error.connect(lambda msg: send_err(window, msg))
    _track(w).start()


def cache_upsert_send_operation(window, opid: str, status: str) -> None:
    if window.cache is None:
        return
    ctx = window._pending_send or {}
    try:
        window.cache.upsert_operation(
            opid,
            op_type="send",
            status=status,
            from_address=ctx.get("from"),
            to_address=ctx.get("to"),
            amount=ctx.get("amount"),
            fee=ctx.get("fee"),
        )
    except Exception:
        pass


def cache_update_send_operation(window, status: str, *, txid: str | None = None, error: str | None = None, result=None) -> None:
    opid = getattr(window, "_active_opid", "")
    if window.cache is None or not opid or opid == "submitting":
        return
    try:
        window.cache.update_operation_status(opid, status, txid=txid, error=error, result=result)
    except Exception:
        pass


def send_ok(window, opid: str) -> None:
    window._active_opid = opid
    cache_upsert_send_operation(window, opid, "submitted")
    QTimer.singleShot(5000, lambda: window.refresh(force_full=True))
    pw = PollWorker(window.rpc, opid)
    window._threads.append(pw)
    pw.finished.connect(lambda: window._threads.remove(pw) if pw in window._threads else None)
    pw.status_update.connect(lambda status: poll_status(window, status))
    pw.success.connect(lambda txid: poll_success(window, txid))
    pw.failed.connect(lambda msg: poll_failed(window, msg))
    pw.cancelled.connect(lambda msg: poll_cancelled(window, msg))
    pw.unknown.connect(lambda msg: poll_unknown(window, msg))
    _track(pw).start()


def poll_status(window, status: str) -> None:
    node_status = str(status or "").strip().lower()
    if node_status not in {"queued", "executing"}:
        node_status = "executing"
    cache_update_send_operation(window, node_status)


def poll_success(window, txid: str) -> None:
    cache_update_send_operation(window, "success", txid=txid, result={"txid": txid})
    window._active_opid = ""
    window._pending_send = None
    window.e_memo.clear()
    window._update_send_btn()
    window.refresh(force_full=True)


def poll_failed(window, msg: str) -> None:
    cache_update_send_operation(window, "failed", error=msg)
    window._active_opid = ""
    window._pending_send = None
    window._update_send_btn()
    window.refresh(force_full=True)
    _msg_critical(window, tr("dialogs.send_flow.send_error"), msg)


def poll_cancelled(window, msg: str) -> None:
    cache_update_send_operation(window, "cancelled", error=msg)
    window._active_opid = ""
    window._pending_send = None
    window._update_send_btn()
    window.refresh(force_full=True)


def poll_unknown(window, msg: str) -> None:
    cache_update_send_operation(window, "unknown", error=msg)
    window._active_opid = ""
    window._pending_send = None
    window._update_send_btn()
    window.refresh(force_full=True)
    _msg_warning(
        window,
        tr("dialogs.send_flow.operation_unknown_title"),
        tr("dialogs.send_flow.operation_unknown_note"),
    )


def send_err(window, msg: str) -> None:
    if window.cache is not None:
        try:
            window.cache.set_state("last_send_error", msg)
        except Exception:
            pass
    window._active_opid = ""
    window._pending_send = None
    window._update_send_btn()
    _msg_critical(window, tr("dialogs.send_flow.send_error"), msg)
