from __future__ import annotations

import io

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QVBoxLayout

from .common import qrcode
from .dialogs import KeyDisplayDialog, _DraggableDialog, _get_save_file_name, _msg_critical
from .helpers import fmt_btcz, safe_set_text
from .locales import tr
from .rpc import BitcoinZRPC, RPCError


def _clone_rpc(rpc: BitcoinZRPC) -> BitcoinZRPC:
    clone = getattr(rpc, "clone", None)
    return clone() if callable(clone) else rpc


class ExportKeyWorker(QThread):
    done = Signal(str)
    error = Signal(str)

    def __init__(self, rpc, addr: str, is_z: bool):
        super().__init__()
        self.rpc = _clone_rpc(rpc)
        self.addr = addr
        self.is_z = bool(is_z)

    def run(self):
        try:
            key = self.rpc.z_ExportKey(self.addr) if self.is_z else self.rpc.DumpPrivKey(self.addr)
            self.done.emit(key)
        except RPCError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(str(e))


def row_addr(window, tbl, pos):
    idx = tbl.indexAt(pos)
    if not idx.isValid():
        return ""
    model = tbl.model()
    if hasattr(model, "address_at"):
        return model.address_at(idx.row())
    return ""


def selected_addr(window, tbl) -> str:
    sel = tbl.selectionModel()
    if not sel or not sel.hasSelection():
        return ""
    rows = sel.selectedRows()
    if not rows:
        return ""
    model = tbl.model()
    if hasattr(model, "address_at"):
        return model.address_at(rows[0].row())
    return ""


def copy_selected_address(window, tbl) -> None:
    addr = selected_addr(window, tbl)
    if not addr:
        return
    QApplication.clipboard().setText(addr)


def copy_address_balance(window, addr: str) -> None:
    if not addr:
        return
    balance = getattr(window, "_addr_balances", {}).get(addr, 0.0)
    QApplication.clipboard().setText(fmt_btcz(balance))


def show_selected_address_qr(window, tbl) -> None:
    addr = selected_addr(window, tbl)
    if addr:
        show_address_qr(window, addr)


def prefill_send(window, addr: str) -> None:
    if not addr:
        return
    window.tabs.setCurrentIndex(2)
    for i in range(window.combo_from.count()):
        if window.combo_from.itemData(i) == addr:
            window.combo_from.setCurrentIndex(i)
            break
    window.combo_from.setFocus()


def show_address_qr(window, addr: str) -> None:
    if not addr:
        return
    dlg = _DraggableDialog(window)
    dlg.setWindowTitle(tr("dialogs.address_actions.qr_title"))
    dlg.setMinimumWidth(420)
    v = QVBoxLayout(dlg)
    v.setContentsMargins(20, 18, 20, 18)
    v.setSpacing(10)

    title = QLabel(tr("dialogs.address_actions.receive_title"))
    title.setStyleSheet("font-size:14px;font-weight:700;")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(title)

    note = QLabel(tr("dialogs.address_actions.receive_note"))
    note.setWordWrap(True)
    note.setStyleSheet("color:#8b949e;font-size:12px;")
    note.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(note)

    qr_holder = QLabel()
    qr_holder.setAlignment(Qt.AlignmentFlag.AlignCenter)
    qr_holder.setMinimumHeight(260)
    pix = None
    if qrcode is not None:
        qr = qrcode.QRCode(border=2, box_size=8)
        qr.add_data(tr("dialogs.address_actions.uri_scheme", address=addr))
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        qr_holder.setPixmap(pix)
    else:
        qr_holder.setText(tr("dialogs.address_actions.qr_unavailable"))
        qr_holder.setStyleSheet("color:#f85149;font-size:12px;")
    v.addWidget(qr_holder)

    val = QLabel(addr)
    val.setWordWrap(True)
    val.setAlignment(Qt.AlignmentFlag.AlignCenter)
    val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    val.setStyleSheet("font-family:Consolas,'Courier New',monospace;color:#58a6ff;")
    v.addWidget(val)

    h = QHBoxLayout()
    bcopy = QPushButton(tr("dialogs.address_actions.copy_address"))
    bcopy.setObjectName("primary")
    bcopy.setMinimumWidth(120)
    bcopy.clicked.connect(lambda: _copy_qr_address(addr, bcopy))
    h.addWidget(bcopy)
    bsave = QPushButton(tr("dialogs.address_actions.save_qr"))
    bsave.setMinimumWidth(130)
    bsave.setEnabled(pix is not None)
    bsave.clicked.connect(lambda: _save_qr_png(window, addr, pix, bsave))
    h.addWidget(bsave)
    h.addStretch()
    btn = QPushButton(tr("common.buttons.close"))
    btn.setMinimumWidth(90)
    btn.clicked.connect(dlg.accept)
    h.addWidget(btn)
    v.addLayout(h)
    dlg.exec()


