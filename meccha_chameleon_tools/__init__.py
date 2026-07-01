#!/usr/bin/env python3
"""
MECCHA CHAMELEON Box ESP — Entry Point
Fully external box ESP for MECCHA CHAMELEON (Steam / UE5.6).
"""
import sys
import os
import ctypes

from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout
from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QFont

# Re-export for backward compatibility with debug scripts
from meccha_chameleon_tools.core import (
    MecchaESP, rp, ru32, ru16, rfloat, wfloat, rvec3, rvec3_f,
    read_array, read_tarray_ptr, dist, OFFSETS,
    PatternScanner, FNameResolver, UObjectArray, OffsetResolver,
)
from meccha_chameleon_tools.config import Config, load_config, save_config, CONFIG_FILE
from meccha_chameleon_tools.ui import Menu, Overlay


def _set_dpi_aware():
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _enable_camouflage(config):
    """Camouflage is always on — no startup prompt."""
    config.camouflage_enabled = True
    return config


class GameWaitWindow(QWidget):
    """Wait for PenguinHotel-Win64-Shipping.exe, then hand off to the main UI."""

    POLL_MS = 800

    def __init__(self):
        super().__init__()
        self._on_ready = None
        self.setWindowTitle("MECCHA CHAMELEON TOOLS")
        self.setFixedSize(460, 160)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Waiting for MECCHA CHAMELEON…")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        title.setFont(title_font)
        self.status = QLabel(
            f"Start the game ({MecchaESP.PROCESS_NAME}).\n"
            "Peterhack will connect automatically."
        )
        self.status.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(self.status)
        self._dismissed = False
        self._timer = None

    def start(self, on_ready):
        self._on_ready = on_ready
        self._dismissed = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(self.POLL_MS)
        self._poll()

    def _set_status(self, text):
        self.status.setText(text)

    def _dismiss(self):
        if self._dismissed:
            return
        self._dismissed = True
        print("[UI] game detected — hiding wait window", flush=True)
        self.hide()

    def finish(self):
        """Remove wait window after the main UI is up."""
        self._timer.stop()
        self.hide()
        self.deleteLater()

    def _poll(self):
        if not MecchaESP.is_game_detected():
            if not self._dismissed:
                self._set_status(
                    f"Start the game ({MecchaESP.PROCESS_NAME}).\n"
                    "Peterhack will connect automatically."
                )
            return

        self._dismiss()

        if not MecchaESP.is_process_running():
            return

        try:
            esp = MecchaESP()
        except Exception as exc:
            err = str(exc)
            if "Could not find process" in err:
                return
            print(f"[UI] game loading, retrying Peterhack connect: {err}", flush=True)
            return

        self._timer.stop()
        if self._on_ready:
            self._on_ready(esp)


def _ensure_admin():
    """Re-launch with UAC elevation when not already running as administrator."""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:
        return

    import subprocess

    params = (
        subprocess.list2cmdline(sys.argv[1:])
        if len(sys.argv) > 1
        else "-m meccha_chameleon_tools"
    )
    cwd = os.getcwd()
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, cwd, 1,
    )
    if int(ret) <= 32:
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                "Peterhack needs administrator rights to attach to the game.\n\n"
                f"Elevation failed (code {int(ret)}).",
                "MECCHA CHAMELEON TOOLS",
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)
    sys.exit(0)


def main():
    _ensure_admin()
    from meccha_chameleon_tools.log_util import setup_file_logging

    setup_file_logging()
    _set_dpi_aware()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    from meccha_chameleon_tools.qt_util import configure_app_font
    configure_app_font(app)

    def _qt_message_handler(mode, context, message):
        msg = (message or "").strip()
        if not msg:
            return
        print(f"[QT] {msg}", flush=True)

    try:
        from PyQt5.QtCore import qInstallMessageHandler
        qInstallMessageHandler(_qt_message_handler)
    except Exception:
        pass

    config = load_config()
    config = _enable_camouflage(config)

    from meccha_chameleon_tools.webhook import notify_peterhack_launch
    notify_peterhack_launch(config)

    wait = GameWaitWindow()
    _esp_holder = []

    def _on_game_ready(esp):
        _esp_holder.append(esp)
        from meccha_chameleon_tools.webhook import bind_webhook_config
        bind_webhook_config(esp, config)
        menu = Menu(config, esp)
        overlay = Overlay(esp, config, menu=menu)
        menu.attach_overlay(overlay)
        overlay.show()
        menu.show()
        wait.finish()

    def _on_quit():
        save_config(config)
        if _esp_holder:
            try:
                _esp_holder[0].camo_cleanup()
            except Exception:
                pass

    app.aboutToQuit.connect(_on_quit)

    wait.start(_on_game_ready)
    wait.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
