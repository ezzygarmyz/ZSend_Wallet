from __future__ import annotations

import json
import time
from secrets import token_hex
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QLocale, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .common import (
    CONF_PATH,
    DATA_DIR,
    DEVELOPER_TIP_ADDRESS,
    _RUNNING_WORKERS,
    _is_z_addr,
    _track,
    ensure_exportdir,
    is_port_open,
    tx_fingerprint,
)
from .rpc import BitcoinZRPC, RPCError
from .wallet_cache import WalletCache, btcz_to_zat, zat_to_float
from .wallet_export import _sanitize_dump_basename, FullWalletExportWorker
from .wallet_import import FullWalletImportWorker, read_recent_wallet_rescan_state
from .workers import NewAddressWorker, PollWorker, RefreshWorker, SendWorker, ShutdownWorker, StatusWorker
from .dialogs import AboutDialog, BusyDialog, ConfigDialog, DiagDialog, ImportKeyDialog, KeyDisplayDialog, TxDetailDialog, _DraggableDialog, _ask_yes_no, _get_open_file_name, _get_save_file_name, _msg, _msg_critical, _msg_info
from .helpers import _fmt_addr, _sort_addr_items, fmt_btcz, fmt_ts, tx_status_code
from .locales import tr
from .models import AddressTableModel, TransactionTableModel, _AddrBalanceDelegate, _FromCombo, mk_view
from .ui import _CenteredTabWidget, mk_card, slbl
from . import address_actions, send_flow, shutdown_flow

MIN_NODE_FEE_BTCZ = 0.00001
MAX_NODE_FEE_BTCZ = 0.1
MAX_BTCZ_MONEY = 21_000_000_000


