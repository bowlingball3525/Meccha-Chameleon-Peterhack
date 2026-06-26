#!/usr/bin/env python3
"""Qt5 overlay and menu widgets for Peterhack ESP."""
import math
import ctypes
import sys
import os
from typing import Tuple, Optional

from PyQt5.QtWidgets import (
    QApplication, QWidget, QCheckBox, QComboBox, QLabel,
    QVBoxLayout, QHBoxLayout, QPushButton, QFrame, QColorDialog,
    QSpinBox, QDoubleSpinBox, QSlider, QListWidget, QStackedWidget,
    QFileDialog, QLineEdit,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QPainter, QPen, QColor, QFont, QBrush, QPolygonF, QImage
from PyQt5.QtCore import QPointF

from meccha_chameleon_tools.core import (
    MecchaESP, rp, ru32, rfloat, wfloat, wvec3, rvec3, rvec3_f, dist,
    read_array, OFFSETS,
)
from meccha_chameleon_tools.config import Config, save_config


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------
def rotation_to_axes(rot):
    pitch, yaw, roll = [math.radians(x) for x in rot]
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)
    sr, cr = math.sin(roll), math.cos(roll)
    forward = (cp * cy, cp * sy, sp)
    right = (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp)
    up = (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp)
    return forward, right, up


def w2s(world_pos, camera, screen_w, screen_h):
    """Project world pos to screen.
    Returns (sx, sy, on_screen).  on_screen is True only when the point is in
    front of the camera AND inside the viewport.  Otherwise (sx, sy) is clamped
    toward the screen edge for off-screen (OOF) indicators."""
    cam_loc = camera["loc"]
    cam_rot = camera["rot"]
    fov = camera["fov"]
    forward, right, up = rotation_to_axes(cam_rot)
    dx = world_pos[0] - cam_loc[0]
    dy = world_pos[1] - cam_loc[1]
    dz = world_pos[2] - cam_loc[2]
    view_x = dx * forward[0] + dy * forward[1] + dz * forward[2]
    view_y = dx * right[0]   + dy * right[1]   + dz * right[2]
    view_z = dx * up[0]      + dy * up[1]       + dz * up[2]

    behind = view_x <= 0.1
    if behind:
        view_x = -view_x or 0.1
        view_y = -view_y
        view_z = -view_z

    aspect   = screen_w / screen_h if screen_h > 0 else 16.0 / 9.0
    safe_fov = max(5.0, min(fov, 170.0))
    tan_hfov = math.tan(math.radians(safe_fov) / 2.0) or 1e-6
    ndc_x = view_y / (view_x * tan_hfov)
    ndc_y = view_z / (view_x * tan_hfov / aspect)
    if not math.isfinite(ndc_x):
        ndc_x = 0.0
    if not math.isfinite(ndc_y):
        ndc_y = 0.0
    screen_x = (1.0 + ndc_x) * screen_w / 2.0
    screen_y = (1.0 - ndc_y) * screen_h / 2.0

    cx, cy = screen_w / 2.0, screen_h / 2.0
    ex, ey = screen_x - cx, screen_y - cy
    edge_m = max(abs(ex) / max(screen_w / 2.0 - 24, 1),
                 abs(ey) / max(screen_h / 2.0 - 24, 1))
    if behind or edge_m > 1.0:
        if edge_m > 0:
            screen_x = cx + ex / edge_m
            screen_y = cy + ey / edge_m

    margin = 8
    on_screen = (
        (not behind)
        and (margin <= screen_x <= screen_w - margin)
        and (margin <= screen_y <= screen_h - margin)
    )
    return (screen_x, screen_y, on_screen)


def clamp_screen(x, y, w, h, margin=10):
    return max(margin, min(x, w - margin)), max(margin, min(y, h - margin))


def oof_indicator_pos(sx, sy, screen_w, screen_h, radius_px=0):
    """Place an off-screen indicator on a ring around screen center.

    radius_px = 0 sticks to the screen edge (uses sx/sy from w2s as-is).
    radius_px > 0 places the marker on a circle that many pixels from center.
    """
    cx, cy = screen_w / 2.0, screen_h / 2.0
    dx, dy = sx - cx, sy - cy
    length = math.sqrt(dx * dx + dy * dy) or 1.0
    ux, uy = dx / length, dy / length
    if radius_px <= 0:
        return int(sx), int(sy), ux, uy
    max_r = min(screen_w, screen_h) * 0.5 - 16
    r = min(float(radius_px), max_r)
    return int(cx + ux * r), int(cy + uy * r), ux, uy


# ---------------------------------------------------------------------------
# Key name mapping (shared between Menu and Overlay)
# ---------------------------------------------------------------------------
KEY_NAMES = {
    0x01: "LMB", 0x02: "RMB", 0x04: "MMB", 0x05: "MB4", 0x06: "MB5",
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift",
    0x11: "Ctrl", 0x12: "Alt", 0x13: "Pause", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
    0x30: "0", 0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4",
    0x35: "5", 0x36: "6", 0x37: "7", 0x38: "8", 0x39: "9",
    0x41: "A", 0x42: "B", 0x43: "C", 0x44: "D", 0x45: "E", 0x46: "F",
    0x47: "G", 0x48: "H", 0x49: "I", 0x4A: "J", 0x4B: "K", 0x4C: "L",
    0x4D: "M", 0x4E: "N", 0x4F: "O", 0x50: "P", 0x51: "Q", 0x52: "R",
    0x53: "S", 0x54: "T", 0x55: "U", 0x56: "V", 0x57: "W", 0x58: "X",
    0x59: "Y", 0x5A: "Z",
    0x60: "Num0", 0x61: "Num1", 0x62: "Num2", 0x63: "Num3", 0x64: "Num4",
    0x65: "Num5", 0x66: "Num6", 0x67: "Num7", 0x68: "Num8", 0x69: "Num9",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5",
    0x75: "F6", 0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10",
    0x7A: "F11", 0x7B: "F12",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}

KEY_VK = {v: k for k, v in KEY_NAMES.items()}


def vk_from_name(name):
    return KEY_VK.get(name, 0x2D)  # default Insert


def name_from_vk(vk):
    return KEY_NAMES.get(vk, f"VK_{vk:02X}")


WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
HK_MENU_INSERT = 1
HK_MENU_F1 = 2
HK_CAMO_F10 = 3


# ---------------------------------------------------------------------------
# Key recording helper
# ---------------------------------------------------------------------------
class KeyRecorder:
    def __init__(self, on_record):
        self.on_record = on_record
        self.active = False
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll)
        self._start_tick = 0

    def start(self):
        self.active = True
        self._start_tick = ctypes.windll.kernel32.GetTickCount()
        self._timer.start(50)

    def stop(self):
        self.active = False
        self._timer.stop()

    def _poll(self):
        elapsed = ctypes.windll.kernel32.GetTickCount() - self._start_tick
        if elapsed < 300:
            return
        for vk in range(1, 0x100):
            if ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000:
                name = name_from_vk(vk)
                self.stop()
                self.on_record(name)
                return
        if elapsed > 5000:
            self.stop()


# ---------------------------------------------------------------------------
# ESP drawing utilities
# ---------------------------------------------------------------------------
def draw_health_bar(painter, x, y, w, h, health_pct, shield_pct, spacing=2):
    """Draw stacked health (green top) and shield (blue bottom) bars."""
    bar_w = max(4, w)
    bar_h = 4
    # Shield bar (bottom)
    if shield_pct is not None and shield_pct > 0:
        sy = y + bar_h + spacing
        sfill = int(bar_w * min(shield_pct / 100.0, 1.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(30, 30, 30, 180))
        painter.drawRect(int(x), int(sy), int(bar_w), bar_h)
        painter.setBrush(QColor(0, 120, 255, 220))
        painter.drawRect(int(x), int(sy), int(sfill), bar_h)
    # Health bar (above)
    if health_pct is not None and health_pct >= 0:
        hy = y
        hfill = int(bar_w * min(health_pct / 100.0, 1.0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(30, 30, 30, 180))
        painter.drawRect(int(x), int(hy), int(bar_w), bar_h)
        pct_clamped = max(0.0, min(100.0, float(health_pct or 0)))
        r = int(255 * (1 - pct_clamped / 100.0))
        g = int(255 * (pct_clamped / 100.0))
        painter.setBrush(QColor(r, g, 0, 220))
        painter.drawRect(int(x), int(hy), int(hfill), bar_h)


def draw_2d_box(painter, pos, camera, screen_w, screen_h,
                height_world, half_width_world, rot, color, scale=1.0):
    """Draw a 2D bounding box around a world position with given rotation."""
    h = height_world * scale
    hw = half_width_world * scale
    corners_local = [
        (-hw, 0, -hw), (-hw, 0, hw), (hw, 0, hw), (hw, 0, -hw),
        (-hw, h, -hw), (-hw, h, hw), (hw, h, hw), (hw, h, -hw),
    ]
    pitch, yaw, _ = rot if rot else (0, 0, 0)
    yaw_rad = math.radians(yaw)
    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    screen_points = []
    for lx, ly, lz in corners_local:
        # Rotate around Y axis (yaw)
        rx = lx * cy - lz * sy
        rz = lx * sy + lz * cy
        wx = pos[0] + rx
        wy = pos[1] + ly
        wz = pos[2] + rz
        s = w2s((wx, wy, wz), camera, screen_w, screen_h)
        if s[2]:   # only on-screen corners make a valid 2D box
            screen_points.append(s[:2])
    if len(screen_points) < 4:
        return
    xs = [p[0] for p in screen_points]
    ys = [p[1] for p in screen_points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    # Draw connected lines for the 4 vertical edges
    painter.setPen(QPen(QColor(*color), 1))
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(int(min_x), int(min_y), int(max_x - min_x), int(max_y - min_y))


def draw_skeleton(painter, bone_positions, camera, screen_w, screen_h, color):
    """Draw skeleton lines connecting bones."""
    bone_screen = {}
    for name, pos in bone_positions.items():
        sx, sy, on_screen = w2s(pos, camera, screen_w, screen_h)
        if not (math.isfinite(sx) and math.isfinite(sy)):
            continue
        cx, cy = clamp_screen(sx, sy, screen_w, screen_h)
        bone_screen[name] = (cx, cy)
    connections = [
        ("pelvis", "spine_01"), ("spine_01", "spine_02"),
        ("spine_02", "spine_03"), ("spine_03", "neck_01"),
        ("neck_01", "head"),
        ("clavicle_l", "upperarm_l"), ("upperarm_l", "lowerarm_l"),
        ("lowerarm_l", "hand_l"),
        ("clavicle_r", "upperarm_r"), ("upperarm_r", "lowerarm_r"),
        ("lowerarm_r", "hand_r"),
        ("pelvis", "thigh_l"), ("thigh_l", "calf_l"), ("calf_l", "foot_l"),
        ("pelvis", "thigh_r"), ("thigh_r", "calf_r"), ("calf_r", "foot_r"),
    ]
    painter.save()
    painter.setPen(QPen(QColor(*color), 2))
    painter.setBrush(Qt.NoBrush)
    for a, b in connections:
        if a in bone_screen and b in bone_screen:
            x1, y1 = bone_screen[a]
            x2, y2 = bone_screen[b]
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))
    painter.restore()


def draw_radar(painter, cam, local_pos, players, radar_cx, radar_cy, radar_size, radar_range, color, opacity):
    """Draw a 2D radar overlay in the corner."""
    half = radar_size / 2
    painter.setPen(QPen(QColor(255, 255, 255, opacity), 1))
    painter.setBrush(QBrush(QColor(0, 0, 0, opacity)))
    painter.drawEllipse(int(radar_cx - half), int(radar_cy - half), radar_size, radar_size)
    # Crosshair
    painter.drawLine(int(radar_cx - half), int(radar_cy), int(radar_cx + half), int(radar_cy))
    painter.drawLine(int(radar_cx), int(radar_cy - half), int(radar_cx), int(radar_cy + half))
    # Draw local player at center
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 255, 0, 220))
    painter.drawEllipse(int(radar_cx - 2), int(radar_cy - 2), 5, 5)
    # Draw enemies
    cam_yaw = math.radians(cam["rot"][1])
    for p in players:
        pos = p["pos"]
        dx = pos[0] - local_pos[0]
        dz = pos[2] - local_pos[2]
        d2d = math.sqrt(dx * dx + dz * dz)
        if d2d > radar_range or d2d < 1.0:
            continue
        # Rotate by inverse camera yaw
        angle = math.atan2(dx, dz) - cam_yaw
        r = (d2d / radar_range) * (half - 8)
        rx = radar_cx + r * math.sin(angle)
        ry = radar_cy - r * math.cos(angle)
        color_rgba = QColor(*p.get("color", color), 220) if not p["is_local"] else QColor(0, 255, 0, 220)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color_rgba)
        painter.drawEllipse(int(rx - 2), int(ry - 2), 5, 5)


# ---------------------------------------------------------------------------
# Quality preset tables
# ---------------------------------------------------------------------------

# Image quality (1-5) — controls UV stamp grid for Apply Image.
_IMAGE_QUALITY_TABLE = {
    1: 64,    # Draft  — fastest, blocky
    2: 96,    # Low
    3: 128,   # Medium — default
    4: 192,   # High
    5: 256,   # Ultra  — sharpest
}

# Camo quality (1-20) — controls UV grid density and sub-sample count.
# (camo_sample_size G, camo_quality q)
#   UV stamps painted on body = G * G
#   Screen pixels sampled per stamp = q * q
# Higher level = finer colour resolution = more indistinguishable from environment.
_CAMO_QUALITY_TABLE = {
    1:  (8,   1),   # Draft      —    64 stamps
    2:  (12,  1),   # Draft+     —   144 stamps
    3:  (16,  1),   # Draft++    —   256 stamps
    4:  (20,  1),   # Low-       —   400 stamps
    5:  (24,  1),   # Low        —   576 stamps
    6:  (32,  2),   # Low+       — 1,024 stamps / 4 sub-samples
    7:  (40,  2),   # Medium-    — 1,600 stamps
    8:  (48,  2),   # Medium     — 2,304 stamps
    9:  (56,  2),   # Medium+    — 3,136 stamps
    10: (64,  3),   # High-      — 4,096 stamps / 9 sub-samples
    11: (80,  3),   # High       — 6,400 stamps
    12: (96,  3),   # High+      — 9,216 stamps
    13: (112, 4),   # Ultra-     — 12,544 stamps / 16 sub-samples
    14: (128, 4),   # Ultra      — 16,384 stamps
    15: (160, 4),   # Ultra+     — 25,600 stamps
    16: (192, 5),   # Max-       — 36,864 stamps / 25 sub-samples
    17: (224, 5),   # Max        — 50,176 stamps
    18: (256, 6),   # Max+       — 65,536 stamps / 36 sub-samples
    19: (384, 7),   # Extreme    — 147,456 stamps / 49 sub-samples
    20: (512, 8),   # God Mode   — 262,144 stamps / 64 sub-samples (photo-realistic)
}

_CAMO_QLABELS = {
    1: "Draft", 2: "Draft+", 3: "Draft++",
    4: "Low-",  5: "Low",    6: "Low+",
    7: "Medium-", 8: "Medium", 9: "Medium+",
    10: "High-",  11: "High",  12: "High+",
    13: "Ultra-", 14: "Ultra", 15: "Ultra+",
    16: "Max-",   17: "Max",   18: "Max+",
    19: "Extreme", 20: "God Mode",
}