def _confirm_key_export(window, addr: str, kind: str, funds: str) -> bool:
    dlg = _DraggableDialog(window)
    dlg.setWindowTitle(tr("dialogs.address_actions.security_warning_title"))
    dlg.setFixedWidth(500)

    v = QVBoxLayout(dlg)
    v.setContentsMargins(18, 14, 18, 16)
    v.setSpacing(8)

    title = QLabel(tr("dialogs.address_actions.security_warning_title"))
    title.setStyleSheet("color:#ffd166;font-size:23px;font-weight:900;")
    v.addWidget(title)

    intro = QLabel(tr("dialogs.address_actions.export_warning_intro", kind=kind))
    intro.setStyleSheet("color:#c9d1d9;font-size:13px;")
    v.addWidget(intro)

    address_label = QLabel(addr)
    address_label.setWordWrap(True)
    address_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    address_label.setStyleSheet(
        "font-family:Consolas,'Courier New',monospace;color:#f7a32c;"
        "background:#21262d;border:1px solid #5c4222;border-radius:6px;"
        "padding:8px 12px;font-size:12px;"
    )
    v.addWidget(address_label)

    warning_panel = QFrame()
    warning_panel.setStyleSheet(
        "QFrame{background:#21262d;border:none;border-radius:7px;}"
    )
    wp = QHBoxLayout(warning_panel)
    wp.setContentsMargins(10, 8, 10, 8)
    wp.setSpacing(10)
    shield = QLabel("!")
    shield.setAlignment(Qt.AlignmentFlag.AlignCenter)
    shield.setFixedSize(26, 26)
    shield.setStyleSheet(
        "QLabel{color:#f7a32c;border:2px solid #f7a32c;border-radius:13px;"
        "font-size:16px;font-weight:900;background:transparent;}"
    )
    wp.addWidget(shield)

    warning = QLabel(tr("dialogs.address_actions.export_warning_control", funds=funds))
    warning.setWordWrap(True)
    warning.setStyleSheet("color:#c9d1d9;font-size:13px;")
    wp.addWidget(warning, 1)
    v.addWidget(warning_panel)

    note = QLabel(tr("dialogs.address_actions.export_warning_confirm"))
    note.setWordWrap(True)
    note.setStyleSheet("color:#c9d1d9;font-size:13px;")
    v.addWidget(note)

    divider = QFrame()
    divider.setFixedHeight(1)
    divider.setStyleSheet("background:#30363d;")
    v.addWidget(divider)

    row = QHBoxLayout()
    row.setSpacing(12)
    row.addStretch()
    btn_cancel = QPushButton(tr("dialogs.address_actions.cancel"))
    btn_cancel.setMinimumWidth(100)
    btn_cancel.setMinimumHeight(36)
    row.addWidget(btn_cancel)
    btn_continue = QPushButton(tr("dialogs.address_actions.continue"))
    btn_continue.setObjectName("primary")
    btn_continue.setMinimumWidth(118)
    btn_continue.setMinimumHeight(36)
    row.addWidget(btn_continue)
    v.addLayout(row)

    dlg._confirmed = False
    btn_continue.clicked.connect(lambda: (setattr(dlg, "_confirmed", True), dlg.accept()))
    btn_cancel.clicked.connect(dlg.reject)
    return bool(dlg.exec() and getattr(dlg, "_confirmed", False))