class BtcZAmountSpinBox(QDoubleSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLocale(QLocale.c())

    def textFromValue(self, value: float) -> str:
        return fmt_btcz(value)

    def valueFromText(self, text: str) -> float:
        raw = str(text or "").replace("BTCZ", "").replace(",", ".").strip()
        if not raw:
            return 0.0
        try:
            return float(raw.split()[0])
        except (TypeError, ValueError, IndexError):
            return 0.0


class MainWindow(QMainWindow):

    raise_window = Signal()

    def __init__(self, rpc: BitcoinZRPC, cache: WalletCache | None = None):
        super().__init__()
        self.setWindowTitle(tr("dialogs.ui.app_title"))
        self.setMinimumSize(640, 560)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(760, max(640, available.width() - 80)),
                min(780, max(560, available.height() - 80)),
            )
        else:
            self.resize(760, 760)
        self.rpc = rpc
        self.cache = cache
        self._data: dict            = {}
        self._cur_blocks: int       = 0
        self._cached_txs: list      = []
        self._addr_balances: dict   = {}
        self._max_mode: bool        = False
        self._refresh_running: bool = False
        self._status_refresh_running: bool = False
        self._status_rpc_failures: int = 0
        self._had_balance: bool     = False
        self._tx_cache_key: str     = ""
        self._threads: list         = []
        self._pending_send: dict | None = None
        self._active_opid: str      = ""
        self._t_sort_mode: str      = 'balance_desc'
        self._z_sort_mode: str      = 'balance_desc'
        self._status_visual_state: str = ""
        self._sync_visual_state: str = ""
        self._last_sync_ts: int | None = None
        self._wallet_synced: bool = False
        self._busy_addresses: set[str] = set()
        self._rescan_status_active: bool = False
        self._full_export_worker = None
        self._full_import_worker = None
        self._busy_dialog = None
        self._temp_wallet_dump_path: Path | None = None
        self._t_model = AddressTableModel(tr("dialogs.models.address"))
        self._z_model = AddressTableModel(tr("dialogs.main_window.tab_z"))
        self._tx_model = TransactionTableModel()

        self.raise_window.connect(self._bring_to_front)
        self._build_menu(); self._build_ui(); self._build_sb()
        self._load_cached_snapshot()

        self._timer = QTimer(self); self._timer.timeout.connect(self.refresh)
        self._timer.start(30_000)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self.refresh_status)
        self._status_timer.start(2_000)
        self._reconcile_timer = QTimer(self)
        self._reconcile_timer.timeout.connect(lambda: self.refresh(force_full=True))
        self._reconcile_timer.start(300_000)
        self.refresh()
        self.refresh_status()

    def _bring_to_front(self):
        self.setWindowState(Qt.WindowState.WindowActive)
        self.show(); self.raise_(); self.activateWindow()

    def closeEvent(self, event):
        self._timer.stop()
        if hasattr(self, "_status_timer"):
            self._status_timer.stop()
        if hasattr(self, "_reconcile_timer"):
            self._reconcile_timer.stop()
        for w in list(_RUNNING_WORKERS):
            if hasattr(w, 'stop'):
                try:
                    w.stop()
                except Exception:
                    pass
        for w in list(_RUNNING_WORKERS):
            try:
                w.wait(1500)
            except Exception:
                pass
        super().closeEvent(event)

    def _build_menu(self):
        mb = self.menuBar()
        wm = mb.addMenu(tr("dialogs.main_window.wallet_menu"))
        self._act(wm, tr("dialogs.main_window.refresh"),           self._manual_refresh, "F5")
        wm.addSeparator()
        self._act_import = self._act(wm, tr("dialogs.main_window.import_private_key"), self._import_key)
        self._act_import.setEnabled(False)
        wm.addSeparator()
        self._act_export_all_keys = self._act(wm, tr("dialogs.main_window.export_full_wallet_keys"), self._export_full_wallet_keys)
        self._act_import_keys_file = self._act(wm, tr("dialogs.main_window.import_wallet_keys_file"), self._import_wallet_keys_file)
        self._update_wallet_key_actions()
        wm.addSeparator()
        self._act(wm, tr("dialogs.main_window.stop_node_exit"),  self._quit_and_stop)
        self._act(wm, tr("dialogs.main_window.exit"),               self.close)
        sm = mb.addMenu(tr("dialogs.main_window.settings_menu"))
        self._act(sm, tr("dialogs.main_window.rpc_connection"), self._open_cfg)
        hm = mb.addMenu(tr("dialogs.main_window.help_menu"))
        self._act(hm, tr("dialogs.main_window.full_diagnostics"), self._open_diag)
        hm.addSeparator()
        self._act(hm, tr("dialogs.main_window.about"), self._open_about)

    def _act(self, menu, text, slot, sc=None):
        a = QAction(text, self); a.triggered.connect(slot)
        if sc: a.setShortcut(sc)
        menu.addAction(a)
        return a

    def _wallet_key_actions_enabled(self) -> bool:
        return (
            bool(getattr(self, "_wallet_synced", False))
            and not self._refresh_running
            and not self._rescan_status_active
            and self._full_export_worker is None
            and self._full_import_worker is None
        )

    def _update_wallet_key_actions(self):
        enabled = self._wallet_key_actions_enabled()
        for name in ("_act_import", "_act_export_all_keys", "_act_import_keys_file"):
            action = getattr(self, name, None)
            if action is not None:
                action.setEnabled(enabled)

    def _build_ui(self):
        cw = QWidget(); self.setCentralWidget(cw)
        root = QVBoxLayout(cw); root.setContentsMargins(16, 12, 16, 12); root.setSpacing(10)

        cr = QHBoxLayout(); cr.setSpacing(12)
        self.lbl_transp = QLabel("-")
        self.lbl_transp.setStyleSheet("color:#1f6feb;font-size:19px;font-weight:700;")
        self.lbl_priv   = QLabel("-")
        self.lbl_priv.setStyleSheet("color:#238636;font-size:19px;font-weight:700;")
        self.lbl_total  = QLabel("-")
        self.lbl_total.setStyleSheet("color:#f7a32c;font-size:19px;font-weight:700;")
        self.card_transp = mk_card(tr("dialogs.main_window.card_transparent"), self.lbl_transp)
        self.card_priv = mk_card(tr("dialogs.main_window.card_shielded"), self.lbl_priv)
        self.card_total = mk_card(tr("dialogs.main_window.card_total"), self.lbl_total)
        cr.addWidget(self.card_transp)
        cr.addWidget(self.card_priv)
        cr.addWidget(self.card_total)
        root.addLayout(cr)

        self.tabs = _CenteredTabWidget()
        self.tabs.addTab(self._tab_t(),    tr("dialogs.main_window.tab_t"))
        self.tabs.addTab(self._tab_z(),    tr("dialogs.main_window.tab_z"))
        self.tabs.addTab(self._tab_send(), tr("dialogs.main_window.tab_send"))
        self.tabs.addTab(self._tab_tx(),   tr("dialogs.main_window.tab_transactions"))
        root.addWidget(self.tabs)

    def _tab_t(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(12, 12, 12, 12); v.setSpacing(8)
        h = QHBoxLayout()
        lbl = QLabel(tr("dialogs.main_window.t_addresses_title"))
        lbl.setStyleSheet("font-weight:600;font-size:14px;"); h.addWidget(lbl); h.addStretch()
        b1 = QPushButton(tr("dialogs.main_window.new_t_address"))
        b1.setStyleSheet(
            "QPushButton{background:#1f6feb;color:#fff;border:none;border-radius:6px;"
            "padding:8px 18px;font-weight:700;}"
            "QPushButton:hover{background:#388bfd;}"
            "QPushButton:pressed{background:#1158c7;}")
        b1.clicked.connect(self._new_t); h.addWidget(b1)
        v.addLayout(h)
        self.tbl_t = mk_view()
        self.tbl_t.setModel(self._t_model)
        self.tbl_t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tbl_t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tbl_t.setColumnWidth(1, 148)
        self.tbl_t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl_t.customContextMenuRequested.connect(self._t_ctx)
        self.tbl_t.horizontalHeader().sectionClicked.connect(self._t_header_click)
        v.addWidget(self.tbl_t); return w

    def _tab_z(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(12, 12, 12, 12); v.setSpacing(8)
        h = QHBoxLayout()
        lbl = QLabel(tr("dialogs.main_window.z_addresses_title"))
        lbl.setStyleSheet("font-weight:600;font-size:14px;"); h.addWidget(lbl); h.addStretch()
        b1 = QPushButton(tr("dialogs.main_window.new_z_address")); b1.setObjectName("shield")
        b1.clicked.connect(self._new_z); h.addWidget(b1)
        v.addLayout(h)
        self.tbl_z = mk_view()
        self.tbl_z.setModel(self._z_model)
        self.tbl_z.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tbl_z.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tbl_z.setColumnWidth(1, 148)
        self.tbl_z.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl_z.customContextMenuRequested.connect(self._z_ctx)
        self.tbl_z.horizontalHeader().sectionClicked.connect(self._z_header_click)
        v.addWidget(self.tbl_z); return w

    def _t_header_click(self, col: int):
        if col == 0:
            self._t_sort_mode = 'name_desc' if self._t_sort_mode == 'name_asc' else 'name_asc'
        else:
            self._t_sort_mode = 'balance_asc' if self._t_sort_mode == 'balance_desc' else 'balance_desc'
        if self._data: self._fill_t_table(self._data.get('t_balances', {}))

    def _z_header_click(self, col: int):
        if col == 0:
            self._z_sort_mode = 'name_desc' if self._z_sort_mode == 'name_asc' else 'name_asc'
        else:
            self._z_sort_mode = 'balance_asc' if self._z_sort_mode == 'balance_desc' else 'balance_desc'
        if self._data: self._fill_z_table(self._data.get('z_balances', {}))

    def _fill_t_table(self, t_bal: dict):
        selected = self._selected_model_address(self.tbl_t)
        scroll = self._view_scroll_state(self.tbl_t)
        self._t_model.set_balances(t_bal or {}, self._t_sort_mode, self._busy_addresses)
        self._restore_model_address_selection(self.tbl_t, self._t_model, selected)
        self._restore_view_scroll_state(self.tbl_t, scroll)

    def _fill_z_table(self, z_bal: dict):
        selected = self._selected_model_address(self.tbl_z)
        scroll = self._view_scroll_state(self.tbl_z)
        self._z_model.set_balances(z_bal or {}, self._z_sort_mode, self._busy_addresses)
        self._restore_model_address_selection(self.tbl_z, self._z_model, selected)
        self._restore_view_scroll_state(self.tbl_z, scroll)

    @staticmethod
    def _view_scroll_state(view) -> tuple[int, int]:
        return (view.verticalScrollBar().value(), view.horizontalScrollBar().value())

    @staticmethod
    def _restore_view_scroll_state(view, state: tuple[int, int] | None):
        if not state:
            return
        v_value, h_value = state

        def restore():
            vbar = view.verticalScrollBar()
            hbar = view.horizontalScrollBar()
            vbar.setValue(min(v_value, vbar.maximum()))
            hbar.setValue(min(h_value, hbar.maximum()))

        QTimer.singleShot(0, restore)

    def _selected_model_address(self, view) -> str:
        idx = view.currentIndex()
        if not idx.isValid():
            return ""
        data = idx.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        return str((data or {}).get("address", "") or "") if isinstance(data, dict) else ""

    def _restore_model_address_selection(self, view, model, address: str):
        if not address:
            return
        for row in range(model.rowCount()):
            if model.address_at(row) == address:
                view.selectRow(row)
                view.setCurrentIndex(model.index(row, 0))
                return

    def _selected_txid(self) -> str:
        idx = self.tbl_tx.currentIndex()
        tx = self._tx_model.transaction_at(idx.row()) if idx.isValid() else None
        return str((tx or {}).get("txid", "") or "")

    def _restore_tx_selection(self, txid: str):
        if not txid:
            return
        for row in range(self._tx_model.rowCount()):
            tx = self._tx_model.transaction_at(row)
            if tx and tx.get("txid") == txid:
                self.tbl_tx.selectRow(row)
                self.tbl_tx.setCurrentIndex(self._tx_model.index(row, 0))
                return

    def _fill_combo_from(self, t_addrs, z_addrs, t_bal: dict, z_bal: dict, prev_data):
        t_items = _sort_addr_items([(a, t_bal.get(a, 0.0)) for a in t_addrs], 'balance_desc')
        z_items = _sort_addr_items([(a, z_bal.get(a, 0.0)) for a in z_addrs], 'balance_desc')

        self.combo_from.blockSignals(True)
        self.combo_from.clear()
        for addr, bal in t_items + z_items:
            busy_prefix = "\U0001F504 " if addr in self._busy_addresses else ""
            self.combo_from.addItem(f"{busy_prefix}{_fmt_addr(addr)}   {fmt_btcz(bal)} BTCZ", userData=addr)
        self.combo_from.blockSignals(False)

        for i in range(self.combo_from.count()):
            if self.combo_from.itemData(i) == prev_data:
                self.combo_from.setCurrentIndex(i); break

    def _tab_send(self):
        w = QWidget(); outer = QHBoxLayout(w); outer.setContentsMargins(8, 0, 8, 0)
        p = QWidget(); p.setMinimumWidth(560)
        p.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        v = QVBoxLayout(p); v.setContentsMargins(16, 20, 16, 20); v.setSpacing(10)

        ttl = QLabel(tr("dialogs.main_window.send_btcz")); ttl.setStyleSheet("font-size:18px;font-weight:700;")
        v.addWidget(ttl)

        v.addWidget(slbl(tr("dialogs.main_window.from_address")))
        self.combo_from = _FromCombo()
        self.combo_from.setItemDelegate(_AddrBalanceDelegate(self.combo_from))
        self.combo_from.setFixedHeight(38)
        self.combo_from.currentIndexChanged.connect(self._on_from_changed)
        v.addWidget(self.combo_from)

        v.addWidget(slbl(tr("dialogs.main_window.to_address")))
        self.e_to = QLineEdit(); self.e_to.setPlaceholderText(tr("dialogs.main_window.to_placeholder"))
        self.e_to.setFixedHeight(38)
        self.e_to.textChanged.connect(self._validate_to_addr)
        v.addWidget(self.e_to)

        v.addWidget(slbl(tr("dialogs.main_window.amount")))
        amt_row = QHBoxLayout(); amt_row.setSpacing(6)
        self.spin_amt = BtcZAmountSpinBox()
        self.spin_amt.setDecimals(8); self.spin_amt.setMaximum(MAX_BTCZ_MONEY)
        self.spin_amt.setMinimum(0); self.spin_amt.setValue(0)
        self.spin_amt.setSpecialValueText("0")
        self.spin_amt.setSuffix("  BTCZ")
        self.spin_amt.setFixedHeight(38)
        self.spin_amt.valueChanged.connect(self._on_amt_changed)
        amt_row.addWidget(self.spin_amt)
        self.btn_max = QPushButton(tr("dialogs.main_window.max")); self.btn_max.setMaximumWidth(54)
        self.btn_max.setFixedHeight(38)
        self.btn_max.setCheckable(True)
        self.btn_max.setStyleSheet(
            "QPushButton{background:#21262d;color:#8b949e;border:1px solid #30363d;"
            "border-radius:6px;padding:6px 8px;font-weight:700;font-size:12px;}"
            "QPushButton:hover{background:#2d2d1a;color:#f7a32c;border-color:#f7a32c;}"
            "QPushButton:checked{background:#2d1a00;color:#f7a32c;border:2px solid #f7a32c;}"
            "QPushButton:checked:hover{background:#3d2600;}")
        self.btn_max.toggled.connect(self._toggle_max)
        amt_row.addWidget(self.btn_max); v.addLayout(amt_row)

        v.addWidget(slbl(tr("dialogs.main_window.network_fee")))
        self.spin_fee = BtcZAmountSpinBox()
        self.spin_fee.setDecimals(8); self.spin_fee.setMaximum(MAX_NODE_FEE_BTCZ)
        self.spin_fee.setMinimum(MIN_NODE_FEE_BTCZ); self.spin_fee.setValue(MIN_NODE_FEE_BTCZ)
        self.spin_fee.setSingleStep(0.000005)
        self.spin_fee.setSuffix("  BTCZ")
        self.spin_fee.setFixedHeight(38)
        self.spin_fee.setKeyboardTracking(True)
        self.spin_fee.valueChanged.connect(self._on_fee_changed)
        self.spin_fee.lineEdit().textChanged.connect(self._on_fee_text_edited)
        self.spin_fee.editingFinished.connect(self._clamp_fee_to_node_limit)
        v.addWidget(self.spin_fee)

        self.lbl_memo = slbl(tr("dialogs.main_window.memo"))
        self.lbl_memo.setVisible(False)
        v.addWidget(self.lbl_memo)
        self.e_memo = QLineEdit(); self.e_memo.setPlaceholderText(tr("dialogs.main_window.memo_placeholder"))
        self.e_memo.setFixedHeight(38)
        self.e_memo.setVisible(False)
        v.addWidget(self.e_memo)

        self.send_summary = QFrame()
        self.send_summary.setStyleSheet("QFrame{background:transparent;border:none;}")
        sg = QVBoxLayout(self.send_summary); sg.setContentsMargins(14, 10, 14, 10); sg.setSpacing(4)

        def _srow(label, attr):
            h = QHBoxLayout()
            lbl = QLabel(label); lbl.setStyleSheet("color:#8b949e;font-size:12px;")
            h.addWidget(lbl); h.addStretch()
            val = QLabel("-"); val.setStyleSheet("color:#e6edf3;font-size:12px;font-weight:600;")
            h.addWidget(val); sg.addLayout(h); setattr(self, attr, val)

        _srow(tr("dialogs.main_window.recipient_receives"), "lbl_sum_recv")
        _srow(tr("dialogs.main_window.network_fee_row"),        "lbl_sum_fee")
        div2 = QFrame(); div2.setStyleSheet("background:#30363d;"); div2.setFixedHeight(1)
        sg.addWidget(div2)
        h_tot = QHBoxLayout()
        lt = QLabel(tr("dialogs.main_window.total_amount")); lt.setStyleSheet("color:#8b949e;font-size:12px;font-weight:700;")
        h_tot.addWidget(lt); h_tot.addStretch()
        self.lbl_sum_total = QLabel("-")
        self.lbl_sum_total.setStyleSheet("color:#f7a32c;font-size:13px;font-weight:700;")
        h_tot.addWidget(self.lbl_sum_total); sg.addLayout(h_tot)
        v.addWidget(self.send_summary)

        self.btn_send = QPushButton(tr("dialogs.send_flow.send_button")); self.btn_send.setObjectName("primary")
        self.btn_send.setMinimumHeight(44); self.btn_send.clicked.connect(self._do_send)
        self.btn_send.setEnabled(False)
        v.addWidget(self.btn_send)
        v.addStretch()
        outer.addWidget(p, 1)
        return w

    def _tab_tx(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(12, 12, 12, 12); v.setSpacing(8)
        h = QHBoxLayout()
        lbl = QLabel(tr("dialogs.main_window.transactions_title")); lbl.setStyleSheet("font-weight:600;font-size:14px;")
        h.addWidget(lbl); h.addStretch()
        self.btn_sort = QPushButton(tr("dialogs.main_window.sort_date"))
        self.btn_sort.setMinimumWidth(132)
        self.btn_sort.setStyleSheet(
            "QPushButton{background:#1c2128;color:#f7a32c;border:1px solid #f7a32c;"
            "border-radius:6px;padding:6px 14px;font-weight:700;}"
            "QPushButton:hover{background:#2d1a00;}")
        self.btn_sort.clicked.connect(lambda: self._set_tx_sort("date")); h.addWidget(self.btn_sort)
        btn_ref = QPushButton(tr("dialogs.main_window.refresh")); btn_ref.clicked.connect(self._manual_refresh); h.addWidget(btn_ref)
        v.addLayout(h)

        self.tbl_tx = mk_view()
        self.tbl_tx.setModel(self._tx_model)
        hdr = self.tbl_tx.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.sectionClicked.connect(self._tx_header_click)
        self.tbl_tx.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tbl_tx.customContextMenuRequested.connect(self._tx_ctx)
        self.tbl_tx.doubleClicked.connect(self._tx_double_click)
        v.addWidget(self.tbl_tx); return w

    def _build_sb(self):
        sb = self.statusBar()
        sb.setStyleSheet("QStatusBar{background:#1c2128;color:#8b949e;font-size:12px;}"
                         "QStatusBar::item{border:none;}")

        self.lbl_status = QLabel("  " + tr("dialogs.main_window.not_connected"))
        self.lbl_status.setStyleSheet("color:#f85149;font-weight:700;padding:0 6px;")
        sb.addWidget(self.lbl_status)

        sep1 = QLabel("|"); sep1.setStyleSheet("color:#30363d;"); sb.addWidget(sep1)
        self.lbl_blocks = QLabel(tr("dialogs.main_window.blocks", value="-"))
        self.lbl_blocks.setStyleSheet("color:#8b949e;padding:0 6px;"); sb.addWidget(self.lbl_blocks)
        sep2 = QLabel("|"); sep2.setStyleSheet("color:#30363d;"); sb.addWidget(sep2)
        self.lbl_peers = QLabel(tr("dialogs.main_window.peers", value="-"))
        self.lbl_peers.setStyleSheet("color:#8b949e;padding:0 6px;"); sb.addWidget(self.lbl_peers)
        sep3 = QLabel("|"); sep3.setStyleSheet("color:#30363d;"); sb.addWidget(sep3)

        self.sync_bar = QProgressBar()
        self.sync_bar.setObjectName("sbar")
        self.sync_bar.setRange(0, 10000); self.sync_bar.setValue(0)
        self.sync_bar.setFormat(tr("dialogs.main_window.connecting"))
        self.sync_bar.setFixedHeight(15)
        sb.addWidget(self.sync_bar, 1)

    def _update_summary_titles(self, *, has_transparent_pending: bool, has_shielded_pending: bool):
        spin = " \U0001F504" if has_transparent_pending else ""
        self.card_transp._title_label.setText((tr("dialogs.main_window.card_transparent") + spin).upper())
        spin = " \U0001F504" if has_shielded_pending else ""
        self.card_priv._title_label.setText((tr("dialogs.main_window.card_shielded") + spin).upper())
        spin = " \U0001F504" if (has_transparent_pending or has_shielded_pending) else ""
        self.card_total._title_label.setText((tr("dialogs.main_window.card_total") + spin).upper())

    def _derive_busy_addresses(self, data: dict) -> set[str]:
        busy: set[str] = set()
        live_txid_confirms: dict[str, int] = {}
        live_txid_status: dict[str, str] = {}
        inactive_tx_statuses = {"failed", "expired", "conflicted", "reorged", "stale"}
        for tx in data.get("txs", []) or []:
            try:
                confirms = int(tx.get("confirmations", 0) or 0)
            except Exception:
                confirms = 0
            txid = str(tx.get("txid", "") or "").strip()
            tx_status = str(tx.get("status", "") or "").strip().lower()
            if txid:
                live_txid_confirms[txid] = confirms
                live_txid_status[txid] = tx_status
            if tx_status in inactive_tx_statuses or confirms < 0:
                continue
            if confirms > 0:
                continue
            addr = str(tx.get("address", "") or "").strip()
            if addr:
                busy.add(addr)
            for entry in tx.get("_entries", []) or []:
                entry_addr = str(entry.get("address", "") or "").strip()
                if entry_addr:
                    busy.add(entry_addr)
        pending_from = str((self._pending_send or {}).get("from", "") or "").strip()
        if pending_from and self._active_opid:
            busy.add(pending_from)
        if self.cache is not None:
            try:
                for op in self.cache.list_operations(limit=200):
                    from_addr = str(op.get("from_address", "") or "").strip()
                    to_addr = str(op.get("to_address", "") or "").strip()
                    if not from_addr:
                        continue
                    status = str(op.get("status", "") or "")
                    txid = str(op.get("txid", "") or "").strip()
                    if status in {"submitted", "executing"}:
                        busy.add(from_addr)
                        if to_addr:
                            busy.add(to_addr)
                        continue
                    if status == "success":
                        if not txid:
                            busy.add(from_addr)
                            continue
                        if live_txid_status.get(txid, "") in inactive_tx_statuses:
                            continue
                        confirms = live_txid_confirms.get(txid)
                        if confirms is None and self.cache is not None:
                            cached_entries = self.cache.get_transaction_entries(txid)
                            cached_statuses = {
                                str(row.get("status", "") or "").strip().lower()
                                for row in cached_entries
                            }
                            if cached_statuses & inactive_tx_statuses:
                                continue
                            cached_confirms = [
                                int(row.get("confirmations", 0) or 0)
                                for row in cached_entries
                            ]
                            if cached_confirms:
                                confirms = max(cached_confirms)
                        if confirms is None or confirms <= 0:
                            busy.add(from_addr)
                            if to_addr:
                                busy.add(to_addr)
            except Exception:
                pass
        return busy

    def _success_operation_txids(self) -> set[str]:
        if self.cache is None:
            return set()
        txids: set[str] = set()
        try:
            for op in self.cache.list_operations(status="success", limit=200):
                txid = str(op.get("txid", "") or "").strip()
                if txid:
                    txids.add(txid)
        except Exception:
            pass
        return txids

    def _fast_status_txids(self) -> list[str]:
        tracked: list[str] = []
        seen: set[str] = set()
        inactive = {"failed", "expired", "conflicted", "reorged", "stale"}

        def add(txid: str):
            txid = str(txid or "").strip()
            if txid and txid not in seen:
                seen.add(txid)
                tracked.append(txid)

        for tx in list(self._cached_txs or []) + list((self._data or {}).get("txs", []) or []):
            txid = str(tx.get("txid", "") or "").strip()
            if not txid:
                continue
            status = tx_status_code(tx)
            try:
                confirms = int(tx.get("confirmations", 0) or 0)
            except Exception:
                confirms = 0
            if status not in inactive and confirms < 6:
                add(txid)

        if self.cache is not None:
            try:
                for op in self.cache.list_operations(limit=200):
                    txid = str(op.get("txid", "") or "").strip()
                    status = str(op.get("status", "") or "").strip().lower()
                    if not txid or status not in {"success", "submitted", "executing", "unknown"}:
                        continue
                    if status == "success":
                        entries = self.cache.get_transaction_entries(txid)
                        if entries:
                            try:
                                max_confirms = max(int(row.get("confirmations", 0) or 0) for row in entries)
                            except Exception:
                                max_confirms = 0
                            if max_confirms >= 6:
                                continue
                    add(txid)
            except Exception:
                pass

        return tracked

    @staticmethod
    def _merge_tx_status_update(row: dict, update: dict) -> bool:
        changed = False
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
            if row.get(field) != value:
                row[field] = value
                changed = True
        return changed

    def _apply_fast_block_state(self, chain: dict, tx_updates: list[dict]):
        if not self._data:
            return

        info = self._data.setdefault("info", {})
        if isinstance(chain, dict) and chain.get("blocks") is not None:
            info["blocks"] = chain.get("blocks")
        if isinstance(chain, dict):
            self._data.setdefault("chain", {}).update(chain)

        updates_by_txid = {
            str(update.get("txid", "") or "").strip(): update
            for update in tx_updates or []
            if str(update.get("txid", "") or "").strip()
        }
        if not updates_by_txid:
            return

        rows = list(self._data.get("txs", []) or [])
        found_txids: set[str] = set()
        changed_txids: set[str] = set()
        for row in rows:
            txid = str(row.get("txid", "") or "").strip()
            update = updates_by_txid.get(txid)
            if not update:
                continue
            found_txids.add(txid)
            if self._merge_tx_status_update(row, update):
                changed_txids.add(txid)

        operation_txids = self._success_operation_txids()
        for txid, update in updates_by_txid.items():
            if txid in found_txids or txid not in operation_txids:
                continue
            new_row = {
                "txid": txid,
                "category": "",
                "address": "",
                "amount": 0.0,
                "time": update.get("time") or update.get("blocktime") or update.get("timereceived") or int(time.time()),
                **update,
            }
            rows.append(new_row)
            changed_txids.add(txid)

        if not changed_txids:
            return

        self._data["txs"] = rows
        if self.cache is not None:
            for txid in changed_txids:
                update = updates_by_txid.get(txid) or {}
                try:
                    self.cache.update_transaction_reconcile(
                        txid,
                        status=update.get("status"),
                        confirmations=update.get("confirmations"),
                        blockhash=update.get("blockhash"),
                    )
                except Exception:
                    pass

        old_busy = set(self._busy_addresses)
        self._busy_addresses = self._derive_busy_addresses(self._data)

        new_t_bal = self._data.get("t_balances", {}) or {}
        new_z_bal = self._data.get("z_balances", {}) or {}
        own_addresses = set(self._data.get("t_addrs", []) or []) | set(self._data.get("z_addrs", []) or [])
        self._cached_txs = self._txs_with_wallet_operation_receives(rows, own_addresses)
        new_key = tx_fingerprint(self._cached_txs)
        if new_key != self._tx_cache_key:
            self._tx_cache_key = new_key
            self._fill_tx(self._cached_txs)

        if old_busy != self._busy_addresses:
            self._update_summary_titles(
                has_transparent_pending=bool(set(new_t_bal) & self._busy_addresses),
                has_shielded_pending=bool(set(new_z_bal) & self._busy_addresses),
            )
            self._fill_t_table(new_t_bal)
            self._fill_z_table(new_z_bal)
            prev_data = self.combo_from.currentData()
            self._fill_combo_from(
                self._data.get("t_addrs", []), self._data.get("z_addrs", []),
                new_t_bal, new_z_bal, prev_data
            )
            self._update_send_btn()

    def _txs_with_wallet_operation_receives(self, txs: list, own_addresses: set[str]) -> list:
        rows = list(txs or [])
        if self.cache is None or not own_addresses:
            return rows
        existing = {
            (str(tx.get("txid", "") or ""), str(tx.get("category", "") or ""), str(tx.get("address", "") or ""))
            for tx in rows
        }
        by_txid = {str(tx.get("txid", "") or ""): tx for tx in rows if tx.get("txid")}
        try:
            operations = self.cache.list_operations(limit=500)
        except Exception:
            return rows
        for op in operations:
            txid = str(op.get("txid", "") or "").strip()
            from_addr = str(op.get("from_address", "") or "").strip()
            to_addr = str(op.get("to_address", "") or "").strip()
            status = str(op.get("status", "") or "")
            if not txid or status != "success":
                continue
            base = by_txid.get(txid) or {}
            amount_zat = int(op.get("amount_zat") or 0)
            if amount_zat <= 0:
                continue
            common = {
                "txid": txid,
                "confirmations": int(base.get("confirmations", 0) or 0),
                "blockhash": base.get("blockhash", ""),
                "blockheight": base.get("blockheight"),
                "blockindex": base.get("blockindex"),
                "time": base.get("time") or base.get("blocktime") or base.get("timereceived") or op.get("created_at"),
                "blocktime": base.get("blocktime"),
                "timereceived": base.get("timereceived") or op.get("created_at"),
                "created_at": op.get("created_at"),
                "status": base.get("status", ""),
            }
            if from_addr in own_addresses:
                key = (txid, "send", from_addr)
                has_send = any(str(tx.get("txid", "") or "") == txid and tx.get("category") == "send" for tx in rows)
                if key not in existing and not has_send:
                    fee_zat = int(op.get("fee_zat") or 0)
                    synthetic = {
                        **common,
                        "category": "send",
                        "address": from_addr,
                        "amount": -zat_to_float(amount_zat),
                        "fee": -zat_to_float(fee_zat) if fee_zat else None,
                        "_synthetic": "own_operation_send",
                    }
                    rows.append(synthetic)
                    existing.add(key)
            if to_addr in own_addresses:
                key = (txid, "receive", to_addr)
                if key not in existing:
                    synthetic = {
                        **common,
                        "category": "receive",
                        "address": to_addr,
                        "amount": zat_to_float(amount_zat),
                        "_synthetic": "own_operation_receive",
                    }
                    rows.append(synthetic)
                    existing.add(key)
        return rows

    def _update_memo_visibility(self):
        allow_memo = _is_z_addr(self.e_to.text().strip())
        self.lbl_memo.setVisible(allow_memo)
        self.e_memo.setVisible(allow_memo)
        if not allow_memo and self.e_memo.text():
            self.e_memo.clear()

    def _set_status_visual(self, state: str, text: str):
        colors = {
            "offline": "#f85149",
            "cached": "#f7a32c",
            "connected": "#238636",
            "syncing": "#f7a32c",
        }
        self.lbl_status.setText(text)
        if self._status_visual_state != state:
            self.lbl_status.setStyleSheet(
                f"color:{colors.get(state, '#8b949e')};font-weight:700;padding:0 6px;"
            )
            self._status_visual_state = state

    def _set_sync_visual(self, state: str, *, value: int, text: str):
        colors = {
            "offline": "#f85149",
            "cached": "#f7a32c",
            "syncing": "#f7a32c",
            "synced": "#238636",
            "idle": "#30363d",
        }
        self.sync_bar.setValue(value)
        self.sync_bar.setFormat(text)
        if self._sync_visual_state != state:
            self.sync_bar.setStyleSheet(
                f"QProgressBar#sbar::chunk{{background:{colors.get(state, '#30363d')};border-radius:3px}}"
            )
            self._sync_visual_state = state

    def _load_cached_snapshot(self):
        if self.cache is None:
            return
        try:
            if not self.cache.has_cached_wallet_data():
                return
            data = self.cache.get_refresh_snapshot(tx_limit=200)
        except Exception:
            return
        self._apply_wallet_data(data, cached=True)

    def _apply_wallet_data(self, data: dict, cached: bool = False):
        self._data = data
        self._busy_addresses = self._derive_busy_addresses(data)
        info  = data.get("info",  {}); chain = data.get("chain", {})
        blocks = info.get("blocks", "-"); peers = info.get("connections", "-")
        try: self._cur_blocks = int(blocks)
        except Exception: pass

        if cached:
            self._set_status_visual("cached", "  " + tr("dialogs.main_window.cached_data"))
        else:
            self._set_status_visual("connected", "  " + tr("dialogs.main_window.connected"))
        self.lbl_blocks.setText(tr("dialogs.main_window.blocks", value=blocks))
        self.lbl_peers.setText(tr("dialogs.main_window.peers", value=peers))

        vp = chain.get("verificationprogress")
        if cached:
            self._wallet_synced = False
            last_seen = data.get("last_refresh_at")
            self._last_sync_ts = last_seen or self._last_sync_ts
            if last_seen:
                self._set_sync_visual("cached", value=0, text=f"{tr('dialogs.main_window.cached')}  {fmt_ts(last_seen)}")
            else:
                self._set_sync_visual("cached", value=0, text=tr("dialogs.main_window.cached"))
        elif vp is not None:
            pct = float(vp) * 100
            synced = pct >= 99.9
            self._wallet_synced = synced
            self._last_sync_ts = int(time.time())
            self._set_sync_visual(
                "synced" if synced else "syncing",
                value=int(pct * 100),
                text=tr("dialogs.main_window.synchronization", percent=pct) if not synced else f"{tr('dialogs.main_window.connected')}  {pct:.2f}%"
            )
        else:
            self._wallet_synced = False
            self._set_sync_visual("idle", value=0, text="-")
        self._update_wallet_key_actions()

        tb        = data.get("total_bal",  {})
        new_t_bal = data.get("t_balances", {})
        new_z_bal = data.get("z_balances", {})

        self._addr_balances.update(new_t_bal)
        self._addr_balances.update(new_z_bal)

        total_val  = float(tb.get("total",       0) or 0)
        priv_val   = float(tb.get("private",     0) or 0)
        transp_val = float(tb.get("transparent", 0) or 0)
        self._update_summary_titles(
            has_transparent_pending=bool(set(new_t_bal) & self._busy_addresses),
            has_shielded_pending=bool(set(new_z_bal) & self._busy_addresses),
        )

        self.lbl_total.setText(fmt_btcz(total_val))
        self.lbl_priv.setText(fmt_btcz(priv_val))
        self.lbl_transp.setText(fmt_btcz(transp_val))
        if total_val > 0:
            self._had_balance = True

        self._fill_t_table(new_t_bal)
        self._fill_z_table(new_z_bal)

        prev_data = self.combo_from.currentData()
        self._fill_combo_from(
            data.get("t_addrs", []), data.get("z_addrs", []),
            new_t_bal, new_z_bal, prev_data
        )
        self._update_memo_visibility()
        self._update_summary()

        own_addresses = set(data.get("t_addrs", []) or []) | set(data.get("z_addrs", []) or [])
        self._cached_txs = self._txs_with_wallet_operation_receives(data.get("txs", []), own_addresses)
        new_key = tx_fingerprint(self._cached_txs)
        if cached:
            self._tx_cache_key = new_key
            self._fill_tx(self._cached_txs)
        elif new_key != self._tx_cache_key:
            self._tx_cache_key = new_key
            self._fill_tx(self._cached_txs)

    def _row_addr(self, tbl, pos):
        return address_actions.row_addr(self, tbl, pos)

    def _selected_addr(self, tbl) -> str:
        return address_actions.selected_addr(self, tbl)

    def _copy_selected_address(self, tbl):
        address_actions.copy_selected_address(self, tbl)

    def _show_selected_address_qr(self, tbl):
        address_actions.show_selected_address_qr(self, tbl)

    def _prefill_send(self, addr):
        address_actions.prefill_send(self, addr)

    def _open_tip_for_developer(self):
        self.tabs.setCurrentIndex(2)
        self.e_to.setText(DEVELOPER_TIP_ADDRESS)
        self.e_to.setFocus()
        try:
            self.e_to.selectAll()
        except Exception:
            pass

    def _toggle_max(self, checked: bool):
        self._max_mode = checked
        if checked:
            self._recalc_max()

    def _on_from_changed(self):
        if self._max_mode:
            self._recalc_max()
        else:
            self._update_summary()

    def _on_fee_changed(self):
        if getattr(self, "_clamping_fee", False):
            return
        fee_zat = btcz_to_zat(self.spin_fee.value())
        if fee_zat > btcz_to_zat(MAX_NODE_FEE_BTCZ) or fee_zat < btcz_to_zat(MIN_NODE_FEE_BTCZ):
            self._schedule_fee_clamp()
            return
        if self._max_mode: self._recalc_max()
        self._update_summary()

    def _fee_text_zat(self, text: str) -> int | None:
        raw = str(text or "").replace("BTCZ", "").replace(",", ".").strip()
        if not raw:
            return None
        try:
            return btcz_to_zat(float(raw.split()[0]))
        except (TypeError, ValueError, IndexError):
            return None

    def _on_fee_text_edited(self, text: str):
        if getattr(self, "_clamping_fee", False):
            return
        fee_zat = self._fee_text_zat(text)
        if fee_zat is not None and (
            fee_zat > btcz_to_zat(MAX_NODE_FEE_BTCZ)
            or fee_zat < btcz_to_zat(MIN_NODE_FEE_BTCZ)
        ):
            self._schedule_fee_clamp()

    def _schedule_fee_clamp(self):
        if getattr(self, "_fee_clamp_pending", False):
            return
        self._fee_clamp_pending = True
        QTimer.singleShot(0, self._apply_scheduled_fee_clamp)

    def _apply_scheduled_fee_clamp(self):
        self._fee_clamp_pending = False
        fee_zat = self._fee_text_zat(self.spin_fee.lineEdit().text())
        if fee_zat is None:
            return
        if fee_zat > btcz_to_zat(MAX_NODE_FEE_BTCZ):
            self._set_fee_to_node_limit()
        elif fee_zat < btcz_to_zat(MIN_NODE_FEE_BTCZ):
            self._set_fee_to_node_minimum()

    def _set_fee_to_node_limit(self):
        self._clamping_fee = True
        self.spin_fee.blockSignals(True)
        self.spin_fee.setValue(MAX_NODE_FEE_BTCZ)
        self.spin_fee.blockSignals(False)
        self._clamping_fee = False
        if self._max_mode:
            self._recalc_max()
        else:
            self._update_summary()

    def _set_fee_to_node_minimum(self):
        self._clamping_fee = True
        self.spin_fee.blockSignals(True)
        self.spin_fee.setValue(MIN_NODE_FEE_BTCZ)
        self.spin_fee.blockSignals(False)
        self._clamping_fee = False
        if self._max_mode:
            self._recalc_max()
        else:
            self._update_summary()

    def _clamp_fee_to_node_limit(self):
        try:
            self.spin_fee.interpretText()
        except Exception:
            pass
        fee_zat = btcz_to_zat(self.spin_fee.value())
        max_zat = btcz_to_zat(MAX_NODE_FEE_BTCZ)
        min_zat = btcz_to_zat(MIN_NODE_FEE_BTCZ)
        if min_zat <= fee_zat <= max_zat:
            return
        if fee_zat > max_zat:
            self._set_fee_to_node_limit()
        else:
            self._set_fee_to_node_minimum()

    def _on_amt_changed(self):
        if not getattr(self, '_setting_max', False) and self._max_mode:
            self._max_mode = False
            self.btn_max.blockSignals(True)
            self.btn_max.setChecked(False)
            self.btn_max.blockSignals(False)
        self._update_summary()

    def _recalc_max(self):
        addr = self.combo_from.currentData()
        if not addr:
            return
        bal_zat = btcz_to_zat(self._addr_balances.get(addr, 0.0))
        fee_zat = btcz_to_zat(self.spin_fee.value())
        amt_zat = max(0, bal_zat - fee_zat)
        amt = zat_to_float(amt_zat)
        self._setting_max = True
        self.spin_amt.setValue(amt)
        self._setting_max = False
        self._update_summary()

    def _update_send_btn(self):
        if not hasattr(self, 'btn_send'):
            return
        if getattr(self, "_active_opid", ""):
            self.btn_send.setEnabled(False)
            self.btn_send.setToolTip("")
            return
        to = self.e_to.text().strip()
        amt = btcz_to_zat(self.spin_amt.value())
        frm = str(self.combo_from.currentData() or "").strip()
        from_balance_zat = btcz_to_zat(self._addr_balances.get(frm, 0.0)) if frm else 0
        to_ok = bool(to) and (
            ((to.startswith("t1") or to.startswith("t3")) and len(to) == 35) or
            (to.startswith("zs1") and len(to) == 78) or
            (to.startswith("zc")  and len(to) >= 40)
        )
        same_address = bool(frm and to and frm == to)
        has_balance = from_balance_zat > 0
        enabled = to_ok and amt > 0 and has_balance and not same_address and frm not in self._busy_addresses
        self.btn_send.setEnabled(enabled)
        if enabled:
            self.btn_send.setToolTip("")
        elif same_address:
            self.btn_send.setToolTip(tr("dialogs.send_flow.send_disabled_same_address"))
        elif frm and not has_balance:
            self.btn_send.setToolTip(tr("dialogs.send_flow.send_disabled_zero_balance"))
        elif frm in self._busy_addresses:
            self.btn_send.setToolTip(tr("dialogs.send_flow.address_busy_note"))
        else:
            self.btn_send.setToolTip("")

    def _validate_to_addr(self, text: str):
        t = text.strip()
        if not t:
            self.e_to.setStyleSheet("")
        else:
            valid = (
                ((t.startswith("t1") or t.startswith("t3")) and len(t) == 35) or
                (t.startswith("zs1") and len(t) == 78) or
                (t.startswith("zc")  and len(t) >= 40)
            )
            self.e_to.setStyleSheet(
                "" if valid
                else "QLineEdit{border:1px solid #f85149;background:#1c1010;border-radius:6px;}"
            )
        self._update_memo_visibility()
        self._update_send_btn()

    def _update_summary(self):
        amt_zat = btcz_to_zat(self.spin_amt.value())
        fee_zat = btcz_to_zat(self.spin_fee.value())
        self.lbl_sum_recv.setText(f"{fmt_btcz(zat_to_float(amt_zat))} BTCZ")
        self.lbl_sum_fee.setText(f"{fmt_btcz(zat_to_float(fee_zat))} BTCZ")
        self.lbl_sum_total.setText(f"{fmt_btcz(zat_to_float(amt_zat + fee_zat))} BTCZ")
        self._update_send_btn()

    def _show_address_qr(self, addr: str):
        address_actions.show_address_qr(self, addr)

    def _t_ctx(self, pos):
        address_actions.t_context_menu(self, pos)

    def _z_ctx(self, pos):
        address_actions.z_context_menu(self, pos)

    def _export_key(self, addr: str, is_z: bool):
        address_actions.export_key(self, addr, is_z)

    def _import_key(self):
        self._timer.stop()
        ImportKeyDialog(self, self.rpc, self.cache).exec()
        self._timer.start(30_000)
        self.refresh(force_full=True)

    def _ensure_export_support(self, action_label: str) -> Path | None:
        try:
            export_dir, changed = ensure_exportdir()
        except Exception as e:
            _msg_critical(self, action_label, str(e))
            return None
        if changed and is_port_open(self.rpc.host, self.rpc.port, timeout=0.5):
            _msg_info(
                self,
                action_label,
                tr("dialogs.main_window.restart_exportdir_message"),
            )
            return None
        return export_dir

    def _cleanup_temp_wallet_dump(self):
        if not self._temp_wallet_dump_path:
            return
        try:
            if self._temp_wallet_dump_path.exists():
                self._temp_wallet_dump_path.unlink()
        except Exception:
            pass
        self._temp_wallet_dump_path = None

    def _export_full_wallet_keys(self):
        if not self._wallet_key_actions_enabled():
            return
        export_dir = self._ensure_export_support(tr("dialogs.main_window.export_full_wallet_keys"))
        if export_dir is None:
            return
        if not _ask_yes_no(
            self,
            tr("dialogs.main_window.export_full_wallet_keys"),
            tr("dialogs.main_window.full_export_warning"),
            yes_text=tr("dialogs.main_window.export"),
            no_text=tr("dialogs.address_actions.cancel"),
            kind="warning",
        ):
            return
        default_name = f"ZSend_Wallet_Export_{datetime.now().strftime('%Y%m%d_%H.%M.%S')}.json"
        path, _ = _get_save_file_name(
            self,
            tr("dialogs.main_window.export_full_wallet_keys"),
            str(Path.home() / default_name),
            tr("dialogs.main_window.json_filter"),
        )
        if not path:
            return
        dump_basename = _sanitize_dump_basename(f"ZSendWalletExport{datetime.now().strftime('%Y%m%d%H%M%S')}")
        self._busy_dialog = BusyDialog(
            self,
            tr("dialogs.main_window.export_full_wallet_keys"),
            tr("dialogs.main_window.busy_export_message"),
        )
        self._full_export_worker = FullWalletExportWorker(self.rpc, export_dir, dump_basename)
        self._full_export_worker.done.connect(lambda payload: self._on_full_wallet_export_done(payload, Path(path)))
        self._full_export_worker.error.connect(self._on_full_wallet_export_error)
        self._update_wallet_key_actions()
        _track(self._full_export_worker).start()
        self._busy_dialog.exec()

    def _on_full_wallet_export_done(self, payload: dict, json_path: Path):
        try:
            json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            self._on_full_wallet_export_error(str(e))
            return
        if self._busy_dialog is not None:
            self._busy_dialog.mark_finished()
            self._busy_dialog.accept()
            self._busy_dialog = None
        self._full_export_worker = None
        self._update_wallet_key_actions()
        summary = payload.get("summary") or {}
        _msg_info(
            self,
            tr("dialogs.main_window.export_complete"),
            tr(
                "dialogs.main_window.export_complete_message",
                path=json_path,
                entries=summary.get("entry_count", 0),
                reserved=summary.get("reserved_count", 0),
            ),
        )

    def _on_full_wallet_export_error(self, message: str):
        if self._busy_dialog is not None:
            self._busy_dialog.mark_finished()
            self._busy_dialog.accept()
            self._busy_dialog = None
        self._full_export_worker = None
        self._update_wallet_key_actions()
        hint = ""
        if "exportdir" in message.lower():
            hint = "\n\nFull wallet export needs the node started with -exportdir or exportdir in bitcoinz.conf."
        _msg_critical(self, tr("dialogs.main_window.export_failed"), f"{message}{hint}")

    def _import_wallet_keys_file(self):
        if not self._wallet_key_actions_enabled():
            return
        self._cleanup_temp_wallet_dump()
        path, _ = _get_open_file_name(
            self,
            tr("dialogs.main_window.import_wallet_keys_file"),
            str(Path.home()),
            tr("dialogs.main_window.json_filter"),
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            _msg_critical(self, tr("dialogs.main_window.import_failed"), tr("dialogs.main_window.import_json_read_error", error=e))
            return
        if payload.get("format") != "zsend_wallet_export_v1":
            _msg_critical(self, tr("dialogs.main_window.import_failed"), tr("dialogs.main_window.unsupported_export"))
            return
        dump_text = str(payload.get("node_dump_text") or "").strip()
        if not dump_text:
            _msg_critical(self, tr("dialogs.main_window.import_failed"), tr("dialogs.main_window.missing_dump_data"))
            return
        if not _ask_yes_no(
            self,
            tr("dialogs.main_window.import_wallet_keys_file"),
            tr("dialogs.main_window.full_import_warning"),
            yes_text=tr("dialogs.main_window.import"),
            no_text=tr("dialogs.address_actions.cancel"),
            kind="warning",
        ):
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp_name = f"zsend_import_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{token_hex(8)}.dump"
        self._temp_wallet_dump_path = DATA_DIR / temp_name
        try:
            self._temp_wallet_dump_path.write_text(dump_text + "\n", encoding="utf-8")
            dump_text = ""
            payload = {}
        except Exception as e:
            _msg_critical(self, tr("dialogs.main_window.import_failed"), str(e))
            self._temp_wallet_dump_path = None
            return
        self._busy_dialog = BusyDialog(
            self,
            tr("dialogs.main_window.import_wallet_keys_file"),
            tr("dialogs.main_window.busy_import_message"),
        )
        self._full_import_worker = FullWalletImportWorker(self.rpc, self._temp_wallet_dump_path)
        self._full_import_worker.done.connect(self._on_full_wallet_import_done)
        self._full_import_worker.error.connect(self._on_full_wallet_import_error)
        self._update_wallet_key_actions()
        _track(self._full_import_worker).start()
        self._busy_dialog.exec()

    def _on_full_wallet_import_done(self):
        self._cleanup_temp_wallet_dump()
        if self._busy_dialog is not None:
            self._busy_dialog.mark_finished()
            self._busy_dialog.accept()
            self._busy_dialog = None
        self._full_import_worker = None
        self._update_wallet_key_actions()
        _msg_info(
            self,
            tr("dialogs.main_window.import_complete"),
            tr("dialogs.main_window.import_complete_message"),
        )
        self.refresh(force_full=True)

    def _on_full_wallet_import_error(self, message: str):
        self._cleanup_temp_wallet_dump()
        if self._busy_dialog is not None:
            self._busy_dialog.mark_finished()
            self._busy_dialog.accept()
            self._busy_dialog = None
        self._full_import_worker = None
        self._update_wallet_key_actions()
        _msg_critical(self, tr("dialogs.main_window.import_failed"), message)

    def _open_cfg(self):
        dlg = ConfigDialog(self, self.rpc.host, self.rpc.port,
                           self.rpc.user, self.rpc.password, str(CONF_PATH))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            h, p, u, pw = dlg.values(); self.rpc = BitcoinZRPC(h, p, u, pw); self.refresh(force_full=True)

    def _open_diag(self):  DiagDialog(self, self.rpc, self.cache).exec()
    def _open_about(self): AboutDialog(self, self.rpc).exec()

    def _quit_and_stop(self):
        shutdown_flow.start_shutdown(self)

    def _on_shutdown_done(self):
        shutdown_flow.finish_shutdown(self)

    def _new_t(self):
        self._create_address(shielded=False)

    def _new_z(self):
        self._create_address(shielded=True)

    def _create_address(self, *, shielded: bool):
        w = NewAddressWorker(self.rpc, shielded)
        self._threads.append(w)
        w.finished.connect(lambda: self._threads.remove(w) if w in self._threads else None)
        w.done.connect(lambda _addr: self.refresh(force_full=True))
        w.error.connect(lambda msg: _msg_critical(self, tr("dialogs.main_window.error"), msg))
        _track(w).start()

    def _manual_refresh(self, *_):
        self.refresh(force_full=True)

    def refresh_status(self):
        if self._status_refresh_running:
            return
        self._status_refresh_running = True
        w = StatusWorker(self.rpc, txids=self._fast_status_txids())
        self._threads.append(w)
        w.finished.connect(lambda: self._threads.remove(w) if w in self._threads else None)
        w.done.connect(self._on_status_done)
        w.error.connect(self._on_status_err)
        _track(w).start()

    def _on_status_done(self, data: dict):
        self._status_refresh_running = False
        self._status_rpc_failures = 0
        chain = data.get("chain", {}) if isinstance(data, dict) else {}
        blocks = chain.get("blocks", "-")
        peers = data.get("peers", "-") if isinstance(data, dict) else "-"
        try:
            self._cur_blocks = int(blocks)
        except Exception:
            pass
        self.lbl_blocks.setText(tr("dialogs.main_window.blocks", value=blocks))
        self.lbl_peers.setText(tr("dialogs.main_window.peers", value=peers))
        self._apply_fast_block_state(chain, data.get("tx_updates", []) if isinstance(data, dict) else [])

        if self._rescan_status_active:
            return

        vp = chain.get("verificationprogress")
        if vp is None:
            self._set_status_visual("connected", "  " + tr("dialogs.main_window.connected"))
            self._set_sync_visual("idle", value=0, text="-")
            return
        try:
            pct = max(0.0, min(100.0, float(vp) * 100))
        except Exception:
            pct = 0.0
        syncing = bool(chain.get("initialblockdownload") or chain.get("reindex")) or pct < 99.9
        self._set_status_visual(
            "syncing" if syncing else "connected",
            "  " + (tr("dialogs.main_window.synchronizing") if syncing else tr("dialogs.main_window.connected")),
        )
        self._set_sync_visual(
            "syncing" if syncing else "synced",
            value=int(pct * 100),
            text=tr("dialogs.main_window.synchronization", percent=pct)
            if syncing else f"{tr('dialogs.main_window.connected')}  {pct:.2f}%",
        )

    def _on_status_err(self, msg: str):
        self._status_refresh_running = False
        self._status_rpc_failures += 1
        if self._status_rpc_failures < 3:
            return
        if self._data:
            last_sync = fmt_ts(self._last_sync_ts) if self._last_sync_ts else "-"
            self._set_status_visual("offline", "  " + tr("dialogs.main_window.offline_cached"))
            self._set_sync_visual("offline", value=0, text=tr("dialogs.main_window.offline_last_sync", value=last_sync))
        else:
            self._set_status_visual("offline", "  " + tr("dialogs.main_window.not_connected"))
            self._set_sync_visual("offline", value=0, text=tr("dialogs.main_window.not_connected"))

    def refresh(self, force_full: bool = False):
        if self._refresh_running: return
        self._refresh_running = True
        self._update_wallet_key_actions()
        w = RefreshWorker(self.rpc, self.cache, force_full=force_full)
        self._threads.append(w)
        w.finished.connect(lambda: self._threads.remove(w) if w in self._threads else None)
        w.step.connect(lambda s: None)
        w.done.connect(self._on_done)
        w.error.connect(self._on_err)
        w.reindexing.connect(self._on_reindexing)
        _track(w).start()

    def _on_done(self, data: dict):
        self._refresh_running = False
        self._status_rpc_failures = 0
        self._apply_wallet_data(data, cached=False)
        self._run_reconciliation(data)
        self._update_wallet_key_actions()

    def _on_err(self, msg: str):
        self._refresh_running = False
        self._wallet_synced = False
        self._update_wallet_key_actions()
        rescan = read_recent_wallet_rescan_state()
        if rescan is not None:
            block = int(rescan.get("block", 0) or 0)
            self._set_status_visual("syncing", "  " + tr("dialogs.main_window.wallet_rescan_in_progress"))
            self.lbl_blocks.setText(tr("dialogs.main_window.blocks_rescanning", suffix=(" @ " + str(block)) if block else ""))
            self._set_sync_visual(
                "syncing",
                value=0,
                text=tr("dialogs.main_window.wallet_rescan_in_progress")
            )
            return
        if self._data and is_port_open(self.rpc.host, self.rpc.port, timeout=0.5):
            last_sync = fmt_ts(self._last_sync_ts) if self._last_sync_ts else "-"
            self._set_status_visual("syncing", "  " + tr("dialogs.main_window.node_busy_cached"))
            self._set_sync_visual(
                "syncing",
                value=0,
                text=tr("dialogs.main_window.cached_last_sync", value=last_sync),
            )
            return
        self._set_status_visual("offline", "  " + (tr("dialogs.main_window.offline_cached") if self._data else tr("dialogs.main_window.not_connected")))
        last_sync = fmt_ts(self._last_sync_ts) if self._last_sync_ts else "-"
        self._set_sync_visual("offline", value=0, text=tr("dialogs.main_window.offline_last_sync", value=last_sync) if self._data else tr("dialogs.main_window.not_connected"))
        if not self._data:
            if _msg(
                self,
                tr("dialogs.main_window.connection_failed"),
                tr("dialogs.main_window.connection_failed_message") + "\n\n" + msg,
                kind="warning",
                buttons=[
                    (tr("common.buttons.ok"), int(QMessageBox.StandardButton.Ok)),
                    (tr("dialogs.main_window.diagnostics"), 1001),
                ],
                default_button=1001,
            ) == 1001:
                self._open_diag()

    def _on_reindexing(self, data: dict):
        self._refresh_running = False
        self._wallet_synced = False
        self._update_wallet_key_actions()
        info  = data.get("info",  {})
        chain = data.get("chain", {})
        blocks  = info.get("blocks",  "-")
        headers = info.get("headers", "-")
        peers   = info.get("connections", "-")

        self._set_status_visual("syncing", "  " + tr("dialogs.main_window.synchronizing"))
        self.lbl_blocks.setText(tr("dialogs.main_window.blocks", value=f"{blocks}"))
        self.lbl_peers.setText(tr("dialogs.main_window.peers", value=peers))

        vp = chain.get("verificationprogress")
        if vp is not None:
            pct = float(vp) * 100
        elif str(headers).isdigit() and str(blocks).isdigit():
            h = int(headers); b = int(blocks)
            pct = (b / h * 100) if h > 0 else 0.0
        else:
            pct = 0.0

        self._set_sync_visual("syncing", value=int(pct * 100), text=tr("dialogs.main_window.synchronization", percent=pct))

    def _run_reconciliation(self, data: dict):
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

    def _tx_header_click(self, col: int):
        mapping = {0: "date", 2: "status", 3: "amount"}
        sort_key = mapping.get(col)
        if sort_key:
            self._set_tx_sort(sort_key)

    def _set_tx_sort(self, sort_key: str):
        self._tx_model.set_sort(sort_key)
        arrow = "\u2193" if self._tx_model.desc else "\u2191"
        label_map = {"date": tr("dialogs.main_window.sort_date"), "status": tr("dialogs.main_window.sort_status"), "amount": tr("dialogs.main_window.sort_amount")}
        base = label_map.get(self._tx_model.sort_key, tr("dialogs.main_window.sort_date"))
        self.btn_sort.setText(f"{arrow} {base}")

    def _fill_tx(self, txs: list):
        selected = self._selected_txid()
        scroll = self._view_scroll_state(self.tbl_tx)
        self._tx_model.set_transactions(txs or [])
        self._restore_tx_selection(selected)
        self._restore_view_scroll_state(self.tbl_tx, scroll)

    def _tx_double_click(self, index):
        tx = self._tx_model.transaction_at(index.row())
        if tx:
            TxDetailDialog(self, tx, self.rpc).exec()

    def _tx_ctx(self, pos):
        index = self.tbl_tx.indexAt(pos)
        if not index.isValid(): return
        tx = self._tx_model.transaction_at(index.row())
        if not tx: return
        txid = str(tx.get("txid", "") or "")
        addr_to_copy = str(tx.get("address", "") or "").strip()
        m = QMenu(self)
        a_det  = m.addAction(tr("dialogs.main_window.tx_details"))
        a_exp = m.addAction(tr("dialogs.tx_details.view_on_explorer"))
        m.addSeparator()
        a_copy = m.addAction(tr("dialogs.main_window.copy_txid"))
        a_addr = m.addAction(tr("dialogs.main_window.copy_address"))
        a_addr.setEnabled(bool(addr_to_copy))
        act = m.exec(self.tbl_tx.viewport().mapToGlobal(pos))
        if act == a_det:
            TxDetailDialog(self, tx, self.rpc).exec()
        elif act == a_exp and txid:
            QDesktopServices.openUrl(QUrl(f"https://explorer.btcz.zelcore.io/tx/{txid}"))
        elif act == a_copy:
            QApplication.clipboard().setText(txid)
        elif act == a_addr and addr_to_copy:
            QApplication.clipboard().setText(addr_to_copy)

    def _set_rescan_status_active(self, active: bool):
        self._rescan_status_active = active
        if active:
            self._set_status_visual("syncing", "  " + tr("dialogs.main_window.wallet_rescan_in_progress"))
            self._set_sync_visual("syncing", value=0, text=tr("dialogs.main_window.wallet_rescan_in_progress"))
        self._update_wallet_key_actions()

    def _do_send(self):
        self._clamp_fee_to_node_limit()
        send_flow.do_send(self)

    def _cache_upsert_send_operation(self, opid: str, status: str):
        send_flow.cache_upsert_send_operation(self, opid, status)

    def _cache_update_send_operation(self, status: str, *, txid: str | None = None,
                                     error: str | None = None, result=None):
        send_flow.cache_update_send_operation(self, status, txid=txid, error=error, result=result)

    def _send_ok(self, opid: str):
        send_flow.send_ok(self, opid)

    def _poll_status(self, status: str):
        send_flow.poll_status(self, status)

    def _poll_success(self, txid: str):
        send_flow.poll_success(self, txid)

    def _poll_failed(self, msg: str):
        send_flow.poll_failed(self, msg)

    def _send_err(self, msg: str):
        send_flow.send_err(self, msg)
