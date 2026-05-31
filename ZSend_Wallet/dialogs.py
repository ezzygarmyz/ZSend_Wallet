from __future__ import annotations

import hashlib
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
)

from .common import (
    DEVELOPER_TIP_ADDRESS,
    _RPC_DEFAULT_HOST,
    _RPC_DEFAULT_PORT,
    _is_z_addr,
    _track,
    find_node,
    is_port_open,
    load_rpc_cfg,
    node_running,
    read_conf,
    wallet_version,
)
from .helpers import fmt_btcz, fmt_ts, safe_set_text, tx_status_code
from .locales import tr
from .rpc import BitcoinZRPC, RPCError
from .wallet_cache import WalletCache
from .wallet_import import ImportKeyWorker, ImportPreflightWorker, LogTailWorker


class _DraggableDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos = None
        self._initial_positioned = False
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)

    def _center_on_parent(self):
        parent = self.parentWidget()
        if parent and parent.isVisible():
            rect = parent.frameGeometry()
        else:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            rect = screen.availableGeometry()
        screen = (parent.screen() if parent else self.screen()) or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else rect
        x = rect.x() + max(0, (rect.width() - self.width()) // 2)
        y = rect.y() + max(0, (rect.height() - self.height()) // 2)
        x = max(available.x(), min(x, available.x() + max(0, available.width() - self.width())))
        y = max(available.y(), min(y, available.y() + max(0, available.height() - self.height())))
        self.move(x, y)

    def _fit_to_screen(self, preferred_w: int, preferred_h: int,
                       min_w: int = 420, min_h: int = 320, margin: int = 80) -> tuple[int, int]:
        parent = self.parentWidget()
        screen = (parent.screen() if parent else self.screen()) or QApplication.primaryScreen()
        if screen is None:
            width, height = preferred_w, preferred_h
        else:
            available = screen.availableGeometry()
            max_w = max(360, available.width() - margin)
            max_h = max(280, available.height() - margin)
            width = min(preferred_w, max_w)
            height = min(preferred_h, max_h)
        self.setMinimumSize(min(min_w, width), min(min_h, height))
        self.resize(width, height)
        return int(width), int(height)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_positioned:
            self._initial_positioned = True
            QTimer.singleShot(0, self._center_on_parent)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


class _DraggableFileDialog(QFileDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_pos = None
        self._initial_positioned = False
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self._apply_sidebar_urls()

    def _apply_sidebar_urls(self):
        home = Path.home()
        desktop = home / "Desktop"
        documents = home / "Documents"
        downloads = home / "Downloads"
        existing = []
        for path in (desktop, home, documents, downloads):
            try:
                if path.exists():
                    existing.append(QUrl.fromLocalFile(str(path)))
            except Exception:
                pass
        if existing:
            self.setSidebarUrls(existing)

    def _center_on_parent(self):
        parent = self.parentWidget()
        if parent and parent.isVisible():
            rect = parent.frameGeometry()
        else:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            rect = screen.availableGeometry()
        screen = (parent.screen() if parent else self.screen()) or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else rect
        x = rect.x() + max(0, (rect.width() - self.width()) // 2)
        y = rect.y() + max(0, (rect.height() - self.height()) // 2)
        x = max(available.x(), min(x, available.x() + max(0, available.width() - self.width())))
        y = max(available.y(), min(y, available.y() + max(0, available.height() - self.height())))
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._initial_positioned:
            self._initial_positioned = True
            QTimer.singleShot(0, self._center_on_parent)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)


class _MessageDialog(_DraggableDialog):
    def __init__(self, parent, title: str, text: str, kind: str = "info",
                 buttons: list[tuple[str, int]] | None = None, default_button: int | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        self._result = 0
        if buttons is None:
            buttons = [(tr("common.buttons.ok"), int(QMessageBox.StandardButton.Ok))]

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)

        hdr = QLabel(title)
        hdr.setStyleSheet(
            "font-size:14px;font-weight:700;"
            + {
                "info": "color:#58a6ff;",
                "warning": "color:#f7a32c;",
                "critical": "color:#f85149;",
            }.get(kind, "color:#e6edf3;")
        )
        v.addWidget(hdr)

        body_text = str(text or "")
        if len(body_text) > 500 or body_text.count("\n") > 8:
            body = QTextEdit()
            body.setReadOnly(True)
            body.setPlainText(body_text)
            body.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            body.setMinimumHeight(120)
            body.setMaximumHeight(320)
            body.setStyleSheet(
                "QTextEdit{background:#161b22;color:#e6edf3;border:1px solid #30363d;"
                "border-radius:6px;padding:8px;font-size:12px;}"
            )
        else:
            body = QLabel(body_text)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setStyleSheet("color:#e6edf3;font-size:12px;")
        v.addWidget(body)

        row = QHBoxLayout()
        row.addStretch()
        for label, value in buttons:
            btn = QPushButton(label)
            btn.setMinimumWidth(88)
            if default_button is not None and value == default_button:
                btn.setObjectName("primary")
            btn.clicked.connect(lambda _=False, v=value: self._finish(v))
            row.addWidget(btn)
        v.addLayout(row)
        self._fit_to_screen(560, min(520, max(180, self.sizeHint().height() + 20)), min_w=360, min_h=160)

    def _finish(self, value: int):
        self._result = value
        self.accept()

    @property
    def result_button(self) -> int:
        return self._result


def _msg(parent, title: str, text: str, kind: str = "info",
         buttons: list[tuple[str, int]] | None = None,
         default_button: int | None = None) -> int:
    dlg = _MessageDialog(parent, title, text, kind=kind, buttons=buttons, default_button=default_button)
    dlg.exec()
    return dlg.result_button or int(default_button or QMessageBox.StandardButton.Ok)


def _msg_info(parent, title: str, text: str) -> None:
    _msg(parent, title, text, kind="info")


def _msg_warning(parent, title: str, text: str) -> None:
    _msg(parent, title, text, kind="warning")


def _msg_critical(parent, title: str, text: str) -> None:
    _msg(parent, title, text, kind="critical")


def _ask_yes_no(parent, title: str, text: str, *, yes_text: str | None = None, no_text: str | None = None,
                kind: str = "warning") -> bool:
    yes_text = yes_text or tr("common.buttons.yes")
    no_text = no_text or tr("common.buttons.no")
    result = _msg(
        parent,
        title,
        text,
        kind=kind,
        buttons=[
            (no_text, int(QMessageBox.StandardButton.No)),
            (yes_text, int(QMessageBox.StandardButton.Yes)),
        ],
        default_button=int(QMessageBox.StandardButton.Yes),
    )
    return result == int(QMessageBox.StandardButton.Yes)


def _get_save_file_name(parent, title: str, path: str, flt: str) -> tuple[str, str]:
    dlg = _DraggableFileDialog(parent)
    dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dlg.setFileMode(QFileDialog.FileMode.AnyFile)
    dlg.setNameFilter(flt)
    p = Path(path)
    if p.parent.exists():
        dlg.setDirectory(str(p.parent))
    dlg.selectFile(p.name if p.name else str(p))
    dlg.setWindowTitle(title)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        files = dlg.selectedFiles()
        return (files[0], dlg.selectedNameFilter()) if files else ("", "")
    return "", ""


def _get_open_file_name(parent, title: str, path: str, flt: str) -> tuple[str, str]:
    dlg = _DraggableFileDialog(parent)
    dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
    dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
    p = Path(path)
    dlg.setDirectory(str(p if p.is_dir() else p.parent))
    dlg.setNameFilter(flt)
    dlg.setWindowTitle(title)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        files = dlg.selectedFiles()
        return (files[0], dlg.selectedNameFilter()) if files else ("", "")
    return "", ""


class ConfigDialog(_DraggableDialog):
    def __init__(self, parent, host, port, user, pw, conf_path=""):
        super().__init__(parent)
        self.setWindowTitle(tr("dialogs.config.title")); self.setMinimumWidth(500)
        f = QFormLayout(self); f.setSpacing(12); f.setContentsMargins(18, 18, 18, 18)
        if conf_path:
            h = QLabel(tr("dialogs.config.config_path", path=conf_path)); h.setStyleSheet("color:#8b949e;font-size:11px;")
            f.addRow(h)
        self.e_host = QLineEdit(host); self.e_port = QLineEdit(str(port))
        self.e_user = QLineEdit(user); self.e_pass = QLineEdit(pw)
        self.e_pass.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow(tr("dialogs.config.rpc_host"), self.e_host)
        f.addRow(tr("dialogs.config.rpc_port"), self.e_port)
        f.addRow(tr("dialogs.config.rpc_user"), self.e_user)
        f.addRow(tr("dialogs.config.rpc_password"), self.e_pass)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept); btns.rejected.connect(self.reject); f.addRow(btns)

    def values(self):
        return (self.e_host.text().strip(), int(self.e_port.text().strip() or "1979"),
                self.e_user.text().strip(), self.e_pass.text().strip())


class DiagDialog(_DraggableDialog):
    def __init__(self, parent, rpc: BitcoinZRPC, cache: WalletCache | None = None):
        super().__init__(parent)
        self.setWindowTitle(tr("dialogs.diagnostics.title"))
        self._fit_to_screen(720, 560, min_w=560, min_h=420)
        self._rpc = rpc
        self._cache = cache
        self._diag_worker = None
        v = QVBoxLayout(self); v.setContentsMargins(18, 18, 18, 18); v.setSpacing(10)
        t = QLabel(tr("dialogs.diagnostics.heading")); t.setStyleSheet("font-size:15px;font-weight:700;")
        v.addWidget(t)
        self.txt = QTextEdit(); self.txt.setReadOnly(True)
        self.txt.setPlainText(tr("dialogs.diagnostics.running"))
        v.addWidget(self.txt, 1)
        h = QHBoxLayout()
        self.btn_rerun = QPushButton(tr("common.buttons.refresh")); self.btn_rerun.clicked.connect(self._start)
        h.addWidget(self.btn_rerun)
        self.btn_copy = QPushButton(tr("dialogs.diagnostics.copy_report"))
        self.btn_copy.setMinimumWidth(150)
        self.btn_copy.clicked.connect(self._copy_report)
        h.addWidget(self.btn_copy)
        h.addStretch()
        bc = QPushButton(tr("common.buttons.close")); bc.clicked.connect(self.accept); h.addWidget(bc)
        v.addLayout(h)
        self._start()

    def _start(self):
        if self._diag_worker is not None:
            try: self._diag_worker.done.disconnect()
            except Exception: pass
            self._diag_worker = None
        self.btn_rerun.setEnabled(False)
        self.btn_copy.setEnabled(False)
        self.txt.setPlainText(tr("dialogs.diagnostics.running"))
        self._diag_worker = _DiagWorker(self._rpc, self._cache)
        self._diag_worker.done.connect(self._on_done)
        self._diag_worker.finished.connect(self._diag_worker.deleteLater)
        _track(self._diag_worker).start()

    def _on_done(self, text: str):
        self.txt.setPlainText(text)
        self.btn_rerun.setEnabled(True)
        self.btn_copy.setEnabled(True)
        self._diag_worker = None

    def closeEvent(self, event):
        if self._diag_worker is not None:
            try:
                self._diag_worker.done.disconnect(self._on_done)
            except Exception:
                pass
            self._diag_worker = None
        super().closeEvent(event)

    def _copy_report(self):
        QApplication.clipboard().setText(self.txt.toPlainText())
        self.btn_copy.setText(tr("common.buttons.copied"))
        QTimer.singleShot(2000, lambda: safe_set_text(self.btn_copy, tr("dialogs.diagnostics.copy_report")))

class BusyDialog(_DraggableDialog):
    def __init__(self, parent, title: str, message: str):
        super().__init__(parent)
        self._busy = True
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18)
        v.setSpacing(12)
        hdr = QLabel(title)
        hdr.setStyleSheet("font-size:14px;font-weight:700;color:#e6edf3;")
        v.addWidget(hdr)
        self.lbl = QLabel(message)
        self.lbl.setWordWrap(True)
        self.lbl.setStyleSheet("color:#8b949e;font-size:12px;")
        v.addWidget(self.lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setFormat(tr("dialogs.busy.please_wait"))
        self.bar.setFixedHeight(20)
        v.addWidget(self.bar)
        self.btn_close = QPushButton(tr("common.buttons.close"))
        self.btn_close.setEnabled(False)
        self.btn_close.clicked.connect(self.accept)
        v.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignRight)

    def set_message(self, text: str):
        self.lbl.setText(text)

    def mark_finished(self):
        self._busy = False
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self.btn_close.setEnabled(True)

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            return
        super().closeEvent(event)


class _DiagWorker(QThread):
    done = Signal(str)

    def __init__(self, rpc: BitcoinZRPC, cache: WalletCache | None = None):
        super().__init__()
        self.rpc = rpc
        self.cache = cache

    def run(self):
        lines = []
        rpc = self.rpc
        lines.append(fmt_ts(int(time.time())))
        lines.append("")
        cfg = load_rpc_cfg()
        lines.append(tr("dialogs.diag_worker.section_config"))
        if cfg["conf_found"]:
            import re as _re
            safe_path = _re.sub(r'(?i)([\\/]Users[\\/])[^\\/]+', r'\1***', cfg['conf_path'])
            lines.append(f"  OK  {safe_path}")
            c = read_conf()
            v_pass = c.get("rpcpassword", "")
            v_user = c.get("rpcuser", "")
            v_node = c.get("addnode", "")
            missing = tr("dialogs.diag_worker.missing")
            lines.append(f"  {'OK' if v_user else '!!' }  {'rpcuser':<14}= {'***' if v_user else missing}")
            lines.append(f"  {'OK' if v_pass else '!!'  }  {'rpcpassword':<14}= {'***' if v_pass else missing}")
            lines.append(f"  {'OK' if v_node else '!!'  }  {'addnode':<14}= {v_node if v_node else missing}")
            rpcport_val = c.get("rpcport")
            lines.append(f"  OK  {'rpcport':<14}= "
                         f"{rpcport_val if rpcport_val else str(_RPC_DEFAULT_PORT) + tr('dialogs.diag_worker.default_suffix')}")
            rpcip_val = c.get("rpcallowip")
            lines.append(f"  OK  {'rpcallowip':<14}= "
                         f"{rpcip_val if rpcip_val else _RPC_DEFAULT_HOST + tr('dialogs.diag_worker.default_suffix')}")
        else:
            lines.append(tr("dialogs.diag_worker.config_not_found", path=cfg["conf_path"]))
        lines.append("")
        lines.append(tr("dialogs.diag_worker.section_database"))
        if self.cache is None:
            lines.append("  !! database cache unavailable")
        else:
            try:
                ok = self.cache.integrity_check()
                lines.append(f"  {'OK' if ok else '!!'} database integrity")
            except Exception as e:
                lines.append(f"  !! database integrity: {e}")
        lines.append(tr("dialogs.diag_worker.section_node"))
        nb = find_node()
        lines.append(f"  {'OK' if nb else '!!'} {nb.name if nb else tr('dialogs.diag_worker.node_not_found')}")
        lines.append(tr("dialogs.diag_worker.section_process"))
        lines.append(tr("dialogs.diag_worker.node_running") if node_running() else tr("dialogs.diag_worker.node_not_running"))
        lines.append(tr("dialogs.diag_worker.section_port"))
        open_ = is_port_open(rpc.host, rpc.port)
        lines.append(tr(
            "dialogs.diag_worker.port_line",
            mark="OK" if open_ else "!!",
            port=rpc.port,
            state=tr("dialogs.diag_worker.port_open") if open_ else tr("dialogs.diag_worker.port_closed"),
        ))
        lines.append(tr("dialogs.diag_worker.section_rpc"))
        for name, fn, show_data in [
            ("getnetworkinfo",    rpc.getNetworkInfo,                  True),
            ("getblockchaininfo", rpc.getBlockchainInfo,               True),
            ("listaddresses",     rpc.ListAddresses,                   False),
            ("z_listaddresses",   rpc.z_listAddresses,                 False),
            ("z_gettotalbalance", rpc.z_getTotalBalance,               False),
            ("listtransactions",  lambda: rpc.listTransactions(3, 0),  False),
        ]:
            try:
                result = fn()
                if show_data:
                    s = str(result)
                    s = s[:120] + "..." if len(s) > 120 else s
                    lines.append(tr("dialogs.diag_worker.rpc_ok_with_data", name=name, data=s))
                else:
                    lines.append(tr("dialogs.diag_worker.rpc_ok", name=name))
            except RPCError as e:
                lines.append(tr("dialogs.diag_worker.rpc_error", name=name, error=e))
        self.done.emit("\n".join(lines))


class KeyDisplayDialog(_DraggableDialog):
    def __init__(self, parent, key_type, addr, key):
        super().__init__(parent)
        self.setWindowTitle(tr("dialogs.key_display.title", key_type=key_type)); self.setMinimumWidth(620)
        self._key = key
        self._addr = addr
        v = QVBoxLayout(self); v.setContentsMargins(18, 18, 18, 18); v.setSpacing(12)

        for label_text, attr_edit, attr_btn, value, echo, reset_label in [
            (tr("dialogs.key_display.address"), "_ae", "_bc_addr", addr, QLineEdit.EchoMode.Normal, tr("common.buttons.copy")),
            (tr("dialogs.key_display.key_type", key_type=key_type), "_ke", "_bc_key", key, QLineEdit.EchoMode.Password, tr("common.buttons.copy")),
        ]:
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color:#8b949e;font-size:12px;font-weight:700;")
            v.addWidget(lbl)
            row = QHBoxLayout(); row.setSpacing(6)
            edit = QLineEdit(value); edit.setReadOnly(True); edit.setEchoMode(echo)
            setattr(self, attr_edit, edit); row.addWidget(edit)
            btn = QPushButton(tr("common.buttons.copy")); btn.setMaximumWidth(70)
            btn.clicked.connect(lambda _=False, t=value, b=btn, r=reset_label:
                                self._copy_btn(t, b, r))
            setattr(self, attr_btn, btn); row.addWidget(btn)
            v.addLayout(row)

        h = QHBoxLayout()
        bs = QPushButton(tr("common.buttons.show")); bs.setCheckable(True)
        bs.toggled.connect(lambda on: self._ke.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        h.addWidget(bs)
        self._bc_all = QPushButton(tr("dialogs.key_display.copy_all")); self._bc_all.setObjectName("primary")
        self._bc_all.clicked.connect(
            lambda: self._copy_btn(f"{self._addr} {self._key}", self._bc_all, tr("dialogs.key_display.copy_all")))
        h.addWidget(self._bc_all)
        h.addStretch()
        bc2 = QPushButton(tr("common.buttons.close")); bc2.clicked.connect(self.accept); h.addWidget(bc2)
        v.addLayout(h)

    @staticmethod
    def _copy_btn(text: str, btn: QPushButton, reset: str):
        QApplication.clipboard().setText(text)
        btn.setText(tr("common.buttons.copied"))
        QTimer.singleShot(2000, lambda: safe_set_text(btn, reset))


class ImportKeyDialog(_DraggableDialog):
    _DIALOG_WIDTH = 540
    _DIALOG_HEIGHT = 290
    _STATUS_AREA_HEIGHT = 64
    _T_WIF_PREFIXES = ("5", "K", "L", "9", "c")
    _Z_KEY_PREFIXES = (
        "secret-extended-key-main",
        "secret-extended-key-test",
        "secret-extended-key-regtest",
        "SK",
        "ST",
    )

    def __init__(self, parent, rpc: BitcoinZRPC, cache: WalletCache | None = None):
        super().__init__(parent)
        self._rpc     = rpc
        self._cache   = cache
        self._running = False
        self._worker  = None
        self._preflight_w = None
        self._log_w   = None
        self._cache_job_id = None
        self._import_started_block = None
        self._scan_start_height = 0
        self._imported_address = ""
        self.setWindowTitle(tr("dialogs.import_key.title"))
        self._build_ui()
        self.ensurePolished()
        self.setFixedSize(self._DIALOG_WIDTH, self._DIALOG_HEIGHT)
        self._set_import_state("idle")

    @staticmethod
    def _pin_height(widget):
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        return widget

    def _build_ui(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(10)

        body_frame = QFrame()
        body_frame.setStyleSheet("QFrame{border:none;background:transparent;}")
        body_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        body = QVBoxLayout(body_frame)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(10)
        v.addWidget(body_frame, 0, Qt.AlignmentFlag.AlignTop)

        title = QLabel(tr("dialogs.import_key.title"))
        title.setStyleSheet("font-size:15px;font-weight:700;")
        self._pin_height(title)
        body.addWidget(title)

        self.lbl_info = QLabel(tr("dialogs.import_key.info"))
        self.lbl_info.setStyleSheet("color:#8b949e;font-size:12px;")
        self.lbl_info.setWordWrap(True)
        self._pin_height(self.lbl_info)
        body.addWidget(self.lbl_info)

        self.e_key = QLineEdit()
        self.e_key.setPlaceholderText(tr("dialogs.import_key.placeholder"))
        self.e_key.setMinimumHeight(36)
        self.e_key.setEchoMode(QLineEdit.EchoMode.Password)
        body.addWidget(self.e_key)

        self.progress_box = QFrame()
        self.progress_box.setStyleSheet("QFrame{border:none;background:transparent;}")
        self.progress_box.setFixedHeight(self._STATUS_AREA_HEIGHT)
        self.progress_box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        progress_layout = QVBoxLayout(self.progress_box)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(6)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#8b949e;font-size:12px;")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setFixedHeight(30)
        self._pin_height(self.lbl_status)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        progress_layout.addWidget(self.lbl_status)

        self.prog_bar = QProgressBar(); self.prog_bar.setObjectName("rbar")
        self.prog_bar.setRange(0, 1000); self.prog_bar.setValue(0)
        self.prog_bar.setFormat(tr("dialogs.import_key.idle_progress")); self.prog_bar.setFixedHeight(22)
        self.prog_bar.setVisible(False)
        progress_layout.addWidget(self.prog_bar)
        v.addWidget(self.progress_box, 0, Qt.AlignmentFlag.AlignTop)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        self.btn_import = QPushButton(tr("dialogs.import_key.import_and_scan"))
        self.btn_import.setObjectName("primary"); self.btn_import.setMinimumHeight(40)
        self.btn_import.clicked.connect(self._start)
        btn_row.addWidget(self.btn_import)
        self.btn_close = QPushButton(tr("common.buttons.close")); self.btn_close.setMinimumHeight(40)
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        v.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)

    def _set_import_state(self, state: str, message: str = "", progress: float | None = None):
        running = state in {"checking", "scanning"}
        finished = state in {"success", "error"}

        self._running = running
        self.e_key.setEnabled(not running)
        self.e_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.btn_import.setEnabled(not running)
        self.btn_close.setEnabled(not running)

        self.btn_import.setObjectName("primary" if not running else "")
        self.btn_import.setStyle(self.btn_import.style())

        is_error = state == "error"
        status_color = "#f85149" if is_error else "#8b949e"
        self.lbl_status.setStyleSheet(f"color:{status_color};font-size:12px;")
        status_text = message or ""
        if state == "idle":
            status_text = tr("dialogs.wallet_import.importing_private_wait")
        self.lbl_status.setText(status_text.replace("\n", "  "))
        self.lbl_status.setToolTip(status_text)

        show_progress = True
        self.prog_bar.setVisible(show_progress)
        self.prog_bar.setTextVisible(True)
        if state == "idle":
            self.prog_bar.setRange(0, 1000)
            self.prog_bar.setValue(0)
            self.prog_bar.setFormat(tr("dialogs.import_key.idle_progress"))
        elif state == "error":
            self.prog_bar.setRange(0, 1)
            self.prog_bar.setValue(0)
            self.prog_bar.setFormat(tr("dialogs.import_key.failed"))
        elif state == "success":
            self.prog_bar.setRange(0, 1000)
            self.prog_bar.setValue(1000)
            self.prog_bar.setFormat(tr("dialogs.import_key.complete"))
        else:
            frac = 0.0 if progress is None else max(0.0, min(float(progress), 1.0))
            self.prog_bar.setRange(0, 1000)
            self.prog_bar.setValue(int(frac * 1000))
            self.prog_bar.setFormat(tr("dialogs.import_key.rescanning_progress", percent=frac * 100))

        if not finished and state == "idle":
            self.lbl_status.setToolTip(status_text)

    @classmethod
    def _classify_private_key(cls, key: str) -> str:
        key = (key or "").strip()
        if key.startswith(cls._Z_KEY_PREFIXES):
            return "z"
        if key.startswith(cls._T_WIF_PREFIXES):
            return "t"
        return ""

    def closeEvent(self, event):
        if self._running: event.ignore()
        else: event.accept()

    def _start(self):
        key = self.e_key.text().strip()
        if not key:
            _msg_warning(self, tr("dialogs.import_key.missing_title"), tr("dialogs.import_key.missing_key")); return

        key_type = self._classify_private_key(key)
        if not key_type:
            _msg_warning(
                self,
                tr("dialogs.import_key.unknown_key_title"),
                tr("dialogs.import_key.unknown_key_message"),
            )
            return
        is_z = key_type == "z"
        self._set_import_state("checking", tr("dialogs.import_key.checking_wallet"), progress=0.0)

        self._pending_import = {
            "key": key,
            "is_z": is_z,
        }
        self._preflight_w = ImportPreflightWorker(self._rpc, is_z)
        self._preflight_w.done.connect(self._on_preflight_done)
        self._preflight_w.error.connect(self._on_preflight_error)
        self._preflight_w.finished.connect(self._preflight_w.deleteLater)
        _track(self._preflight_w).start()

    def _reset_after_preflight(self):
        self._set_import_state("idle")
        self._preflight_w = None
        self._pending_import = {}

    def _on_preflight_error(self, msg: str):
        self._reset_after_preflight()
        _msg_critical(self, tr("dialogs.import_key.wallet_check_failed"), msg)

    def _on_preflight_done(self, result: dict):
        self._preflight_w = None
        if result.get("pruned"):
            self._reset_after_preflight()
            _msg_warning(
                self,
                tr("dialogs.import_key.pruned_title"),
                tr("dialogs.import_key.pruned_message")
            )
            return
        pending = getattr(self, "_pending_import", {}) or {}
        started_block = int(result.get("started_block", 0) or 0)
        key = pending.get("key", "")
        is_z = bool(pending.get("is_z"))
        self._pending_import = {}
        self._begin_import(
            key,
            is_z,
            started_block,
        )

    def _begin_import(self, key: str, is_z: bool, started_block: int):
        start_height = 0

        self._is_z = is_z
        self._imported_address = ""
        self._cache_job_id = None
        self._import_started_block = started_block
        self._scan_start_height = start_height

        self._set_import_state("scanning", tr("dialogs.import_key.starting"), progress=0.0)
        parent = self.parentWidget()
        if parent is not None and hasattr(parent, "_set_rescan_status_active"):
            try:
                parent._set_rescan_status_active(True)
            except Exception:
                pass

        if self._cache is not None:
            try:
                job_type = "import_shielded_key" if is_z else "import_transparent_key"
                self._cache_job_id = self._cache.start_sync_job(
                    job_type,
                    last_seen_block=self._import_started_block,
                )
                self._cache.set_state("last_import_type", "shielded" if is_z else "transparent")
                self._cache.set_state("last_import_start_height", start_height)
                self._cache.set_state("last_import_rescan", True)
            except Exception:
                self._cache_job_id = None

        self._log_w = LogTailWorker()
        self._log_w.progress.connect(self._on_log_progress)
        self._log_w.done.connect(self._on_log_idle)
        _track(self._log_w).start()

        self._worker = ImportKeyWorker(self._rpc, key, is_z, start_height)
        self._worker.progress.connect(self._on_msg)
        self._worker.error.connect(self._on_error)
        if is_z:
            self._worker.z_key_accepted.connect(self._on_z_accepted)
        self._worker.done.connect(self._on_done)
        _track(self._worker).start()

    def _on_msg(self, msg: str):
        if not self._running:
            return
        progress = self.prog_bar.value() / 1000 if self.prog_bar.maximum() == 1000 else 0.0
        self._set_import_state("scanning", msg.replace("\n", "  "), progress=progress)

    def _on_log_progress(self, frac: float, _block: int):
        if not self._running:
            return
        self._set_import_state("scanning", self.lbl_status.text(), progress=frac)

    def _on_log_idle(self):
        if self._running:
            progress = self.prog_bar.value() / 1000 if self.prog_bar.maximum() == 1000 else 0.0
            self._set_import_state("scanning", tr("dialogs.import_key.finalizing"), progress=progress)

    def _on_z_accepted(self, addr: str):
        self._imported_address = addr or ""
        self._cache_imported_address(addr)
        progress = self.prog_bar.value() / 1000 if self.prog_bar.maximum() == 1000 else 0.0
        message = (
            tr("dialogs.import_key.accepted_with_address", address=addr) if addr
            else tr("dialogs.import_key.accepted_without_address")
        )
        self._set_import_state("scanning", message, progress=progress)

    def _on_done(self, msg: str):
        self._cache_import_done("success")
        self._cleanup()
        self.e_key.clear()
        self._set_import_state("success", msg, progress=1.0)
        parent = self.parentWidget()
        if parent is not None and hasattr(parent, "_set_rescan_status_active"):
            try:
                parent._set_rescan_status_active(False)
            except Exception:
                pass
        if parent is not None and hasattr(parent, "refresh"):
            QTimer.singleShot(500, lambda: parent.refresh(force_full=True))

    def _on_error(self, msg: str):
        self._cache_import_done("failed", error=msg)
        self._cleanup()
        self._set_import_state("error", tr("dialogs.import_key.error", message=msg))
        parent = self.parentWidget()
        if parent is not None and hasattr(parent, "_set_rescan_status_active"):
            try:
                parent._set_rescan_status_active(False)
            except Exception:
                pass

    def _cache_imported_address(self, addr: str):
        if self._cache is None or not addr:
            return
        try:
            addr_type = "sapling" if addr.startswith("zs") else ("sprout" if addr.startswith("zc") else "shielded")
            self._cache.upsert_address(
                addr,
                addr_type=addr_type,
                imported=True,
                import_height=self._scan_start_height,
                source="import",
            )
        except Exception:
            pass

    def _cache_import_done(self, status: str, error: str | None = None):
        if self._cache is None:
            return
        try:
            if self._cache_job_id is not None:
                self._cache.finish_sync_job(
                    self._cache_job_id,
                    status=status,
                    last_error=error,
                    last_seen_block=self._import_started_block,
                )
            if status == "success":
                self._cache.set_state("last_import_status", "success")
                self._cache.set_state("last_import_address", self._imported_address)
            else:
                self._cache.set_state("last_import_status", "failed")
                self._cache.set_state("last_import_error", error or "")
        except Exception:
            pass

    def _cleanup(self):
        self._running = False
        if self._log_w:
            self._log_w.stop()
            self._log_w.finished.connect(self._log_w.deleteLater)
            self._log_w = None
        if self._worker:
            try:
                self._worker.progress.disconnect()
                self._worker.error.disconnect()
                if getattr(self, '_is_z', False):
                    self._worker.z_key_accepted.disconnect()
                self._worker.done.disconnect()
            except Exception:
                pass
            self._worker.finished.connect(self._worker.deleteLater)
            self._worker = None


class TxDetailsWorker(QThread):
    done = Signal(dict)

    def __init__(self, rpc: BitcoinZRPC, txid: str):
        super().__init__()
        self.rpc = rpc
        self.txid = txid

    def run(self):
        payload = {"full": {}, "raw": {}}
        try:
            chain = self.rpc.getBlockchainInfo() or {}
            payload["chain_height"] = int(chain.get("blocks", 0) or 0)
        except Exception:
            payload["chain_height"] = 0
        try:
            payload["full"] = self.rpc.getTransaction(self.txid) or {}
        except Exception:
            pass
        try:
            payload["raw"] = self.rpc.getRawTransaction(self.txid) or {}
        except Exception:
            pass
        try:
            payload["shielded"] = self.rpc.z_viewTransaction(self.txid) or {}
        except Exception:
            payload["shielded"] = {}
        payload["prevouts"] = self._load_prevouts(payload.get("raw") or {})
        self.done.emit(payload)

    def _load_prevouts(self, raw: dict) -> dict:
        prevouts = {}
        tx_cache = {}
        for vin in raw.get("vin", []) or []:
            prev_txid = vin.get("txid")
            prev_n = vin.get("vout")
            if not prev_txid or prev_n is None:
                continue
            key = f"{prev_txid}:{prev_n}"
            try:
                if prev_txid not in tx_cache:
                    tx_cache[prev_txid] = self.rpc.getRawTransaction(prev_txid) or {}
                prev_tx = tx_cache.get(prev_txid) or {}
                prev_vouts = prev_tx.get("vout") or []
                if 0 <= int(prev_n) < len(prev_vouts):
                    prevouts[key] = prev_vouts[int(prev_n)]
            except Exception:
                continue
        return prevouts


class TxStatusWorker(QThread):
    done = Signal(dict)

    def __init__(self, rpc: BitcoinZRPC, txid: str):
        super().__init__()
        self.rpc = rpc
        self.txid = txid

    @staticmethod
    def _status_from_confirmations(confirmations: int) -> str:
        if confirmations < 0:
            return "conflicted"
        if confirmations == 0:
            return "pending"
        return "confirmed"

    def run(self):
        payload = {"chain_height": 0, "tx": {}}
        try:
            chain = self.rpc.getBlockchainInfo() or {}
            payload["chain_height"] = int(chain.get("blocks", 0) or 0)
        except Exception:
            pass
        source = {}
        try:
            source = self.rpc.getTransaction(self.txid) or {}
        except Exception:
            try:
                source = self.rpc.getRawTransaction(self.txid) or {}
            except Exception:
                source = {}
        if isinstance(source, dict) and source:
            try:
                confirmations = int(source.get("confirmations", 0) or 0)
            except Exception:
                confirmations = 0
            tx = {
                "txid": self.txid,
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
                    tx[target_key] = value
            payload["tx"] = tx
        self.done.emit(payload)


class TxDetailDialog(_DraggableDialog):
    _T_P2PKH_PREFIX = bytes.fromhex("1cb8")
    _BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    _DEFAULT_TX_EXPIRY_DELTA = 20
    _EXPIRING_SOON_BLOCKS = 3

    def __init__(self, parent, tx: dict, rpc: BitcoinZRPC):
        super().__init__(parent)
        self.setWindowTitle(tr("dialogs.tx_details.title"))
        self._rpc = rpc
        self._tx = tx
        self._txid = tx.get("txid", "")
        self._confirms = 0
        self._payload = {"full": {}, "raw": {}}
        self._detail_worker = None
        self._status_worker = None
        self._live_refresh_failed = False
        self._dynamic_rows: dict[str, QLabel] = {}
        self._layout_signature = None
        self._build_ui()
        self._fit_to_screen(720, 650, min_w=560, min_h=420)
        self._load()
        self._request_refresh()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start(2_000)

    def _auto_refresh(self):
        if self._confirms >= 6 and tx_status_code(self._current_detail_tx()) == "confirmed":
            self._timer.stop()
            return
        if self._layout_signature is None or self._live_refresh_failed:
            self._request_refresh()
        else:
            self._request_status_refresh()

    def closeEvent(self, event):
        self._timer.stop()
        if self._detail_worker is not None:
            try:
                self._detail_worker.done.disconnect(self._on_payload_ready)
            except Exception:
                pass
            self._detail_worker = None
        if self._status_worker is not None:
            try:
                self._status_worker.done.disconnect(self._on_status_payload_ready)
            except Exception:
                pass
            self._status_worker = None
        super().closeEvent(event)

    def _request_refresh(self):
        if self._detail_worker is not None:
            return
        if self._layout_signature is None or self._live_refresh_failed:
            self._state_hint.setText(tr("dialogs.tx_details.refreshing"))
            self._state_hint.setVisible(True)
        self._detail_worker = TxDetailsWorker(self._rpc, self._txid)
        self._detail_worker.done.connect(self._on_payload_ready)
        self._detail_worker.finished.connect(self._detail_worker.deleteLater)
        _track(self._detail_worker).start()

    def _request_status_refresh(self):
        if self._status_worker is not None or self._detail_worker is not None:
            return
        self._status_worker = TxStatusWorker(self._rpc, self._txid)
        self._status_worker.done.connect(self._on_status_payload_ready)
        self._status_worker.finished.connect(self._status_worker.deleteLater)
        _track(self._status_worker).start()

    def _on_payload_ready(self, payload: dict):
        self._payload = payload or {"full": {}, "raw": {}}
        self._live_refresh_failed = not (self._payload.get("full") or self._payload.get("raw"))
        if self._live_refresh_failed:
            self._state_hint.setText(tr("dialogs.tx_details.cached_only"))
            self._state_hint.setVisible(True)
        else:
            self._state_hint.setVisible(False)
        self._detail_worker = None
        new_signature = self._make_layout_signature(self._payload)
        if self._layout_signature is not None and new_signature == self._layout_signature:
            self._update_dynamic_rows()
        else:
            self._load()

    def _merge_live_tx_update(self, update: dict) -> bool:
        changed = False
        if not update:
            return changed
        targets = [self._tx]
        full_payload = self._payload.get("full")
        if isinstance(full_payload, dict) and full_payload:
            targets.append(full_payload)
        for target in targets:
            if not isinstance(target, dict):
                continue
            for field in (
                "confirmations", "status", "blockhash", "blockheight", "blockindex",
                "time", "blocktime", "timereceived", "fee",
            ):
                if field not in update:
                    continue
                value = update.get(field)
                if value in (None, ""):
                    continue
                if field in {"confirmations", "blockheight", "blockindex", "time", "blocktime", "timereceived"}:
                    try:
                        value = int(value)
                    except Exception:
                        pass
                if target.get(field) != value:
                    target[field] = value
                    changed = True
        return changed

    def _on_status_payload_ready(self, payload: dict):
        payload = payload or {}
        self._status_worker = None
        if payload.get("chain_height"):
            self._payload["chain_height"] = payload.get("chain_height")
        update = payload.get("tx") or {}
        if not update:
            self._update_dynamic_rows()
            return
        old_signature = self._layout_signature
        self._merge_live_tx_update(update)
        new_signature = self._make_layout_signature(self._payload)
        if old_signature is not None and new_signature == old_signature:
            self._update_dynamic_rows()
        else:
            self._request_refresh()

    def _build_ui(self):
        root = QVBoxLayout(self); root.setContentsMargins(18, 16, 18, 16); root.setSpacing(10)

        title = QLabel(tr("dialogs.tx_details.title"))
        title.setStyleSheet("font-size:15px;font-weight:700;")
        root.addWidget(title)

        self._state_hint = QLabel("")
        self._state_hint.setWordWrap(True)
        self._state_hint.setStyleSheet("color:#8b949e;font-size:11px;")
        self._state_hint.setVisible(False)
        root.addWidget(self._state_hint)

        self._scroll_area = QFrame()
        self._scroll_area.setObjectName("card")
        self._content_layout = QVBoxLayout(self._scroll_area)
        self._content_layout.setContentsMargins(16, 14, 16, 14)
        self._content_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._scroll_area)
        root.addWidget(scroll, 1)

        btns = QHBoxLayout()
        self._btn_copy = QPushButton(tr("dialogs.tx_details.copy_txid")); self._btn_copy.setObjectName("primary")
        self._btn_copy.setMinimumWidth(110)
        self._btn_copy.clicked.connect(self._copy_txid)
        btns.addWidget(self._btn_copy)
        self._btn_explorer = QPushButton(tr("dialogs.tx_details.view_on_explorer"))
        self._btn_explorer.setMinimumWidth(135)
        self._btn_explorer.clicked.connect(self._open_explorer)
        btns.addWidget(self._btn_explorer)
        btns.addStretch()
        bclose = QPushButton(tr("common.buttons.close")); bclose.clicked.connect(self.accept)
        btns.addWidget(bclose)
        root.addLayout(btns)

    def _copy_txid(self):
        QApplication.clipboard().setText(self._txid)
        self._btn_copy.setText(tr("common.buttons.copied"))
        QTimer.singleShot(2000, lambda: safe_set_text(self._btn_copy, tr("dialogs.tx_details.copy_txid")))

    def _open_explorer(self):
        if self._txid:
            QDesktopServices.openUrl(QUrl(f"https://explorer.btcz.zelcore.io/tx/{self._txid}"))

    def _sep(self):
        f = QFrame()
        f.setStyleSheet("background:#30363d;")
        f.setFixedHeight(1)
        self._content_layout.addWidget(f)
        self._content_layout.addSpacing(6)

    def _row_style(self, color: str = "", mono: bool = False) -> str:
        style = "font-size:12px;"
        if color:
            style += f"color:{color};"
        if mono:
            style += "font-family:Consolas,'Courier New',monospace;"
        return style

    def _row(self, label: str, value: str, selectable: bool = False, color: str = "",
             mono: bool = False, wrap: bool = True, key: str | None = None):
        h = QHBoxLayout(); h.setSpacing(8)
        lbl = QLabel(label)
        lbl.setStyleSheet("color:#8b949e;font-size:12px;")
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lbl.setMinimumWidth(78)
        lbl.setMaximumWidth(128)
        lbl.setWordWrap(True)
        h.addWidget(lbl)
        val = QLabel(value)
        val.setWordWrap(wrap)
        val.setStyleSheet(self._row_style(color, mono))
        if selectable:
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        h.addWidget(val, 1)
        self._content_layout.addLayout(h)
        self._content_layout.addSpacing(4)
        if key:
            self._dynamic_rows[key] = val

    def _section(self, title: str):
        lbl = QLabel(title)
        lbl.setStyleSheet(
            "color:#8b949e;font-size:11px;font-weight:700;"
            "background:#21262d;border-radius:3px;padding:3px 8px;"
        )
        self._content_layout.addSpacing(6)
        self._content_layout.addWidget(lbl)
        self._content_layout.addSpacing(6)

    def _clear(self):
        self._dynamic_rows.clear()
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

    @staticmethod
    def _addr_row_parts(addr: str) -> tuple[str, str]:
        pfx = tr("dialogs.tx_details.shielded_prefix") if _is_z_addr(addr) else ""
        return pfx, addr

    @staticmethod
    def _fmt_tx_amount(value: float) -> str:
        return fmt_btcz(value)

    @classmethod
    def _b58encode(cls, payload: bytes) -> str:
        n = int.from_bytes(payload, "big")
        chars = []
        while n:
            n, rem = divmod(n, 58)
            chars.append(cls._BASE58_ALPHABET[rem])
        pad = 0
        for byte in payload:
            if byte == 0:
                pad += 1
            else:
                break
        return ("1" * pad) + ("".join(reversed(chars)) if chars else "")

    @classmethod
    def _t_addr_from_pubkey(cls, pubkey_hex: str) -> str:
        try:
            pubkey = bytes.fromhex(pubkey_hex)
        except ValueError:
            return ""
        if len(pubkey) not in (33, 65):
            return ""
        sha = hashlib.sha256(pubkey).digest()
        ripe = hashlib.new("ripemd160", sha).digest()
        body = cls._T_P2PKH_PREFIX + ripe
        checksum = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
        return cls._b58encode(body + checksum)

    @classmethod
    def _input_address_from_scriptsig(cls, vin: dict) -> str:
        script = vin.get("scriptSig") or {}
        asm = str(script.get("asm") or "")
        for token in reversed(asm.split()):
            if len(token) in (66, 130):
                addr = cls._t_addr_from_pubkey(token)
                if addr:
                    return addr
        return ""

    @staticmethod
    def _script_addresses(script: dict) -> list[str]:
        addrs = script.get("addresses") or script.get("address") or []
        if isinstance(addrs, str):
            return [addrs]
        return [str(a) for a in addrs if a]

    def _raw_outputs(self, vouts: list[dict]) -> list[dict]:
        outputs = []
        for out in vouts or []:
            addrs = self._script_addresses(out.get("scriptPubKey") or {})
            amount = float(out.get("value", 0))
            for addr in addrs:
                outputs.append({
                    "address": addr,
                    "amount": amount,
                    "n": out.get("n"),
                })
        return outputs

    def _raw_inputs(self, raw: dict, prevouts: dict) -> list[dict]:
        inputs = []
        for vin in raw.get("vin", []) or []:
            prev_txid = vin.get("txid")
            prev_n = vin.get("vout")
            if not prev_txid or prev_n is None:
                continue
            prev = prevouts.get(f"{prev_txid}:{prev_n}") or {}
            addrs = self._script_addresses(prev.get("scriptPubKey") or {})
            amount = float(prev.get("value", 0)) if prev.get("value") is not None else None
            if not addrs:
                addr = self._input_address_from_scriptsig(vin)
                addrs = [addr] if addr else []
            for addr in addrs:
                inputs.append({
                    "address": addr,
                    "amount": amount,
                    "prevout": f"{prev_txid}:{prev_n}",
                })
        return inputs

    @staticmethod
    def _sum_outputs(items: list[dict]) -> float:
        return sum(float(item.get("amount") or 0) for item in items)

    @staticmethod
    def _decode_memo_text(value) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        if all(ch in "0123456789abcdefABCDEF" for ch in text) and len(text) % 2 == 0:
            try:
                raw = bytes.fromhex(text).rstrip(b"\x00")
                if not raw:
                    return ""
                if raw[0] >= 0xF5:
                    return ""
                return raw.decode("utf-8", errors="replace").strip()
            except Exception:
                return text
        return text

    @classmethod
    def _memo_values_from_object(cls, obj) -> list[str]:
        found: list[str] = []
        if isinstance(obj, dict):
            for key in ("memoStr", "memo_str", "memo", "memoHex", "memohex"):
                if key in obj:
                    memo = cls._decode_memo_text(obj.get(key))
                    if memo:
                        found.append(memo)
            for value in obj.values():
                found.extend(cls._memo_values_from_object(value))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(cls._memo_values_from_object(item))
        return found

    @classmethod
    def _first_memo(cls, *objects) -> str:
        seen = set()
        for obj in objects:
            for memo in cls._memo_values_from_object(obj):
                if memo not in seen:
                    return memo
                seen.add(memo)
        return ""

    @staticmethod
    def _shielded_output_value(raw: dict) -> float:
        try:
            value_balance = float(raw.get("valueBalance", 0) or 0)
        except (TypeError, ValueError):
            value_balance = 0.0
        return abs(value_balance) if value_balance < 0 else 0.0

    @staticmethod
    def _extract_fee(tx: dict, details: list[dict]) -> float:
        candidates = [tx.get("fee")]
        candidates.extend(detail.get("fee") for detail in details or [])
        for value in candidates:
            if value is None or value == "":
                continue
            try:
                return abs(float(value))
            except (TypeError, ValueError):
                continue
        return 0.0

    def _pending_expiry_summary(self, expiryheight, chain_height: int, confirms: int) -> tuple[str, str] | None:
        if confirms != 0 or expiryheight in (None, "", 0) or not chain_height:
            return None
        try:
            expiry = int(expiryheight)
            height = int(chain_height)
        except (TypeError, ValueError):
            return None
        blocks_left = max(0, expiry - height)
        skipped = max(0, self._DEFAULT_TX_EXPIRY_DELTA - blocks_left)
        if blocks_left <= 0:
            text = tr("dialogs.tx_details.pending_expired_wait", skipped=skipped)
            return text, "#f85149"
        color = "#f85149" if blocks_left <= self._EXPIRING_SOON_BLOCKS else "#f7a32c"
        text = tr("dialogs.tx_details.pending_wait_summary", skipped=skipped, left=blocks_left)
        return text, color

    def _current_detail_tx(self) -> dict:
        full = (self._payload or {}).get("full") or {}
        return full or self._tx

    def _status_parts(self, tx: dict) -> tuple[int, str, str]:
        if not tx.get("status") and self._tx.get("status"):
            tx = {**tx, "status": self._tx.get("status")}
        confirms = int(tx.get("confirmations", 0) or 0)
        status = tx_status_code(tx)
        if status == "expired":
            return confirms, tr("dialogs.models.status_expired"), "#f85149"
        if status == "failed":
            return confirms, tr("dialogs.models.status_failed"), "#f85149"
        if status == "conflicted":
            return confirms, tr("dialogs.models.status_conflicted"), "#f85149"
        if status == "reorged":
            return confirms, tr("dialogs.models.status_reorged"), "#f7a32c"
        if status == "stale":
            return confirms, tr("dialogs.models.status_stale"), "#f7a32c"
        if status == "pending":
            return confirms, tr("dialogs.tx_details.status_pending"), "#f7a32c"
        if status == "confirming":
            return confirms, tr("dialogs.tx_details.status_confirming", confirms=confirms), "#58a6ff"
        return confirms, tr("dialogs.tx_details.status_confirmed", confirms=confirms), "#238636"

    def _make_layout_signature(self, payload: dict | None = None) -> tuple:
        payload = payload or self._payload or {}
        tx = (payload.get("full") or {}) or self._tx
        raw = payload.get("raw") or {}
        vouts = raw.get("vout") or []
        details = tx.get("details") or self._tx.get("_entries") or []
        return (
            bool(tx.get("blockhash")),
            bool(raw),
            len(raw.get("vin") or []),
            len(vouts),
            len(raw.get("vShieldedSpend") or []),
            len(raw.get("vShieldedOutput") or []),
            len(details),
            bool(self._first_memo(details, self._tx.get("_entries") or [], payload.get("shielded") or {})),
        )

    def _set_dynamic_row(self, key: str, text: str, color: str = "", mono: bool = False):
        row = self._dynamic_rows.get(key)
        if row is None:
            return
        row.setText(text)
        row.setStyleSheet(self._row_style(color, mono))

    def _update_dynamic_rows(self):
        payload = self._payload or {}
        tx = self._current_detail_tx()
        raw = payload.get("raw") or {}
        chain_height = int(payload.get("chain_height", 0) or 0)
        confirms, status_txt, status_color = self._status_parts(tx)
        self._confirms = confirms
        self._set_dynamic_row("status", status_txt, status_color)

        ts_block = tx.get("blocktime")
        ts_received = tx.get("timereceived") or tx.get("time")
        if ts_block:
            self._set_dynamic_row("date", fmt_ts(ts_block))
        elif ts_received:
            self._set_dynamic_row("date", fmt_ts(ts_received))

        blockheight = tx.get("blockheight") or self._tx.get("blockheight")
        if blockheight:
            self._set_dynamic_row("blockheight", f"{int(blockheight):,}")
        blocktime = tx.get("blocktime")
        if blocktime:
            self._set_dynamic_row("blocktime", fmt_ts(blocktime))
        blockhash = tx.get("blockhash", "")
        if blockhash:
            self._set_dynamic_row("blockhash", blockhash, mono=True)
        else:
            self._set_dynamic_row("blockstate", tr("dialogs.tx_details.mempool_unconfirmed"), "#f7a32c")

        expiryheight = raw.get("expiryheight")
        if expiryheight not in (None, "", 0):
            self._set_dynamic_row("expiryheight", f"{int(expiryheight):,}")
            pending_summary = self._pending_expiry_summary(expiryheight, chain_height, confirms)
            if pending_summary:
                text, color = pending_summary
                self._set_dynamic_row("pendingwait", text, color)

    def _fill_missing_input_amounts(self, inputs: list[dict], outputs: list[dict], fee: float,
                                    shielded_output_value: float = 0.0) -> None:
        missing = [item for item in inputs if item.get("amount") is None]
        if len(inputs) != 1 or len(missing) != 1:
            return
        missing[0]["amount"] = self._sum_outputs(outputs) + float(shielded_output_value or 0) + float(fee or 0)

    def _load(self):
        self._clear()
        payload = self._payload or {}
        tx = self._tx
        txid = self._txid
        cached_blockheight = self._tx.get("blockheight")
        cached_entries = self._tx.get("_entries") or []

        self._row(tr("dialogs.tx_details.row_txid"), txid, selectable=True, mono=True)
        self._sep()

        full = payload.get("full") or {}
        if full:
            tx = full

        confirms, status_txt, sc = self._status_parts(tx)
        self._confirms = confirms
        self._row(tr("dialogs.tx_details.row_status"), status_txt, color=sc, key="status")

        ts_block = tx.get("blocktime")
        ts_received = tx.get("timereceived") or tx.get("time")
        if ts_block:
            self._row(tr("dialogs.tx_details.row_date"), fmt_ts(ts_block), key="date")
        elif ts_received:
            self._row(tr("dialogs.tx_details.row_date"), fmt_ts(ts_received), key="date")

        details = full.get("details") or []
        if cached_entries:
            seen_details = {
                (
                    str(d.get("category", "") or ""),
                    str(d.get("address", "") or ""),
                    str(d.get("amount", "") or ""),
                )
                for d in details
            }
            merged = list(details)
            for entry in cached_entries:
                key = (
                    str(entry.get("category", "") or ""),
                    str(entry.get("address", "") or ""),
                    str(entry.get("amount", "") or ""),
                )
                if key not in seen_details:
                    merged.append(entry)
                    seen_details.add(key)
            details = merged

        cat = self._tx.get("category", "")
        if not cat and details:
            cat = details[0].get("category", "")
        self._row(tr("dialogs.tx_details.row_type"), cat.capitalize() if cat else tr("dialogs.tx_details.type_unknown"))

        amt = float(self._tx.get("amount", tx.get("amount", 0)))
        sign = "+" if amt >= 0 else ""
        self._row(
            tr("dialogs.tx_details.row_amount"),
            f"{sign}{self._fmt_tx_amount(amt)} BTCZ",
            color="#238636" if amt >= 0 else "#f85149",
        )

        fee = tx.get("fee")
        if fee is not None and fee != 0:
            self._row(tr("dialogs.tx_details.row_fee"), f"{self._fmt_tx_amount(abs(float(fee)))} BTCZ")
        memo_value = self._first_memo(details, self._tx.get("_entries") or [], payload.get("shielded") or {})
        if memo_value:
            self._row(tr("dialogs.tx_details.row_memo"), memo_value, selectable=True, wrap=True)

        addr = self._tx.get("address", "")
        raw = payload.get("raw") or {}
        shielded = payload.get("shielded") or {}
        prevouts = payload.get("prevouts") or {}
        chain_height = int(payload.get("chain_height", 0) or 0)
        vouts = raw.get("vout", [])
        size = raw.get("size", tr("dialogs.tx_details.value_missing"))
        locktime = raw.get("locktime")
        has_shielded_in = bool(raw.get("vJoinSplit") or raw.get("vShieldedSpend"))
        has_shielded_out = bool(raw.get("vJoinSplit") or raw.get("vShieldedOutput"))
        raw_inputs = self._raw_inputs(raw, prevouts)
        raw_outputs = self._raw_outputs(vouts)
        detected_fee = self._extract_fee(tx, details)
        shielded_output_value = self._shielded_output_value(raw)
        self._fill_missing_input_amounts(raw_inputs, raw_outputs, detected_fee, shielded_output_value)
        input_addrs = {item["address"] for item in raw_inputs if item.get("address")}
        change_outputs = [
            item for item in raw_outputs
            if item.get("address") in input_addrs and input_addrs
        ]
        payment_outputs = [
            item for item in raw_outputs
            if item.get("address") not in input_addrs or not input_addrs
        ]

        sends = [d for d in details if d.get("category") == "send"]
        receives = [d for d in details if d.get("category") == "receive"]
        shielded_receives = [
            d for d in receives
            if _is_z_addr(str(d.get("address", "") or "")) or (not d.get("address") and has_shielded_out)
        ]
        if not shielded_receives and isinstance(shielded, dict):
            for out in shielded.get("outputs", []) or shielded.get("vShieldedOutput", []) or []:
                out_addr = str(out.get("address") or out.get("recipient") or "").strip()
                try:
                    out_amount = float(out.get("value") if out.get("value") is not None else out.get("amount"))
                except (TypeError, ValueError):
                    out_amount = 0.0
                if out_addr or out_amount:
                    shielded_receives.append({"address": out_addr, "amount": out_amount})

        self._sep()
        self._section(tr("dialogs.tx_details.section_from"))
        if raw_inputs:
            for item in raw_inputs:
                pfx, disp = self._addr_row_parts(item["address"])
                amount = item.get("amount")
                suffix = f"   {self._fmt_tx_amount(amount)} BTCZ" if amount is not None else ""
                self._row(
                    f"{pfx}{tr('dialogs.tx_details.row_from')}",
                    f"{disp}{suffix}",
                    selectable=True,
                    mono=True,
                    color="#f85149" if amount is not None else "#8b949e",
                )
        elif sends:
            for d in sends:
                d_addr = d.get("address", "")
                d_amt = abs(float(d.get("amount", 0)))
                if d_addr:
                    pfx, disp = self._addr_row_parts(d_addr)
                    self._row(
                        f"{pfx}{tr('dialogs.tx_details.row_from')}",
                        f"{disp}   -{self._fmt_tx_amount(d_amt)} BTCZ",
                        selectable=True,
                        mono=True,
                        color="#f85149",
                    )
                else:
                    self._row(
                        f"{tr('dialogs.tx_details.shielded_prefix')}{tr('dialogs.tx_details.row_from')}",
                        tr("dialogs.tx_details.shielded_sender_hidden_with_amount", amount=self._fmt_tx_amount(d_amt)),
                        color="#f85149",
                    )
        elif has_shielded_in:
            self._row(
                f"{tr('dialogs.tx_details.shielded_prefix')}{tr('dialogs.tx_details.row_from')}",
                tr("dialogs.tx_details.shielded_sender_summary"),
                color="#8b949e",
            )
        elif addr:
            pfx, disp = self._addr_row_parts(addr)
            self._row(f"{pfx}{tr('dialogs.tx_details.row_from')}", disp, selectable=True, mono=True, color="#8b949e")
        else:
            self._row(tr("dialogs.tx_details.row_from"), tr("dialogs.tx_details.value_missing"), color="#8b949e")

        self._section(tr("dialogs.tx_details.section_to"))
        if shielded_receives or payment_outputs:
            for d in shielded_receives:
                d_addr = str(d.get("address", "") or "")
                try:
                    d_amt = float(d.get("amount", 0) or 0)
                except (TypeError, ValueError):
                    d_amt = 0.0
                label = f"{tr('dialogs.tx_details.shielded_prefix')}{tr('dialogs.tx_details.row_to')}"
                if d_addr:
                    _, disp = self._addr_row_parts(d_addr)
                    value = f"{disp}   {self._fmt_tx_amount(d_amt)} BTCZ" if d_amt else disp
                    self._row(label, value, selectable=True, mono=True, color="#238636")
                else:
                    amount = d_amt or shielded_output_value
                    value = (
                        tr("dialogs.tx_details.shielded_recipient_hidden_with_amount", amount=self._fmt_tx_amount(amount))
                        if amount else tr("dialogs.tx_details.shielded_recipient_summary")
                    )
                    self._row(label, value, color="#238636")
            for item in payment_outputs:
                pfx, disp = self._addr_row_parts(item["address"])
                self._row(
                    f"{pfx}{tr('dialogs.tx_details.row_to')}",
                    f"{disp}   {self._fmt_tx_amount(item['amount'])} BTCZ",
                    selectable=True,
                    mono=True,
                    color="#238636",
                )
        elif receives:
            for d in receives:
                d_addr = d.get("address", "")
                d_amt = float(d.get("amount", 0))
                if d_addr:
                    pfx, disp = self._addr_row_parts(d_addr)
                    self._row(
                        f"{pfx}{tr('dialogs.tx_details.row_to')}",
                        f"{disp}   +{self._fmt_tx_amount(d_amt)} BTCZ",
                        selectable=True,
                        mono=True,
                        color="#238636",
                    )
                else:
                    self._row(
                        f"{tr('dialogs.tx_details.shielded_prefix')}{tr('dialogs.tx_details.row_to')}",
                        tr("dialogs.tx_details.shielded_recipient_hidden_with_amount", amount=self._fmt_tx_amount(d_amt)),
                        color="#238636",
                    )
        elif has_shielded_out:
            self._row(
                f"{tr('dialogs.tx_details.shielded_prefix')}{tr('dialogs.tx_details.row_to')}",
                tr("dialogs.tx_details.shielded_recipient_hidden_with_amount", amount=self._fmt_tx_amount(shielded_output_value))
                if shielded_output_value else tr("dialogs.tx_details.shielded_recipient_summary"),
                color="#238636",
            )
        elif addr:
            pfx, disp = self._addr_row_parts(addr)
            self._row(f"{pfx}{tr('dialogs.tx_details.row_to')}", disp, selectable=True, mono=True, color="#238636")
        else:
            self._row(tr("dialogs.tx_details.row_to"), tr("dialogs.tx_details.value_missing"), color="#8b949e")

        blockhash = tx.get("blockhash", "")
        if blockhash:
            self._sep()
            blockheight = tx.get("blockheight") or cached_blockheight
            if blockheight:
                self._row(tr("dialogs.tx_details.row_block_height"), f"{int(blockheight):,}", key="blockheight")
            elif tx.get("blockindex") not in (None, ""):
                self._row(tr("dialogs.tx_details.row_block_index"), f"{int(tx.get('blockindex')):,}")
            blocktime = tx.get("blocktime")
            if blocktime:
                self._row(tr("dialogs.tx_details.row_block_time"), fmt_ts(blocktime), key="blocktime")
            self._row(tr("dialogs.tx_details.row_block_hash"), blockhash, selectable=True, mono=True, key="blockhash")
        else:
            self._sep()
            self._row(tr("dialogs.tx_details.row_block_state"), tr("dialogs.tx_details.mempool_unconfirmed"), color="#f7a32c", key="blockstate")

        if raw:
            self._sep()
            self._row(
                tr("dialogs.tx_details.row_size"),
                tr("dialogs.tx_details.size_bytes", size=size) if isinstance(size, int) else str(size),
            )
            if locktime is not None:
                self._row(
                    tr("dialogs.tx_details.row_locktime"),
                    tr("dialogs.tx_details.locktime_height", value=locktime) if locktime < 500_000_000 else fmt_ts(locktime),
                )
            expiryheight = raw.get("expiryheight")
            if expiryheight not in (None, "", 0):
                self._row(tr("dialogs.tx_details.row_expiry_height"), f"{int(expiryheight):,}", key="expiryheight")
                pending_summary = self._pending_expiry_summary(expiryheight, chain_height, confirms)
                if pending_summary:
                    text, color = pending_summary
                    self._row(tr("dialogs.tx_details.row_pending_wait"), text, color=color, key="pendingwait")
            shielded_spends_count = len(raw.get("vShieldedSpend") or [])
            shielded_outputs_count = len(raw.get("vShieldedOutput") or [])
            if shielded_spends_count:
                self._row(tr("dialogs.tx_details.row_shielded_spends"), str(shielded_spends_count))
            self._row(tr("dialogs.tx_details.row_transparent_outputs"), str(len(vouts or [])))
            if shielded_outputs_count:
                self._row(tr("dialogs.tx_details.row_shielded_outputs"), str(shielded_outputs_count))
            value_balance = raw.get("valueBalance")
            if value_balance not in (None, "", 0, 0.0, "0", "0.00000000"):
                try:
                    value_balance_text = f"{self._fmt_tx_amount(float(value_balance))} BTCZ"
                except (TypeError, ValueError):
                    value_balance_text = str(value_balance)
                self._row(tr("dialogs.tx_details.row_value_balance"), value_balance_text)

            if vouts or raw.get("vShieldedOutput"):
                self._sep()
                self._section(tr("dialogs.tx_details.section_outputs", count=len(vouts) + len(raw.get("vShieldedOutput") or [])))
                change_keys = {(item.get("address"), item.get("n")) for item in change_outputs}
                payment_keys = {(item.get("address"), item.get("n")) for item in payment_outputs}
                for item in raw_outputs:
                    a = item["address"]
                    pfx, _ = self._addr_row_parts(a)
                    key = (a, item.get("n"))
                    if key in change_keys:
                        role = tr("dialogs.tx_details.output_change")
                        color = "#8b949e"
                    elif key in payment_keys:
                        role = tr("dialogs.tx_details.output_recipient")
                        color = "#e6edf3"
                    else:
                        role = tr("dialogs.tx_details.output_unknown")
                        color = "#e6edf3"
                    self._row(
                        f"{pfx}{tr('dialogs.tx_details.output_arrow')}",
                        f"{a}   {self._fmt_tx_amount(item['amount'])} BTCZ   {role}",
                        selectable=True,
                        mono=True,
                        color=color,
                    )
                shielded_count = len(raw.get("vShieldedOutput") or [])
                if shielded_count:
                    amount = shielded_output_value
                    label = f"{tr('dialogs.tx_details.shielded_prefix')}{tr('dialogs.tx_details.output_arrow')}"
                    if shielded_receives and shielded_receives[0].get("address"):
                        addr = str(shielded_receives[0].get("address"))
                        value = f"{addr}   {self._fmt_tx_amount(amount)} BTCZ   {tr('dialogs.tx_details.output_recipient')}"
                        self._row(label, value, selectable=True, mono=True, color="#238636")
                    else:
                        self._row(
                            label,
                            tr("dialogs.tx_details.shielded_output_summary", count=shielded_count, amount=self._fmt_tx_amount(amount)),
                            color="#238636",
                        )

        self._content_layout.addStretch()
        self._layout_signature = self._make_layout_signature(payload)


class AboutDialog(_DraggableDialog):
    def __init__(self, parent, rpc: BitcoinZRPC):
        super().__init__(parent)
        self.setWindowTitle(tr("dialogs.about.title"))
        self.setMinimumWidth(520)
        self._rpc = rpc
        self._build_ui()
        self._fit_to_screen(560, 620, min_w=480, min_h=420)
        self._ver_worker = _NodeVersionWorker(rpc)
        self._ver_worker.done.connect(self._on_version)
        self._ver_worker.finished.connect(self._ver_worker.deleteLater)
        _track(self._ver_worker).start()

    @staticmethod
    def _div():
        f = QFrame(); f.setStyleSheet("background:#30363d;"); f.setFixedHeight(1)
        return f

    @staticmethod
    def _link(url: str, label: str) -> str:
        return f'<a href="{url}" style="color:#58a6ff;text-decoration:none;">{label}</a>'

    def _build_links_html(self) -> str:
        return (
            self._link("https://github.com/zalpader/ZSend_Wallet", tr("dialogs.about.link_wallet"))
            + "&nbsp;&nbsp;|&nbsp;&nbsp;"
            + self._link("https://getbtcz.com", tr("dialogs.about.link_site"))
        )

    def _build_licenses_html(self) -> str:
        bullet = "&bull;"
        lines = [f"<b>{tr('dialogs.about.libraries_heading')}</b>"]
        lines.append(
            f"{bullet} <b>{tr('dialogs.about.lib_python_name')}</b> - "
            + self._link("https://docs.python.org/3/license.html", tr("dialogs.about.lib_python_license"))
        )
        lines.append(
            f"{bullet} <b>{tr('dialogs.about.lib_pyside_name')}</b> - "
            + self._link("https://doc.qt.io/qtforpython-6/", tr("dialogs.about.lib_pyside_site"))
            + f" ({self._link('https://www.gnu.org/licenses/lgpl-3.0.en.html', tr('dialogs.about.lib_pyside_license'))})"
        )
        lines.append(
            f"{bullet} <b>{tr('dialogs.about.lib_requests_name')}</b> - "
            + self._link("https://github.com/psf/requests/blob/main/LICENSE", tr("dialogs.about.lib_requests_license"))
        )
        lines.append(
            f"{bullet} <b>{tr('dialogs.about.lib_qrcode_name')}</b> - "
            + self._link("https://github.com/lincolnloop/python-qrcode/blob/main/LICENSE", tr("dialogs.about.lib_qrcode_license"))
        )
        lines.append(
            f"{bullet} <b>{tr('dialogs.about.lib_pillow_name')}</b> - "
            + self._link("https://github.com/python-pillow/Pillow/blob/main/LICENSE", tr("dialogs.about.lib_pillow_license"))
        )
        lines.append(
            f"{bullet} <b>{tr('dialogs.about.lib_pyinstaller_name')}</b> - "
            + self._link("https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt", tr("dialogs.about.lib_pyinstaller_license"))
        )
        lines.append(
            f"{bullet} <b>{tr('dialogs.about.lib_core_name')}</b> - "
            + self._link("https://github.com/btcz/bitcoinz/blob/master/COPYING", tr("dialogs.about.lib_core_license"))
            + " ("
            + self._link("https://github.com/btcz/bitcoinz", tr("dialogs.about.lib_core_source"))
            + ")"
        )
        return "<br>".join(lines)

    def _build_ui(self):
        v = QVBoxLayout(self); v.setContentsMargins(24, 24, 24, 24); v.setSpacing(14)

        title = QLabel(tr("dialogs.about.heading"))
        title.setStyleSheet("font-size:22px;font-weight:900;color:#f7a32c;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter); v.addWidget(title)

        ver_lbl = QLabel(tr("dialogs.about.version_author", version=wallet_version))
        ver_lbl.setStyleSheet("color:#8b949e;font-size:12px;")
        ver_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter); v.addWidget(ver_lbl)

        v.addWidget(self._div())

        desc = QLabel(tr("dialogs.about.description"))
        desc.setStyleSheet("color:#c9d1d9;font-size:12px;line-height:160%;")
        desc.setWordWrap(True); desc.setAlignment(Qt.AlignmentFlag.AlignCenter); v.addWidget(desc)

        v.addWidget(self._div())

        links = QLabel(self._build_links_html())
        links.setStyleSheet("font-size:12px;")
        links.setAlignment(Qt.AlignmentFlag.AlignCenter)
        links.setOpenExternalLinks(True)
        v.addWidget(links)

        v.addWidget(self._div())

        self.lbl_node = QLabel(tr("dialogs.about.node_fetching"))
        self.lbl_node.setStyleSheet("color:#e6edf3;font-size:12px;")
        self.lbl_node.setAlignment(Qt.AlignmentFlag.AlignCenter); v.addWidget(self.lbl_node)

        v.addWidget(self._div())

        lic = QLabel(self._build_licenses_html())
        lic.setStyleSheet("color:#8b949e;font-size:11px;line-height:160%;")
        lic.setWordWrap(True)
        lic.setOpenExternalLinks(True)
        lic_scroll = QScrollArea()
        lic_scroll.setWidgetResizable(True)
        lic_scroll.setFrameShape(QFrame.Shape.NoFrame)
        lic_scroll.setMaximumHeight(190)
        lic_scroll.setWidget(lic)
        v.addWidget(lic_scroll)

        h = QHBoxLayout()
        bt = QPushButton(tr("dialogs.about.tip_developer"))
        bt.setObjectName("primary")
        bt.clicked.connect(self._tip_developer)
        h.addWidget(bt)
        h.addStretch()
        bc = QPushButton(tr("common.buttons.close"))
        bc.clicked.connect(self.accept); h.addWidget(bc)
        v.addLayout(h)

    def _tip_developer(self):
        QApplication.clipboard().setText(DEVELOPER_TIP_ADDRESS)
        parent = self.parent()
        if parent is not None and hasattr(parent, "_open_tip_for_developer"):
            try:
                parent._open_tip_for_developer()
            except Exception:
                pass
        self.accept()

    def _on_version(self, info: dict):
        version = info.get("version", "")
        build = info.get("build", "")
        subver = info.get("subversion", "")
        if version or build:
            parts = []
            if build:
                parts.append(tr("dialogs.about.build_part", value=build))
            if version:
                parts.append(tr("dialogs.about.protocol_part", value=version))
            if subver:
                parts.append(tr("dialogs.about.agent_part", value=subver.strip('/')))
            self.lbl_node.setText(tr("dialogs.about.node_summary", parts="  |  ".join(parts)))
        else:
            self.lbl_node.setText(tr("dialogs.about.node_not_connected"))
        self._ver_worker = None

    def closeEvent(self, event):
        if self._ver_worker is not None:
            try:
                self._ver_worker.done.disconnect()
            except Exception:
                pass
            self._ver_worker = None
        super().closeEvent(event)


class _NodeVersionWorker(QThread):
    done = Signal(object)

    def __init__(self, rpc: BitcoinZRPC):
        super().__init__(); self.rpc = rpc

    def run(self):
        try:
            info = self.rpc.getNetworkInfo()
            self.done.emit({
                "version":    str(info.get("version", "")),
                "build":      "",
                "subversion": str(info.get("subversion", "")),
            })
        except Exception:
            self.done.emit({})