def export_key(window, addr: str, is_z: bool) -> None:
    kind = tr("dialogs.address_actions.kind_spending_key") if is_z else tr("dialogs.address_actions.kind_private_key")
    funds = tr("dialogs.address_actions.funds_all_shielded") if is_z else tr("dialogs.address_actions.funds_all")
    title = tr("dialogs.address_actions.title_spending_key") if is_z else tr("dialogs.address_actions.title_private_key")
    if not _confirm_key_export(window, addr, kind, funds):
        return
    w = ExportKeyWorker(window.rpc, addr, is_z)
    window._threads.append(w)
    w.finished.connect(lambda: window._threads.remove(w) if w in window._threads else None)
    w.done.connect(lambda key: KeyDisplayDialog(window, title, addr, key).exec())
    w.error.connect(lambda msg: _msg_critical(window, tr("dialogs.address_actions.export_error"), msg))
    from .common import _track
    _track(w).start()


def _copy_qr_address(addr: str, btn: QPushButton) -> None:
    QApplication.clipboard().setText(addr)
    btn.setText(tr("common.buttons.copied"))
    from PySide6.QtCore import QTimer
    QTimer.singleShot(2000, lambda: safe_set_text(btn, tr("dialogs.address_actions.copy_address")))


def _save_qr_png(window, addr: str, pix: QPixmap | None, btn: QPushButton) -> None:
    if pix is None or pix.isNull():
        return
    safe_name = "".join(ch for ch in addr if ch.isalnum())[:24] or "address_qr"
    path, _ = _get_save_file_name(
        window,
        tr("dialogs.address_actions.save_qr"),
        f"{safe_name}.png",
        tr("dialogs.address_actions.png_filter"),
    )
    if not path:
        return
    target = path if path.lower().endswith(".png") else f"{path}.png"
    if pix.save(target, "PNG"):
        btn.setText(tr("common.buttons.saved"))
        from PySide6.QtCore import QTimer
        QTimer.singleShot(2000, lambda: safe_set_text(btn, tr("dialogs.address_actions.save_qr")))


def view_address_on_explorer(addr: str) -> None:
    if addr:
        QDesktopServices.openUrl(QUrl(f"https://explorer.btcz.zelcore.io/address/{addr}"))


def t_context_menu(window, pos) -> None:
    addr = row_addr(window, window.tbl_t, pos)
    if not addr:
        return
    m = QMenu(window)
    ac = m.addAction(tr("dialogs.address_actions.copy_address"))
    abal = m.addAction(tr("dialogs.address_actions.copy_balance"))
    aqr = m.addAction(tr("dialogs.main_window.show_qr"))
    aexpl = m.addAction(tr("dialogs.address_actions.view_on_explorer"))
    asend = m.addAction(tr("dialogs.address_actions.send_from_address"))
    m.addSeparator()
    aexp = m.addAction(tr("dialogs.address_actions.export_private_key"))
    act = m.exec(window.tbl_t.viewport().mapToGlobal(pos))
    if act == ac:
        QApplication.clipboard().setText(addr)
    elif act == abal:
        copy_address_balance(window, addr)
    elif act == aqr:
        show_address_qr(window, addr)
    elif act == aexpl:
        view_address_on_explorer(addr)
    elif act == asend:
        prefill_send(window, addr)
    elif act == aexp:
        export_key(window, addr, is_z=False)


def z_context_menu(window, pos) -> None:
    addr = row_addr(window, window.tbl_z, pos)
    if not addr:
        return
    m = QMenu(window)
    ac = m.addAction(tr("dialogs.address_actions.copy_address"))
    abal = m.addAction(tr("dialogs.address_actions.copy_balance"))
    aqr = m.addAction(tr("dialogs.main_window.show_qr"))
    asend = m.addAction(tr("dialogs.address_actions.send_from_address"))
    m.addSeparator()
    aexp = m.addAction(tr("dialogs.address_actions.export_spending_key"))
    act = m.exec(window.tbl_z.viewport().mapToGlobal(pos))
    if act == ac:
        QApplication.clipboard().setText(addr)
    elif act == abal:
        copy_address_balance(window, addr)
    elif act == aqr:
        show_address_qr(window, addr)
    elif act == asend:
        prefill_send(window, addr)
    elif act == aexp:
        export_key(window, addr, is_z=True)