def _quality_to_camo_settings(level: int):
    """Return (camo_sample_size, camo_quality) for the given quality level (1-20)."""
    row = _CAMO_QUALITY_TABLE.get(max(1, min(20, int(level))), _CAMO_QUALITY_TABLE[8])
    return row[0], row[1]

def _quality_to_image_grid(level: int) -> int:
    """Return the image stamp grid size for the given quality level (1-5)."""
    return _IMAGE_QUALITY_TABLE.get(max(1, min(5, int(level))), _IMAGE_QUALITY_TABLE[3])


# ---------------------------------------------------------------------------
# Menu widget
# ---------------------------------------------------------------------------
class Menu(QWidget):
    paint_job_finished = pyqtSignal(bool, str)
    paint_job_progress = pyqtSignal(int, int)

    # Peter pants green sampled from logo — RGB(32, 96, 16)
    PETER_GREEN       = "#206010"
    PETER_GREEN_HOVER = "#2a7818"
    PETER_GREEN_LIGHT = "#7ec850"
    PETER_GREEN_MID   = "#389020"
    BG_DARK           = "rgba(10, 16, 8, 242)"
    BG_PANEL          = "#141e10"
    BG_INPUT          = "#1a2814"
    BORDER            = "#2a4a1a"
    BORDER_FOCUS      = "#489020"

    STYLE = """
        QFrame {
            background-color: rgba(10, 16, 8, 242);
            border: 1px solid #2a4a1a;
            border-radius: 10px;
        }
        QLabel { color: #c8d8bc; font-size: 12px; }
        QCheckBox { color: #d4e4c8; font-size: 12px; spacing: 8px; padding: 2px 0; }
        QCheckBox::indicator { width: 16px; height: 16px; border-radius: 3px; border: 1px solid #3a5a28; background: #1a2814; }
        QCheckBox::indicator:checked {
            background: #206010; border-color: #389020;
        }
        QComboBox {
            background-color: #1a2814; color: #eee;
            border: 1px solid #3a5a28; padding: 4px;
        }
        QPushButton {
            background-color: #182412; color: #d4e4c8;
            border: 1px solid #2a4a1a; padding: 5px 10px; border-radius: 5px;
            font-size: 12px;
        }
        QPushButton:hover { background-color: #243a18; border-color: #389020; }
        QPushButton:pressed { background-color: #2a4a18; }
        QSpinBox, QDoubleSpinBox {
            background-color: #1a2814; color: #d4e4c8;
            border: 1px solid #2a4a1a; padding: 1px 3px; border-radius: 3px;
            font-size: 12px; min-height: 22px;
        }
        QSpinBox:focus, QDoubleSpinBox:focus { border-color: #489020; }
    """

    def __init__(self, config: Config, esp: MecchaESP):
        super().__init__()
        self.config = config
        self.esp = esp
        self.setWindowTitle("Peterhack | Meccha Chameleon")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag_pos = None
        self._key_recorder = KeyRecorder(self._on_key_recorded)
        self._paint_last_apply = 0.0
        self._paint_busy = False
        self.paint_job_finished.connect(self._finish_paint_job)
        self.paint_job_progress.connect(self._on_paint_job_progress)
        self._paint_watchdog = QTimer(self)
        self._paint_watchdog.setSingleShot(True)
        self._paint_watchdog.timeout.connect(self._paint_watchdog_timeout)
        self._overlay = None
        self._hotkeys_registered = False
        self._hotkeys_native = False
        # Seed legacy camo fields from the camo quality level on startup.
        _camo_sz, _camo_q = _quality_to_camo_settings(
            max(1, min(20, getattr(self.config, "paint_quality", 8)))
        )
        self.config.camouflage_sample_size = _camo_sz
        self.config.camouflage_quality     = _camo_q
        # image_quality is read directly by _apply_paint_image — no seeding needed.
        self._build_ui()
        self.setFixedSize(580, 820)

    def attach_overlay(self, overlay):
        self._overlay = overlay

    def _register_global_hotkeys(self):
        """RegisterHotKey works when Peterhack is elevated and the game is not."""
        if self._hotkeys_registered:
            return
        hwnd = int(self.winId())
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        ok_insert = bool(user32.RegisterHotKey(hwnd, HK_MENU_INSERT, MOD_NOREPEAT, 0x2D))
        ok_f1 = bool(user32.RegisterHotKey(hwnd, HK_MENU_F1, MOD_NOREPEAT, 0x70))
        ok_f10 = bool(user32.RegisterHotKey(hwnd, HK_CAMO_F10, MOD_NOREPEAT, 0x79))
        self._hotkeys_registered = ok_insert or ok_f1 or ok_f10
        self._hotkeys_native = ok_insert and ok_f1
        print(
            f"[UI] global hotkeys Insert={ok_insert} F1={ok_f1} F10={ok_f10} hwnd=0x{hwnd:X}",
            flush=True,
        )
        if not ok_insert:
            print(f"[UI] RegisterHotKey Insert err={ctypes.windll.kernel32.GetLastError()}", flush=True)

    def _unregister_global_hotkeys(self):
        if not self._hotkeys_registered:
            return
        hwnd = int(self.winId())
        if hwnd:
            user32 = ctypes.windll.user32
            for hid in (HK_MENU_INSERT, HK_MENU_F1, HK_CAMO_F10):
                user32.UnregisterHotKey(hwnd, hid)
        self._hotkeys_registered = False
        self._hotkeys_native = False

    def _toggle_menu_visibility(self):
        self.setVisible(not self.isVisible())
        if self.isVisible():
            self.raise_()
            self.activateWindow()

    def _on_hotkey_f10(self):
        if not self.config.camouflage_enabled or not self._overlay:
            return
        import time
        if time.monotonic() - self._overlay._camo_last_apply >= 3.0:
            self._overlay._toggle_camouflage()
        else:
            self._overlay._camo_feedback = "WAIT..."
            self._overlay._camo_feedback_count = 30

    def showEvent(self, event):
        super().showEvent(event)
        self._register_global_hotkeys()

    def closeEvent(self, event):
        self._unregister_global_hotkeys()
        super().closeEvent(event)

    def nativeEvent(self, eventType, message):
        if eventType in (b"windows_generic_MSG", "windows_generic_MSG"):
            import ctypes.wintypes
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                if msg.wParam in (HK_MENU_INSERT, HK_MENU_F1):
                    self._toggle_menu_visibility()
                    return True, 0
                if msg.wParam == HK_CAMO_F10:
                    self._on_hotkey_f10()
                    return True, 0
        return super().nativeEvent(eventType, message)

    # ------------------------------------------------------------------
    # Peter Griffin logo — async URL fetch
    # ------------------------------------------------------------------
    _PETER_URL = (
        "https://raw.githubusercontent.com/bowlingball3525/"
        "Meccha-Chameleon-Peterhack/refs/heads/main/peter.png"
    )

    def _start_peter_logo_fetch(self):
        """Download the Peter logo in a background thread; apply on main thread."""
        import threading as _threading
        _result = [None]
        _done = _threading.Event()

        def _fetch():
            try:
                import urllib.request as _ur
                req = _ur.Request(
                    self._PETER_URL,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                )
                with _ur.urlopen(req, timeout=10) as r:
                    _result[0] = r.read()
            except Exception as exc:
                print(f"[UI] Peter logo fetch failed: {exc}")
            _done.set()

        _threading.Thread(target=_fetch, daemon=True).start()

        # Poll every 200 ms on the main thread; apply once the data arrives.
        _timer = QTimer(self)
        _timer.setSingleShot(False)

        def _check():
            if not _done.is_set():
                return
            _timer.stop()
            data = _result[0]
            if not data:
                return
            from PyQt5.QtCore import QByteArray
            from PyQt5.QtGui import QPixmap
            pix = QPixmap()
            if pix.loadFromData(QByteArray(data)) and not pix.isNull():
                # Scale up to fill the logo slot; transparent PNG can bleed slightly.
                self._peter_lbl.setPixmap(
                    pix.scaled(96, 96, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                )

        _timer.timeout.connect(_check)
        _timer.start(200)

    # ------------------------------------------------------------------

    def _close_app(self):
        QApplication.quit()

    def _on_key_recorded(self, name):
        self.config.aimbot_key = name
        self.lbl_aim_key.setText(f"Aim Key: {name}")
        self.btn_record_key.setEnabled(True)
        self.btn_record_key.setText("Record Key")

    def _build_ui(self):
        import os as _os
        container = QFrame(self)
        container.setObjectName("menuFrame")
        container.setStyleSheet(self.STYLE)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)

        # ── Header: title left, Peter logo right ─────────────────────────────
        LOGO_SIZE = 88   # logo is square; title block matches this height exactly

        header = QHBoxLayout()
        header.setSpacing(8)
        header.setContentsMargins(0, 0, 0, 0)

        # Title block — two-line stacked label, same height as the logo so the
        # header row sits flush on both sides.
        title_widget = QWidget()
        title_widget.setFixedHeight(LOGO_SIZE)
        title_layout = QVBoxLayout(title_widget)
        title_layout.setContentsMargins(4, 0, 4, 0)
        title_layout.setSpacing(2)
        title_layout.setAlignment(Qt.AlignVCenter)

        lbl_main = QLabel("Peterhack")
        lbl_main.setObjectName("titleLbl")
        lbl_main.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_main.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {self.PETER_GREEN_LIGHT};"
            " letter-spacing: 1px; border: none; background: transparent;"
        )

        lbl_sub = QLabel("Meccha Chameleon")
        lbl_sub.setObjectName("subtitleLbl")
        lbl_sub.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lbl_sub.setStyleSheet(
            "font-size: 11px; font-weight: normal; color: #7a9a6a;"
            " letter-spacing: 0.5px; border: none; background: transparent;"
        )

        title_layout.addStretch(1)
        title_layout.addWidget(lbl_main)
        title_layout.addWidget(lbl_sub)
        title_layout.addStretch(1)

        header.addWidget(title_widget, 1)

        # Peter Griffin logo (top-right) — fetched from URL in a background thread.
        peter_lbl = QLabel()
        peter_lbl.setFixedSize(LOGO_SIZE, LOGO_SIZE)
        peter_lbl.setAlignment(Qt.AlignCenter)
        peter_lbl.setScaledContents(True)
        self._peter_lbl = peter_lbl   # keep reference for async update
        header.addWidget(peter_lbl)
        # Kick off a background fetch; result is applied on the main thread via timer.
        self._start_peter_logo_fetch()

        outer.addLayout(header)

        # Tab list + stacked pages
        body = QHBoxLayout()
        body.setSpacing(10)

        self.tab_list = QListWidget()
        self.tab_list.setFixedWidth(112)   # fits CAMOUFLAGE without scrollbar
        self.tab_list.setFocusPolicy(Qt.NoFocus)
        # Hide both scrollbars so they never eat into item text width
        self.tab_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tab_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tab_list.setStyleSheet(f"""
            QListWidget {{
                background: {self.BG_INPUT}; border: 1px solid {self.BORDER};
                border-radius: 6px; padding: 4px; outline: none;
            }}
            QListWidget::item {{
                color: #7a9a6a; padding: 9px 4px; border-radius: 4px;
                font-size: 12px; font-weight: bold; text-align: center;
            }}
            QListWidget::item:selected {{
                background: #2a4a18; color: {self.PETER_GREEN_LIGHT};
            }}
            QListWidget::item:hover:!selected {{
                background: #243a18; color: #a8c898;
            }}
            QScrollBar:vertical {{ width: 0px; }}
            QScrollBar:horizontal {{ height: 0px; }}
        """)
        self.tab_list.addItems(["VISUALS","RADAR","AIMBOT","COLORS","CAMOUFLAGE","CHANGELOG"])
        self.tab_list.currentRowChanged.connect(self._switch_tab)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")

        self._pages = {}
        for tab_name in ["VISUALS","RADAR","AIMBOT","COLORS","CAMOUFLAGE","CHANGELOG"]:
            page = QWidget()
            page.setStyleSheet("background: transparent;")
            self._pages[tab_name] = page
            self.stack.addWidget(page)

        body.addWidget(self.tab_list)
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)

        # Bottom bar
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_save = QPushButton("Save Config")
        self.btn_save.clicked.connect(self._save_config)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self._close_app)
        self.btn_close.setStyleSheet("QPushButton { background-color: #3a1a1a; border-color: #5a2a2a; } QPushButton:hover { background-color: #5a2a2a; }")

        hint = QLabel("Ins/F1 toggle | Drag to move")
        hint.setStyleSheet("color: #5a7a4a; font-size: 9px;")
        bar.addWidget(self.btn_save)
        bar.addWidget(self.btn_close)
        bar.addStretch()
        bar.addWidget(hint)
        outer.addLayout(bar)

        outer2 = QVBoxLayout(self)
        outer2.addWidget(container)
        outer2.setContentsMargins(0, 0, 0, 0)
        self.setLayout(outer2)

        # Build each tab page
        self._build_visuals_tab()
        self._build_radar_tab()
        self._build_aimbot_tab()
        self._build_colors_tab()
        self._build_camouflage_tab()
        self._build_changelog_tab()

    def _switch_tab(self, idx):
        names = ["VISUALS","RADAR","AIMBOT","COLORS","CAMOUFLAGE","CHANGELOG"]
        if 0 <= idx < len(names):
            self.stack.setCurrentIndex(idx)

    def _build_visuals_tab(self):
        p = self._pages["VISUALS"]
        lo = QVBoxLayout(p)
        lo.setContentsMargins(4, 4, 4, 4)
        lo.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.cb_dot = self._chk("Dot","dot_esp")
        self.cb_box = self._chk("2D Box","box_esp")
        self.cb_skeleton = self._chk("Skeleton","skeleton_esp")
        row.addWidget(self.cb_dot)
        row.addWidget(self.cb_box)
        row.addWidget(self.cb_skeleton)
        lo.addLayout(row)
        for cfg, label in [("show_local","Show Local Player"), ("show_names","Show Names"),
                           ("show_distance","Show Distance"), ("snap_lines","Snap Lines"),
                           ("team_filter","Team Filter"), ("distance_scaling","Dist. Scaling")]:
            cb = self._chk(label, cfg)
            lo.addWidget(cb)
        dr = QHBoxLayout()
        dr.addWidget(QLabel("Dot Radius:"))
        self.spn_dot = QSpinBox()
        self.spn_dot.setRange(2, 32)
        self.spn_dot.setValue(self.config.dot_radius)
        self.spn_dot.valueChanged.connect(lambda v: setattr(self.config, "dot_radius", v))
        dr.addWidget(self.spn_dot)
        lo.addLayout(dr)
        # Health ESP (merged from old Health tab)
        lo.addWidget(QLabel("Health ESP"))
        self.cb_hp = self._chk("Health Bar","health_bar")
        self.cb_shield = self._chk("Shield Bar","shield_bar")
        lo.addWidget(self.cb_hp)
        lo.addWidget(self.cb_shield)
        hr = QHBoxLayout()
        hr.addWidget(QLabel("Model Height:"))
        self.spn_height = QSpinBox()
        self.spn_height.setRange(50, 250)
        self.spn_height.setValue(int(self.config.box_height_world))
        self.spn_height.valueChanged.connect(lambda v: setattr(self.config, "box_height_world", float(v)))
        hr.addWidget(self.spn_height)
        lo.addLayout(hr)
        yr = QHBoxLayout()
        yr.addWidget(QLabel("Y Offset:"))
        self.spn_yoff = QSpinBox()
        self.spn_yoff.setRange(-50, 50)
        self.spn_yoff.setValue(self.config.box_y_offset)
        self.spn_yoff.valueChanged.connect(lambda v: setattr(self.config, "box_y_offset", v))
        yr.addWidget(self.spn_yoff)
        lo.addLayout(yr)
        # OOF arrows
        lo.addWidget(QLabel("OOF Arrows"))
        oof_row = QHBoxLayout()
        oof_row.addWidget(QLabel("Radius:"))
        self.sld_oof = QSlider(Qt.Horizontal)
        self.sld_oof.setRange(0, 1000)
        self.sld_oof.setValue(self.config.oof_arrow_radius)
        self.lbl_oof = QLabel(self._oof_radius_label(self.config.oof_arrow_radius))
        self.lbl_oof.setMinimumWidth(52)
        self.sld_oof.valueChanged.connect(self._on_oof_radius_changed)
        oof_row.addWidget(self.sld_oof, 1)
        oof_row.addWidget(self.lbl_oof)
        lo.addLayout(oof_row)
        oof_lbl_row = QHBoxLayout()
        oof_lbl_row.setSpacing(6)
        self.cb_oof_names = self._chk("Names","oof_show_names")
        self.cb_oof_dist = self._chk("Distance","oof_show_distance")
        self.cb_oof_hp = self._chk("Health #","oof_show_health")
        oof_lbl_row.addWidget(self.cb_oof_names)
        oof_lbl_row.addWidget(self.cb_oof_dist)
        oof_lbl_row.addWidget(self.cb_oof_hp)
        lo.addLayout(oof_lbl_row)
        lo.addStretch()

    def _build_radar_tab(self):
        p = self._pages["RADAR"]
        lo = QVBoxLayout(p)
        lo.setContentsMargins(4, 4, 4, 4)
        lo.setSpacing(4)
        self.cb_radar = self._chk("Radar Enabled","radar_enabled")
        lo.addWidget(self.cb_radar)
        sr = QHBoxLayout()
        sr.addWidget(QLabel("Radar Size:"))
        self.spn_radar_size = QSpinBox()
        self.spn_radar_size.setRange(80, 400)
        self.spn_radar_size.setValue(self.config.radar_size)
        self.spn_radar_size.valueChanged.connect(lambda v: setattr(self.config, "radar_size", v))
        sr.addWidget(self.spn_radar_size)
        lo.addLayout(sr)
        rr = QHBoxLayout()
        rr.addWidget(QLabel("Radar Range:"))
        self.spn_radar_range = QSpinBox()
        self.spn_radar_range.setRange(1000, 50000)
        self.spn_radar_range.setSingleStep(500)
        self.spn_radar_range.setValue(int(self.config.radar_range))
        self.spn_radar_range.valueChanged.connect(lambda v: setattr(self.config, "radar_range", float(v)))
        rr.addWidget(self.spn_radar_range)
        lo.addLayout(rr)
        lo.addStretch()

    def _build_aimbot_tab(self):
        p = self._pages["AIMBOT"]
        lo = QVBoxLayout(p)
        lo.setContentsMargins(4, 4, 4, 4)
        lo.setSpacing(4)
        self.cb_aimbot = self._chk("Aimbot Enabled","aimbot_enabled")
        self.cb_aim_fov = self._chk("Show FOV Circle","aimbot_show_fov")
        lo.addWidget(self.cb_aimbot)
        lo.addWidget(self.cb_aim_fov)
        kr = QHBoxLayout()
        self.lbl_aim_key = QLabel("Aim Key: " + self.config.aimbot_key)
        self.btn_record_key = QPushButton("Record Key")
        self.btn_record_key.clicked.connect(self._start_aim_key_record)
        kr.addWidget(self.lbl_aim_key)
        kr.addWidget(self.btn_record_key)
        lo.addLayout(kr)
        fr = QHBoxLayout()
        fr.addWidget(QLabel("FOV Radius:"))
        self.spn_aim_fov = QSpinBox()
        self.spn_aim_fov.setRange(10, 600)
        self.spn_aim_fov.setValue(self.config.aimbot_fov)
        self.spn_aim_fov.valueChanged.connect(lambda v: setattr(self.config, "aimbot_fov", v))
        fr.addWidget(self.spn_aim_fov)
        lo.addLayout(fr)
        sr = QHBoxLayout()
        sr.addWidget(QLabel("Smooth:"))
        self.spn_aim_smooth = QDoubleSpinBox()
        self.spn_aim_smooth.setRange(0.01, 1.0)
        self.spn_aim_smooth.setSingleStep(0.05)
        self.spn_aim_smooth.setValue(self.config.aimbot_smooth)
        self.spn_aim_smooth.valueChanged.connect(lambda v: setattr(self.config, "aimbot_smooth", v))
        sr.addWidget(self.spn_aim_smooth)
        lo.addLayout(sr)
        ar = QHBoxLayout()
        ar.addWidget(QLabel("Chest Offset (fallback):"))
        self.spn_aim_off = QSpinBox()
        self.spn_aim_off.setRange(-200, 200)
        self.spn_aim_off.setValue(int(self.config.aimbot_target_offset))
        self.spn_aim_off.valueChanged.connect(lambda v: setattr(self.config, "aimbot_target_offset", float(v)))
        ar.addWidget(self.spn_aim_off)
        lo.addLayout(ar)
        lo.addStretch()

    def _build_colors_tab(self):
        p = self._pages["COLORS"]
        lo = QVBoxLayout(p)
        lo.setContentsMargins(4, 4, 4, 4)
        lo.setSpacing(6)
        self.btn_local_color = QPushButton("Local Player")
        self.btn_local_color.clicked.connect(lambda: self._pick_color("local_color"))
        self.btn_hunter_color = QPushButton("Hunter (enemy team)")
        self.btn_hunter_color.clicked.connect(lambda: self._pick_color("hunter_color"))
        self.btn_survivor_color = QPushButton("Survivor (friendly team)")
        self.btn_survivor_color.clicked.connect(lambda: self._pick_color("survivor_color"))
        self.btn_enemy_color = QPushButton("Unknown Team (fallback)")
        self.btn_enemy_color.clicked.connect(lambda: self._pick_color("enemy_color"))
        self.btn_skeleton_color = QPushButton("Skeleton Color")
        self.btn_skeleton_color.clicked.connect(lambda: self._pick_color("skeleton_color"))
        lo.addWidget(self.btn_local_color)
        lo.addWidget(self.btn_hunter_color)
        lo.addWidget(self.btn_survivor_color)
        lo.addWidget(self.btn_enemy_color)
        lo.addWidget(self.btn_skeleton_color)
        lo.addStretch()

    def _build_camouflage_tab(self):
        p = self._pages["CAMOUFLAGE"]
        lo = QVBoxLayout(p)
        lo.setContentsMargins(4, 4, 4, 4)
        lo.setSpacing(4)

        self.cb_camo = self._chk("Camouflage Enabled", "camouflage_enabled")
        lo.addWidget(self.cb_camo)

        btn_camo = QPushButton("Apply Camouflage")
        btn_camo.setToolTip("Sample environment colours and paint them onto your character (also F10).")
        btn_camo.clicked.connect(
            lambda: self._overlay._toggle_camouflage() if self._overlay else None
        )
        lo.addWidget(btn_camo)

        # ── Camo quality slider (1-20) ────────────────────────────────────────
        camo_q_row = QHBoxLayout()
        camo_q_row.addWidget(QLabel("Camo quality:"))
        self.sld_paint_quality = QSlider(Qt.Horizontal)
        self.sld_paint_quality.setRange(1, 20)
        _camo_q_init = max(1, min(20, getattr(self.config, "paint_quality", 8)))
        self.sld_paint_quality.setValue(_camo_q_init)
        self.sld_paint_quality.setTickPosition(QSlider.TicksBelow)
        self.sld_paint_quality.setTickInterval(1)
        self.sld_paint_quality.setToolTip(
            "Camo quality 1-20.\n"
            "1 = Draft (fastest, blocky)  |  10 = High  |  20 = God Mode (photo-realistic, slowest).\n"
            "Levels 15+ paint 25,000+ UV stamps for near-perfect environment matching."
        )
        camo_q_row.addWidget(self.sld_paint_quality)
        self.lbl_paint_quality = QLabel(
            f"{_CAMO_QLABELS.get(_camo_q_init, str(_camo_q_init))} ({_camo_q_init})"
        )
        self.lbl_paint_quality.setStyleSheet(
            "color: #eee; font-size: 11px; min-width: 90px;"
        )
        def _on_camo_quality_change(v):
            setattr(self.config, "paint_quality", v)
            _camo_size, _camo_qual = _quality_to_camo_settings(v)
            self.config.camouflage_sample_size = _camo_size
            self.config.camouflage_quality     = _camo_qual
            self.lbl_paint_quality.setText(f"{_CAMO_QLABELS.get(v, str(v))} ({v})")
        self.sld_paint_quality.valueChanged.connect(_on_camo_quality_change)
        camo_q_row.addWidget(self.lbl_paint_quality)
        lo.addLayout(camo_q_row)

        # Opacity slider
        or_ = QHBoxLayout()
        or_.addWidget(QLabel("Blend:"))
        self.sld_camo_opacity = QSlider(Qt.Horizontal)
        self.sld_camo_opacity.setRange(0, 255)
        self.sld_camo_opacity.setValue(self.config.camouflage_opacity)
        self.sld_camo_opacity.valueChanged.connect(lambda v: setattr(self.config, "camouflage_opacity", v))
        or_.addWidget(self.sld_camo_opacity)
        self.lbl_camo_val = QLabel(str(self.config.camouflage_opacity))
        self.lbl_camo_val.setStyleSheet("color: #eee; font-size: 11px; min-width: 30px;")
        self.sld_camo_opacity.valueChanged.connect(lambda v: self.lbl_camo_val.setText(str(v)))
        or_.addWidget(self.lbl_camo_val)
        lo.addLayout(or_)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #2a4a1a;")
        lo.addWidget(sep)

        hdr = QLabel("Custom Character Paint")
        hdr.setStyleSheet("color: #7ec850; font-size: 11px; font-weight: bold; padding-top: 4px;")
        lo.addWidget(hdr)

        img_row = QHBoxLayout()
        self.txt_paint_image = QLineEdit(self.config.paint_image_path)
        self.txt_paint_image.setPlaceholderText("Image file (PNG/JPG)...")
        self.txt_paint_image.textChanged.connect(
            lambda t: setattr(self.config, "paint_image_path", t)
        )
        img_row.addWidget(self.txt_paint_image, 1)
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self._browse_paint_image)
        img_row.addWidget(btn_browse)
        lo.addLayout(img_row)

        btn_apply_img = QPushButton("Apply image to character")
        btn_apply_img.setToolTip(
            "Face your character toward the camera (3rd person) for best placement."
        )
        btn_apply_img.clicked.connect(self._apply_paint_image)
        lo.addWidget(btn_apply_img)

        btn_uv_test = QPushButton("Run UV Test")
        btn_uv_test.setToolTip(
            "Paints a UV diagnostic overlay on your character.\n"
            "Use the mode dropdown to switch views:\n"
            "  Full     — quadrants + paint islands + grid (recommended)\n"
            "  Islands  — each centered-mode island in a unique colour\n"
            "  Slices   — head/torso/legs image thirds on island rects\n"
            "  Grid     — 12×12 UV grid (red=u, green=v)\n"
            "  Quadrants— original 4-colour atlas quadrants\n"
            "Screenshot front & back, then share — island coords are logged."
        )
        btn_uv_test.setStyleSheet("background-color: #2a4a6a; color: #aef;")
        btn_uv_test.clicked.connect(self._run_uv_diagnostic)
        lo.addWidget(btn_uv_test)

        uv_diag_row = QHBoxLayout()
        uv_diag_row.addWidget(QLabel("UV test mode:"))
        self.cmb_uv_diag_mode = QComboBox()
        _uv_modes = [
            ("Full (islands + grid)", "full"),
            ("Islands only", "islands"),
            ("Image slices", "slices"),
            ("UV grid", "grid"),
            ("Quadrants only", "quadrants"),
        ]
        for label, val in _uv_modes:
            self.cmb_uv_diag_mode.addItem(label, val)
        _cur_uv = getattr(self.config, "uv_diag_mode", "full")
        _uv_idx = next(
            (i for i, (_, v) in enumerate(_uv_modes) if v == _cur_uv), 0,
        )
        self.cmb_uv_diag_mode.setCurrentIndex(_uv_idx)

        def _on_uv_diag_mode_change(i):
            setattr(self.config, "uv_diag_mode", self.cmb_uv_diag_mode.itemData(i))
            save_config(self.config)

        self.cmb_uv_diag_mode.currentIndexChanged.connect(_on_uv_diag_mode_change)
        uv_diag_row.addWidget(self.cmb_uv_diag_mode, 1)
        lo.addLayout(uv_diag_row)

        # ── Image quality slider ──────────────────────────────────────────────
        _IMG_QLABELS = {1: "Draft", 2: "Low", 3: "Medium", 4: "High", 5: "Ultra"}
        img_q_row = QHBoxLayout()
        img_q_row.addWidget(QLabel("Image quality:"))
        self.sld_image_quality = QSlider(Qt.Horizontal)
        self.sld_image_quality.setRange(1, 5)
        self.sld_image_quality.setValue(getattr(self.config, "image_quality", 3))
        self.sld_image_quality.setTickPosition(QSlider.TicksBelow)
        self.sld_image_quality.setTickInterval(1)
        self.sld_image_quality.setToolTip(
            "Apply Image quality — 1=Draft (fast, rough) to 5=Ultra (slow, sharp)."
        )
        img_q_row.addWidget(self.sld_image_quality)
        self.lbl_image_quality = QLabel(
            f"{_IMG_QLABELS.get(self.sld_image_quality.value(), '')} "
            f"({self.sld_image_quality.value()})"
        )
        self.lbl_image_quality.setStyleSheet(
            "color: #eee; font-size: 11px; min-width: 80px;"
        )
        def _on_image_quality_change(v):
            setattr(self.config, "image_quality", v)
            self.lbl_image_quality.setText(f"{_IMG_QLABELS.get(v, '')} ({v})")
        self.sld_image_quality.valueChanged.connect(_on_image_quality_change)
        img_q_row.addWidget(self.lbl_image_quality)
        lo.addLayout(img_q_row)

        # ── Wrap mode dropdown ────────────────────────────────────────────────
        wrap_row = QHBoxLayout()
        wrap_row.addWidget(QLabel("Wrap mode:"))
        self.cmb_wrap_mode = QComboBox()
        self.cmb_wrap_mode.addItem("Projector  (front → back)", "projector")
        self.cmb_wrap_mode.addItem("Centered   (chest outward)", "centered")
        _wrap_list = ["projector", "centered"]
        _cur_wrap = getattr(self.config, "image_wrap_mode", "projector")
        self.cmb_wrap_mode.setCurrentIndex(
            _wrap_list.index(_cur_wrap) if _cur_wrap in _wrap_list else 0
        )
        self.cmb_wrap_mode.setToolTip(
            "Projector: image starts at the front side and wraps to the back.\n"
            "Centered:  image centre sits on the chest; top→head, bottom→feet."
        )
        def _on_wrap_mode_change(i):
            setattr(self.config, "image_wrap_mode", self.cmb_wrap_mode.itemData(i))
            save_config(self.config)
        self.cmb_wrap_mode.currentIndexChanged.connect(_on_wrap_mode_change)
        wrap_row.addWidget(self.cmb_wrap_mode, 1)
        lo.addLayout(wrap_row)

        ior = QHBoxLayout()
        ior.addWidget(QLabel("Image opacity:"))
        self.sld_paint_opacity = QSlider(Qt.Horizontal)
        self.sld_paint_opacity.setRange(1, 255)
        self.sld_paint_opacity.setValue(self.config.paint_image_opacity)
        self.sld_paint_opacity.valueChanged.connect(
            lambda v: setattr(self.config, "paint_image_opacity", v)
        )
        ior.addWidget(self.sld_paint_opacity)
        self.lbl_paint_opacity = QLabel(str(self.config.paint_image_opacity))
        self.lbl_paint_opacity.setStyleSheet("color: #eee; font-size: 11px; min-width: 30px;")
        self.sld_paint_opacity.valueChanged.connect(
            lambda v: self.lbl_paint_opacity.setText(str(v))
        )
        ior.addWidget(self.lbl_paint_opacity)
        lo.addLayout(ior)

        # Grid is now auto-calculated from the live texture resolution (atlas_res // 8).
        # No manual input needed — quality is always optimised for the game's texture.

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset detail:"))
        self.txt_preset_grid = QLineEdit()
        self._wire_int_field(self.txt_preset_grid, "preset_paint_grid", 32)
        self.txt_preset_grid.setFixedWidth(72)
        self.txt_preset_grid.setToolTip(
            "UV grid N×N when loading a saved paint preset. Enter any positive integer."
        )
        preset_row.addWidget(self.txt_preset_grid)
        preset_row.addWidget(QLabel("(N×N grid for Load Preset)"))
        lo.addLayout(preset_row)

        save_row = QHBoxLayout()
        self.txt_preset_name = QLineEdit()
        self.txt_preset_name.setPlaceholderText("Preset name")
        save_row.addWidget(self.txt_preset_name, 1)
        btn_save_paint = QPushButton("Save Current Paint")
        btn_save_paint.clicked.connect(self._save_paint_preset)
        save_row.addWidget(btn_save_paint)
        lo.addLayout(save_row)

        load_row = QHBoxLayout()
        self.cmb_paint_presets = QComboBox()
        self.cmb_paint_presets.setMinimumWidth(160)
        load_row.addWidget(self.cmb_paint_presets, 1)
        btn_refresh_presets = QPushButton("Refresh")
        btn_refresh_presets.clicked.connect(self._refresh_paint_presets)
        load_row.addWidget(btn_refresh_presets)
        btn_load_paint = QPushButton("Load Preset")
        btn_load_paint.clicked.connect(self._load_paint_preset)
        load_row.addWidget(btn_load_paint)
        lo.addLayout(load_row)

        self.lbl_paint_status = QLabel("")
        self.lbl_paint_status.setStyleSheet("color: #7a9a6a; font-size: 10px; padding: 2px 0;")
        self.lbl_paint_status.setWordWrap(True)
        lo.addWidget(self.lbl_paint_status)

        self._refresh_paint_presets()
        lo.addStretch()

    def _set_paint_status(self, text, ok=True):
        if ok is None:
            color = "#a0a8b0"
        else:
            color = "#7ec850" if ok else "#e07070"
        self.lbl_paint_status.setStyleSheet(f"color: {color}; font-size: 10px; padding: 2px 0;")
        self.lbl_paint_status.setText(text)

    def _wire_int_field(self, line_edit, config_attr, default=32):
        val = getattr(self.config, config_attr, default)
        line_edit.setText(str(val))
        line_edit.setPlaceholderText(str(default))

        def _sync(text):
            try:
                setattr(self.config, config_attr, max(1, int(text.strip())))
            except ValueError:
                pass

        line_edit.textChanged.connect(_sync)

    def _read_int_field(self, line_edit, config_attr, default=32):
        try:
            return max(1, int(line_edit.text().strip()))
        except (TypeError, ValueError, AttributeError):
            return MecchaESP.parse_grid_value(
                getattr(self.config, config_attr, default), default
            )

    def _finish_paint_job(self, ok, message):
        self._paint_watchdog.stop()
        self._paint_busy = False
        if self._overlay:
            self._overlay.set_paint_throttle(False)
        if ok:
            import time
            self._paint_last_apply = time.monotonic()
        self._set_paint_status(message, ok=ok)

    def _on_paint_job_progress(self, done, total):
        pct = int(done * 100 / max(1, total))
        label = getattr(self, "_paint_progress_label", "Painting")
        self._set_paint_status(
            f"{label}... {pct}% (game frozen — wait until done)",
            ok=None,
        )

    def _paint_watchdog_timeout(self):
        if self._paint_busy:
            self._paint_busy = False
            if self._overlay:
                self._overlay.set_paint_throttle(False)
            self._set_paint_status("Paint timed out — you can try again.", ok=False)

    def _run_paint_job(self, status_wait, worker_fn, progress_label="Painting"):
        """Run a blocking paint worker on a background thread."""
        import threading

        if self._paint_busy:
            self._set_paint_status("Paint already in progress...", ok=None)
            return
        self._paint_busy = True
        self._paint_progress_label = progress_label
        self._set_paint_status(status_wait, ok=None)
        self._paint_watchdog.start(180_000)
        # Throttle overlay while painting; quality 5 keeps a 15-fps floor.
        if self._overlay:
            img_q = getattr(self.config, "image_quality", 3)
            self._overlay.set_paint_throttle(True, quality=img_q)

        def _thread_main():
            try:
                ok, msg = worker_fn()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                ok, msg = False, str(exc)
            self.paint_job_finished.emit(ok, msg)

        threading.Thread(target=_thread_main, daemon=True).start()

    # -------------------------------------------------------------------------
    def _build_changelog_tab(self):
        from PyQt5.QtWidgets import QTextEdit
        p = self._pages["CHANGELOG"]
        lo = QVBoxLayout(p)
        lo.setContentsMargins(4, 4, 4, 4)
        lo.setSpacing(6)

        hdr = QLabel("Changelog")
        hdr.setStyleSheet(
            "color: #7ec850; font-size: 13px; font-weight: bold; padding-bottom: 2px;"
        )
        lo.addWidget(hdr)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet("""
            QTextEdit {
                background: #111a0d;
                color: #c8e8b0;
                border: 1px solid #2a4a1a;
                border-radius: 6px;
                padding: 6px;
                font-family: Consolas, monospace;
                font-size: 11px;
                selection-background-color: #2a5a1a;
            }
            QScrollBar:vertical {
                background: #111a0d; width: 10px; border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background: #3a6a2a; border-radius: 5px; min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        txt.setPlainText(
            "=== Peterhack Changelog ===\n"
            "\n"
            "--- Jun 26, 2026 (latest) ---\n"
            "\n"
            "[UV Diagnostic Tool — expanded]\n"
            "  + UV test mode dropdown with 5 overlay types:\n"
            "      Full      — dim quadrants + island rects + grid (default)\n"
            "      Islands   — each centered-mode paint island, unique colour\n"
            "      Slices    — head/torso/legs image thirds on island rects\n"
            "      Grid      — 12×12 UV grid (R=u, G=v) + reference lines\n"
            "      Quadrants — original 4-colour atlas quadrants\n"
            "  + Island u/v bounds logged to latest.log on every run.\n"
            "  + Reference crosses: white=u0.25, cyan=u0.75, yellow/orange\n"
            "    at u0.5 v0.25/v0.75 for fine coordinate reading.\n"
            "\n"
            "[Image Paint — Centered mode: island-calibrated UV map]\n"
            "  + UV diagnostic screenshots revealed the atlas is NOT one rectangle\n"
            "    per side.  Head, torso, and legs are separate UV islands:\n"
            "      GREEN  (u 0.5-1, v 0-0.5)  → front head + outer arms\n"
            "      BLUE   (u 0-0.5, v 0.5-1)  → front chest / back spine\n"
            "      YELLOW (u 0.5-1, v 0.5-1)  → front-left leg, back-right\n"
            "      RED    (u 0-0.5, v 0-0.5)  → inner leg, back-head-left\n"
            "  + Centered mode now splits the image into vertical thirds and maps\n"
            "    each third onto the correct island (head/torso/legs) on both\n"
            "    front and back, instead of one big rectangle at u=0.25/0.75.\n"
            "\n"
            "[UV Diagnostic Tool]\n"
            "  + New 'UV Test (diagnostic)' button in the Image Paint tab.\n"
            "    Paints 4 colored quadrants across the UV atlas so you can see\n"
            "    exactly which UV coordinate maps to which part of the body:\n"
            "      RED    = top-left UV quadrant  (u 0.0–0.5, v 0.0–0.5)\n"
            "      GREEN  = top-right UV quadrant (u 0.5–1.0, v 0.0–0.5)\n"
            "      BLUE   = bottom-left UV quad   (u 0.0–0.5, v 0.5–1.0)\n"
            "      YELLOW = bottom-right UV quad  (u 0.5–1.0, v 0.5–1.0)\n"
            "    WHITE cross = assumed front centre at u=0.25, v=0.5.\n"
            "    CYAN  cross = assumed back centre at u=0.75, v=0.5.\n"
            "    Screenshot front & back after running this so UV offsets\n"
            "    can be corrected if the colours appear in wrong spots.\n"
            "\n"
            "--- Jun 25, 2026 ---\n"
            "\n"
            "[Image Paint — Centered mode overhauled]\n"
            "  + Centered mode now uses pure UV-space stamping on both the front\n"
            "    and back panels — no PaintAtScreenPosition raycasting.\n"
            "    PaintAtScreenPosition was tried but raycasts from a first-person\n"
            "    camera hit the inside of the mesh at random UV islands (back of\n"
            "    head, inner leg, back of arm) rather than the chest / spine.\n"
            "    UV-space is reliable: the full image is mapped to the front panel\n"
            "    (chest centre u=0.25, u±0.23, v 0.01-0.99 head-to-toe) and the\n"
            "    back panel (spine centre u=0.75, same extents).  Two full copies\n"
            "    of the image — front and back — both perfectly centred.\n"
            "  + Projector mode: image stretched across the full UV atlas\n"
            "    (u 0→1) for a continuous single wrap front-to-back.\n"
            "  + Fixed crash (EXCEPTION_ACCESS_VIOLATION reading 0x38): all\n"
            "    native HitTestAtScreenPosition calls removed from paint path.\n"
            "\n"
            "[Paint Presets]\n"
            "  + Fixed crash when saving a paint preset.  The export function\n"
            "    (ExportChannelToBytes) internally dispatches to render/job threads;\n"
            "    wrapping it in a game-freeze deadlocked those threads and caused a\n"
            "    WAIT_TIMEOUT crash.  The export now runs while the game is live.\n"
            "\n"
            "[Image Paint]\n"
            "  + Camera-based UV calibration: before painting, Peterhack\n"
            "    hit-tests the character model from the current camera view to\n"
            "    find the actual UV v-coordinates of the head and feet.\n"
            "    The image is then mapped so the top always reaches the head\n"
            "    and the bottom always reaches the feet — no more flipped or\n"
            "    off-center painting regardless of wrap mode.\n"
            "  + 'Apply image to character' button renamed.\n"
            "  + Wrap mode selection now auto-saves to config immediately.\n"
            "\n"
            "[Camouflage]\n"
            "  + 'Apply Camouflage' button added — no longer F10-only.\n"
            "  + Camo quality expanded to 1-20 (was 1-5) for truly\n"
            "    indistinguishable environment blending:\n"
            "      1  = Draft    (64 UV stamps — fastest)\n"
            "      8  = Medium   (2,304 stamps — default)\n"
            "      10 = High-    (4,096 stamps / 9 sub-samples)\n"
            "      14 = Ultra    (16,384 stamps / 16 sub-samples)\n"
            "      17 = Max      (50,176 stamps / 25 sub-samples)\n"
            "      20 = God Mode (262,144 stamps / 64 sub-samples,\n"
            "                     photo-realistic — takes a few seconds)\n"
            "\n"
            "--- Jun 24, 2026 ---\n"
            "\n"
            "[FPS]\n"
            "  + In-game FPS counter added via 500 Hz camera-tick tracker.\n"
            "  + Top-left overlay now shows OVL (overlay) and GAME fps separately.\n"
            "\n"
            "--- Jun 23, 2026 ---\n"
            "\n"
            "[Image Paint]\n"
            "  + Wrap mode dropdown — choose how the image is projected onto\n"
            "    the character before painting:\n"
            "\n"
            "      Projector  (front to back)  [default]\n"
            "        The left edge of the image starts at the body side seam\n"
            "        (front edge), covers the entire front, continues across the\n"
            "        back, and the right edge finishes at the same side seam.\n"
            "        One continuous image wraps all the way around.\n"
            "\n"
            "      Centered  (chest outward)\n"
            "        The center of the image sits exactly on the chest.\n"
            "        Top of image -> character head;  bottom -> feet.\n"
            "        Image edges meet at the back center (spine seam — hidden).\n"
            "\n"
            "[Camouflage]\n"
            "  + Directed edge walk: camo now samples pixels immediately outside\n"
            "    the body edge in each UV cell's nearest direction, giving a true\n"
            "    'see-through' effect — top copies sky, bottom copies ground,\n"
            "    sides copy surroundings.\n"
            "\n"
            "--- Jun 25, 2026 ---\n"
            "\n"
            "[Image Paint]\n"
            "  + Game process priority lowered while painting instead of\n"
            "    throttling the overlay.  Overlay runs at full 60fps; the GAME\n"
            "    loses CPU time-slices so its FPS drops, freeing CPU for paints:\n"
            "      Quality 1-4 -> BELOW_NORMAL_PRIORITY_CLASS\n"
            "      Quality 5   -> IDLE_PRIORITY_CLASS (max game fps drop)\n"
            "    Priority restores to NORMAL immediately after painting.\n"
            "  + Quality 5 (Ultra) fast-paint mode: batch size doubles to 256.\n"
            "  + Separate Image Quality slider (1-5) added:\n"
            "      1 = Draft  (64x64  stamps, fastest, roughest)\n"
            "      2 = Low    (96x96  stamps)\n"
            "      3 = Medium (128x128 stamps, default)\n"
            "      4 = High   (192x192 stamps)\n"
            "      5 = Ultra  (256x256 stamps, sharpest)\n"
            "  + Auto-trim solid-color AND transparent borders from source images\n"
            "    before painting — white-background JPEGs/PNGs now trim correctly.\n"
            "  + White base coat: 4 large stamps (r=0.35) at UV quadrant centres\n"
            "    — full atlas coverage in one call, no leftover paint gaps.\n"
            "  + Brush hardness raised to 0.95 — crisp edges, no watercolour blur.\n"
            "  + Stamp batch size raised from 24 to 128 — significantly faster.\n"
            "  + Images scaled with IgnoreAspectRatio — no letterbox gaps on body.\n"
            "\n"
            "[Camouflage]\n"
            "  + Separate Camo Quality slider (1-5) — independent from image slider.\n"
            "  + Quality 5 fast-camo: batch raised to 96, sleep cut to 5ms (~4x speed).\n"
            "  + Game priority lowered to IDLE while camo is active (quality 5).\n"
            "\n"
            "[FPS / Performance]\n"
            "  + Overlay label changed to 'OVL: Xfps' to distinguish from game FPS.\n"
            "  + During painting label shows 'PAINTING... (game: IDLE priority)'\n"
            "    confirming the game throttle is active.\n"
            "\n"
            "[UI]\n"
            "  + Removed manual 'Image detail (NxN grid)' input — replaced by slider.\n"
            "  + Added Changelog tab (this screen).\n"
            "  + FPS indicator added to overlay top-left (under player indicator).\n"
            "\n"
            "[Stability]\n"
            "  + Fixed crash when applying image — disabled hit-test raycasts and\n"
            "    screen-space pass during frozen game state.\n"
            "  + Fixed ESP vanishing on death — added (0,0,0) camera fallback.\n"
            "\n"
            "[Launcher]\n"
            "  + Peterhack.bat now self-elevates to Administrator automatically.\n"
            "\n"
            "[Misc]\n"
            "  + Removed all GitHub auto-update and mitigation logic.\n"
            "\n"
        )
        lo.addWidget(txt, 1)

    def _browse_paint_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select image for character paint",
            os.path.dirname(self.config.paint_image_path) if self.config.paint_image_path else "",
            "Images (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if path:
            self.txt_paint_image.setText(path)
            self.config.paint_image_path = path

    @staticmethod
    def _trim_transparent_borders(img: QImage) -> QImage:
        """
        Crop to the content bounding box, removing uniform-color or transparent borders.

        Works for:
        - PNG with transparent background (any alpha < 24 is background)
        - JPEG / opaque PNG with a solid white (or any solid) background:
          the four corner pixels determine the background color; pixels within
          BG_THRESH of that color on every channel are treated as background.
        - Complex / photographic images whose corners differ significantly are
          left unchanged to avoid false cropping.
        """
        img32 = img.convertToFormat(QImage.Format_ARGB32)
        w, h = img32.width(), img32.height()
        if w < 4 or h < 4:
            return img

        buf = img32.bits()
        buf.setsize(w * h * 4)
        data = bytes(buf)
        bpl = img32.bytesPerLine()

        # ARGB32 on little-endian: byte layout is B G R A at offsets 0 1 2 3
        def px(x, y):
            off = y * bpl + x * 4
            return data[off + 2], data[off + 1], data[off], data[off + 3]  # R G B A

        # ── Detect background colour from the four corners ────────────────────
        corners = [px(0, 0), px(w - 1, 0), px(0, h - 1), px(w - 1, h - 1)]
        bg_r = sum(c[0] for c in corners) // 4
        bg_g = sum(c[1] for c in corners) // 4
        bg_b = sum(c[2] for c in corners) // 4
        # Only use solid-colour detection when all four corners are similar.
        # Large spread → complex background → fall back to alpha-only.
        r_spread = max(c[0] for c in corners) - min(c[0] for c in corners)
        g_spread = max(c[1] for c in corners) - min(c[1] for c in corners)
        b_spread = max(c[2] for c in corners) - min(c[2] for c in corners)
        solid_ok = (r_spread <= 60 and g_spread <= 60 and b_spread <= 60)

        BG_THRESH = 30  # per-channel tolerance for "same as background"

        def is_bg(r, g, b, a):
            if a < 24:
                return True   # transparent → always background
            if not solid_ok:
                return False  # complex image → only transparent counts
            return (abs(r - bg_r) <= BG_THRESH and
                    abs(g - bg_g) <= BG_THRESH and
                    abs(b - bg_b) <= BG_THRESH)

        def row_has_content(y):
            base = y * bpl
            for x in range(w):
                o = base + x * 4
                if not is_bg(data[o + 2], data[o + 1], data[o], data[o + 3]):
                    return True
            return False

        # ── Scan top / bottom ─────────────────────────────────────────────────
        top = 0
        while top < h and not row_has_content(top):
            top += 1
        if top >= h:
            return img  # fully background

        bottom = h - 1
        while bottom > top and not row_has_content(bottom):
            bottom -= 1

        # ── Scan left / right (only within the found row range) ───────────────
        def col_has_content(x):
            for y in range(top, bottom + 1):
                o = y * bpl + x * 4
                if not is_bg(data[o + 2], data[o + 1], data[o], data[o + 3]):
                    return True
            return False

        left = 0
        while left < w and not col_has_content(left):
            left += 1
        right = w - 1
        while right > left and not col_has_content(right):
            right -= 1

        if top == 0 and bottom == h - 1 and left == 0 and right == w - 1:
            return img  # nothing to trim

        cropped = img.copy(left, top, right - left + 1, bottom - top + 1)
        print(
            f"[PAINT] trim {w}x{h} -> {cropped.width()}x{cropped.height()} "
            f"(bg #{bg_r:02x}{bg_g:02x}{bg_b:02x})"
        )
        return cropped

    def _qimage_to_bgra_bytes(self, img: QImage):
        img = img.convertToFormat(QImage.Format_ARGB32)
        w, h = img.width(), img.height()
        bpl = img.bytesPerLine()
        buf = img.bits()
        buf.setsize(img.byteCount())
        raw = bytes(buf)
        if bpl == w * 4:
            return raw[: w * h * 4], w, h
        out = bytearray(w * h * 4)
        for y in range(h):
            row_off = y * bpl
            out_off = y * w * 4
            out[out_off:out_off + w * 4] = raw[row_off:row_off + w * 4]
        return bytes(out), w, h

    def _apply_paint_image(self):
        import time
        if time.monotonic() - self._paint_last_apply < 3.0:
            self._set_paint_status("Wait 3s between paint applies.", ok=False)
            return
        path = self.txt_paint_image.text().strip()
        if not path or not os.path.isfile(path):
            self._set_paint_status("Pick a valid image file first.", ok=False)
            return
        img = QImage(path)
        if img.isNull():
            self._set_paint_status("Could not load image.", ok=False)
            return

        pawn_hint = self.esp._find_local_pawn()
        resolution = self.esp.get_albedo_resolution(pawn=pawn_hint or None)

        # Trim transparent/empty borders so the image content fills the frame
        # edge-to-edge. Without this, a PNG with whitespace above/below the
        # subject causes the character head/feet to receive blank white paint.
        img = self._trim_transparent_borders(img)

        orig_w = max(1, img.width())
        orig_h = max(1, img.height())
        img_aspect = orig_w / orig_h
        # Scale to fill the full canvas (IgnoreAspectRatio = no letterbox bars).
        scaled = img.scaled(
            resolution, resolution,
            Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation,
        )
        canvas = QImage(resolution, resolution, QImage.Format_ARGB32)
        # Fill with white so any remaining transparent areas paint as white
        # rather than leaving the character's existing texture showing through.
        canvas.fill(QColor(255, 255, 255, 255))
        painter = QPainter(canvas)
        painter.drawImage(0, 0, scaled)
        painter.end()
        bgra, w, h = self._qimage_to_bgra_bytes(canvas)
        if w != resolution or h != resolution:
            self._set_paint_status("Image scaling failed.", ok=False)
            return
        opacity = int(self.config.paint_image_opacity)
        # Grid size driven by the Image quality slider (1-5).
        grid = _quality_to_image_grid(getattr(self.config, "image_quality", 3))

        def _paint_progress(done, total):
            self.paint_job_progress.emit(done, total)

        fast_paint = getattr(self.config, "image_quality", 3) >= 5

        def worker():
            pawn = self.esp.wait_for_paintable_pawn()
            if not pawn:
                return False, "Could not find your character — spawn in a match first."
            ok = self.esp.paint_image_bgra(
                pawn, bgra, resolution, opacity=opacity, grid=grid,
                progress_cb=_paint_progress,
                img_aspect=img_aspect, img_w=orig_w, img_h=orig_h,
                fast_paint=fast_paint,
                wrap_mode=getattr(self.config, "image_wrap_mode", "projector"),
            )
            if ok:
                return True, (
                    f"Applied {orig_w}×{orig_h} image on front+back."
                )
            return False, "Image apply failed — try again in a match."

        self._run_paint_job(
            "Looking for character... game will pause briefly while painting",
            worker,
            progress_label="Painting",
        )

    def _run_uv_diagnostic(self):
        """Launch a UV diagnostic paint job using the selected overlay mode."""
        mode = getattr(self.config, "uv_diag_mode", "full")
        mode_labels = {
            "full": "Full (quadrants + islands + grid)",
            "islands": "Islands only",
            "slices": "Image slices (head/torso/legs)",
            "grid": "UV coordinate grid",
            "quadrants": "Quadrants only",
        }
        mode_hint = {
            "full": (
                "Full-bright quadrants (RED/GREEN/BLUE/YELLOW) + white island borders.\n"
                "Borders show where centered-mode image islands are mapped.\n"
                "White cross=u0.25,v0.5  Cyan=u0.75,v0.5  Yellow/Orange at u0.5"
            ),
            "islands": (
                "Each paint island in a unique colour with white border.\n"
                "F-head=magenta, F-chest=cyan, F-legL=orange, F-legR=purple, etc.\n"
                "Island u/v coords printed to latest.log."
            ),
            "slices": (
                "Pink=head third, Teal=torso third, Orange=legs third.\n"
                "Shows how centered mode splits your image across islands.\n"
                "Grid lines at image slice boundaries (34%, 68%)."
            ),
            "grid": (
                "12×12 UV grid — red channel=u, green channel=v.\n"
                "White lines at u/v = 0.25, 0.5, 0.75.\n"
                "Use to read exact UV coordinates from screenshots."
            ),
            "quadrants": (
                "RED=top-left UV, GREEN=top-right, BLUE=bottom-left, YELLOW=bottom-right.\n"
                "White cross=u0.25,v0.5  Cyan cross=u0.75,v0.5"
            ),
        }

        def worker():
            pawn = self.esp.wait_for_paintable_pawn()
            if not pawn:
                return False, "Could not find your character — spawn in a match first."
            ok = self.esp.paint_uv_diagnostic(mode=mode)
            if ok:
                label = mode_labels.get(mode, mode)
                hint = mode_hint.get(mode, "")
                return True, f"UV diagnostic painted ({label}).\n{hint}\nScreenshot front & back!"
            return False, "UV diagnostic failed — are you in a match?"

        self._run_paint_job(
            f"Painting UV diagnostic ({mode})… game will pause briefly",
            worker,
            progress_label="UV Diagnostic",
        )

    def _refresh_paint_presets(self):
        self.cmb_paint_presets.clear()
        presets = self.esp.list_paint_presets()
        if presets:
            self.cmb_paint_presets.addItems(presets)
        else:
            self.cmb_paint_presets.addItem("(no saved presets)")

    def _save_paint_preset(self):
        name = self.txt_preset_name.text().strip()
        if not name:
            self._set_paint_status("Enter a preset name to save.", ok=False)
            return
        ok, msg = self.esp.save_paint_preset(name, grid=0)
        if ok:
            self._set_paint_status(f"Saved preset: {os.path.basename(msg)}", ok=True)
            self._refresh_paint_presets()
            safe = self.esp._sanitize_preset_name(name)
            idx = self.cmb_paint_presets.findText(safe)
            if idx >= 0:
                self.cmb_paint_presets.setCurrentIndex(idx)
        else:
            self._set_paint_status(msg, ok=False)

    def _load_paint_preset(self):
        import time
        if time.monotonic() - self._paint_last_apply < 3.0:
            self._set_paint_status("Wait 3s between paint applies.", ok=False)
            return
        name = self.cmb_paint_presets.currentText()
        if not name or name.startswith("("):
            self._set_paint_status("No preset selected.", ok=False)
            return
        grid = self._read_int_field(self.txt_preset_grid, "preset_paint_grid", 32)

        def _paint_progress(done, total):
            self.paint_job_progress.emit(done, total)

        def worker():
            pawn = self.esp.wait_for_paintable_pawn()
            if not pawn:
                return False, "Could not find your character — spawn in a match first."
            return self.esp.load_paint_preset(
                name, pawn=pawn, grid=grid, progress_cb=_paint_progress
            )

        self._run_paint_job(
            "Looking for character... game will freeze while loading preset",
            worker,
            progress_label="Loading preset",
        )

    def _oof_radius_label(self, value):
        return "Edge" if value <= 0 else f"{value}px"

    def _on_oof_radius_changed(self, value):
        self.config.oof_arrow_radius = value
        self.lbl_oof.setText(self._oof_radius_label(value))

    def _chk(self, text, attr):
        cb = QCheckBox(text)
        cb.setChecked(getattr(self.config, attr))
        cb.stateChanged.connect(lambda s, a=attr: setattr(self.config, a, bool(s)))
        return cb

    def _pick_color(self, attr):
        current = getattr(self.config, attr)
        c = QColorDialog.getColor(QColor(*current), self)
        if c.isValid():
            setattr(self.config, attr, (c.red(), c.green(), c.blue()))

    def _start_aim_key_record(self):
        self.btn_record_key.setEnabled(False)
        self.btn_record_key.setText('Press key...')
        self._key_recorder.start()

    def _save_config(self):
        if save_config(self.config):
            self.btn_save.setText('Config Saved!')
            QTimer.singleShot(1500, lambda: self.btn_save.setText('Save Config'))
        else:
            self.btn_save.setText('Save Failed!')
            QTimer.singleShot(1500, lambda: self.btn_save.setText('Save Config'))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None


# ---------------------------------------------------------------------------
# Overlay widget
# ---------------------------------------------------------------------------
class Overlay(QWidget):
    def __init__(self, esp: MecchaESP, config: Config, menu=None):
        super().__init__()
        self.esp = esp
        self.config = config
        self.menu = menu
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setWindowTitle("Meccha Chameleon Tools - Overlay")
        self._key_states = {}
        self._last_cam = None           # last-known-good camera; survives free-cam gaps
        self._camouflage_active = False
        self._camouflage_color = None  # Tuple[int,int,int] when sampled
        self._camo_key_held = False    # edge detection for F10
        self._camo_feedback = ""       # brief status text ("Sampling...", "Camo ON", etc.)
        self._camo_feedback_count = 0
        self._original_pawn_color = None  # (r,g,b) to restore when camo toggled off
        self._camo_last_apply = 0.0    # time.monotonic() of last successful apply

        self._fps_times: list = []   # rolling 1-second timestamp buffer
        self._current_fps: int = 0
        self._paint_throttle: bool = False  # True while a paint job is running
        self._paint_throttle_quality: int = 3  # quality level active during last throttle

        # Game FPS tracker — 500 Hz background thread counts camera ticks/sec
        self._game_fps_info = self.esp.start_fps_tracker()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_overlay)
        self.timer.start(16)

        self.game_hwnd = self._find_game_window()
        self._resize_to_game()

        # Poll menu toggle key
        self.key_timer = QTimer(self)
        self.key_timer.timeout.connect(self._poll_keys)
        self.key_timer.start(50)

        # Auto-refresh timer exists but is not started — camo is one-shot (F10 per apply)
        self._camo_auto_timer = QTimer(self)
        self._camo_auto_timer.timeout.connect(self._auto_refresh_camo)
        self._camo_sampling = False

    def _is_esp_pixel(self, rgb):
        """True if a screen colour likely comes from our ESP overlay drawing."""
        if not rgb or len(rgb) < 3:
            return False
        r, g, b = rgb[0], rgb[1], rgb[2]
        esp_colors = (
            self.config.local_color,
            self.config.hunter_color,
            self.config.survivor_color,
            self.config.enemy_color,
            self.config.skeleton_color,
            self.config.box_color,
            self.config.radar_color,
            (255, 255, 255),
            (0, 255, 0),
            (255, 0, 0),
            (0, 255, 255),
        )
        for cr, cg, cb in esp_colors:
            if abs(r - cr) + abs(g - cg) + abs(b - cb) < 55:
                return True
        # Health / shield bar fills
        if b > 180 and g > 90 and r < 60:
            return True
        if g > 180 and r < 60 and b < 60:
            return True
        return False

    def _capture_region_bits(self, rx0, ry0, rw, rh, hide_overlay=True):
        """
        BitBlt a screen region aligned to the game client area.

        When hide_overlay is True the ESP overlay is hidden briefly so sampled
        pixels come from the game only (not our drawn boxes, dots, or labels).
        """
        hid = False
        try:
            import win32gui, win32ui, win32con
        except Exception as e:
            print(f"[CAMO-CAP] win32 import failed: {e}", flush=True)
            return None

        if rw < 1 or rh < 1:
            return None

        try:
            if hide_overlay:
                hid = True
                self._camo_sampling = True
                self.hide()
                QApplication.processEvents()

            sx = int(self.x() + rx0)
            sy = int(self.y() + ry0)

            hwnd_dc = win32gui.GetDC(0)
            if not hwnd_dc:
                return None
            mfc_dc = save_dc = bmp = None
            bits = None
            try:
                mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
                save_dc = mfc_dc.CreateCompatibleDC()
                bmp = win32ui.CreateBitmap()
                bmp.CreateCompatibleBitmap(mfc_dc, rw, rh)
                save_dc.SelectObject(bmp)
                save_dc.BitBlt((0, 0), (rw, rh), mfc_dc, (sx, sy), win32con.SRCCOPY)
                bits = bmp.GetBitmapBits(True)
            except Exception as e:
                print(f"[CAMO-CAP] BitBlt failed: {e}", flush=True)
            finally:
                try:
                    if bmp is not None:
                        win32gui.DeleteObject(bmp.GetHandle())
                    if save_dc is not None:
                        save_dc.DeleteDC()
                    if mfc_dc is not None:
                        mfc_dc.DeleteDC()
                except Exception:
                    pass
                win32gui.ReleaseDC(0, hwnd_dc)

            if not bits or len(bits) < rw * rh * 4:
                return None
            return bits
        finally:
            if hid:
                self._camo_sampling = False
                self.setVisible(True)
                self.raise_()
                QApplication.processEvents()
                self.update()

    def _find_game_window(self):
        try:
            import win32gui
            return win32gui.FindWindow(None, "Chameleon  ")
        except Exception:
            return 0

    def _resize_to_game(self):
        try:
            import win32gui
            if self.game_hwnd:
                rect = win32gui.GetClientRect(self.game_hwnd)
                tl = win32gui.ClientToScreen(self.game_hwnd, (rect[0], rect[1]))
                br = win32gui.ClientToScreen(self.game_hwnd, (rect[2], rect[3]))
                self.setGeometry(tl[0], tl[1], br[0] - tl[0], br[1] - tl[1])
            else:
                self.setGeometry(0, 0, 1920, 1080)
        except Exception:
            self.setGeometry(0, 0, 1920, 1080)

    def update_overlay(self):
        import time as _time
        now = _time.monotonic()
        self._fps_times.append(now)
        # Keep only timestamps within the last second
        cutoff = now - 1.0
        while self._fps_times and self._fps_times[0] < cutoff:
            self._fps_times.pop(0)
        self._current_fps = len(self._fps_times)

        self._resize_to_game()
        if not self._camo_sampling and not self.isVisible():
            self.setVisible(True)
            self.raise_()
        self.update()

    def set_paint_throttle(self, on: bool, quality: int = 3):
        """Throttle the GAME process priority while painting.

        The overlay timer is left at 16 ms (60 fps) the entire time — the
        overlay runs freely.  Instead, the game process priority class is
        lowered so the OS gives the game fewer CPU slices, naturally reducing
        the game's own FPS and freeing CPU for our painting thread:

          quality 5   → IDLE_PRIORITY_CLASS        (max throttle)
          quality 1-4 → BELOW_NORMAL_PRIORITY_CLASS (moderate)
          on=False    → NORMAL_PRIORITY_CLASS        (restored)
        """
        self._paint_throttle = on
        if on:
            self._paint_throttle_quality = quality
        if self.esp:
            try:
                self.esp.throttle_game_process(on, quality)
            except Exception:
                pass

    def _poll_keys(self):
        """Fallback key poll when RegisterHotKey is unavailable (non-admin runs)."""
        menu = self.menu
        if menu and getattr(menu, "_hotkeys_native", False):
            return

        VK_INSERT = 0x2D
        VK_F1 = 0x70
        for vk, name in [(VK_INSERT, "insert"), (VK_F1, "f1")]:
            state = ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
            if state and not self._key_states.get(name):
                if menu:
                    menu._toggle_menu_visibility()
                else:
                    for w in QApplication.topLevelWidgets():
                        if isinstance(w, Menu):
                            w._toggle_menu_visibility()
                            break
            self._key_states[name] = bool(state)

        if self.config.camouflage_enabled and menu and not getattr(menu, "_hotkeys_registered", False):
            VK_F10 = 0x79
            camo_held = bool(ctypes.windll.user32.GetAsyncKeyState(VK_F10) & 0x8000)
            if camo_held and not self._camo_key_held:
                menu._on_hotkey_f10()
            self._camo_key_held = camo_held

    def _toggle_camouflage(self):
        """
        F10 = (re)apply the TRUE chameleon blend. Each press re-samples the floor
        around the body and repaints via screen-space raycasts, so each body part
        takes the colour of the floor beneath it. Re-applies fresh every press
        (no solid-colour 'off' flood).
        """
        local_pawn = self.esp._get_local_pawn()
        if not local_pawn:
            self._camo_feedback = "NO PAWN FOUND"
            self._camo_feedback_count = 60
            return

        self._camo_feedback = "SAMPLING..."
        self._camo_feedback_count = 15
        camo_q = max(1, min(20, getattr(self.menu.config, "paint_quality", 8) if self.menu else 8))
        self.set_paint_throttle(True, quality=camo_q)

        try:
            pattern = self._sample_screenspace_pattern()
        except Exception:
            import traceback
            print("[CAMO] sampling raised:\n" + traceback.format_exc(), flush=True)
            self._camo_sampling = False
            self.set_paint_throttle(False)
            self.setVisible(True)
            self.raise_()
            self.update()
            self._camo_feedback = "SAMPLE ERR"
            self._camo_feedback_count = 60
            return

        if not pattern:
            self._camo_sampling = False
            self.set_paint_throttle(False)
            self.setVisible(True)
            self.raise_()
            self.update()
            self._camo_feedback = "SAMPLE FAIL"
            self._camo_feedback_count = 60
            return

        # Average sampled colour purely for the on-screen indicator
        ar = sum(p[2][0] for p in pattern) // len(pattern)
        ag = sum(p[2][1] for p in pattern) // len(pattern)
        ab = sum(p[2][2] for p in pattern) // len(pattern)

        ok = self.esp.set_camouflage_screenspace(
            local_pawn, pattern,
            brush_opacity=self.config.camouflage_opacity / 255.0,
            brush_hardness=0.42,
            fast_paint=(camo_q >= 6),  # fast batches from Low+ upward
        )
        self.set_paint_throttle(False)
        if ok:
            import time
            self._camo_last_apply = time.monotonic()
            self._camouflage_color = (ar, ag, ab)
            self._camouflage_active = True
            self._camo_feedback = "CAMO BLEND"
            self._camo_feedback_count = 90
        else:
            self._camo_feedback = "PAINT FAIL"
            self._camo_feedback_count = 60

    def _auto_refresh_camo(self):
        """Silently re-apply the blend every 1.5 s while camo is active.
        Keeps the character blended as they move over new floor splatters."""
        if self._camouflage_active and self.config.camouflage_enabled:
            local_pawn = self.esp._get_local_pawn()
            if not local_pawn:
                return
            try:
                pattern = self._sample_screenspace_pattern()
            except Exception:
                return
            if not pattern:
                return
            ok = self.esp.set_camouflage_screenspace(
            local_pawn, pattern,
            brush_opacity=self.config.camouflage_opacity / 255.0,
            brush_hardness=0.42,
        )
            if ok:
                ar = sum(p[2][0] for p in pattern) // len(pattern)
                ag = sum(p[2][1] for p in pattern) // len(pattern)
                ab = sum(p[2][2] for p in pattern) // len(pattern)
                self._camouflage_color = (ar, ag, ab)

    def _sample_environment_pattern(self):
        """
        Chameleon sampler (3rd-person, camera behind player).

        Projects the local player's body to a screen bounding box, expands it to
        capture the immediate surroundings, then samples a GxG grid of colours over
        that region and maps each cell to a body-texture UV. Returns a list of
        (u, v, (r,g,b)) for core.set_camouflage_pattern, or None on failure.
        """
        try:
            import win32gui
        except Exception:
            print("[CAMO-SAMPLE] win32gui import failed", flush=True)
            return None

        cam = self.esp.get_camera()
        if not cam:
            print("[CAMO-SAMPLE] no camera", flush=True)
            return None
        local_pawn = self.esp._get_local_pawn()
        if not local_pawn:
            print("[CAMO-SAMPLE] no local pawn", flush=True)
            return None
        pos = self.esp.get_actor_root_pos(local_pawn)
        if not pos:
            print("[CAMO-SAMPLE] no root pos", flush=True)
            return None

        w = self.width()
        h = self.height()
        print(f"[CAMO-SAMPLE] overlay size w={w} h={h} campos={cam.get('loc')} pawnpos={pos}", flush=True)
        if w < 100 or h < 100:
            print("[CAMO-SAMPLE] overlay too small", flush=True)
            return None

        # Project a world-space box around the body (root = feet) to screen, mirroring
        # draw_2d_box, to find the player's on-screen silhouette rectangle.
        BODY_H  = 180.0   # approx character height (world units)
        BODY_HW = 45.0    # approx half width
        corners = [
            (-BODY_HW, 0,       -BODY_HW), (-BODY_HW, 0,       BODY_HW),
            ( BODY_HW, 0,        BODY_HW), ( BODY_HW, 0,      -BODY_HW),
            (-BODY_HW, BODY_H,  -BODY_HW), (-BODY_HW, BODY_H,  BODY_HW),
            ( BODY_HW, BODY_H,   BODY_HW), ( BODY_HW, BODY_H, -BODY_HW),
        ]
        rot = self.esp.get_actor_root_rotation(local_pawn)
        yaw = math.radians(rot[1]) if rot else 0.0
        cyaw, syaw = math.cos(yaw), math.sin(yaw)

        xs, ys = [], []
        for lx, ly, lz in corners:
            rx = lx * cyaw - lz * syaw
            rz = lx * syaw + lz * cyaw
            s = w2s((pos[0] + rx, pos[1] + ly, pos[2] + rz), cam, w, h)
            if s[2]:   # only on-screen corners
                xs.append(s[0])
                ys.append(s[1])
        print(f"[CAMO-SAMPLE] projected corners={len(xs)}/8", flush=True)
        if len(xs) < 4:
            print("[CAMO-SAMPLE] body behind camera / not enough corners", flush=True)
            return None

        bx0, bx1 = min(xs), max(xs)
        by0, by1 = min(ys), max(ys)
        bw = max(8.0, bx1 - bx0)
        bh = max(8.0, by1 - by0)

        # Expand outward so the grid captures the environment around the body.
        margin = 0.45
        ex0 = bx0 - bw * margin
        ey0 = by0 - bh * margin
        ew  = bw * (1.0 + 2.0 * margin)
        eh  = bh * (1.0 + 2.0 * margin)

        # Clamp the sample region to the screen bounds.
        rx0 = max(0, int(ex0))
        ry0 = max(0, int(ey0))
        rx1 = min(w, int(ex0 + ew))
        ry1 = min(h, int(ey0 + eh))
        rw = rx1 - rx0
        rh = ry1 - ry0
        print(f"[CAMO-SAMPLE] bbox=({bx0:.0f},{by0:.0f})-({bx1:.0f},{by1:.0f}) region=({rx0},{ry0}) {rw}x{rh}", flush=True)
        if rw < 8 or rh < 8:
            print("[CAMO-SAMPLE] region too small after clamp", flush=True)
            return None

        # Capture game pixels only — ESP overlay is hidden for the duration.
        bits = self._capture_region_bits(rx0, ry0, rw, rh, hide_overlay=True)

        if not bits:
            print("[CAMO-SAMPLE] capture returned no data", flush=True)
            return None
        print(f"[CAMO-SAMPLE] captured bits={len(bits)} (expected {rw*rh*4})", flush=True)

        stride = rw * 4

        # Body silhouette in region-local coords (so we can EXCLUDE it — we want the
        # floor/surroundings, not the character's own pixels).
        ib_x0 = bx0 - rx0
        ib_y0 = by0 - ry0
        ib_x1 = bx1 - rx0
        ib_y1 = by1 - ry0
        # Floor row just below the feet (region-local), used to recolour body-interior
        # cells with the ground the player is standing on.
        floor_ly = min(rh - 1, int(ib_y1 + bh * 0.12))

        def sample_block(lx, ly):
            rs = gs = bs = cnt = 0
            for ox in (-3, 0, 3):
                for oy in (-3, 0, 3):
                    sx = max(0, min(rw - 1, lx + ox))
                    sy = max(0, min(rh - 1, ly + oy))
                    idx = sy * stride + sx * 4
                    if idx + 2 < len(bits):
                        bs += bits[idx]
                        gs += bits[idx + 1]
                        rs += bits[idx + 2]
                        cnt += 1
            if cnt == 0:
                return None
            return (rs // cnt, gs // cnt, bs // cnt)

        G = 10          # 10x10 mosaic = 100 patches
        points = []
        relocated = 0
        for j in range(G):
            for i in range(G):
                lx = int(((i + 0.5) / G) * rw)
                ly = int(((j + 0.5) / G) * rh)
                # If this cell falls on the body, sample the floor beneath the feet
                # instead (same column), so the body picks up ground colour, not itself.
                if ib_x0 <= lx <= ib_x1 and ib_y0 <= ly <= ib_y1:
                    ly = floor_ly
                    relocated += 1
                col = sample_block(lx, ly)
                if col is None:
                    continue
                u = (i + 0.5) / G
                v = (j + 0.5) / G
                points.append((u, v, col))

        print(f"[CAMO-SAMPLE] built points={len(points)} (body-cells relocated to floor={relocated})", flush=True)
        if len(points) < 4:
            print("[CAMO-SAMPLE] too few valid points", flush=True)
            return None
        return points

    def _sample_screenspace_pattern(self):
        """
        See-through camouflage sampler (3rd-person, camera behind player).

        For each UV cell on the body, determines the background colour that
        would be visible AT THAT SCREEN POSITION if the character were not
        there.  This creates a true transparency effect: the top of the body
        gets the sky/ceiling colour, the bottom gets the ground colour, the
        sides get the lateral environment colours.

        Implementation:
          1. Expand the capture region to 50% beyond the body bounding box so
             there is real background to sample even for body-centre UV cells.
          2. Walk budget is proportional to body size so we can always reach
             the body edge and step into the background from any UV cell.
          3. Directed walk: each UV cell walks TOWARD ITS NEAREST BODY EDGE
             first, then continues outward into the background.  This means
             upper-body cells preferentially sample what's above/above-left/
             above-right, lower-body cells sample the ground, etc.

        Returns list of (u, v, (r,g,b)) or None on failure.
        """
        try:
            import win32gui, win32ui, win32con
        except Exception as e:
            print(f"[CAMO-SS] win32 import failed: {e}", flush=True)
            return None

        cam = self.esp.get_camera()
        if not cam:
            print("[CAMO-SS] no camera", flush=True)
            return None
        local_pawn = self.esp._get_local_pawn()
        if not local_pawn:
            print("[CAMO-SS] no local pawn", flush=True)
            return None
        pos = self.esp.get_actor_root_pos(local_pawn)
        if not pos:
            print("[CAMO-SS] no root pos", flush=True)
            return None

        sw = self.width()
        sh = self.height()
        if sw < 100 or sh < 100:
            return None

        # ── Project body bounding box to screen ──────────────────────────────
        BODY_H  = 180.0   # character height (world units ≈ cm)
        BODY_HW = 45.0    # half-width
        corners = [
            (-BODY_HW, 0,       -BODY_HW), (-BODY_HW, 0,        BODY_HW),
            ( BODY_HW, 0,        BODY_HW), ( BODY_HW, 0,       -BODY_HW),
            (-BODY_HW, BODY_H,  -BODY_HW), (-BODY_HW, BODY_H,   BODY_HW),
            ( BODY_HW, BODY_H,   BODY_HW), ( BODY_HW, BODY_H,  -BODY_HW),
        ]
        rot  = self.esp.get_actor_root_rotation(local_pawn)
        yaw  = math.radians(rot[1]) if rot else 0.0
        cyaw, syaw = math.cos(yaw), math.sin(yaw)
        xs, ys = [], []
        for lx, ly, lz in corners:
            rx = lx * cyaw - lz * syaw
            rz = lx * syaw + lz * cyaw
            s = w2s((pos[0] + rx, pos[1] + ly, pos[2] + rz), cam, sw, sh)
            if s[2]:
                xs.append(s[0]); ys.append(s[1])
        if len(xs) < 4:
            print("[CAMO-SS] body not on screen", flush=True)
            return None

        bx0, bx1 = min(xs), max(xs)
        by0, by1 = min(ys), max(ys)
        bw = max(8.0, bx1 - bx0)
        bh = max(8.0, by1 - by0)

        # ── Background region: 50% expansion so body-centre UV cells can
        # reach real environment pixels with the directed walk below.
        HALO = 0.50
        quality = max(1, int(getattr(self.config, "camouflage_quality", 2)))
        # Walk budget: enough to cross the body from any interior point to
        # its edge, then step a bit further into the background.
        MAX_WALK_PX = int(max(bw, bh) * 0.65) + 8

        halo_px_x = max(6, int(bw * HALO))
        halo_px_y = max(6, int(bh * HALO))

        rx0 = max(0,  int(bx0) - halo_px_x)
        ry0 = max(0,  int(by0) - halo_px_y)
        rx1 = min(sw, int(bx1) + halo_px_x + 1)
        ry1 = min(sh, int(by1) + halo_px_y + 1)
        rw  = rx1 - rx0
        rh  = ry1 - ry0
        print(f"[CAMO-SS] body=({bx0:.0f},{by0:.0f})-({bx1:.0f},{by1:.0f}) "
              f"halo={halo_px_x}×{halo_px_y}px region={rw}×{rh}", flush=True)
        if rw < 8 or rh < 8:
            print("[CAMO-SS] region too small", flush=True)
            return None

        # ── Single capture (ESP overlay hidden so game pixels are sampled) ───
        bits = self._capture_region_bits(rx0, ry0, rw, rh, hide_overlay=True)

        if not bits or len(bits) < rw * rh * 4:
            print("[CAMO-SS] capture failed", flush=True)
            return None

        stride = rw * 4

        # ── Pixel helpers (region-local coordinates) ─────────────────────────
        def px(lx, ly):
            if lx < 0 or ly < 0 or lx >= rw or ly >= rh:
                return None
            i = ly * stride + lx * 4
            return (bits[i + 2], bits[i + 1], bits[i])   # BGR→RGB

        def block7(lx, ly):
            """7×7 average for maximum quality colour reads."""
            rs = gs = bs = n = 0
            for ox in range(-3, 4):
                for oy in range(-3, 4):
                    c = px(lx + ox, ly + oy)
                    if c:
                        rs += c[0]; gs += c[1]; bs += c[2]; n += 1
            return (rs // n, gs // n, bs // n) if n else None

        def block5(lx, ly):
            """5×5 average for smoother, less noisy colour reads."""
            rs = gs = bs = n = 0
            for ox in range(-2, 3):
                for oy in range(-2, 3):
                    c = px(lx + ox, ly + oy)
                    if c:
                        rs += c[0]; gs += c[1]; bs += c[2]; n += 1
            return (rs // n, gs // n, bs // n) if n else None

        def block3(lx, ly):
            """3×3 average around (lx,ly)."""
            rs = gs = bs = n = 0
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    c = px(lx + ox, ly + oy)
                    if c:
                        rs += c[0]; gs += c[1]; bs += c[2]; n += 1
            return (rs // n, gs // n, bs // n) if n else None

        q = max(1, int(getattr(self.config, "camouflage_quality", 2)))
        if q >= 5:
            sample_block = block7
        elif q >= 2:
            sample_block = block5
        else:
            sample_block = block3

        # ── Body silhouette in region-local coords ────────────────────────────
        ib_x0 = int(bx0) - rx0
        ib_y0 = int(by0) - ry0
        ib_x1 = int(bx1) - rx0
        ib_y1 = int(by1) - ry0
        bcx   = (ib_x0 + ib_x1) * 0.5
        bcy   = (ib_y0 + ib_y1) * 0.5

        # ── Halo background analysis (used only as last-resort fallback) ────────
        # Sample the halo ring for a representative background colour.
        # We do NOT use this for body-colour detection because the character's
        # body is painted with a multi-colour image — any single-colour threshold
        # would mistake painted body pixels for background.
        _halo_samples = []
        for hx in range(0, rw, 2):
            for hy in list(range(0, ib_y0)) + list(range(ib_y1, rh)):
                c = sample_block(hx, hy)
                if c and not self._is_esp_pixel(c):
                    _halo_samples.append(c)
        for hy in range(ib_y0, ib_y1, 2):
            for hx in list(range(0, ib_x0)) + list(range(ib_x1, rw)):
                c = sample_block(hx, hy)
                if c and not self._is_esp_pixel(c):
                    _halo_samples.append(c)

        if _halo_samples:
            _halo_samples.sort(key=lambda c: c[0] + c[1] + c[2])
            _q1 = len(_halo_samples) // 4
            _q3 = 3 * len(_halo_samples) // 4
            _mid = _halo_samples[_q1:_q3] or _halo_samples
            median_bg = (
                sum(c[0] for c in _mid) // len(_mid),
                sum(c[1] for c in _mid) // len(_mid),
                sum(c[2] for c in _mid) // len(_mid),
            )
        else:
            median_bg = (128, 128, 128)
        print(f"[CAMO-SS] median_bg={median_bg} halo_samples={len(_halo_samples)}", flush=True)

        # ── Directed background lookup ────────────────────────────────────────
        # For each body UV cell, walk toward the nearest bbox edge and sample
        # the first pixels found OUTSIDE the body bounding box.
        #
        # KEY DESIGN CHOICE: body detection is PURELY GEOMETRIC (bbox-based).
        # Color-based body detection breaks when the character is painted with a
        # multi-colour image — painted reds/yellows/greens all look like valid
        # background colours to a single-colour threshold.  Using the bbox avoids
        # this entirely: head area walks UP → finds sign/sky; feet walk DOWN →
        # find floor; sides walk LEFT/RIGHT → find environment walls/objects.

        def nearest_bg(lx, ly):
            """
            Walk outward from the character body bbox to find background colour.

            Walks toward the nearest bbox edge first so each body region gets
            the environment colour visible from that direction:
              head (rel_y ≈ 0) → walks UP  → samples sign / sky
              feet (rel_y ≈ 1) → walks DOWN → samples floor / ground
              sides             → walks L/R → samples surrounding objects
            """
            bw_px = max(1, ib_x1 - ib_x0)
            bh_px = max(1, ib_y1 - ib_y0)
            rel_x = max(0.0, min(1.0, (lx - ib_x0) / bw_px))
            rel_y = max(0.0, min(1.0, (ly - ib_y0) / bh_px))

            dir_candidates = sorted([
                (rel_x,       -1,  0),   # toward left  edge
                (1.0 - rel_x,  1,  0),   # toward right edge
                (rel_y,        0, -1),   # toward top   edge (head → up)
                (1.0 - rel_y,  0,  1),   # toward bottom edge (feet → down)
            ], key=lambda d: d[0])

            for _, dx, dy in dir_candidates:
                samples = []
                in_body = True
                for k in range(1, MAX_WALK_PX + 1):
                    sx = int(lx + dx * k)
                    sy = int(ly + dy * k)
                    if not (0 <= sx < rw and 0 <= sy < rh):
                        break
                    # Transition: detect the moment we leave the body bbox
                    if in_body:
                        still_in = (ib_x0 <= sx <= ib_x1 and ib_y0 <= sy <= ib_y1)
                        if not still_in:
                            in_body = False
                    # Once outside the body bbox, collect background pixels
                    if not in_body:
                        c = px(sx, sy)
                        if c and not self._is_esp_pixel(c):
                            samples.append(c)
                            if len(samples) >= 5:
                                break
                if samples:
                    r = sum(c[0] for c in samples) // len(samples)
                    g = sum(c[1] for c in samples) // len(samples)
                    b = sum(c[2] for c in samples) // len(samples)
                    return (r, g, b)

            return median_bg

        # ── Build the UV paint grid ───────────────────────────────────────────
        G = max(1, int(getattr(self.config, "camouflage_sample_size", 32)))
        quality = max(1, int(getattr(self.config, "camouflage_quality", 2)))
        points  = []
        seen_uv = set()

        def add_point_col(u, v, col):
            key = (round(u * 2000), round(v * 2000))
            if key in seen_uv or not col:
                return
            seen_uv.add(key)
            points.append((u, v, col))

        for j in range(G):
            for i in range(G):
                rs = gs = bs = ns = 0
                for sj in range(quality):
                    for si in range(quality):
                        fu = (i + (si + 0.5) / quality) / G
                        fv = (j + (sj + 0.5) / quality) / G
                        gsx = bx0 + fu * bw
                        gsy = by0 + fv * bh
                        lx = gsx - rx0
                        ly = gsy - ry0
                        col = nearest_bg(int(lx), int(ly))
                        if col:
                            rs += col[0]; gs += col[1]; bs += col[2]; ns += 1
                if ns:
                    add_point_col(
                        (i + 0.5) / G, (j + 0.5) / G,
                        (rs // ns, gs // ns, bs // ns),
                    )

        # Border UV points (u/v = 0.01 & 0.99) for arm/edge UV islands
        border_lx, border_ly = bcx, bcy
        for edge in (0.01, 0.99):
            for frac in [(k + 0.5) / G for k in range(G)]:
                col = nearest_bg(int(border_lx), int(border_ly))
                if col:
                    add_point_col(edge, frac, col)
                    add_point_col(frac, edge, col)

        print(f"[CAMO-SS] points={len(points)} G={G} quality={quality} "
              f"bg={median_bg}", flush=True)
        if len(points) < 4:
            print("[CAMO-SS] too few points", flush=True)
            return None
        return points

    def _sample_screen_color(self):
        """Sample N×N pixels around crosshair. Tries game DC, desktop DC, then BitBlt fallback."""
        try:
            import win32gui
            w = self.width()
            h = self.height()
            if w < 100 or h < 100:
                return None
            cx, cy = w // 2, h // 2
            grid = self.config.camouflage_sample_size
            half = grid // 2

            # Try game window DC first, then desktop DC
            hdc = None
            hwnd_used = 0
            if self.game_hwnd:
                hdc = win32gui.GetDC(self.game_hwnd)
                hwnd_used = self.game_hwnd
            if not hdc:
                hdc = win32gui.GetDC(0)
            if not hdc:
                return None

            total = 0
            r_sum = g_sum = b_sum = 0
            CLR_INVALID = 0xFFFFFFFF
            for dx in range(-half, half + 1):
                for dy in range(-half, half + 1):
                    px = cx + dx * 4
                    py = cy + dy * 4
                    pixel = win32gui.GetPixel(hdc, px, py)
                    if pixel != CLR_INVALID:
                        r_sum += pixel & 0xFF
                        g_sum += (pixel >> 8) & 0xFF
                        b_sum += (pixel >> 16) & 0xFF
                        total += 1

            win32gui.ReleaseDC(hwnd_used, hdc)

            if total > 0:
                return (r_sum // total, g_sum // total, b_sum // total)

            # Fallback: BitBlt the crosshair region
            return self._sample_screen_bitblt(cx, cy, half)
        except Exception:
            return None

    def _sample_screen_bitblt(self, cx, cy, half):
        """Fallback: capture region via BitBlt then read pixels."""
        try:
            import win32gui, win32ui, win32con
            hwnd = self.game_hwnd if self.game_hwnd else 0
            hdc_src = win32gui.GetDC(hwnd) if hwnd else win32gui.GetDC(0)
            if not hdc_src:
                return None

            step = 4
            region_w = (half * 2 + 1) * step
            region_h = (half * 2 + 1) * step
            x0 = cx - half * step
            y0 = cy - half * step

            hdc_mem = win32gui.CreateCompatibleDC(hdc_src)
            bmp = win32gui.CreateCompatibleBitmap(hdc_src, region_w, region_h)
            win32gui.SelectObject(hdc_mem, bmp)

            win32gui.BitBlt(hdc_mem, 0, 0, region_w, region_h,
                           hdc_src, x0, y0, win32con.SRCCOPY)

            # Read pixels: GetBitmapBits returns bytes (BGR format)
            bits = win32gui.GetBitmapBits(bmp, True)

            win32gui.DeleteObject(bmp)
            win32gui.DeleteDC(hdc_mem)
            win32gui.ReleaseDC(hwnd, hdc_src)

            total = 0
            r_sum = g_sum = b_sum = 0
            stride = region_w * 4
            for dy in range(0, region_h, step):
                for dx in range(0, region_w, step):
                    idx = dy * stride + dx * 4
                    if idx + 3 < len(bits):
                        # BMP stores BGR, we want RGB
                        b = bits[idx]
                        g = bits[idx + 1]
                        r = bits[idx + 2]
                        r_sum += r
                        g_sum += g
                        b_sum += b
                        total += 1

            if total == 0:
                return None
            return (r_sum // total, g_sum // total, b_sum // total)
        except Exception:
            return None

    def paintEvent(self, event):
        try:
            self._paint_esp(event)
        except Exception:
            pass

    def _paint_esp(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont("Consolas", 10)
        painter.setFont(font)

        w = self.width()
        h = self.height()

        def _draw_label(painter, x, y, text, fg_color, bg_alpha=160,
                        pad_x=6, pad_y=3, radius=4):
            """Draw text with a semi-transparent dark rounded-rect background."""
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            rx = x - pad_x
            ry = y - th + 1 - pad_y
            rw = tw + pad_x * 2
            rh = th + pad_y * 2 - 1
            painter.save()
            painter.setBrush(QColor(0, 0, 0, bg_alpha))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(rx, ry, rw, rh, radius, radius)
            painter.setPen(QPen(fg_color))
            painter.drawText(x, y, text)
            painter.restore()

        cam = self.esp.get_camera()
        # During free-cam / transitions get_camera may fail briefly.
        # Fall back to the last valid camera so the overlay stays rendered.
        if cam:
            self._last_cam = cam
        else:
            cam = getattr(self, "_last_cam", None)
        if not cam:
            painter.setPen(QPen(QColor(255, 255, 255)))
            painter.drawText(10, 20, "NO CAMERA")
            return

        # Sticky player cache in core.get_players() — survives empty/partial reads (max 24).
        all_players = self.esp.get_players(
            include_local=self.config.show_local,
            team_filter=self.config.team_filter,
        )

        # Get local player position for radar
        local_pos = None
        if all_players:
            for p in all_players:
                if p["is_local"]:
                    local_pos = p["pos"]
                    break

        # Draw each player — wrapped so one bad player never kills the whole frame
        for pdata in all_players:
          try:
            is_local = pdata["is_local"]
            pos = pdata["pos"]
            actor = pdata["actor"]
            ps = pdata["player_state"]
            idx = pdata["idx"]

            # Distance scaling
            d = dist(pos, cam["loc"])
            scale = 1.0
            if self.config.distance_scaling and d > 0:
                scale = self.config.scale_reference_dist / d
                scale = max(0.3, min(scale, 3.0))

            # Project center position — always returns (sx, sy, on_screen)
            screen_center = w2s(pos, cam, w, h)
            sx, sy, on_screen = screen_center
            sy += self.config.box_y_offset

            if is_local:
                color = self.config.local_color
            else:
                # Determine base color by team
                is_hunter = pdata.get("is_hunter")
                if is_hunter is True:
                    base_color = self.config.hunter_color
                elif is_hunter is False:
                    base_color = self.config.survivor_color
                else:
                    base_color = self.config.enemy_color  # unknown team fallback

                color = base_color

            # --- Off-screen indicator: triangle arrow pointing toward player ---
            if not on_screen:
                ex, ey, ux, uy = oof_indicator_pos(
                    sx, sy, w, h, self.config.oof_arrow_radius)
                # Perpendicular to arrow direction
                px, py = -uy, ux
                # Triangle: tip at edge, two base points behind
                SZ = 10
                tip  = (int(ex + ux * SZ), int(ey + uy * SZ))
                base1 = (int(ex - ux * SZ + px * SZ), int(ey - uy * SZ + py * SZ))
                base2 = (int(ex - ux * SZ - px * SZ), int(ey - uy * SZ - py * SZ))
                tri = QPolygonF([QPointF(*tip), QPointF(*base1), QPointF(*base2)])
                painter.setPen(QPen(QColor(0, 0, 0), 1))
                painter.setBrush(QColor(*color))
                painter.drawPolygon(tri)
                # OOF label — toggles for name / distance / health number
                oof_parts = []
                player_name = pdata.get("player_name", "").strip()
                if is_hunter is True:
                    team_tag = "H"
                elif is_hunter is False:
                    team_tag = "S"
                else:
                    team_tag = "?"
                if self.config.oof_show_names:
                    if player_name:
                        oof_parts.append(f"[{team_tag}] {player_name}")
                    else:
                        oof_parts.append(f"[{team_tag}]")
                if self.config.oof_show_distance:
                    d_m = int(d / 100.0)
                    oof_parts.append(f"{d_m}m")
                if self.config.oof_show_health and actor:
                    try:
                        health_info = self.esp.get_health(actor, ps)
                        if health_info and health_info[0] is not None:
                            oof_parts.append(f"{int(health_info[0])} HP")
                    except Exception:
                        pass
                if oof_parts:
                    _draw_label(painter, int(ex + ux * 14), int(ey + uy * 14),
                                " | ".join(oof_parts), QColor(*color))
                continue  # skip on-screen rendering for this player

            # Clamped coords for on-screen elements (dots, bars, labels)
            # Snap lines use raw sx/sy so they reach screen edges
            dsx, dsy = clamp_screen(sx, sy - self.config.box_y_offset, w, h)
            dsy += self.config.box_y_offset

            # Dot ESP
            if self.config.dot_esp:
                radius = int(self.config.dot_radius * scale)
                self._draw_dot(painter, dsx, dsy, max(2, radius), color)

            # 2D Box ESP
            if self.config.box_esp:
                rot = self.esp.get_actor_root_rotation(actor) if actor else None
                hw = self.config.box_height_world / 3.0
                draw_2d_box(painter, pos, cam, w, h,
                            self.config.box_height_world, hw, rot, color, scale)

            # Skeleton ESP — isolated so bone-read failures never affect dot/box/labels
            if self.config.skeleton_esp and actor and not is_local:
                try:
                    bones = self.esp.get_skeleton_positions_by_indices(
                        actor, self.config.bone_indices)
                    if not bones:
                        bones = self.esp.get_skeleton_positions(actor)
                    if bones:
                        draw_skeleton(
                            painter, bones, cam, w, h, self.config.skeleton_color)
                except Exception:
                    pass

            # Health / Shield bars
            if self.config.health_bar or self.config.shield_bar:
                health_info = self.esp.get_health(actor, ps)
                if health_info and health_info[0] is not None:
                    hp, sh = health_info
                    bar_x = dsx - 12 * scale
                    bar_y = dsy - 20 * scale
                    draw_health_bar(painter, bar_x, bar_y, 24 * scale, 4, hp, sh if self.config.shield_bar else None)

            # Snap lines
            if self.config.snap_lines:
                painter.setPen(QPen(QColor(*color), 1))
                painter.drawLine(int(w / 2), int(h), int(sx), int(sy))

            # Labels
            label_parts = []
            if self.config.show_names:
                if is_local:
                    label_parts.append("YOU")
                else:
                    is_hunter   = pdata.get("is_hunter")
                    player_name = pdata.get("player_name", "").strip()
                    team_str    = "Hunter" if is_hunter is True else ("Survivor" if is_hunter is False else "Player")
                    if player_name:
                        label_parts.append(f"[{team_str}] {player_name}")
                    else:
                        label_parts.append(f"[{team_str}]")
            if self.config.show_distance:
                dm = int(d / 100)
                label_parts.append(f"{dm}m")
            if label_parts:
                painter.setPen(QPen(QColor(*color)))
                text = " | ".join(label_parts)
                label_x = int(dsx + self.config.dot_radius * scale + 4)
                label_y = int(dsy)
                painter.drawText(label_x, label_y, text)

          except Exception:
            pass  # never let one bad player crash the whole frame

        # Player count
        non_local = [p for p in all_players if not p["is_local"]]
        _draw_label(painter, 10, 20, f"Players: {len(non_local)}", QColor(255, 255, 255))

        # FPS counters — overlay (timer-derived) + in-game (500 Hz camera tracker)
        overlay_fps = round(1000 / max(1, self.timer.interval()))
        game_fps    = self._game_fps_info.get("game_fps", 0)

        if self._paint_throttle:
            priority_label = "IDLE" if self._paint_throttle_quality >= 5 else "LOW"
            _draw_label(painter, 10, 40,
                        f"PAINTING... (game: {priority_label} priority)",
                        QColor(255, 200, 80))
        else:
            ovl_color = (
                QColor(100, 255, 140) if overlay_fps >= 45
                else QColor(255, 220, 80) if overlay_fps >= 25
                else QColor(255, 80, 80)
            )
            game_color = (
                QColor(100, 255, 140) if game_fps >= 45
                else QColor(255, 220, 80) if game_fps >= 25
                else QColor(255, 80, 80)
            ) if game_fps > 0 else QColor(150, 150, 150)

            _draw_label(painter, 10, 40, f"OVL: {overlay_fps}fps", ovl_color)
            game_label = f"GAME: {game_fps}fps" if game_fps > 0 else "GAME: --"
            _draw_label(painter, 10, 57, game_label, game_color)

        # Camouflage status — y=74, below both FPS lines
        if self._camo_feedback_count > 0 and self._camo_feedback:
            self._camo_feedback_count -= 1
            fg = QColor(*self._camouflage_color) if (self._camouflage_active and self._camouflage_color) else QColor(200, 200, 200)
            _draw_label(painter, 10, 74, self._camo_feedback, fg)
        elif self._camouflage_active and self._camouflage_color:
            _draw_label(painter, 10, 74, "CAMO ON (3D)", QColor(*self._camouflage_color))
        elif self.config.camouflage_enabled:
            _draw_label(painter, 10, 74, "CAMO OFF (F10)", QColor(150, 150, 150))

        # Aimbot
        if self.config.aimbot_enabled:
            cx, cy = w / 2, h / 2
            if self.config.aimbot_show_fov:
                painter.setPen(QPen(QColor(255, 255, 255), 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(
                    int(cx - self.config.aimbot_fov),
                    int(cy - self.config.aimbot_fov),
                    self.config.aimbot_fov * 2,
                    self.config.aimbot_fov * 2,
                )
            best_target = self._find_best_target(cam, w, h)
            if best_target and self._aim_key_held():
                self._aim_at(best_target)

        # Radar
        if self.config.radar_enabled and local_pos:
            radar_x = w - self.config.radar_size - 20
            radar_y = 20 + self.config.radar_size // 2
            enemy_list = [p for p in all_players if not p["is_local"]]
            for p in enemy_list:
                is_hunter = p.get("is_hunter")
                if is_hunter is True:
                    p["color"] = self.config.hunter_color
                elif is_hunter is False:
                    p["color"] = self.config.survivor_color
                else:
                    p["color"] = self.config.enemy_color
            draw_radar(painter, cam, local_pos, enemy_list,
                       radar_x, radar_y,
                       self.config.radar_size, self.config.radar_range,
                       self.config.radar_color, self.config.radar_opacity)

    def _draw_dot(self, painter, cx, cy, r, color):
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(*color))
        painter.drawEllipse(int(cx - r), int(cy - r), r * 2, r * 2)

    # -----------------------------------------------------------------------
    # Aimbot
    # -----------------------------------------------------------------------
    def _aim_key_held(self):
        vk = vk_from_name(self.config.aimbot_key)
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    def _find_best_target(self, camera, screen_w, screen_h):
        world = self.esp._get_world()
        local_pc = self.esp._get_local_controller(world) if world else 0
        local_pawn = rp(self.esp.pm, local_pc + self.esp.offsets["APlayerController::AcknowledgedPawn"]) if local_pc else 0
        local_pos = self.esp.get_actor_root_pos(local_pawn) if local_pawn else None

        cx, cy = screen_w / 2, screen_h / 2
        cam_loc = camera["loc"]
        best_dist = float("inf")
        best_target = None
        for pdata in self.esp.get_players(include_local=False, team_filter=self.config.team_filter):
            actor = pdata.get("actor")
            if not actor or actor == local_pawn or pdata.get("is_local"):
                continue
            pos = pdata["pos"]
            if local_pos and dist(pos, local_pos) < 150.0:
                continue
            if dist(pos, cam_loc) < 100.0:
                continue
            aim_pos = self._get_aim_point(pdata)
            if not aim_pos:
                continue
            s = w2s(aim_pos, camera, screen_w, screen_h)
            if not s[2]:
                continue
            dx = s[0] - cx
            dy = s[1] - cy
            d = math.sqrt(dx * dx + dy * dy)
            if d <= self.config.aimbot_fov and d < best_dist:
                best_dist = d
                best_target = aim_pos
        return best_target

    def _get_aim_point(self, pdata):
        """World position to aim at — always targets the chest."""
        actor = pdata.get("actor")
        if actor:
            try:
                bi = self.config.bone_indices
                chest_bones = {
                    "spine_02": bi.get("spine_02", 36),
                    "spine_03": bi.get("spine_03", 52),
                }
                bones = self.esp.get_skeleton_positions_by_indices(actor, chest_bones)
                if bones:
                    # Prefer mid-chest (spine_02); average both if available for stability.
                    if "spine_02" in bones and "spine_03" in bones:
                        a, b = bones["spine_02"], bones["spine_03"]
                        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2)
                    if "spine_02" in bones:
                        return bones["spine_02"]
                    if "spine_03" in bones:
                        return bones["spine_03"]
            except Exception:
                pass
        pos = pdata.get("pos")
        if not pos:
            return None
        off = self.config.aimbot_target_offset
        if abs(off) < 1.0:
            off = 60.0   # chest height fallback when bone read fails
        return (pos[0], pos[1], pos[2] + off)

    @staticmethod
    def _look_at_rotation(from_pos, to_pos):
        """UE FRotator (pitch, yaw, roll) to look from -> to."""
        dx = to_pos[0] - from_pos[0]
        dy = to_pos[1] - from_pos[1]
        dz = to_pos[2] - from_pos[2]
        horiz = math.sqrt(dx * dx + dy * dy)
        if horiz < 1e-4 and abs(dz) < 1e-4:
            return None
        pitch = math.degrees(math.atan2(dz, horiz))
        yaw = math.degrees(math.atan2(dy, dx))
        return (pitch, yaw, 0.0)

    @staticmethod
    def _yaw_delta(from_yaw, to_yaw):
        d = to_yaw - from_yaw
        while d > 180.0:
            d -= 360.0
        while d < -180.0:
            d += 360.0
        return d

    def _read_control_rotation(self):
        world = self.esp._get_world()
        if not world:
            return None
        pc = self.esp._get_local_controller(world)
        if not pc:
            return None
        addr = pc + self.esp.offsets["AController::ControlRotation"]
        rot = rvec3(self.esp.pm, addr)   # UE5 FRotator = 3 doubles
        if not all(math.isfinite(v) for v in rot):
            return None
        return rot

    def _write_control_rotation(self, rot):
        world = self.esp._get_world()
        if not world:
            return False
        pc = self.esp._get_local_controller(world)
        if not pc:
            return False
        addr = pc + self.esp.offsets["AController::ControlRotation"]
        return wvec3(self.esp.pm, addr, rot)

    def _aim_at(self, target_pos):
        cam = self.esp.get_camera()
        if not cam:
            return
        current = self._read_control_rotation()
        if current is None:
            return
        target_rot = self._look_at_rotation(cam["loc"], target_pos)
        if target_rot is None:
            return
        smooth = max(0.01, min(1.0, self.config.aimbot_smooth))
        dp = target_rot[0] - current[0]
        dy = self._yaw_delta(current[1], target_rot[1])
        new_pitch = current[0] + dp * smooth
        new_yaw = current[1] + dy * smooth
        self._write_control_rotation((new_pitch, new_yaw, current[2]))
