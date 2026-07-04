from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .common import (
    _SINGLE_INSTANCE_KEY,
    load_rpc_cfg,
    DATA_DIR,
    EXPORT_DIR,
    resource_path,
    wallet_version,
)
from .debug_runtime import debug_exception, debug_log, init_debug_logging, install_qt_debug_logging
from .rpc import BitcoinZRPC
from .wallet_cache import WalletCache
from .wallet_export import cleanup_wallet_dump_artifacts
from .main_window import MainWindow
from .ui import QSS, StartupScreen
from .version import APP_NAME, PRODUCT_VERSION

def main():
    debug_path = init_debug_logging()
    debug_log("Entered app.main", debug_log_path=str(debug_path) if debug_path else None)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    install_qt_debug_logging()
    _base_font = QFont("Segoe UI", 10)
    app.setFont(_base_font)
    app.setStyleSheet(QSS)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(PRODUCT_VERSION)
    app.setWindowIcon(QIcon(resource_path("icons/bitcoinz.ico")))
    debug_log(
        "QApplication initialized",
        app_name=APP_NAME,
        app_version=PRODUCT_VERSION,
        icon_path=resource_path("icons/bitcoinz.ico"),
    )

    probe = QLocalSocket()
    debug_log("Checking single instance server", key=_SINGLE_INSTANCE_KEY)
    probe.connectToServer(_SINGLE_INSTANCE_KEY)
    if probe.waitForConnected(500):
        debug_log("Existing instance detected, sending raise signal")
        probe.write(b"raise"); probe.flush()
        probe.disconnectFromServer()
        sys.exit(0)
    probe.close()

    server = QLocalServer()
    QLocalServer.removeServer(_SINGLE_INSTANCE_KEY)
    server.listen(_SINGLE_INSTANCE_KEY)
    debug_log("Single instance server listening", key=_SINGLE_INSTANCE_KEY)

    cfg = load_rpc_cfg()
    cleanup_wallet_dump_artifacts(EXPORT_DIR, DATA_DIR)
    debug_log(
        "RPC config loaded",
        host=cfg.get("host"),
        port=cfg.get("port"),
        user=cfg.get("user"),
        password_present=bool(cfg.get("password")),
        conf_path=cfg.get("conf_path"),
        conf_found=cfg.get("conf_found"),
        exportdir_configured=bool(cfg.get("exportdir")),
    )
    rpc = BitcoinZRPC(cfg["host"], cfg["port"], cfg["user"], cfg["password"])
    try:
        wallet_cache = WalletCache.open_default()
        wallet_cache.set_state("wallet_version", str(wallet_version))
        debug_log("Wallet cache opened", cache_path=str(getattr(wallet_cache, "path", "")))
    except Exception:
        debug_exception("Wallet cache initialization failed")
        wallet_cache = None

    splash = StartupScreen(rpc)
    debug_log("Startup screen created")
    main_w = None

    def open_main():
        nonlocal main_w
        debug_log("Opening main window")
        main_w = MainWindow(rpc, wallet_cache)

        def _on_new_connection():
            conn = server.nextPendingConnection()
            debug_log("Received raise-window IPC connection", has_connection=bool(conn))
            if conn:
                conn.waitForReadyRead(200)
                conn.readAll()
                conn.disconnectFromServer()
            if main_w:
                main_w.raise_window.emit()

        server.newConnection.connect(_on_new_connection)
        main_w.show()
        splash.close()

    splash.ready.connect(open_main)
    splash.show()
    debug_log("Startup screen shown, entering event loop")
    try:
        sys.exit(app.exec())
    finally:
        debug_log("Application exiting")
        if wallet_cache is not None:
            try:
                wallet_cache.close()
                debug_log("Wallet cache closed")
            except Exception:
                debug_exception("Wallet cache close failed")
                pass


if __name__ == "__main__":
    main()
