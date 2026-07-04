from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import qInstallMessageHandler

_APP_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "BitcoinZ"
_DEBUG_LOG_PATH = _DATA_DIR / "zsend_debug.log"
_BUILD_MARKER_NAME = "_zsend_build_mode.json"

_DEBUG_LOGGER = logging.getLogger("zsend.dev")
_DEBUG_LOG_INIT = False
_QT_MSG_HANDLER_INSTALLED = False
_STD_STREAMS_REDIRECTED = False


def _resource_path(relative_path: str) -> Path:
    try:
        base_path = Path(sys._MEIPASS)
    except Exception:
        base_path = _APP_ROOT
    return base_path / relative_path


def _safe_repr(value, limit: int = 400) -> str:
    try:
        text = repr(value)
    except Exception:
        text = f"<unreprable {type(value).__name__}>"
    if len(text) > limit:
        return text[:limit] + "...<trimmed>"
    return text


def _sanitize_log_text(text: str) -> str:
    text = str(text)
    text = re.sub(r"(?i)(rpcpassword\s*[=:]\s*)\S+", r"\1***", text)
    text = re.sub(r"(?i)(rpcuser\s*[=:]\s*)\S+", r"\1***", text)
    text = re.sub(r"(?i)(['\"]rpcpassword['\"]\s*:\s*['\"])[^'\"]+(['\"])", r"\1***\2", text)
    text = re.sub(r"(?i)(['\"]rpcuser['\"]\s*:\s*['\"])[^'\"]+(['\"])", r"\1***\2", text)
    text = re.sub(
        r"(?i)(?:[A-Za-z]:)?[\\/][^\s'\"<>|]*[\\/]ZSendWallet[\\/]secure(?:[\\/][^\s'\"<>|]*)?",
        "***wallet-dump-path***",
        text,
    )
    text = re.sub(
        r"\b(?:zsend_import_[A-Za-z0-9_]+\.dump|zsend_export_[A-Za-z0-9_]+\.dump|zsendexport[A-Za-z0-9]+|ZSendWalletExport[A-Za-z0-9]*)\b",
        "***wallet-dump***",
        text,
    )
    text = re.sub(
        r"\bsecret-extended-key-(?:main|test|regtest)[0-9A-Za-z]+\b",
        "***shielded-secret***",
        text,
    )
    text = re.sub(r"\b(?:SK|ST)[0-9A-Za-z]{20,}\b", "***shielded-secret***", text)
    text = re.sub(r"\b[59cKL][1-9A-HJ-NP-Za-km-z]{50,52}\b", "***private-key***", text)
    return text


class _LoggerWriter:

    def __init__(self, level: int):
        self.level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                _DEBUG_LOGGER.log(self.level, "[stream] %s", _sanitize_log_text(line))
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            _DEBUG_LOGGER.log(self.level, "[stream] %s", _sanitize_log_text(self._buffer.strip()))
        self._buffer = ""


def read_build_marker() -> dict:
    try:
        marker_path = _resource_path(_BUILD_MARKER_NAME)
        if not marker_path.exists():
            return {}
        return json.loads(marker_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_builder_debug_enabled() -> bool:
    marker = read_build_marker()
    return bool(marker.get("debug_logging"))


def debug_log(message: str, **fields) -> None:
    if not _DEBUG_LOG_INIT:
        return
    if fields:
        details = " ".join(f"{key}={_sanitize_log_text(_safe_repr(value))}" for key, value in fields.items())
        _DEBUG_LOGGER.debug("%s | %s", message, details)
    else:
        _DEBUG_LOGGER.debug("%s", message)


def debug_exception(context: str, exc: Exception | None = None) -> None:
    if not _DEBUG_LOG_INIT:
        return
    safe_context = _sanitize_log_text(context)
    if exc is None:
        _DEBUG_LOGGER.error("%s | %s", safe_context, _sanitize_log_text(traceback.format_exc()))
    else:
        _DEBUG_LOGGER.error(
            "%s | type=%s message=%s",
            safe_context,
            type(exc).__name__,
            _sanitize_log_text(str(exc)),
        )


def install_qt_debug_logging() -> None:
    global _QT_MSG_HANDLER_INSTALLED
    if not _DEBUG_LOG_INIT or _QT_MSG_HANDLER_INSTALLED:
        return

    def _qt_handler(mode, context, message):
        try:
            file_name = getattr(context, "file", "") or ""
            line_no = getattr(context, "line", 0) or 0
            function_name = getattr(context, "function", "") or ""
            _DEBUG_LOGGER.debug(
                "Qt message | mode=%s file=%s line=%s function=%s message=%s",
                int(mode),
                file_name,
                line_no,
                function_name,
                message,
            )
        except Exception:
            pass

    qInstallMessageHandler(_qt_handler)
    _QT_MSG_HANDLER_INSTALLED = True
    debug_log("Qt message handler installed")


def init_debug_logging() -> Path | None:
    global _DEBUG_LOG_INIT, _STD_STREAMS_REDIRECTED
    if _DEBUG_LOG_INIT:
        return _DEBUG_LOG_PATH
    if not is_builder_debug_enabled():
        return None

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(_DEBUG_LOG_PATH, mode="a", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname)s] [pid=%(process)d tid=%(thread)d] %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    _DEBUG_LOGGER.handlers.clear()
    _DEBUG_LOGGER.addHandler(handler)
    _DEBUG_LOGGER.setLevel(logging.DEBUG)
    _DEBUG_LOGGER.propagate = False
    _DEBUG_LOG_INIT = True

    def _log_excepthook(exc_type, exc_value, exc_tb):
        formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _DEBUG_LOGGER.error("Unhandled exception | %s", _sanitize_log_text(formatted))

    def _thread_excepthook(args):
        formatted = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        _DEBUG_LOGGER.error(
            "Unhandled thread exception | thread=%s traceback=%s",
            getattr(args, "thread", None),
            _sanitize_log_text(formatted),
        )

    sys.excepthook = _log_excepthook
    threading.excepthook = _thread_excepthook

    if not _STD_STREAMS_REDIRECTED:
        sys.stdout = _LoggerWriter(logging.INFO)
        sys.stderr = _LoggerWriter(logging.ERROR)
        _STD_STREAMS_REDIRECTED = True

    marker = read_build_marker()
    debug_log("===== ZSend debug session started =====")
    debug_log(
        "Runtime environment",
        frozen=bool(getattr(sys, "frozen", False)),
        executable=sys.executable,
        argv=sys.argv,
        cwd=os.getcwd(),
        app_root=str(_APP_ROOT),
        resource_root=str(getattr(sys, "_MEIPASS", _APP_ROOT)),
        python=sys.version,
        platform=platform.platform(),
        marker=marker,
    )
    return _DEBUG_LOG_PATH
