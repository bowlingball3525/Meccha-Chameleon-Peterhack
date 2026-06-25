#!/usr/bin/env python3
"""
Core game reading engine for MECCA CHAMELEON (UE5.6) ESP.
Memory primitives, pattern scanning, FName resolution, object array,
offset resolution, and game state reading.
"""
import struct
import math
import os
import ctypes
import pymem
from contextlib import contextmanager

PAINT_PRESETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paint_presets")
PAINT_FILE_MAGIC = b"MECHPAINT\x00"
PAINT_FILE_VERSION = 2
PAINT_FILE_VERSION_V1 = 1


def _rotation_to_axes(rot):
    pitch, yaw, roll = [math.radians(x) for x in rot]
    sp, cp = math.sin(pitch), math.cos(pitch)
    sy, cy = math.sin(yaw), math.cos(yaw)
    sr, cr = math.sin(roll), math.cos(roll)
    forward = (cp * cy, cp * sy, sp)
    right = (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp)
    up = (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp)
    return forward, right, up


def world_to_screen(world_pos, camera, screen_w, screen_h):
    """Project a world position to screen pixels. Returns (sx, sy, on_screen)."""
    cam_loc = camera["loc"]
    cam_rot = camera["rot"]
    fov = camera["fov"]
    forward, right, up = _rotation_to_axes(cam_rot)
    dx = world_pos[0] - cam_loc[0]
    dy = world_pos[1] - cam_loc[1]
    dz = world_pos[2] - cam_loc[2]
    view_x = dx * forward[0] + dy * forward[1] + dz * forward[2]
    view_y = dx * right[0] + dy * right[1] + dz * right[2]
    view_z = dx * up[0] + dy * up[1] + dz * up[2]

    behind = view_x <= 0.1
    if behind:
        view_x = -view_x or 0.1
        view_y = -view_y
        view_z = -view_z

    aspect = screen_w / screen_h if screen_h > 0 else 16.0 / 9.0
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

    margin = 8
    on_screen = (
        not behind
        and margin <= screen_x <= screen_w - margin
        and margin <= screen_y <= screen_h - margin
    )
    return screen_x, screen_y, on_screen


# ---------------------------------------------------------------------------
# Bootstrap offsets: stable UObject/UStruct/FField layout
# ---------------------------------------------------------------------------
OFFSETS = {
    "UObjectBase::ClassPrivate": 0x10,
    "UObjectBase::NamePrivate": 0x18,
    "UObjectBase::OuterPrivate": 0x20,
    "UStruct::SuperStruct": 0x40,
    "UStruct::ChildProperties": 0x50,
    "FField::Next": 0x18,
    "FField::NamePrivate": 0x20,
    "FProperty::Offset_Internal": 0x44,
    "FCameraCacheEntry::POV": 0x10,
    "FMinimalViewInfo::Location": 0x0,
    "FMinimalViewInfo::Rotation": 0x18,
    "FMinimalViewInfo::FOV": 0x30,
}

# ---------------------------------------------------------------------------
# Memory primitives
# ---------------------------------------------------------------------------
def rp(pm, addr):
    try:
        return struct.unpack("<Q", pm.read_bytes(addr, 8))[0]
    except Exception:
        return 0

def ru32(pm, addr):
    try:
        return struct.unpack("<I", pm.read_bytes(addr, 4))[0]
    except Exception:
        return 0

def ru16(pm, addr):
    try:
        return struct.unpack("<H", pm.read_bytes(addr, 2))[0]
    except Exception:
        return 0

def rfloat(pm, addr):
    try:
        return struct.unpack("<f", pm.read_bytes(addr, 4))[0]
    except Exception:
        return 0.0

def wfloat(pm, addr, value):
    try:
        pm.write_bytes(addr, struct.pack("<f", value), 4)
        return True
    except Exception:
        return False

def wvec3(pm, addr, vec):
    """Write 3 doubles (FVector / FRotator) to memory."""
    try:
        pm.write_bytes(addr, struct.pack("<ddd", vec[0], vec[1], vec[2]), 24)
        return True
    except Exception:
        return False

def rvec3(pm, addr):
    try:
        return struct.unpack("<ddd", pm.read_bytes(addr, 24))
    except Exception:
        return (0.0, 0.0, 0.0)

def rvec3_f(pm, addr):
    try:
        return struct.unpack("<fff", pm.read_bytes(addr, 12))
    except Exception:
        return (0.0, 0.0, 0.0)

def rfquat(pm, addr):
    try:
        return struct.unpack("<dddd", pm.read_bytes(addr, 32))
    except Exception:
        return (0.0, 0.0, 0.0, 1.0)

def read_array(pm, addr):
    try:
        data = rp(pm, addr)
        count = ru32(pm, addr + 8)
        cap = ru32(pm, addr + 0x10)
        return data, count, cap
    except Exception:
        return 0, 0, 0

def read_tarray_ptr(pm, addr):
    try:
        data = rp(pm, addr)
        count = ru32(pm, addr + 8)
        return data, count
    except Exception:
        return 0, 0

def dist(a, b):
    return math.sqrt(
        (a[0] - b[0]) ** 2 +
        (a[1] - b[1]) ** 2 +
        (a[2] - b[2]) ** 2
    )

# ---------------------------------------------------------------------------
# Pattern scanner
# ---------------------------------------------------------------------------
class PatternScanner:
    CHUNK_SIZE = 0x200000

    def __init__(self, pm, module_name):
        self.pm = pm
        self.module = pymem.process.module_from_name(pm.process_handle, module_name)
        if not self.module:
            raise RuntimeError(f"Module {module_name} not found")
        self.base = self.module.lpBaseOfDll
        self.size = self.module.SizeOfImage

    def _match_at(self, data, offset, pattern, mask):
        for j in range(len(pattern)):
            if mask[j] and data[offset + j] != pattern[j]:
                return False
        return True

    def scan_all(self, pattern, mask):
        pat_len = len(pattern)
        if pat_len == 0 or self.size == 0:
            return
        step = self.CHUNK_SIZE
        for start in range(0, self.size, step):
            end = min(start + step + pat_len, self.size)
            read_size = end - start
            try:
                data = self.pm.read_bytes(self.base + start, read_size)
            except Exception:
                continue
            scan_len = len(data) - pat_len
            for i in range(scan_len):
                if self._match_at(data, i, pattern, mask):
                    yield self.base + start + i

    def scan(self, pattern, mask):
        for addr in self.scan_all(pattern, mask):
            return addr
        return 0

# ---------------------------------------------------------------------------
# FName resolution
# ---------------------------------------------------------------------------
class FNameResolver:
    BLOCK_TABLE_OFFSETS = (
        0x8, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38,
        0x40, 0x48, 0x50, 0x58, 0x60, 0x68, 0x70,
    )

    def __init__(self, pm, fname_pool):
        self.pm = pm
        self.fname_pool = fname_pool
        self.block_table_off = 0x10
        self.header_style = "ue5"
        self._detect_layout()

    def _read_entry(self, entry_id, table_off, style):
        block_idx = entry_id >> 16
        within = (entry_id & 0xFFFF) << 1
        block_addr = rp(self.pm, self.fname_pool + table_off + block_idx * 8)
        if not block_addr:
            return None
        hdr = ru16(self.pm, block_addr + within)
        if style == "ue4":
            is_wide = hdr & 1
            length = hdr >> 1
        elif style == "custom":
            is_wide = hdr & 1
            length = (hdr >> 6) & 0x3FF
        else:
            length = hdr & 0x3FF
            is_wide = (hdr >> 10) & 1
        if length == 0 or length > 512:
            return None
        if is_wide:
            raw = self.pm.read_bytes(block_addr + within + 2, length * 2)
            return raw.decode("utf-16-le", errors="ignore")
        raw = self.pm.read_bytes(block_addr + within + 2, length)
        return raw.decode("latin-1")

    def _detect_layout(self):
        for off in self.BLOCK_TABLE_OFFSETS:
            for style in ("custom", "ue5", "ue4"):
                try:
                    if self._read_entry(0, off, style) == "None":
                        self.block_table_off = off
                        self.header_style = style
                        return
                except Exception:
                    continue

    def resolve(self, entry_id):
        try:
            name = self._read_entry(entry_id, self.block_table_off, self.header_style)
            if name is not None:
                return name
        except Exception:
            pass
        for off in self.BLOCK_TABLE_OFFSETS:
            for style in ("custom", "ue5", "ue4"):
                if off == self.block_table_off and style == self.header_style:
                    continue
                try:
                    name = self._read_entry(entry_id, off, style)
                    if name is not None:
                        self.block_table_off = off
                        self.header_style = style
                        return name
                except Exception:
                    continue
        return None

# ---------------------------------------------------------------------------
# UE Object array
# ---------------------------------------------------------------------------
class UObjectArray:
    def __init__(self, pm, guobject_array, fname_pool):
        self.pm = pm
        self.guobject_array = guobject_array
        self.fnames = FNameResolver(pm, fname_pool)
        self._meta_class_addr = None
        self._class_cache = {}

    def obj_name(self, obj):
        return self.fnames.resolve(ru32(self.pm, obj + OFFSETS["UObjectBase::NamePrivate"]))

    def obj_class(self, obj):
        return rp(self.pm, obj + OFFSETS["UObjectBase::ClassPrivate"])

    def class_name(self, obj):
        if not obj:
            return ""
        cls = self.obj_class(obj)
        return self.obj_name(cls) if cls else ""

    def iter_objects(self):
        ptr = rp(self.pm, self.guobject_array + 0x10)
        if not ptr:
            return
        for chunk_idx in range(64):
            chunk = rp(self.pm, ptr + chunk_idx * 8)
            if not chunk:
                break
            for within in range(0x10000):
                obj = rp(self.pm, chunk + within * 0x18)
                if obj:
                    yield obj

    def _meta_class(self):
        if self._meta_class_addr is None or not self._meta_class_addr:
            for obj in self.iter_objects():
                if self.obj_name(obj) == "Class":
                    self._meta_class_addr = obj
                    break
        return self._meta_class_addr

    def find_class(self, name):
        cached = self._class_cache.get(name)
        if cached:
            if self.obj_name(cached) == name:
                return cached
            del self._class_cache[name]
        meta = self._meta_class()
        if not meta:
            return 0
        for obj in self.iter_objects():
            if self.obj_class(obj) == meta and self.obj_name(obj) == name:
                self._class_cache[name] = obj
                return obj
        return 0

    def find_first_instance(self, class_name, skip_default=True):
        cls = self.find_class(class_name)
        if not cls:
            return 0
        for obj in self.iter_objects():
            if self.obj_class(obj) == cls:
                name = self.obj_name(obj)
                if skip_default and name and name.startswith("Default__"):
                    continue
                return obj
        return 0

    def find_instances(self, class_name, skip_default=True):
        cls = self.find_class(class_name)
        if not cls:
            return
        for obj in self.iter_objects():
            if self.obj_class(obj) == cls:
                name = self.obj_name(obj)
                if skip_default and name and name.startswith("Default__"):
                    continue
                yield obj

    def find_object_by_name(self, name):
        for obj in self.iter_objects():
            if self.obj_name(obj) == name:
                return obj
        return 0

    def find_objects_by_class_name(self, cls_name_part):
        for obj in self.iter_objects():
            cname = self.class_name(obj)
            if cls_name_part in cname:
                yield obj

# ---------------------------------------------------------------------------
# Offset resolver (resolves FField property chains)
# ---------------------------------------------------------------------------
class OffsetResolver:
    def __init__(self, pm, objects):
        self.pm = pm
        self.objects = objects
        self.cache = dict(OFFSETS)

    def field_name(self, field):
        return self.objects.fnames.resolve(
            ru32(self.pm, field + self.cache["FField::NamePrivate"])
        )

    def search_properties(self, cls, names):
        prop = rp(self.pm, cls + self.cache["UStruct::ChildProperties"])
        depth = 0
        while prop and depth < 512:
            name = self.field_name(prop)
            if name in names:
                return name, ru32(self.pm, prop + self.cache["FProperty::Offset_Internal"])
            prop = rp(self.pm, prop + self.cache["FField::Next"])
            depth += 1
        super_cls = rp(self.pm, cls + self.cache["UStruct::SuperStruct"])
        seen = {cls}
        while super_cls and super_cls not in seen:
            seen.add(super_cls)
            prop = rp(self.pm, super_cls + self.cache["UStruct::ChildProperties"])
            depth = 0
            while prop and depth < 512:
                name = self.field_name(prop)
                if name in names:
                    return name, ru32(self.pm, prop + self.cache["FProperty::Offset_Internal"])
                prop = rp(self.pm, prop + self.cache["FField::Next"])
                depth += 1
            super_cls = rp(self.pm, super_cls + self.cache["UStruct::SuperStruct"])
        return None, 0

    def _resolve_on_class(self, cls, prop_name):
        prop = rp(self.pm, cls + self.cache["UStruct::ChildProperties"])
        depth = 0
        while prop and depth < 512:
            name = self.field_name(prop)
            if name == prop_name:
                return ru32(self.pm, prop + self.cache["FProperty::Offset_Internal"])
            prop = rp(self.pm, prop + self.cache["FField::Next"])
            depth += 1
        return None

    def resolve(self, class_name, prop_name):
        key = f"{class_name}::{prop_name}"
        if key in self.cache:
            return self.cache[key]
        cls = self.objects.find_class(class_name)
        if not cls:
            return None
        offset = self._resolve_on_class(cls, prop_name)
        seen = {cls}
        while offset is None:
            super_cls = rp(self.pm, cls + self.cache["UStruct::SuperStruct"])
            if not super_cls or super_cls in seen:
                break
            seen.add(super_cls)
            offset = self._resolve_on_class(super_cls, prop_name)
        if offset is not None:
            self.cache[key] = offset
        return offset

    def resolve_map(self, mapping):
        out = {}
        for key, (cls, prop) in mapping.items():
            val = self.resolve(cls, prop)
            if val is None:
                raise RuntimeError(f"Could not resolve offset {key} ({cls}.{prop})")
            out[key] = val
        return out

# ---------------------------------------------------------------------------
# Game reader
# ---------------------------------------------------------------------------
class MecchaESP:
    MAX_ESP_PLAYERS = 24
    PAINT_UV_BATCH_SIZE = 128    # default stamps per remote call (frozen apply)
    PAINT_UV_BATCH_FAST = 256    # larger batches when game is frozen
    PAINT_UV_BATCH_SAFE = 24     # max stamps per shellcode (avoids timeout use-after-free)
    PAINT_LIVE_BATCH_PAUSE = 0.022  # seconds between live batches (camo unfrozen path)
    MIN_PAINT_ALPHA = 24         # skip transparent PNG pixels (avoid black stamps)
    # Paint-sphere atlas (matches F10 camo layout):
    #   u∈[0,½) front column, u∈[½,1] back column, v=0 feet → v=1 head.
    #   Border strips at 0.01/0.99 carry head, arms, and torso edges.
    PAINT_FRONT_HEMI = (0.0, 0.0, 0.5, 1.0)
    PAINT_BACK_HEMI = (0.5, 0.0, 1.0, 1.0)
    PAINT_HEMI_RECTS = (PAINT_FRONT_HEMI, PAINT_BACK_HEMI)
    PAINT_UV_BORDER = 0.01
    PAINT_UV_SEAM = 0.49
    # Legacy panel centers (screen-calibration / opposite-side mirror only).
    PAINT_FRONT_U = 0.25
    PAINT_BACK_U = 0.75
    PAINT_BODY_VC = 0.50
    PAINT_BODY_HU = 0.22
    PAINT_BODY_HV = 0.38

    @classmethod
    def paint_body_uv_rect(cls, u_center, v_center, half_u, half_v):
        return (
            max(0.0, u_center - half_u),
            max(0.0, v_center - half_v),
            min(1.0, u_center + half_u),
            min(1.0, v_center + half_v),
        )

    @classmethod
    def paint_body_uv_rects(cls, v_center=None, half_u=None, half_v=None):
        vc = cls.PAINT_BODY_VC if v_center is None else v_center
        hu = cls.PAINT_BODY_HU if half_u is None else half_u
        hv = cls.PAINT_BODY_HV if half_v is None else half_v
        return (
            cls.paint_body_uv_rect(cls.PAINT_FRONT_U, vc, hu, hv),
            cls.paint_body_uv_rect(cls.PAINT_BACK_U, vc, hu, hv),
        )

    PROCESS_NAME = "PenguinHotel-Win64-Shipping.exe"
    MODULE_NAME = "PenguinHotel-Win64-Shipping.exe"

    GUOBJECT_SIG = bytes([
        0x48, 0x8D, 0x05, 0x00, 0x00, 0x00, 0x00,
        0x48, 0x89, 0x01, 0x45, 0x8B, 0xD1,
    ])
    GUOBJECT_MASK = bytes([1, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1])

    FNAMEPOOL_PATTERNS = (
        (bytes([0x48, 0x8D, 0x0D, 0x00, 0x00, 0x00, 0x00,
                0xE8, 0x00, 0x00, 0x00, 0x00,
                0x4C, 0x8B, 0xC0]),
         bytes([1, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 1, 1])),
        (bytes([0x48, 0x8D, 0x0D, 0x00, 0x00, 0x00, 0x00,
                0xE8, 0x00, 0x00, 0x00, 0x00,
                0x48, 0x8B]),
         bytes([1, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 1])),
        (bytes([0x48, 0x8D, 0x35, 0x00, 0x00, 0x00, 0x00]),
         bytes([1, 1, 1, 0, 0, 0, 0])),
        (bytes([0x48, 0x8D, 0x3D, 0x00, 0x00, 0x00, 0x00]),
         bytes([1, 1, 1, 0, 0, 0, 0])),
    )
    FNAMEPOOL_DELTA = 0xE3B40

    OFFSET_MAP = {
        "UWorld::GameState": ("World", "GameState"),
        "UWorld::OwningGameInstance": ("World", "OwningGameInstance"),
        "UGameInstance::LocalPlayers": ("GameInstance", "LocalPlayers"),
        "UPlayer::PlayerController": ("Player", "PlayerController"),
        "UEngine::GameViewport": ("Engine", "GameViewport"),
        "UGameViewportClient::World": ("GameViewportClient", "World"),
        "AGameStateBase::PlayerArray": ("GameStateBase", "PlayerArray"),
        "APlayerState::PawnPrivate": ("PlayerState", "PawnPrivate"),
        "AController::PlayerState": ("Controller", "PlayerState"),
        "AController::ControlRotation": ("Controller", "ControlRotation"),
        "APlayerController::AcknowledgedPawn": ("PlayerController", "AcknowledgedPawn"),
        "APlayerController::PlayerCameraManager": ("PlayerController", "PlayerCameraManager"),
        "APlayerCameraManager::CameraCachePrivate": ("PlayerCameraManager", "CameraCachePrivate"),
        "AActor::RootComponent": ("Actor", "RootComponent"),
        "USceneComponent::RelativeLocation": ("SceneComponent", "RelativeLocation"),
    }
    # Dynaimc property names to try for health
    HEALTH_PROP_NAMES = ("Health", "CurrentHealth", "HP", "HealthPoints", "HitPoints")
    SHIELD_PROP_NAMES = ("Shield", "Armor", "ShieldHealth", "ExtraHealth", "ArmorHealth")

    @classmethod
    def is_process_running(cls):
        """Return True when the game executable is running."""
        target = cls.PROCESS_NAME.lower()
        try:
            pymem.process.process_from_name(cls.PROCESS_NAME)
            return True
        except Exception:
            pass
        return cls._process_exists_toolhelp(target)

    @staticmethod
    def _process_exists_toolhelp(exe_name_lower):
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        k32 = ctypes.windll.kernel32

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (-1, 0xFFFFFFFF):
            return False
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not k32.Process32FirstW(snap, ctypes.byref(entry)):
                return False
            while True:
                if entry.szExeFile.lower() == exe_name_lower:
                    return True
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            k32.CloseHandle(snap)
        return False

    @classmethod
    def is_game_detected(cls):
        """True when the game process or main window is present."""
        if cls.is_process_running():
            return True
        return cls._find_game_window_hwnd() != 0

    @staticmethod
    def _find_game_window_hwnd():
        try:
            import win32gui
            for title in ("Chameleon  ", "MECCHA CHAMELEON", "Penguin Hotel"):
                hwnd = win32gui.FindWindow(None, title)
                if hwnd:
                    return hwnd
        except Exception:
            pass
        try:
            user32 = ctypes.windll.user32
            for title in ("Chameleon  ", "MECCHA CHAMELEON"):
                hwnd = user32.FindWindowW(None, title)
                if hwnd:
                    return int(hwnd)
        except Exception:
            pass
        return 0

    def __init__(self):
        self.pm = pymem.Pymem(self.PROCESS_NAME)
        self.guobject_array = self._scan_guobject_array()
        if not self.guobject_array:
            raise RuntimeError("Could not find GUObjectArray via pattern scan")
        self.fname_pool = self._scan_fname_pool()
        if not self.fname_pool:
            raise RuntimeError("Could not find FNamePool")
        self.objects = UObjectArray(self.pm, self.guobject_array, self.fname_pool)
        self._globals_ok = self._verify_globals()
        self.resolver = OffsetResolver(self.pm, self.objects)
        self.offsets = self.resolver.resolve_map(self.OFFSET_MAP)
        for key in ("FCameraCacheEntry::POV", "FMinimalViewInfo::Location",
                     "FMinimalViewInfo::Rotation", "FMinimalViewInfo::FOV",
                     "UStruct::ChildProperties", "FField::Next",
                     "FProperty::Offset_Internal", "FField::NamePrivate"):
            self.offsets[key] = OFFSETS[key]
        self.gengine = self.objects.find_first_instance("GameEngine")
        if not self.gengine:
            raise RuntimeError("Could not find GEngine instance")
        self._health_offsets = None
        self._shield_offsets = None
        self._bone_cache = {}
        self._paint_screen_worker_rva = None   # cached native PaintAtScreenPosition worker
        self._hittest_screen_worker_rva = None
        self._players_cache = []               # sticky ESP player list; survives empty reads
        self._export_worker_rva = None
        self._import_worker_rva = None
        self._clear_worker_rva = None
        self._channel_io_resolved = False
        self._last_paint_bgra = None
        self._last_paint_resolution = 0
        self._last_paint_grid = 32

    def _scan_guobject_array(self):
        scanner = PatternScanner(self.pm, self.MODULE_NAME)
        addr = scanner.scan(self.GUOBJECT_SIG, self.GUOBJECT_MASK)
        if not addr:
            return 0
        rel = struct.unpack("<i", self.pm.read_bytes(addr + 3, 4))[0]
        return addr + 7 + rel

    def _scan_fname_pool(self):
        delta_candidate = self.guobject_array - self.FNAMEPOOL_DELTA
        if self._verify_fname_pool(delta_candidate):
            return delta_candidate
        scanner = PatternScanner(self.pm, self.MODULE_NAME)
        for sig, mask in self.FNAMEPOOL_PATTERNS:
            for addr in scanner.scan_all(sig, mask):
                rel = struct.unpack("<i", self.pm.read_bytes(addr + 3, 4))[0]
                candidate = addr + 7 + rel
                if self._verify_fname_pool(candidate):
                    return candidate
        return delta_candidate

    def _verify_fname_pool(self, pool_addr):
        resolver = FNameResolver(self.pm, pool_addr)
        if resolver.resolve(0) == "None":
            return True
        for probe in (0, 1, 2, 3, 4, 5):
            name = resolver.resolve(probe)
            if name and 0 < len(name) <= 128 and name.isprintable():
                return True
        return False

    def _verify_globals(self):
        obj_array = self.guobject_array + 0x10
        num = ru32(self.pm, obj_array + 0x14)
        max_chunks = ru32(self.pm, obj_array + 0x18)
        if num == 0 or num > 10_000_000 or max_chunks == 0 or max_chunks > 64:
            return False
        return self.objects.find_class("Class") != 0

    def globals_ok(self):
        return self._globals_ok

    @staticmethod
    def parse_grid_value(grid, default=32):
        """Parse a user-entered grid/quality integer (minimum 1, no upper cap)."""
        try:
            return max(1, int(grid))
        except (TypeError, ValueError):
            try:
                return max(1, int(default))
            except (TypeError, ValueError):
                return 32

    def _get_world(self):
        viewport = rp(self.pm, self.gengine + self.offsets["UEngine::GameViewport"])
        if not viewport:
            return 0
        return rp(self.pm, viewport + self.offsets["UGameViewportClient::World"])

    def _get_local_controller(self, world):
        if not world:
            return 0
        gi = rp(self.pm, world + self.offsets["UWorld::OwningGameInstance"])
        if not gi:
            return 0
        lp_data, lp_count, _ = read_array(self.pm, gi + self.offsets["UGameInstance::LocalPlayers"])
        if not lp_data or lp_count == 0:
            return 0
        local_player = rp(self.pm, lp_data)
        if not local_player:
            return 0
        return rp(self.pm, local_player + self.offsets["UPlayer::PlayerController"])

    def get_camera(self):
        world = self._get_world()
        if not world:
            return None
        pc = self._get_local_controller(world)
        if not pc:
            return None
        cam_mgr = rp(self.pm, pc + self.offsets["APlayerController::PlayerCameraManager"])
        if not cam_mgr:
            return None

        pov_off = self.offsets["FCameraCacheEntry::POV"]
        loc_off = self.offsets["FMinimalViewInfo::Location"]
        rot_off = self.offsets["FMinimalViewInfo::Rotation"]
        fov_off = self.offsets["FMinimalViewInfo::FOV"]
        cc_off = self.offsets["APlayerCameraManager::CameraCachePrivate"]

        def _read_pov(pov_addr):
            loc = rvec3(self.pm, pov_addr + loc_off)
            rot = rvec3(self.pm, pov_addr + rot_off)
            fov = rfloat(self.pm, pov_addr + fov_off)
            if not all(math.isfinite(v) for v in loc + rot):
                return None
            if not (5.0 < fov < 170.0):
                fov = 90.0
            if not self._is_valid_world_loc(loc):
                return None
            return {"loc": loc, "rot": rot, "fov": fov}

        # Try several POV sources — ViewTarget first (best for spectator / free-cam).
        sources = (
            cam_mgr + 0x0340 + pov_off,              # ViewTarget.POV
            cam_mgr + cc_off + pov_off,              # CameraCachePrivate
            cam_mgr + 0x1E00 + pov_off,              # LastFrameCameraCachePrivate
        )
        for pov_addr in sources:
            cam = _read_pov(pov_addr)
            if cam:
                return cam

        # Last resort: controller rotation + acknowledged pawn/root position.
        cr_off = self.offsets.get("AController::ControlRotation", 0x320)
        rot = rvec3(self.pm, pc + cr_off)
        if not all(math.isfinite(v) for v in rot):
            return None
        pawn = rp(self.pm, pc + self.offsets["APlayerController::AcknowledgedPawn"])
        loc = self.get_actor_root_pos(pawn) if pawn else None
        if loc and self._is_valid_world_loc(loc):
            return {"loc": loc, "rot": rot, "fov": 90.0}
        return None

    def get_viewport_size(self):
        """Return game client width/height in pixels."""
        try:
            import win32gui
            hwnd = win32gui.FindWindow(None, "Chameleon  ")
            if hwnd:
                rect = win32gui.GetClientRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w > 100 and h > 100:
                    return w, h
        except Exception:
            pass
        return 1920, 1080

    def project_body_screen_bbox(self, pawn, cam, screen_w, screen_h):
        """Project the local character's body AABB to a screen rectangle."""
        pos = self.get_actor_root_pos(pawn)
        if not pos or not cam:
            return None
        body_h = 180.0
        body_hw = 45.0
        corners = [
            (-body_hw, 0, -body_hw), (-body_hw, 0, body_hw),
            (body_hw, 0, body_hw), (body_hw, 0, -body_hw),
            (-body_hw, body_h, -body_hw), (-body_hw, body_h, body_hw),
            (body_hw, body_h, body_hw), (body_hw, body_h, -body_hw),
        ]
        rot = self.get_actor_root_rotation(pawn)
        yaw = math.radians(rot[1]) if rot else 0.0
        cyaw, syaw = math.cos(yaw), math.sin(yaw)
        xs, ys = [], []
        for lx, ly, lz in corners:
            rx = lx * cyaw - lz * syaw
            rz = lx * syaw + lz * cyaw
            sx, sy, on = world_to_screen(
                (pos[0] + rx, pos[1] + ly, pos[2] + rz), cam, screen_w, screen_h,
            )
            if on:
                xs.append(sx)
                ys.append(sy)
        if len(xs) < 4:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    @staticmethod
    def _is_valid_world_loc(loc):
        if not loc:
            return False
        if not all(math.isfinite(v) for v in loc):
            return False
        if abs(loc[0]) < 1.0 and abs(loc[1]) < 1.0 and abs(loc[2]) < 1.0:
            return False
        if max(abs(loc[0]), abs(loc[1]), abs(loc[2])) > 1e8:
            return False
        return True

    def get_actor_root_pos(self, actor):
        root = rp(self.pm, actor + self.offsets["AActor::RootComponent"])
        if not root:
            return None
        pos, _ = self._get_component_to_world(root)
        if pos is not None and self._is_valid_world_loc(pos):
            return pos
        loc = rvec3(self.pm, root + self.offsets["USceneComponent::RelativeLocation"])
        if self._is_valid_world_loc(loc):
            return loc
        return None

    def get_actor_root_rotation(self, actor):
        """Read root component relative rotation (pitch, yaw, roll in degrees)."""
        root = rp(self.pm, actor + self.offsets["AActor::RootComponent"])
        if not root:
            return None
        # UE5 FRotator = 3 doubles @ RelativeRotation (0x158)
        return rvec3(self.pm, root + 0x158)

    # UE5.6 skeleton offsets (build 44394996)
    _SCENE_COMPONENT_TO_WORLD = 0x1E0          # FTransform (96B) at end of USceneComponent (0x240)
    _CACHED_COMPONENT_SPACE_TRANSFORMS = 0x09B8
    _FTRANSFORM_STRIDE = 0x60                  # FTransform = 96 bytes in this build
    _MESH_OFFSETS = (0x0418, 0x0328)           # BP_FirstPersonCharacter_Main_C::Mesh, ACharacter::Mesh

    def _resolve_health(self, actor, ps):
        """Resolve health/shield offsets on the pawn class once, cache them."""
        if self._health_offsets is not None:
            return self._health_offsets
        cls = self.objects.obj_class(actor)
        if cls == 0 and ps:
            cls = self.objects.obj_class(ps)
        if not cls:
            self._health_offsets = ("", -1, "", -1)
            return self._health_offsets
        h_name, h_off = self.resolver.search_properties(cls, self.HEALTH_PROP_NAMES)
        s_name, s_off = self.resolver.search_properties(cls, self.SHIELD_PROP_NAMES)
        self._health_offsets = (h_name, h_off, s_name, s_off)
        return self._health_offsets

    def get_health(self, actor, player_state):
        h_name, h_off, s_name, s_off = self._resolve_health(actor, player_state)
        health = None
        if h_name and h_off >= 0 and actor:
            health = rfloat(self.pm, actor + h_off)
        shield = None
        if s_name and s_off >= 0 and actor:
            shield = rfloat(self.pm, actor + s_off)
        elif s_name and s_off >= 0 and player_state:
            shield = rfloat(self.pm, player_state + s_off)
        if health is not None:
            return max(0, health), max(0, shield or 0)
        return None, None

    def get_actor_bounds(self, actor):
        """Read FBoxSphereBounds from the root component (Origin, BoxExtent, SphereRadius)."""
        root = rp(self.pm, actor + self.offsets["AActor::RootComponent"])
        if not root:
            return None
        bounds_addr = root + 0x140
        origin = rvec3(self.pm, bounds_addr)
        extent = rvec3(self.pm, bounds_addr + 0x18)
        radius = rfloat(self.pm, bounds_addr + 0x30)
        return origin, extent, radius

    # -----------------------------------------------------------------------
    # Component walking
    # -----------------------------------------------------------------------
    def _owned_components_offset(self):
        key = "AActor::OwnedComponents"
        cached = getattr(self, "_owned_components_off", None)
        if cached is not None:
            return cached
        off = self.resolver.resolve("Actor", "OwnedComponents")
        if off is None:
            off = 0xD0
        self._owned_components_off = off
        return off

    def walk_owned_components(self, actor):
        off = self._owned_components_offset()
        oc_addr = actor + off
        data, count = read_tarray_ptr(self.pm, oc_addr)
        if not data or count == 0 or count > 512:
            return
        for i in range(count):
            comp = rp(self.pm, data + i * 8)
            if comp:
                yield comp

    def find_component_by_class(self, actor, class_name):
        cname_lower = class_name.lower()
        for comp in self.walk_owned_components(actor):
            cn = self.objects.class_name(comp)
            if cn.lower() == cname_lower:
                return comp
        return 0

    def find_component_by_class_partial(self, actor, name_part):
        for comp in self.walk_owned_components(actor):
            cn = self.objects.class_name(comp)
            if name_part in cn:
                return comp
        return 0

    # -----------------------------------------------------------------------
    # Bone / skeletal mesh reading
    # -----------------------------------------------------------------------
    BONE_CONNECTIONS = [
        ("root", "pelvis"),
        ("pelvis", "spine_01"),
        ("spine_01", "spine_02"),
        ("spine_02", "spine_03"),
        ("spine_03", "neck_01"),
        ("neck_01", "head"),
        ("clavicle_l", "upperarm_l"),
        ("upperarm_l", "lowerarm_l"),
        ("lowerarm_l", "hand_l"),
        ("clavicle_r", "upperarm_r"),
        ("upperarm_r", "lowerarm_r"),
        ("lowerarm_r", "hand_r"),
        ("pelvis", "thigh_l"),
        ("thigh_l", "calf_l"),
        ("calf_l", "foot_l"),
        ("pelvis", "thigh_r"),
        ("thigh_r", "calf_r"),
        ("calf_r", "foot_r"),
    ]

    COMMON_BONE_NAMES = [
        "root", "pelvis",
        "spine_01", "spine_02", "spine_03",
        "neck_01", "head",
        "clavicle_l", "upperarm_l", "lowerarm_l", "hand_l",
        "clavicle_r", "upperarm_r", "lowerarm_r", "hand_r",
        "thigh_l", "calf_l", "foot_l",
        "thigh_r", "calf_r", "foot_r",
    ]

    # Fallback: standard UE5 Manny/Quinn mannequin bone indices.
    # Used when name-based resolution fails (game uses same base skeleton).
    _UE5_FALLBACK_BONES = {
        "pelvis":     1,
        "spine_01":   2,
        "spine_02":   3,
        "spine_03":   4,
        "neck_01":    5,
        "head":       6,
        "clavicle_l": 7,
        "upperarm_l": 8,
        "lowerarm_l": 9,
        "hand_l":    10,
        "clavicle_r":11,
        "upperarm_r":12,
        "lowerarm_r":13,
        "hand_r":    14,
        "thigh_l":   15,
        "calf_l":    16,
        "foot_l":    17,
        "thigh_r":   18,
        "calf_r":    19,
        "foot_r":    20,
    }

    # ---- quaternion / rotation helpers ----

    @staticmethod
    def _euler_to_quat(pitch_deg, yaw_deg, roll_deg):
        """FRotator (degrees) → (x,y,z,w) quaternion using UE5 convention."""
        p = math.radians(pitch_deg) * 0.5
        y = math.radians(yaw_deg)   * 0.5
        r = math.radians(roll_deg)  * 0.5
        sp, cp = math.sin(p), math.cos(p)
        sy, cy = math.sin(y), math.cos(y)
        sr, cr = math.sin(r), math.cos(r)
        return (
             cr*sp*sy - sr*cp*cy,   # x
            -cr*sp*cy - sr*cp*sy,   # y
             cr*cp*sy - sr*sp*cy,   # z
             cr*cp*cy + sr*sp*sy,   # w
        )

    @staticmethod
    def _quat_mul(q1, q2):
        """Quaternion multiplication q1 × q2  (apply q1 then q2)."""
        x1, y1, z1, w1 = q1
        x2, y2, z2, w2 = q2
        return (
            w1*x2 + x1*w2 + y1*z2 - z1*y2,
            w1*y2 - x1*z2 + y1*w2 + z1*x2,
            w1*z2 + x1*y2 - y1*x2 + z1*w2,
            w1*w2 - x1*x2 - y1*y2 - z1*z2,
        )

    @staticmethod
    def _quat_rotate(q, v):
        """Rotate vector v by quaternion q.  Returns (x,y,z)."""
        qx, qy, qz, qw = q
        vx, vy, vz = v
        # t = 2 * cross(q.xyz, v)
        tx = 2.0 * (qy*vz - qz*vy)
        ty = 2.0 * (qz*vx - qx*vz)
        tz = 2.0 * (qx*vy - qy*vx)
        return (
            vx + qw*tx + (qy*tz - qz*ty),
            vy + qw*ty + (qz*tx - qx*tz),
            vz + qw*tz + (qx*ty - qy*tx),
        )

    # ---- skeleton helpers ----

    def _read_transform(self, addr, stride=None):
        """Read FTransform → ((tx,ty,tz), (qx,qy,qz,qw))."""
        if stride is None:
            stride = self._FTRANSFORM_STRIDE
        try:
            raw = self.pm.read_bytes(addr, stride)
            qx, qy, qz, qw = struct.unpack_from("<dddd", raw, 0)
            tx, ty, tz     = struct.unpack_from("<ddd",  raw, 32)
            if not all(math.isfinite(v) for v in (qx, qy, qz, qw, tx, ty, tz)):
                return None, None
            return (tx, ty, tz), (qx, qy, qz, qw)
        except Exception:
            return None, None

    def _get_component_to_world(self, scene_comp, ref_pos=None):
        """Read cached ComponentToWorld from a USceneComponent."""
        if not scene_comp:
            return None, None
        best = None
        best_dist = None
        for off in (0x1E0, 0x1F0):
            for stride in (0x60, 0x50):
                pos, quat = self._read_transform(scene_comp + off, stride)
                if pos is None:
                    continue
                qx, qy, qz, qw = quat
                qlen = qx * qx + qy * qy + qz * qz + qw * qw
                if qlen < 1e-6:
                    continue
                if max(abs(pos[0]), abs(pos[1]), abs(pos[2])) > 1e8:
                    continue
                if ref_pos is not None:
                    d = dist(pos, ref_pos)
                    if d > 400.0:
                        continue
                    if best is None or d < best_dist:
                        best, best_dist = (pos, quat), d
                else:
                    return pos, quat
        return best if best else (None, None)

    def _resolve_bone_indices(self, mesh_comp):
        """Map bone names -> indices by scanning the FReferenceSkeleton in the asset.

        Results are cached per skeletal-mesh asset pointer so the expensive scan
        only runs once per unique mesh, not every frame.
        """
        mesh_asset = rp(self.pm, mesh_comp + 0x0578)
        if not mesh_asset:
            return {}
        cached = self._bone_cache.get(mesh_asset)
        if cached is not None:
            return cached
        # Scan for the RefSkeleton TArray in the private section of USkinnedAsset
        for off in range(0x028, 0x0E8, 0x10):
            try:
                data, count = read_tarray_ptr(self.pm, mesh_asset + off)
                if not data or count < 10 or count > 512:
                    continue
                # Try FMeshBoneInfo stride = 16 bytes (FName + ParentIdx + pad)
                for stride in (0x10, 0x18, 0x20):
                    first_fname = ru32(self.pm, data)
                    name0 = self.objects.fnames.resolve(first_fname)
                    if not name0:
                        continue
                    name_lower = name0.lower()
                    if name_lower in ("root", "pelvis", "hips", "spine"):
                        # Looks good — build the full map
                        name_to_idx = {}
                        for i in range(count):
                            fname_idx = ru32(self.pm, data + i * stride)
                            bone_name = self.objects.fnames.resolve(fname_idx)
                            if bone_name:
                                name_to_idx[bone_name.lower()] = i
                        if len(name_to_idx) >= 5:
                            self._bone_cache[mesh_asset] = name_to_idx
                            return name_to_idx
            except Exception:
                continue
        self._bone_cache[mesh_asset] = {}
        return {}

    def get_skeletal_mesh(self, actor):
        """Return the USkeletalMeshComponent pointer for the actor.

        Tries BP_FirstPersonCharacter_Main_C::Mesh @ 0x0418 first (this game),
        then ACharacter::Mesh @ 0x0328, then OwnedComponents walk.
        """
        for off in self._MESH_OFFSETS:
            mesh = rp(self.pm, actor + off)
            if mesh:
                cn = self.objects.class_name(mesh)
                if "SkeletalMesh" in cn or "SkinnedMesh" in cn:
                    return mesh
        for pattern in ("SkeletalMeshComponent", "SkinnedMeshComponent"):
            comp = self.find_component_by_class_partial(actor, pattern)
            if comp:
                return comp
        return 0

    def get_bone_transforms(self, mesh_comp):
        """Read CachedComponentSpaceTransforms from USkeletalMeshComponent.

        Offset confirmed from Dumper-7 SDK dump (build 44394996):
          USkeletalMeshComponent::CachedComponentSpaceTransforms @ comp + 0x09B8
        Each FTransform3d element is 0x50 bytes:
          [0:32]  TQuat<double>   Rotation     (x,y,z,w)
          [32:56] TVector<double> Translation  (x,y,z)
          [56:80] TVector<double> Scale3D      (x,y,z)  — unused here
        """
        if not mesh_comp:
            return None
        data, count = read_tarray_ptr(self.pm, mesh_comp + 0x09B8)
        if not data or count == 0 or count > 1024:
            return None
        bones = []
        try:
            raw_all = self.pm.read_bytes(data, count * self._FTRANSFORM_STRIDE)
        except Exception:
            return None
        for i in range(count):
            base = i * self._FTRANSFORM_STRIDE
            try:
                qx, qy, qz, qw = struct.unpack_from("<dddd", raw_all, base)
                tx, ty, tz     = struct.unpack_from("<ddd",  raw_all, base + 32)
                bones.append(((tx, ty, tz), (qx, qy, qz, qw)))
            except Exception:
                bones.append(None)
        return bones

    def _get_mesh_world_transform(self, actor, mesh_comp):
        """Return (world_pos, world_quat) for the skeletal mesh component."""
        root = rp(self.pm, actor + self.offsets["AActor::RootComponent"])
        ref_pos = rvec3(self.pm, root + 0x140) if root else None
        pos, quat = self._get_component_to_world(mesh_comp, ref_pos)
        if pos is not None:
            return pos, quat
        if ref_pos is not None:
            root_pos2, root_quat = self._get_component_to_world(root)
            if root_quat is not None:
                return ref_pos, root_quat
            return ref_pos, self._euler_to_quat(*rvec3(self.pm, root + 0x158))
        return None, None

    def _bones_array_info(self, mesh_comp):
        """Return (data_ptr, count) for CachedComponentSpaceTransforms."""
        if not mesh_comp:
            return 0, 0
        data, count = read_tarray_ptr(self.pm, mesh_comp + self._CACHED_COMPONENT_SPACE_TRANSFORMS)
        if not data or count <= 0 or count > 512:
            return 0, 0
        return data, count

    def _bone_local_position(self, bone_data, count, idx):
        """Read translation from one FTransform3d in the bone array."""
        if idx < 0 or idx >= count:
            return None
        try:
            raw = self.pm.read_bytes(bone_data + idx * self._FTRANSFORM_STRIDE + 32, 24)
            tx, ty, tz = struct.unpack("<ddd", raw)
            if not all(math.isfinite(v) for v in (tx, ty, tz)):
                return None
            return tx, ty, tz
        except Exception:
            return None

    def _bone_to_world(self, mesh_world_pos, mesh_world_quat, local_pos):
        rotated = self._quat_rotate(mesh_world_quat, local_pos)
        return (
            mesh_world_pos[0] + rotated[0],
            mesh_world_pos[1] + rotated[1],
            mesh_world_pos[2] + rotated[2],
        )

    def get_skeleton_positions(self, actor):
        """Return dict of bone_name -> world_position for the actor."""
        mesh_comp = self.get_skeletal_mesh(actor)
        if not mesh_comp:
            return None
        mesh_world_pos, mesh_world_quat = self._get_mesh_world_transform(actor, mesh_comp)
        if mesh_world_pos is None:
            return None
        bone_data, count = self._bones_array_info(mesh_comp)
        if not bone_data:
            return None
        name_map = self._resolve_bone_indices(mesh_comp)
        if not name_map:
            name_map = self._UE5_FALLBACK_BONES
        result = {}
        for bname in self.COMMON_BONE_NAMES:
            idx = name_map.get(bname.lower())
            if idx is None:
                continue
            local_pos = self._bone_local_position(bone_data, count, idx)
            if local_pos is None:
                continue
            result[bname] = self._bone_to_world(mesh_world_pos, mesh_world_quat, local_pos)
        return result if result else None

    def get_skeleton_positions_by_indices(self, actor, bone_indices):
        """Get bone positions by direct index map {name: index}. Applies world transform."""
        mesh_comp = self.get_skeletal_mesh(actor)
        if not mesh_comp:
            return None
        mesh_world_pos, mesh_world_quat = self._get_mesh_world_transform(actor, mesh_comp)
        if mesh_world_pos is None:
            return None
        bone_data, count = self._bones_array_info(mesh_comp)
        if not bone_data:
            return None
        result = {}
        for name, idx in bone_indices.items():
            local_pos = self._bone_local_position(bone_data, count, idx)
            if local_pos is None:
                continue
            result[name] = self._bone_to_world(mesh_world_pos, mesh_world_quat, local_pos)
        return result if result else None

    # -----------------------------------------------------------------------
    # Player iteration (enhanced)
    # -----------------------------------------------------------------------
    def _read_fstring(self, addr: int) -> str:
        """Read a UE5 FString from memory and return it as a Python str (or '')."""
        try:
            data_ptr = rp(self.pm, addr)          # TChar*
            arr_num  = ru32(self.pm, addr + 8)    # includes null terminator
            if not data_ptr or arr_num == 0 or arr_num > 128:
                return ""
            raw = self.pm.read_bytes(data_ptr, arr_num * 2)
            text = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
            return text.strip()
        except Exception:
            return ""

    def get_player_name(self, ps: int) -> str:
        """Read the player display name from APlayerState.
        Tries PlayerNamePrivate (0x340, base class) first, then CustomPlayerName
        (0x388, ABP_FirstPersonPlayerState_Online_C) as a fallback."""
        if not ps:
            return ""
        name = self._read_fstring(ps + 0x340)     # APlayerState::PlayerNamePrivate
        if not name:
            name = self._read_fstring(ps + 0x388) # CustomPlayerName (online variant)
        return name

    def _read_is_hunter(self, pawn: int):
        """Return True=Hunter, False=Survivor, None=unreadable. IsHunter @ pawn+0x0C3A."""
        try:
            raw = self.pm.read_bytes(pawn + 0x0C3A, 1)
            return bool(raw[0])
        except Exception:
            return None

    @staticmethod
    def _is_player_class(cls_name):
        """True for real playable character classes (not decoys, spectators, props)."""
        if not cls_name:
            return False
        if "Spectator" in cls_name or "Spectate" in cls_name:
            return False
        if "BP_FirstPersonCharacter" in cls_name:
            return True
        if "cLeon" in cls_name and "Character" in cls_name:
            return True
        return False

    def get_players(self, include_local=False, team_filter=False):
        """Return up to MAX_ESP_PLAYERS entries with a sticky cache.

        A transient empty or partial memory read no longer clears ESP — cached
        players are kept until replaced by a fresh read for the same actor.
        """
        cap = self.MAX_ESP_PLAYERS
        try:
            fresh = list(self.iter_players(
                include_local=include_local,
                team_filter=team_filter,
            ))
        except Exception:
            fresh = []

        if fresh:
            merged = []
            seen = set()
            for p in fresh:
                if len(merged) >= cap:
                    break
                merged.append(p)
                actor = p.get("actor")
                if actor:
                    seen.add(actor)
            if len(merged) < len(self._players_cache):
                for p in self._players_cache:
                    if len(merged) >= cap:
                        break
                    actor = p.get("actor")
                    if actor and actor not in seen:
                        merged.append(p)
                        seen.add(actor)
            self._players_cache = merged[:cap]
        elif self._players_cache:
            # Refresh positions for cached actors when a full read briefly fails.
            updated = []
            for p in self._players_cache[:cap]:
                actor = p.get("actor")
                if not actor:
                    continue
                pos = self.get_actor_root_pos(actor)
                if pos:
                    entry = dict(p)
                    entry["pos"] = pos
                    updated.append(entry)
            if updated:
                self._players_cache = updated

        return self._players_cache[:cap]

    def iter_players(self, include_local=False, team_filter=False):
        world = self._get_world()
        if not world:
            return
        gamestate = rp(self.pm, world + self.offsets["UWorld::GameState"])
        pc = self._get_local_controller(world)
        local_pawn = rp(self.pm, pc + self.offsets["APlayerController::AcknowledgedPawn"]) if pc else 0
        local_ps   = rp(self.pm, pc + self.offsets["AController::PlayerState"]) if pc else 0
        local_pawn_cls = self.objects.class_name(local_pawn) if local_pawn else ""
        # If the acknowledged pawn is not a real playable character (e.g. the user
        # is dead and controlling a spectator/ragdoll) treat their class as unknown
        # so team_filter does not mistakenly remove living players of the same class.
        if local_pawn_cls and not self._is_player_class(local_pawn_cls):
            local_pawn_cls = ""

        # Tracks every pawn address already yielded so the same actor is never
        # drawn twice (happens when multiple PlayerState entries share one PawnPrivate).
        seen_pawns = set()
        if local_pawn and self._is_player_class(local_pawn_cls):
            seen_pawns.add(local_pawn)

        total = 0
        if include_local and local_pawn:
            pos = self.get_actor_root_pos(local_pawn)
            if pos:
                total += 1
                yield {
                    "is_local": True,
                    "pos": pos,
                    "idx": 0,
                    "actor": local_pawn,
                    "player_state": local_ps,
                    "is_hunter": self._read_is_hunter(local_pawn),
                    "player_name": self.get_player_name(local_ps),
                }

        if total >= self.MAX_ESP_PLAYERS:
            return

        if gamestate:
            pa_data, pa_count, _ = read_array(
                self.pm, gamestate + self.offsets["AGameStateBase::PlayerArray"]
            )
            if pa_data and pa_count > 0:
                for i in range(min(pa_count, self.MAX_ESP_PLAYERS)):
                    if total >= self.MAX_ESP_PLAYERS:
                        break
                    ps = rp(self.pm, pa_data + i * 8)
                    if not ps or ps == local_ps:
                        continue
                    pawn = rp(self.pm, ps + self.offsets["APlayerState::PawnPrivate"])
                    if not pawn or pawn in seen_pawns:
                        continue
                    pawn_cls = self.objects.class_name(pawn)
                    # Require an actual player character — reject decoys, pickups, props.
                    if not self._is_player_class(pawn_cls):
                        continue
                    if team_filter and local_pawn_cls:
                        if pawn_cls == local_pawn_cls:
                            continue
                        if "Spectate" in pawn_cls:
                            continue
                    pos = self.get_actor_root_pos(pawn)
                    if not pos:
                        continue
                    seen_pawns.add(pawn)
                    total += 1
                    yield {
                        "is_local": False,
                        "pos": pos,
                        "idx": i,
                        "actor": pawn,
                        "player_state": ps,
                        "is_hunter": self._read_is_hunter(pawn),
                        "player_name": self.get_player_name(ps),
                    }

        if total >= self.MAX_ESP_PLAYERS:
            return

        # Supplemental actor scan — always runs as a safety net.
        # Catches players whose APlayerState::PawnPrivate is temporarily null or
        # stale (respawn transition, replication lag) that the PlayerArray loop
        # above would have silently skipped.  seen_pawns deduplicates cleanly so
        # no player already found above is yielded twice.
        # Cap at 2048 actors to cover larger maps while keeping each frame fast.
        persistent_level_off = self.resolver.resolve("World", "PersistentLevel") \
            if hasattr(self, "resolver") else None
        if persistent_level_off is None:
            persistent_level_off = 0x30
        level = rp(self.pm, world + persistent_level_off)
        if level:
            actors_off = self.resolver.resolve("Level", "Actors") \
                if hasattr(self, "resolver") else None
            if actors_off is None:
                actors_off = 0x98
            actors_data, actors_count, _ = read_array(self.pm, level + actors_off)
            if actors_data and 0 < actors_count <= 8192:
                cap = min(actors_count, 4096)
                for i in range(cap):
                    if total >= self.MAX_ESP_PLAYERS:
                        break
                    actor = rp(self.pm, actors_data + i * 8)
                    if not actor or actor in seen_pawns:
                        continue
                    cls_name = self.objects.class_name(actor)
                    if not self._is_player_class(cls_name):
                        continue
                    if team_filter and local_pawn_cls:
                        if cls_name == local_pawn_cls:
                            continue
                        if "Spectate" in cls_name:
                            continue
                    pos = self.get_actor_root_pos(actor)
                    if not pos:
                        continue
                    seen_pawns.add(actor)
                    total += 1
                    yield {
                        "is_local": False,
                        "pos": pos,
                        "idx": i,
                        "actor": actor,
                        "player_state": 0,
                        "is_hunter": self._read_is_hunter(actor),
                        "player_name": "",   # actor-scan fallback: no PlayerState
                    }


    # -----------------------------------------------------------------------
    # Camouflage — offsets verified from Dumper-7 SDK dump (post-patch 2026-06-23, re-verified 2026-06-24)
    #
    # ABP_FirstPersonCharacter_cLeon_Character_C:
    #   pawn + 0x0B68  ->  RuntimePaintable     (URuntimePaintableComponent*)
    #   pawn + 0x0B79  ->  IsPaintMode          (bool)
    #   pawn + 0x0BA8  ->  CurrentPaintColor    (FLinearColor, 16 bytes)
    #   pawn + 0x0BF8  ->  IsBrushing           (bool)
    #   pawn + 0x0C39  ->  BodyShadow           (bool, Net/RepNotify)
    #   pawn + 0x0C50  ->  BodyVisibility       (bool, Net/RepNotify)
    #   pawn + 0x0C99  ->  CurrentLocalAlpha    (bool)
    #
    # URuntimePaintableComponent:
    #   comp + 0x00B8  ->  TextureOptions       (FPaintTextureOptions, 0x54 bytes)
    #   comp + 0x00D0  ->  TextureOptions.AlbedoClearColor  (FLinearColor = 0x00B8 + 0x18)
    #   comp + 0x0148  ->  AlbedoRenderTarget   (UTextureRenderTarget2D*)
    #   comp + 0x0150  ->  MetallicRenderTarget
    #   comp + 0x0158  ->  RoughnessRenderTarget
    #   comp + 0x0160  ->  HeightRenderTarget
    #   comp + 0x0168  ->  DynamicMaterialInstance (UMaterialInstanceDynamic*)
    #   comp + 0x0170  ->  CurrentBrushSettings (FRuntimeBrushSettings, 0x28 bytes)
    #     +0x0000 Radius   float   comp+0x0170
    #     +0x0004 Hardness float   comp+0x0174
    #     +0x0008 Opacity  float   comp+0x0178
    #   comp + 0x01AC  ->  bAutoRecordStrokes   (bool)
    #   comp + 0x01AD  ->  bAutoFlushStrokes    (bool)  [was 0x0199 pre-patch]
    # -----------------------------------------------------------------------

    def _find_local_pawn(self):
        """Resolve the local playable pawn (AcknowledgedPawn, then fallbacks)."""
        world = self._get_world()
        if not world:
            return 0
        pc = self._get_local_controller(world)
        if not pc:
            return 0

        ack = rp(self.pm, pc + self.offsets["APlayerController::AcknowledgedPawn"])
        if ack and ack > 0x100000:
            cls = self.objects.class_name(ack)
            if self._is_player_class(cls):
                return ack

        local_ps = rp(self.pm, pc + self.offsets["AController::PlayerState"])
        if local_ps:
            priv = rp(self.pm, local_ps + self.offsets["APlayerState::PawnPrivate"])
            if priv and priv > 0x100000:
                cls = self.objects.class_name(priv)
                if self._is_player_class(cls):
                    return priv

        try:
            for p in self.iter_players(include_local=True):
                if p.get("is_local"):
                    actor = p.get("actor", 0)
                    if actor:
                        return actor
        except Exception:
            pass

        return ack if ack and ack > 0x100000 else 0

    def _get_local_pawn(self):
        return self._find_local_pawn()

    def is_paintable_pawn(self, pawn):
        """True when pawn is a playable character with RuntimePaintableComponent."""
        if not pawn or pawn <= 0x100000:
            return False
        if not self._is_player_class(self.objects.class_name(pawn)):
            return False
        return bool(self._get_paint_component(pawn))

    def wait_for_paintable_pawn(self, timeout=20.0, poll=0.2):
        """Poll until a paintable local pawn exists (lobby spawn / replication lag)."""
        import time

        deadline = time.monotonic() + timeout
        tries = 0
        while time.monotonic() < deadline:
            pawn = self._find_local_pawn()
            if pawn and self.is_paintable_pawn(pawn):
                if tries:
                    print(f"[PAINT] paintable pawn 0x{pawn:X} after {tries} tries")
                return pawn
            tries += 1
            time.sleep(poll)
        print(f"[PAINT] no paintable pawn within {timeout:.0f}s ({tries} tries)")
        return 0

    def _get_runtime_paint_component(self, pawn, quiet=False):
        """Read RuntimePaintable at known offset pawn+0x0B68."""
        comp = rp(self.pm, pawn + 0x0B68)
        if comp and comp > 0x100000:
            if not quiet:
                cname = self.objects.class_name(comp)
                print(f"[CAMO] RuntimePaintable=0x{comp:X}  class={cname}")
            return comp
        if not quiet:
            print(f"[CAMO] RuntimePaintable ptr invalid: 0x{comp:X}")
        return 0

    def dump_pawn_components(self, pawn):
        """Print pawn class + all owned component class names."""
        print(f"[CAMO-DIAG] Pawn=0x{pawn:X} class={self.objects.class_name(pawn)}")
        try:
            raw = self.pm.read_bytes(pawn + 0x0BA8, 16)
            r, g, b, a = struct.unpack("ffff", raw)
            print(f"[CAMO-DIAG]   CurrentPaintColor: r={r:.3f} g={g:.3f} b={b:.3f}")
        except Exception:
            pass
        print(f"[CAMO-DIAG]   RuntimePaintable ptr = 0x{rp(self.pm, pawn+0x0B68):X}")
        for comp in self.walk_owned_components(pawn):
            cname = self.objects.class_name(comp)
            oname = self.objects.obj_name(comp)
            print(f"  0x{comp:X}  {cname}  ({oname})")

    def read_camouflage_color(self, actor):
        """Read CurrentPaintColor from pawn+0x0BA8."""
        try:
            raw = self.pm.read_bytes(actor + 0x0BA8, 16)
            r, g, b, a = struct.unpack("ffff", raw)
            return (r * 255, g * 255, b * 255)
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Remote function call (no anti-cheat — call game functions directly)
    # -----------------------------------------------------------------------
    # RVAs from Dumper-7 Dumpspace/FunctionsInfo.json — verified for build 44394996
    # (re-dumped 2026-06-24; build number unchanged, all RVAs and offsets confirmed identical)
    #
    # CRITICAL: The RVAs that Dumper-7 lists for these UFUNCTIONs are the Blueprint
    # VM *exec thunks* (execPaintAtUV, etc.) — NOT directly callable native funcs.
    # An exec thunk expects (UObject* Context /*rcx*/, FFrame& Stack /*rdx*/,
    # void* Result /*r8*/) and parses its parameters out of FFrame bytecode via
    # FFrame::Step. Calling a thunk with native-style args makes Step walk a bogus
    # "bytecode" pointer and scribble over the heap — which is exactly what was
    # crashing the game on every camo attempt (the corruption surfaces a moment
    # later on the game thread, hence "ok=True" then crash).
    #
    # The real native implementations are the leaf functions each thunk tail-calls
    # after marshaling (verified by disassembly). Those take ordinary register args
    # and are what we call directly below.
    #
    #   exec thunk            -> real native worker (call THIS one)
    #   ----------------------------------------------------------------
    #   execPaintAtUV 0x50E20A0  -> 0x50FCCE0  PaintAtUV(this, &Uv, &ChannelData, Ch)
    #   execSetBrushRadius       -> 0x51009C0  (just: movss [this+0x170], radius)
    #   execSetBrushOpacity      -> 0x51009A0  (just: clamp; movss [this+0x178])
    #
    # SetBrushRadius/Opacity workers are pure single-field memory writes, so we set
    # those via direct writes instead of a remote call. PaintAtUV's worker records a
    # stroke on the CPU side and enqueues the GPU paint; it reads CurrentBrushSettings
    # straight from this+0x170 (which we populate by direct write beforehand).
    RVA_PAINT_AT_UV_NATIVE = 0x50FCCE0   # real URuntimePaintableComponent::PaintAtUV worker
    RVA_PAINT_AT_UV        = 0x50E20A0   # execPaintAtUV thunk (DO NOT call directly)
    RVA_EXEC_EXPORT_CHANNEL = 0x50E0BF0
    RVA_EXEC_IMPORT_CHANNEL = 0x50E12E0
    RVA_EXEC_CLEAR_CHANNEL  = 0x50E0B30
    RVA_EXPORT_CHANNEL_NATIVE = 0x50F5620
    RVA_IMPORT_CHANNEL_NATIVE = 0x50F8EB0
    RVA_CLEAR_CHANNEL_NATIVE  = 0x50F2FE0
    EPaintChannel_Albedo = 0

    # ---- Screen-space camouflage (true chameleon blend) -------------------
    # URuntimePaintableComponent::PaintAtScreenPosition raycasts a screen point
    # onto the body mesh and paints the correct UV automatically — giving real
    # spatial correspondence (a body part over a red splat becomes red, etc.).
    #
    # exec-thunk RVAs verified against the CURRENT dump's IDAMappings .idmap
    # (build 5.6.1-44394996), demangled names:
    #   0x50E1E60  _ZN26URuntimePaintableComponent25execPaintAtScreenPositionEv
    #   0x50E1120  _ZN26URuntimePaintableComponent27execHitTestAtScreenPositionEv
    #   0x50E0EA0  _ZN26URuntimePaintableComponent27execGetInitializedPaintMeshEv
    #   0x50E20A0  _ZN26URuntimePaintableComponent13execPaintAtUVEv  (self-test)
    #
    # These are Blueprint VM exec thunks — NOT directly callable. The real native
    # worker each thunk tail-calls is found at RUNTIME by disassembling the thunk
    # in the live process (the game binary is not shipped with this tool, so we
    # cannot do it offline). _resolve_paint_screen_worker() does this and gates
    # the result with a self-test: running the same discovery on execPaintAtUV
    # MUST reproduce the already-proven worker sub_50FCCE0, otherwise we refuse to
    # call anything (crash-safe).
    RVA_EXEC_PAINT_AT_SCREEN   = 0x50E1E60
    RVA_EXEC_HITTEST_AT_SCREEN = 0x50E1120
    RVA_EXEC_GET_PAINT_MESH    = 0x50E0EA0

    def _module_base(self):
        mod = pymem.process.module_from_name(self.pm.process_handle, self.MODULE_NAME)
        return mod.lpBaseOfDll if mod else 0

    @staticmethod
    def _remote_alloc(handle, size):
        """VirtualAllocEx with correct 64-bit return type."""
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.VirtualAllocEx.restype = ctypes.c_uint64
        k32.VirtualAllocEx.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_uint32,
        ]
        return k32.VirtualAllocEx(handle, 0, size, 0x3000, 0x40)

    @staticmethod
    def _remote_free(handle, addr):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.VirtualFreeEx.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_uint32,
        ]
        k32.VirtualFreeEx(handle, addr, 0, 0x8000)

    @staticmethod
    def _remote_thread(handle, addr, timeout_ms=5000):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateRemoteThread.restype = ctypes.c_void_p
        k32.CreateRemoteThread.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
        th = k32.CreateRemoteThread(handle, None, 0, addr, None, 0, None)
        if not th:
            return False
        wait = k32.WaitForSingleObject(th, int(timeout_ms))
        k32.CloseHandle(th)
        if wait != 0:
            print(f"[REMOTE] thread wait failed (code=0x{wait & 0xFFFFFFFF:X}, "
                  f"timeout={timeout_ms}ms)")
            return False
        return True

    def _finish_remote_mem(self, handle, mem, ok, label=""):
        """Free injected memory only after the remote thread finished."""
        if ok:
            self._remote_free(handle, mem)
        elif mem:
            print(f"[REMOTE] {label} — remote memory kept allocated (thread may still run)")
        return ok

    def _suspend_game_threads(self):
        """Suspend all threads in the game process (game appears frozen)."""
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        TH32CS_SNAPTHREAD = 0x00000004
        THREAD_SUSPEND_RESUME = 0x0002

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        pid = self.pm.process_id
        handles = []
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snap in (-1, 0xFFFFFFFF):
            return handles
        try:
            te = THREADENTRY32()
            te.dwSize = ctypes.sizeof(THREADENTRY32)
            if not k32.Thread32First(snap, ctypes.byref(te)):
                return handles
            while True:
                if te.th32OwnerProcessID == pid:
                    th = k32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                    if th:
                        k32.SuspendThread(th)
                        handles.append(th)
                if not k32.Thread32Next(snap, ctypes.byref(te)):
                    break
        finally:
            k32.CloseHandle(snap)
        return handles

    @staticmethod
    def _resume_game_threads(handles):
        import ctypes
        k32 = ctypes.windll.kernel32
        for th in handles:
            try:
                k32.ResumeThread(th)
            finally:
                k32.CloseHandle(th)

    def start_fps_tracker(self):
        """
        Spawn a 500 Hz background thread that detects game ticks by polling the
        Y-component of the ViewTarget camera location.  Each time that float
        changes the game rendered a new frame.  The count is aggregated every
        second and stored in the returned dict so the overlay can display it.

        Returns: dict with key "game_fps" (int, updated ~every second).
        """
        import threading as _threading
        import time as _time

        fps_info = {"game_fps": 0}

        def _tracker():
            VIEW_TARGET_OFF = 0x0340          # cam_mgr + 0x340 = ViewTarget
            pc_cam_key = "APlayerController::PlayerCameraManager"
            pov_off = self.offsets.get("FCameraCacheEntry::POV", 0x10)
            loc_off = self.offsets.get("FMinimalViewInfo::Location", 0x0)
            # Y component (+4) changes even when standing still (minor jitter)
            SAMPLE_OFF = VIEW_TARGET_OFF + pov_off + loc_off + 4

            pov_y_addr   = 0
            last_val     = None
            tick_count   = 0
            window_start = _time.perf_counter()
            last_refresh = 0.0

            while True:
                try:
                    now = _time.perf_counter()

                    # Re-resolve the camera chain once per second (or on first run)
                    if now - last_refresh >= 1.0:
                        last_refresh = now
                        try:
                            pc_cam_off = self.offsets.get(pc_cam_key, 0)
                            world = self._get_world()
                            if world and pc_cam_off:
                                pc = self._get_local_controller(world)
                                if pc:
                                    cam_mgr = rp(self.pm, pc + pc_cam_off)
                                    if cam_mgr > 0x100000:
                                        pov_y_addr = cam_mgr + SAMPLE_OFF
                        except Exception:
                            pov_y_addr = 0

                    # Fast poll: read 4 bytes, count changes
                    if pov_y_addr:
                        try:
                            val = self.pm.read_bytes(pov_y_addr, 4)
                            if val != last_val:
                                tick_count += 1
                                last_val = val
                        except Exception:
                            pov_y_addr = 0

                    # Publish FPS once per second
                    elapsed = now - window_start
                    if elapsed >= 1.0:
                        fps_info["game_fps"] = round(tick_count / elapsed)
                        tick_count   = 0
                        window_start = now

                    _time.sleep(0.002)   # 500 Hz

                except Exception:
                    _time.sleep(0.1)

        _threading.Thread(target=_tracker, daemon=True).start()
        return fps_info

    def throttle_game_process(self, on: bool, quality: int = 3):
        """Lower/restore the game process priority class while painting.

        Lowering the game's priority class tells the OS to give its threads
        fewer CPU time-slices, so the game renders fewer frames naturally and
        our painting thread (running at NORMAL priority) gets more CPU.

        on=True, quality=5 → IDLE_PRIORITY_CLASS        (max throttle)
        on=True, quality<5 → BELOW_NORMAL_PRIORITY_CLASS (moderate throttle)
        on=False           → NORMAL_PRIORITY_CLASS        (restore)
        """
        import ctypes
        PROCESS_SET_INFORMATION     = 0x0200
        NORMAL_PRIORITY_CLASS       = 0x00000020
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        IDLE_PRIORITY_CLASS         = 0x00000040
        try:
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(PROCESS_SET_INFORMATION, False, self.pm.process_id)
            if not handle:
                print("[GAME] throttle_game_process: OpenProcess failed")
                return False
            try:
                if on:
                    priority = IDLE_PRIORITY_CLASS if quality >= 5 else BELOW_NORMAL_PRIORITY_CLASS
                    label    = "IDLE"         if quality >= 5 else "BELOW_NORMAL"
                else:
                    priority = NORMAL_PRIORITY_CLASS
                    label    = "NORMAL"
                ok = bool(k32.SetPriorityClass(handle, priority))
                state = "painting" if on else "restored"
                if ok:
                    print(f"[GAME] priority -> {label} ({state})")
                else:
                    print(f"[GAME] SetPriorityClass failed ({state})")
                return ok
            finally:
                k32.CloseHandle(handle)
        except Exception as exc:
            print(f"[GAME] throttle_game_process error: {exc}")
            return False

    @contextmanager
    def _game_frozen(self, label="PAINT"):
        """
        Freeze the game while paint natives run (same idea as in-game paint mods).
        Existing game threads are suspended; our CreateRemoteThread worker still runs.
        """
        handles = self._suspend_game_threads()
        if handles:
            print(f"[{label}] froze game ({len(handles)} threads)")
        else:
            print(f"[{label}] freeze failed — painting anyway")
        try:
            yield bool(handles)
        finally:
            if handles:
                self._resume_game_threads(handles)
                print(f"[{label}] unfroze game")

    def _call_paint_at_uv_grid(self, comp, r_lin, g_lin, b_lin):
        """
        Paint the albedo render target at 5 UV grid points (centre + 4 quadrants)
        in a single remote thread.

        IMPORTANT: we call the *real native* PaintAtUV worker (RVA_PAINT_AT_UV_NATIVE,
        sub_50FCCE0) directly — NOT the Dumper-7 execPaintAtUV thunk. The thunk parses
        its params from an FFrame and corrupts the heap when fed native-style args,
        which is what crashed the game on every prior camo attempt.

        The worker reads CurrentBrushSettings from comp+0x170, so brush Radius/Hardness/
        Opacity are written directly to memory beforehand (those setters are themselves
        pure single-field memory writes). No BeginStroke/EndStroke needed.

        FVector2D = 2 doubles (UE5.6 LWC) = 16 bytes. EPaintChannel::All = 4.

        Native worker signature (register args, no stack params):
          sub_50FCCE0(this /*RCX=comp*/, const FVector2D& Uv /*RDX*/,
                      const FPaintChannelData& ChannelData /*R8*/, EPaintChannel Ch /*R9b*/)
        """
        base = self._module_base()
        if not base:
            return False
        fn = base + self.RVA_PAINT_AT_UV_NATIVE

        # FPaintChannelData (32 bytes):
        #   AlbedoColor  FLinearColor {r,g,b,1.0}  16 bytes
        #   Metallic     float 0.0                   4 bytes
        #   Roughness    float 0.5                   4 bytes
        #   Height       float 0.5                   4 bytes
        #   ApplyMode    uint8 0 (Override) + 3 pad  4 bytes
        cd  = struct.pack('<ffff', r_lin, g_lin, b_lin, 1.0)
        cd += struct.pack('<fff', 0.0, 0.5, 0.5)
        cd += b'\x00\x00\x00\x00'   # ApplyMode=Override + 3 pad bytes

        # UV sample points: center + 4 quadrant centres (5 × 16 bytes = 80 bytes)
        uv_pts = [
            struct.pack('<dd', 0.5,  0.5),   # centre
            struct.pack('<dd', 0.25, 0.25),  # top-left quadrant
            struct.pack('<dd', 0.75, 0.25),  # top-right quadrant
            struct.pack('<dd', 0.25, 0.75),  # bottom-left quadrant
            struct.pack('<dd', 0.75, 0.75),  # bottom-right quadrant
        ]

        SC_SIZE  = 512
        DATA_OFF = SC_SIZE
        # Layout: [SC_SIZE shellcode][32 cd][5×16 uv]
        cd_off  = DATA_OFF
        uv_offs = [DATA_OFF + 32 + i * 16 for i in range(len(uv_pts))]
        total   = SC_SIZE + 32 + len(uv_pts) * 16

        mem = self._remote_alloc(self.pm.process_handle, total)
        if not mem:
            print("[CAMO] VirtualAllocEx failed")
            return False
        mem_i  = int(mem)
        cd_ptr = mem_i + cd_off

        def q(v): return struct.pack('<Q', v)

        # Proper prologue: save rbp, align rsp to 16, allocate shadow space.
        # and rsp,-16 can move RSP down by 0..15 bytes from the original, so we
        # cannot simply add back a fixed amount before ret. Saving rbp lets us
        # restore the exact original RSP value before returning.
        sc  = b'\x55'                       # push rbp        (RSP -= 8, saves callee-saved RBP)
        sc += b'\x48\x89\xE5'              # mov  rbp, rsp   (snapshot current RSP into RBP)
        sc += b'\x48\x83\xE4\xF0'          # and  rsp, -16   (align RSP down to 16-byte boundary)
        sc += b'\x48\x83\xEC\x20'          # sub  rsp, 0x20  (32-byte shadow space)

        for uv_off in uv_offs:
            uv_ptr = mem_i + uv_off
            sc += b'\x48\xB9' + q(comp)            # mov rcx, comp
            sc += b'\x48\xBA' + q(uv_ptr)          # mov rdx, &UV
            sc += b'\x49\xB8' + q(cd_ptr)          # mov r8,  &ChannelData
            sc += b'\x41\xB9\x04\x00\x00\x00'      # mov r9d, 4  (EPaintChannel::All)
            sc += b'\x48\xB8' + q(fn)              # mov rax, fn
            sc += b'\xFF\xD0'                      # call rax

        # Epilogue: restore RSP via RBP, then pop RBP, then ret cleanly.
        sc += b'\x48\x83\xC4\x20'          # add  rsp, 0x20  (undo shadow space)
        sc += b'\x48\x89\xEC'              # mov  rsp, rbp   (restore original RSP exactly)
        sc += b'\x5D'                       # pop  rbp        (restore caller's RBP)
        sc += b'\xC3'                       # ret             (return address is now at correct [rsp])

        sc = sc.ljust(SC_SIZE, b'\x90')
        payload = sc + cd + b''.join(uv_pts)
        payload = payload.ljust(total, b'\x00')

        try:
            self.pm.write_bytes(mem_i, payload, len(payload))
        except Exception as e:
            print(f"[CAMO] write shellcode failed: {e}")
            self._remote_free(self.pm.process_handle, mem)
            return False

        ok = self._remote_thread(self.pm.process_handle, mem_i)
        self._remote_free(self.pm.process_handle, mem)
        print(f"[CAMO] PaintAtUV x{len(uv_pts)} grid ok={ok}")
        return ok

    def set_camouflage_color(self, actor, r, g, b):
        """Apply camouflage by painting a UV grid with the sampled screen color."""
        r_lin = max(0.0, min(1.0, r / 255.0))
        g_lin = max(0.0, min(1.0, g / 255.0))
        b_lin = max(0.0, min(1.0, b / 255.0))
        col_lin = struct.pack("ffff", r_lin, g_lin, b_lin, 1.0)

        print(f"[CAMO] apply pawn=0x{actor:X} rgb=({r},{g},{b})")

        # Write CurrentPaintColor (read by the blueprint's own PaintTick)
        try:
            self.pm.write_bytes(actor + 0x0BA8, col_lin, 16)
        except Exception:
            pass

        comp = self._get_runtime_paint_component(actor)
        if not comp:
            print("[CAMO] No RuntimePaintableComponent — cannot apply camo")
            return False

        # Write AlbedoClearColor (used when the render target is recreated)
        try:
            self.pm.write_bytes(comp + 0x00D0, col_lin, 16)
            print("[CAMO] wrote AlbedoClearColor at comp+0x00D0")
        except Exception as e:
            print(f"[CAMO] AlbedoClearColor write failed: {e}")

        # Write brush settings directly to memory — no function call needed, no crash risk
        # CurrentBrushSettings at comp+0x0170 (FRuntimeBrushSettings):
        #   +0x00 Radius   float  comp+0x0170
        #   +0x04 Hardness float  comp+0x0174
        #   +0x08 Opacity  float  comp+0x0178
        try:
            self.pm.write_bytes(comp + 0x0170, struct.pack('<f', 100000.0), 4)  # Radius
            self.pm.write_bytes(comp + 0x0174, struct.pack('<f', 1.0), 4)       # Hardness
            self.pm.write_bytes(comp + 0x0178, struct.pack('<f', 1.0), 4)       # Opacity
            print("[CAMO] wrote brush Radius=100000 Hardness=1.0 Opacity=1.0")
        except Exception as e:
            print(f"[CAMO] brush settings write failed: {e}")

        # Disable auto-flush-to-server so no network RPC is triggered
        # bAutoFlushStrokes at comp+0x01AD (moved from 0x0199 in post-patch build)
        try:
            self.pm.write_bytes(comp + 0x01AD, b'\x00', 1)
        except Exception:
            pass

        # Paint at a 5-point UV grid — no BeginStroke/EndStroke to avoid game-thread state corruption
        print("[CAMO] calling PaintAtUV (5-pt grid, no stroke) via remote thread...")
        ok = self._call_paint_at_uv_grid(comp, r_lin, g_lin, b_lin)
        if ok:
            print("[CAMO] paint grid executed — character should now show sampled color")
            return True

        print("[CAMO] FAILED")
        return False

    # -----------------------------------------------------------------------
    # Chameleon camouflage — paint a multi-colour mosaic of the environment
    # onto the body texture so the player blends in (instead of one flat colour).
    # -----------------------------------------------------------------------
    def set_camouflage_pattern(self, actor, points):
        """
        Paint a mosaic of environment colours onto the body's albedo render target.

        points: list of (u, v, (r, g, b)) — u,v in 0..1 (body-texture UV),
                colours 0..255. Each point is painted with its own colour, so the
                body ends up showing the surrounding-environment palette.
        """
        if not points:
            print("[CAMO] no sample points — cannot apply camo")
            return False

        print(f"[CAMO] apply pattern pawn=0x{actor:X} cells={len(points)}")

        comp = self._get_runtime_paint_component(actor)
        if not comp:
            print("[CAMO] No RuntimePaintableComponent — cannot apply camo")
            return False

        # Read albedo render-target resolution so the brush is sized to tile cleanly.
        # FPaintTextureOptions @ comp+0x00B8, AlbedoResolution (int32) @ +0x00.
        resolution = 1024
        try:
            res = struct.unpack('<i', self.pm.read_bytes(comp + 0x00B8, 4))[0]
            if 64 <= res <= 8192:
                resolution = res
        except Exception:
            pass

        # Square grid → brush radius (in texture pixels) covers ~one cell with overlap.
        g = max(1, int(round(len(points) ** 0.5)))
        radius   = (resolution / g) * 0.85
        hardness = 0.45   # soft edge so neighbouring cells blend (natural, not blocky)
        opacity  = 1.0

        try:
            self.pm.write_bytes(comp + 0x0170, struct.pack('<f', radius), 4)    # Radius
            self.pm.write_bytes(comp + 0x0174, struct.pack('<f', hardness), 4)  # Hardness
            self.pm.write_bytes(comp + 0x0178, struct.pack('<f', opacity), 4)   # Opacity
            print(f"[CAMO] res={resolution} grid={g}x{g} brush r={radius:.0f} hard={hardness}")
        except Exception as e:
            print(f"[CAMO] brush settings write failed: {e}")

        # Disable auto-flush-to-server so no network RPC is triggered.
        try:
            self.pm.write_bytes(comp + 0x01AD, b'\x00', 1)
        except Exception:
            pass

        ok = self._call_paint_pattern(comp, points)
        if ok:
            print("[CAMO] pattern paint executed — body should now match surroundings")
            return True
        print("[CAMO] FAILED")
        return False

    def _call_paint_pattern(self, comp, points, channel=4):
        """
        Call the native PaintAtUV worker once per point in a single remote thread.
        channel: EPaintChannel — 0=Albedo, 4=All (default for camo).
        """
        base = self._module_base()
        if not base:
            return False
        fn = base + self.RVA_PAINT_AT_UV_NATIVE
        n = len(points)

        # Per-point FPaintChannelData (32 bytes) and UV (16 bytes).
        cd_block = b''
        uv_block = b''
        for (u, v, col) in points:
            rl = max(0.0, min(1.0, col[0] / 255.0))
            gl = max(0.0, min(1.0, col[1] / 255.0))
            bl = max(0.0, min(1.0, col[2] / 255.0))
            cd  = struct.pack('<ffff', rl, gl, bl, 1.0)   # AlbedoColor
            cd += struct.pack('<fff', 0.0, 0.5, 0.5)      # Metallic, Roughness, Height
            cd += b'\x00\x00\x00\x00'                      # ApplyMode=Override + pad
            cd_block += cd
            uv_block += struct.pack('<dd', float(u), float(v))

        # Per-point shellcode is 48 bytes; prologue/epilogue ~20. Reserve generously.
        SC_SIZE = (64 + n * 48 + 64 + 15) & ~15
        cd_off  = SC_SIZE
        uv_off  = SC_SIZE + len(cd_block)
        total   = SC_SIZE + len(cd_block) + len(uv_block)

        mem = self._remote_alloc(self.pm.process_handle, total)
        if not mem:
            print("[CAMO] VirtualAllocEx failed")
            return False
        mem_i = int(mem)

        def q(v): return struct.pack('<Q', v)

        # Prologue: save rbp, align rsp, shadow space (see _call_paint_at_uv_grid notes).
        sc  = b'\x55'                       # push rbp
        sc += b'\x48\x89\xE5'              # mov  rbp, rsp
        sc += b'\x48\x83\xE4\xF0'          # and  rsp, -16
        sc += b'\x48\x83\xEC\x20'          # sub  rsp, 0x20

        for i in range(n):
            cd_ptr = mem_i + cd_off + i * 32
            uv_ptr = mem_i + uv_off + i * 16
            sc += b'\x48\xB9' + q(comp)            # mov rcx, comp
            sc += b'\x48\xBA' + q(uv_ptr)          # mov rdx, &UV
            sc += b'\x49\xB8' + q(cd_ptr)          # mov r8,  &ChannelData
            sc += b'\x41\xB9' + struct.pack("<I", int(channel) & 0xFF)
            sc += b'\x48\xB8' + q(fn)              # mov rax, fn
            sc += b'\xFF\xD0'                      # call rax

        sc += b'\x48\x83\xC4\x20'          # add  rsp, 0x20
        sc += b'\x48\x89\xEC'              # mov  rsp, rbp
        sc += b'\x5D'                       # pop  rbp
        sc += b'\xC3'                       # ret

        if len(sc) > SC_SIZE:
            self._remote_free(self.pm.process_handle, mem)
            print(f"[CAMO] shellcode {len(sc)} exceeds SC_SIZE {SC_SIZE}")
            return False

        sc = sc.ljust(SC_SIZE, b'\x90')
        payload = (sc + cd_block + uv_block).ljust(total, b'\x00')

        try:
            self.pm.write_bytes(mem_i, payload, len(payload))
        except Exception as e:
            print(f"[CAMO] write shellcode failed: {e}")
            self._remote_free(self.pm.process_handle, mem)
            return False

        ok = self._remote_thread(
            self.pm.process_handle, mem_i,
            timeout_ms=max(60000, n * 100),
        )
        self._finish_remote_mem(self.pm.process_handle, mem, ok, f"PaintAtUV x{n}")
        print(f"[CAMO] PaintAtUV pattern x{n} ok={ok}")
        return ok

    # =======================================================================
    # TRUE CHAMELEON BLEND — screen-space painting
    #
    # PaintAtScreenPosition(MeshComponent, ScreenPosition, PlayerController,
    #                       ChannelData, Channel, bUseCachedTriangles) raycasts
    # the given screen point onto the body mesh and paints the hit UV. Feeding it
    # a grid of screen points (each tagged with the floor colour visible at/around
    # that point) makes each body part take the colour of the floor beneath it —
    # real spatial correspondence, not a scrambled UV mosaic.
    # =======================================================================

    # ---- GUObjectArray index resolution (for TWeakObjectPtr) ---------------
    def _object_by_index(self, index):
        """Resolve a UObject* from its GUObjectArray index (FUObjectItem stride 0x18,
        64K elements per chunk — same layout iter_objects() walks)."""
        if index is None or index <= 0 or index > 0x4000000:
            return 0
        ptr = rp(self.pm, self.guobject_array + 0x10)
        if not ptr:
            return 0
        chunk = rp(self.pm, ptr + (index >> 16) * 8)
        if not chunk:
            return 0
        return rp(self.pm, chunk + (index & 0xFFFF) * 0x18)

    # ---- Paintable target mesh resolution ---------------------------------
    def _get_paint_mesh(self, pawn, comp):
        """
        Find the UMeshComponent that the RuntimePaintable paints onto.

        Priority:
          1. comp+0x0208  TargetMeshComponent (TWeakObjectPtr<UMeshComponent>)
             — the mesh the component cached in InitializePaint(). Resolved via
             GUObjectArray from its ObjectIndex.
          2. pawn+0x0B60  Sphere (UStaticMeshComponent) — the blob body mesh on
             ABP_FirstPersonCharacter_cLeon_Character_C that paint is applied to.
          3. Any SkeletalMeshComponent on the pawn (last-ditch fallback).
        Returns (mesh_ptr, source_label).
        """
        # 1. cached TargetMeshComponent weak pointer
        try:
            obj_index = ru32(self.pm, comp + 0x0208)
            mesh = self._object_by_index(obj_index)
            if mesh and mesh > 0x100000:
                cn = self.objects.class_name(mesh)
                if "MeshComponent" in cn:
                    print(f"[CAMO] mesh=TargetMeshComponent 0x{mesh:X} class={cn} (idx={obj_index})")
                    return mesh, "TargetMeshComponent"
        except Exception:
            pass
        # 2. Sphere static mesh on the pawn
        try:
            sphere = rp(self.pm, pawn + 0x0B60)
            if sphere and sphere > 0x100000:
                cn = self.objects.class_name(sphere)
                if "MeshComponent" in cn:
                    print(f"[CAMO] mesh=Sphere 0x{sphere:X} class={cn}")
                    return sphere, "Sphere"
        except Exception:
            pass
        # 3. skeletal mesh fallback
        sk = self.get_skeletal_mesh(pawn)
        if sk:
            print(f"[CAMO] mesh=SkeletalMesh 0x{sk:X} class={self.objects.class_name(sk)}")
            return sk, "SkeletalMesh"
        return 0, ""

    # ---- Native worker discovery (runtime disassembly of the exec thunk) ---
    @staticmethod
    def _scan_call_jmp_targets(code, code_rva, text_lo_rva, text_hi_rva):
        """
        Find `call`/`jmp` rel32 transfers whose targets land in .text. Returns an
        ordered list of (pos_rva, kind, tgt_rva).

        Prefers capstone (proper instruction decode → no false positives). Falls
        back to a dependency-free naive E8/E9 byte scan (after a valid relative
        branch we skip its 5 bytes to cut overlap noise). Either way the self-test
        in _resolve_paint_screen_worker proves the heuristic on a known thunk
        (execPaintAtUV -> sub_50FCCE0) before we trust the result.
        """
        out = []
        try:
            import capstone
            md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
            md.detail = False
            for ins in md.disasm(code, code_rva):
                m = ins.mnemonic
                if m == "call" or m == "jmp":
                    op = ins.op_str.strip()
                    # only relative branches with an absolute immediate target
                    if op.startswith("0x"):
                        try:
                            tgt = int(op, 16)
                        except ValueError:
                            tgt = None
                        if tgt is not None and text_lo_rva <= tgt < text_hi_rva:
                            out.append((ins.address, m, tgt))
                            # Only a FAR jmp (outside the thunk band) is a real
                            # tail-call → end of thunk. Near jmps (e.g. +0x12C)
                            # are intra-thunk control flow; breaking on them
                            # would stop before the worker dispatch call.
                            if m == "jmp" and abs(tgt - code_rva) > MecchaESP.WORKER_FAR_BAND:
                                break
                if ins.mnemonic == "ret":
                    break
            return out
        except Exception:
            pass
        # ---- naive fallback (no capstone) --------------------------------
        n = len(code)
        i = 0
        while i + 5 <= n:
            b = code[i]
            if b == 0xE8 or b == 0xE9:
                rel = int.from_bytes(code[i + 1:i + 5], "little", signed=True)
                tgt = code_rva + i + 5 + rel
                if text_lo_rva <= tgt < text_hi_rva:
                    kind = "call" if b == 0xE8 else "jmp"
                    out.append((code_rva + i, kind, tgt))
                    if kind == "jmp" and abs(tgt - code_rva) > MecchaESP.WORKER_FAR_BAND:
                        break   # far tail-call → end of thunk
                    i += 5
                    continue
            i += 1
        return out

    # The exec-thunk band is a contiguous cluster of UFUNCTION thunks. Real
    # native workers live far away in .text (UV: thunk 0x50E20A0 -> worker
    # 0x50FCCE0, a 0x1AC40 jump). A target only a few hundred bytes from the
    # thunk is an intra-thunk local call, NOT the worker, so require the
    # worker to be well outside the thunk band.
    WORKER_FAR_BAND = 0x4000

    @staticmethod
    def _select_worker(transfers, exec_rva, log=False):
        """Pick the native member-function worker the exec thunk dispatches to.

        Observed thunk layout (verified on execPaintAtUV):
          * shared UE runtime helpers (FFrame::Step / FProperty handling) are
            huge BACKWARD calls into the 0x15xxxxx region and usually repeat
            (n > 1);
          * the per-UFUNCTION native worker sits just AFTER the exec-thunk
            cluster, reached by a single FORWARD call/jmp (positive distance,
            e.g. execPaintAtUV @0x50E21E3 -> 0x50FCCE0, d=+0x1AC40).

        So the worker is the LAST single-occurrence FORWARD transfer that lands
        outside the thunk band. Intra-thunk local jumps (d < band) and the
        backward engine helpers are both excluded."""
        from collections import Counter
        if not transfers:
            return 0
        cnt = Counter(t for _, _, t in transfers)
        band = MecchaESP.WORKER_FAR_BAND
        if log:
            for pos, kind, tgt in transfers:
                d = tgt - exec_rva
                print(f"[CAMO]   xfer @0x{pos:X} {kind:>3} -> 0x{tgt:X} "
                      f"(d={'+' if d >= 0 else '-'}0x{abs(d):X}, n={cnt[tgt]})")
        # single-occurrence, FORWARD, outside the thunk band
        fwd = [(pos, kind, tgt) for pos, kind, tgt in transfers
               if cnt[tgt] == 1 and (tgt - exec_rva) > band]
        if fwd:
            jmps = [t for _, k, t in fwd if k == "jmp"]
            if jmps:
                return jmps[-1]      # tail-call to worker
            return fwd[-1][2]        # last forward call = worker dispatch
        return 0

    def _resolve_paint_screen_worker(self):
        """
        Find the real native worker that execPaintAtScreenPosition tail-calls, by
        disassembling the thunk in the live process. Gated by a self-test: the same
        discovery run on execPaintAtUV must reproduce the proven worker sub_50FCCE0,
        otherwise we return 0 and refuse to call (crash-safe).
        Result is cached.
        """
        if self._paint_screen_worker_rva is not None:
            return self._paint_screen_worker_rva
        self._paint_screen_worker_rva = 0  # default: refuse
        base = self._module_base()
        if not base:
            print("[CAMO] no module base — cannot resolve worker")
            return 0
        mod = pymem.process.module_from_name(self.pm.process_handle, self.MODULE_NAME)
        if not mod:
            return 0
        text_lo = 0                                    # RVA space (relative to base)
        text_hi = mod.SizeOfImage
        try:
            uv_code = self.pm.read_bytes(base + self.RVA_PAINT_AT_UV, 0x600)
            sc_code = self.pm.read_bytes(base + self.RVA_EXEC_PAINT_AT_SCREEN, 0x600)
        except Exception as e:
            print(f"[CAMO] thunk read failed: {e}")
            return 0

        uv_tr = self._scan_call_jmp_targets(uv_code, self.RVA_PAINT_AT_UV, text_lo, text_hi)
        uv_worker = self._select_worker(uv_tr, self.RVA_PAINT_AT_UV)
        if uv_worker != self.RVA_PAINT_AT_UV_NATIVE:
            print("[CAMO] --- execPaintAtUV transfers (self-test) ---")
            self._select_worker(uv_tr, self.RVA_PAINT_AT_UV, log=True)
            print(f"[CAMO] worker self-test FAILED: execPaintAtUV -> 0x{uv_worker:X} "
                  f"(expected 0x{self.RVA_PAINT_AT_UV_NATIVE:X}). Refusing to call "
                  f"PaintAtScreenPosition to avoid a crash.")
            return 0
        print(f"[CAMO] worker self-test PASSED: execPaintAtUV -> sub_{uv_worker:X}")

        print("[CAMO] --- execPaintAtScreenPosition transfers ---")
        sc_tr = self._scan_call_jmp_targets(sc_code, self.RVA_EXEC_PAINT_AT_SCREEN, text_lo, text_hi)
        worker = self._select_worker(sc_tr, self.RVA_EXEC_PAINT_AT_SCREEN, log=True)
        if not worker:
            print("[CAMO] could not locate PaintAtScreenPosition worker")
            return 0
        # The UV self-test proves a real worker lives FAR outside the exec-thunk
        # band (~0x1AC40 away). A "worker" only a few hundred bytes from the
        # thunk is an intra-thunk local call — calling it last time corrupted the
        # game (delayed crash). Refuse anything inside the band.
        dist = abs(worker - self.RVA_EXEC_PAINT_AT_SCREEN)
        if dist <= self.WORKER_FAR_BAND:
            print(f"[CAMO] rejected worker 0x{worker:X}: only 0x{dist:X} from the "
                  f"thunk (intra-band local call, not a native worker). Refusing "
                  f"to call to avoid a crash.")
            return 0
        # sanity: worker should look like a function entry, not random bytes
        try:
            head = self.pm.read_bytes(base + worker, 1)[0]
            ok_prologue = head in (0x40, 0x41, 0x48, 0x4C, 0x53, 0x55, 0x56, 0x57)
        except Exception:
            ok_prologue = False
        print(f"[CAMO] discovered PaintAtScreenPosition worker RVA=0x{worker:X} "
              f"(dist=0x{dist:X}, prologue_ok={ok_prologue})")
        self._paint_screen_worker_rva = worker
        return worker

    def _resolve_hittest_screen_worker(self):
        """Discover native HitTestAtScreenPosition worker (requires paint self-test)."""
        if self._hittest_screen_worker_rva is not None:
            return self._hittest_screen_worker_rva
        self._hittest_screen_worker_rva = 0
        if not self._resolve_paint_screen_worker():
            return 0
        base = self._module_base()
        if not base:
            return 0
        mod = pymem.process.module_from_name(self.pm.process_handle, self.MODULE_NAME)
        if not mod:
            return 0
        text_hi = mod.SizeOfImage
        try:
            ht_code = self.pm.read_bytes(base + self.RVA_EXEC_HITTEST_AT_SCREEN, 0x600)
        except Exception as e:
            print(f"[PAINT] HitTest thunk read failed: {e}")
            return 0
        ht_tr = self._scan_call_jmp_targets(
            ht_code, self.RVA_EXEC_HITTEST_AT_SCREEN, 0, text_hi,
        )
        worker = self._select_worker(ht_tr, self.RVA_EXEC_HITTEST_AT_SCREEN)
        if not worker:
            print("[PAINT] could not locate HitTestAtScreenPosition worker")
            return 0
        dist = abs(worker - self.RVA_EXEC_HITTEST_AT_SCREEN)
        if dist <= self.WORKER_FAR_BAND:
            print(f"[PAINT] rejected HitTest worker 0x{worker:X} (intra-band)")
            return 0
        print(f"[PAINT] HitTest worker RVA=0x{worker:X} (dist=0x{dist:X})")
        self._hittest_screen_worker_rva = worker
        return worker

    def _hit_test_at_screen(self, comp, mesh, pc, sx, sy):
        """Raycast a screen point onto the paint mesh; return (ok, u, v)."""
        worker = self._resolve_hittest_screen_worker()
        if not worker:
            return False, 0.0, 0.0
        base = self._module_base()
        if not base:
            return False, 0.0, 0.0
        fn = base + worker

        SC_SIZE = 512
        RESULT_SZ = 0x50
        SP_OFF = SC_SIZE
        total = SC_SIZE + RESULT_SZ + 16

        mem = self._remote_alloc(self.pm.process_handle, total)
        if not mem:
            return False, 0.0, 0.0
        mem_i = int(mem)
        res_ptr = mem_i + SP_OFF
        sp_ptr = mem_i + SP_OFF + RESULT_SZ

        def q(v):
            return struct.pack("<Q", v)

        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x20"
        sc += b"\x48\xB9" + q(res_ptr)
        sc += b"\x48\xBA" + q(comp)
        sc += b"\x49\xB8" + q(mesh)
        sc += b"\x49\xB9" + q(sp_ptr)
        sc += b"\x48\xB8" + q(pc)
        sc += b"\x48\x89\x44\x24\x20"
        sc += b"\x48\xC7\x44\x24\x28\x00\x00\x00\x00"
        sc += b"\x48\xB8" + q(fn)
        sc += b"\xFF\xD0"
        sc += b"\x48\x89\xEC\x5D\xC3"
        sc = sc.ljust(SC_SIZE, b"\x90")

        sp = struct.pack("<dd", float(sx), float(sy))
        try:
            self.pm.write_bytes(mem_i, sc + (b"\x00" * RESULT_SZ) + sp, total)
        except Exception:
            self._remote_free(self.pm.process_handle, mem)
            return False, 0.0, 0.0

        ok = self._remote_thread(self.pm.process_handle, mem_i)
        success, u, v = False, 0.0, 0.0
        if ok:
            try:
                raw = self.pm.read_bytes(res_ptr, RESULT_SZ)
                success = raw[0] != 0
                u, v = struct.unpack("<dd", raw[8:24])
            except Exception:
                pass
        self._remote_free(self.pm.process_handle, mem)
        return success, u, v

    # ---- Screen-space paint remote call -----------------------------------
    def _call_paint_at_screen(self, comp, mesh, pc, fn_rva, points, channel=4):
        """
        Call the native PaintAtScreenPosition worker once per screen point in a
        single remote thread.

        ABI (MSVC x64, member function returning FScreenSpacePaintResult).
        FScreenSpacePaintResult is 0x48 bytes (>8) so it is returned via a hidden
        pointer in RCX, which shifts `this` to RDX. FVector2D (16B) and
        FPaintChannelData (32B) are passed BY POINTER (declared const-ref):

          RCX            = &ResultScratch        (hidden sret, 0x48+ buffer)
          RDX            = comp                  (this = URuntimePaintableComponent*)
          R8             = mesh                  (UMeshComponent*)
          R9             = &ScreenPosition       (FVector2D{double X, double Y})
          [rsp+0x20]     = PlayerController*
          [rsp+0x28]     = &ChannelData          (FPaintChannelData*)
          [rsp+0x30]     = Channel               (EPaintChannel; All=4, low byte)
          [rsp+0x38]     = bUseCachedTriangles   (bool)

        Crash-safe stack handling (same proven prologue/epilogue as the UV path):
          push rbp / mov rbp,rsp / and rsp,-16 / sub rsp,0x60 / ...calls...
          mov rsp,rbp / pop rbp / ret
        0x60 = 0x20 shadow + 0x20 for the 4 stack args + slack, 16-aligned.
        """
        base = self._module_base()
        if not base or not fn_rva:
            return False
        fn = base + fn_rva
        n = len(points)

        # ---- data blocks -------------------------------------------------
        # ScreenPosition (16B each) + ChannelData (32B each) + one shared result
        # scratch (0x50, zeroed). EPaintChannel::All = 4.
        sp_block = b""
        cd_block = b""
        for (sx, sy, col) in points:
            sp_block += struct.pack("<dd", float(sx), float(sy))
            rl = max(0.0, min(1.0, col[0] / 255.0))
            gl = max(0.0, min(1.0, col[1] / 255.0))
            bl = max(0.0, min(1.0, col[2] / 255.0))
            cd  = struct.pack("<ffff", rl, gl, bl, 1.0)   # AlbedoColor
            cd += struct.pack("<fff", 0.0, 0.5, 0.5)      # Metallic, Roughness, Height
            cd += b"\x00\x00\x00\x00"                      # ApplyMode=Override + pad
            cd_block += cd

        RESULT_SZ = 0x50
        # per-point shellcode is 112 bytes; prologue/epilogue ~20. Reserve generously.
        SC_SIZE  = (32 + n * 128 + 32 + 15) & ~15
        res_off  = SC_SIZE
        sp_off   = res_off + RESULT_SZ
        cd_off   = sp_off + len(sp_block)
        total    = cd_off + len(cd_block)

        mem = self._remote_alloc(self.pm.process_handle, total)
        if not mem:
            print("[CAMO] VirtualAllocEx failed")
            return False
        mem_i   = int(mem)
        res_ptr = mem_i + res_off

        def q(v):
            return struct.pack("<Q", v)

        # Prologue: save rbp, align rsp to 16, allocate shadow + stack-arg space.
        sc  = b"\x55"                       # push rbp
        sc += b"\x48\x89\xE5"              # mov  rbp, rsp
        sc += b"\x48\x83\xE4\xF0"          # and  rsp, -16
        sc += b"\x48\x83\xEC\x60"          # sub  rsp, 0x60

        for i in range(n):
            sp_ptr = mem_i + sp_off + i * 16
            cd_ptr = mem_i + cd_off + i * 32
            btri   = 0 if i == 0 else 1     # build triangle cache once, then reuse
            sc += b"\x48\xB9" + q(res_ptr)        # mov rcx, &ResultScratch (sret)
            sc += b"\x48\xBA" + q(comp)           # mov rdx, comp (this)
            sc += b"\x49\xB8" + q(mesh)           # mov r8,  mesh
            sc += b"\x49\xB9" + q(sp_ptr)         # mov r9,  &ScreenPosition
            sc += b"\x48\xB8" + q(pc)             # mov rax, PlayerController
            sc += b"\x48\x89\x44\x24\x20"        # mov [rsp+0x20], rax
            sc += b"\x48\xB8" + q(cd_ptr)         # mov rax, &ChannelData
            sc += b"\x48\x89\x44\x24\x28"        # mov [rsp+0x28], rax
            sc += b"\x48\xB8" + q(channel & 0xFF)
            sc += b"\x48\x89\x44\x24\x30"        # mov [rsp+0x30], rax
            sc += b"\x48\xB8" + q(btri)           # mov rax, bUseCachedTriangles
            sc += b"\x48\x89\x44\x24\x38"        # mov [rsp+0x38], rax
            sc += b"\x48\xB8" + q(fn)             # mov rax, fn
            sc += b"\xFF\xD0"                      # call rax

        # Epilogue: restore RSP exactly via RBP, pop RBP, ret.
        sc += b"\x48\x89\xEC"              # mov  rsp, rbp
        sc += b"\x5D"                       # pop  rbp
        sc += b"\xC3"                       # ret

        if len(sc) > SC_SIZE:
            self._remote_free(self.pm.process_handle, mem)
            print(f"[CAMO] shellcode {len(sc)} exceeds SC_SIZE {SC_SIZE}")
            return False

        sc = sc.ljust(SC_SIZE, b"\x90")
        payload = sc + (b"\x00" * RESULT_SZ) + sp_block + cd_block
        payload = payload.ljust(total, b"\x00")

        try:
            self.pm.write_bytes(mem_i, payload, len(payload))
        except Exception as e:
            print(f"[CAMO] write shellcode failed: {e}")
            self._remote_free(self.pm.process_handle, mem)
            return False

        ok = self._remote_thread(self.pm.process_handle, mem_i)
        self._remote_free(self.pm.process_handle, mem)
        print(f"[CAMO] PaintAtScreenPosition x{n} ok={ok}")
        return ok

    def _call_paint_at_uv_worker(self, comp, uv_points):
        """
        Call the native PaintAtUV worker (RVA_PAINT_AT_UV_NATIVE = 0x50FCCE0)
        for each (u, v, (r,g,b)) point.

        ABI (MSVC x64, member function, void return — no hidden sret):
          RCX = this  (URuntimePaintableComponent*)
          RDX = &Uv   (FVector2D: two doubles, 16 bytes)
          R8  = &ChannelData (FPaintChannelData, 32 bytes)
          R9  = Channel (EPaintChannel uint8; Albedo=0)

        Only needs 0x20 shadow space, no stack arguments. No physics trace, no
        PlayerController, no render-thread dependency — safe from a remote thread.
        """
        base = self._module_base()
        if not base:
            return False
        fn = base + self.RVA_PAINT_AT_UV_NATIVE

        n = len(uv_points)
        # per-call: 5 × mov-rXX imm64 (10 bytes each) + call rax (2) = 52 bytes
        SC_SIZE = (32 + n * 64 + 16) & ~15
        uv_off  = SC_SIZE            # FVector2D blocks (16 B each)
        cd_off  = uv_off + n * 16   # FPaintChannelData blocks (32 B each)
        total   = cd_off + n * 32

        mem = self._remote_alloc(self.pm.process_handle, total)
        if not mem:
            print("[CAMO] VirtualAllocEx failed")
            return False
        mem_i = int(mem)

        uv_block = b""
        cd_block = b""
        for (u, v, col) in uv_points:
            uv_block += struct.pack("<dd", float(u), float(v))
            rl = max(0.0, min(1.0, col[0] / 255.0))
            gl = max(0.0, min(1.0, col[1] / 255.0))
            bl = max(0.0, min(1.0, col[2] / 255.0))
            cd  = struct.pack("<ffff", rl, gl, bl, 1.0)   # AlbedoColor RGBA
            cd += struct.pack("<fff",  0.0, 0.5, 0.5)     # Metallic, Roughness, Height
            cd += b"\x00\x00\x00\x00"                      # ApplyMode=Override(0) + pad3
            cd_block += cd

        def q(v):
            return struct.pack("<Q", int(v))

        sc  = b"\x55"               # push rbp
        sc += b"\x48\x89\xE5"       # mov  rbp, rsp
        sc += b"\x48\x83\xE4\xF0"   # and  rsp, -16
        sc += b"\x48\x83\xEC\x20"   # sub  rsp, 0x20  (shadow space only)

        for i in range(n):
            uv_ptr = mem_i + uv_off + i * 16
            cd_ptr = mem_i + cd_off + i * 32
            sc += b"\x48\xB9" + q(comp)    # mov rcx, comp (this)
            sc += b"\x48\xBA" + q(uv_ptr)  # mov rdx, &Uv
            sc += b"\x49\xB8" + q(cd_ptr)  # mov r8,  &ChannelData
            sc += b"\x49\xB9" + q(0)       # mov r9,  0 (EPaintChannel::Albedo)
            sc += b"\x48\xB8" + q(fn)      # mov rax, fn
            sc += b"\xFF\xD0"              # call rax

        sc += b"\x48\x83\xC4\x20"   # add  rsp, 0x20  (undo shadow space)
        sc += b"\x48\x89\xEC"   # mov rsp, rbp
        sc += b"\x5D"            # pop rbp
        sc += b"\xC3"            # ret

        if len(sc) > SC_SIZE:
            self._remote_free(self.pm.process_handle, mem)
            print(f"[CAMO] UV shellcode {len(sc)} exceeds SC_SIZE {SC_SIZE}")
            return False

        sc = sc.ljust(SC_SIZE, b"\x90")
        payload = sc + uv_block + cd_block
        payload = payload.ljust(total, b"\x00")

        try:
            self.pm.write_bytes(mem_i, payload, len(payload))
        except Exception as e:
            print(f"[CAMO] write UV shellcode failed: {e}")
            self._remote_free(self.pm.process_handle, mem)
            return False

        ok = self._remote_thread(
            self.pm.process_handle, mem_i, timeout_ms=max(8000, n * 4)
        )
        self._remote_free(self.pm.process_handle, mem)
        print(f"[CAMO] PaintAtUV x{n} ok={ok}")
        return ok

    def _call_clear_paint_channel(self, comp, channel=None):
        """Clear a paint channel (wipes previous strokes) before a full re-apply."""
        channel = self.EPaintChannel_Albedo if channel is None else channel
        if self._clear_worker_rva is None:
            self._resolve_channel_io_workers()
        worker = self._clear_worker_rva or self.RVA_CLEAR_CHANNEL_NATIVE
        base = self._module_base()
        if not base or not comp:
            return False
        fn = base + worker

        def q(v):
            return struct.pack("<Q", v)

        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x20"
        sc += b"\x48\xB9" + q(comp)
        sc += bytes([0xB2, channel & 0xFF])
        sc += b"\x48\xB8" + q(fn) + b"\xFF\xD0"
        sc += b"\x48\x83\xC4\x20\x48\x89\xEC\x5D\xC3"
        sc = sc.ljust(256, b"\x90")

        mem = self._remote_alloc(self.pm.process_handle, len(sc))
        if not mem:
            return False
        try:
            self.pm.write_bytes(int(mem), sc, len(sc))
        except Exception:
            self._remote_free(self.pm.process_handle, mem)
            return False
        ok = self._remote_thread(self.pm.process_handle, int(mem), timeout_ms=8000)
        self._remote_free(self.pm.process_handle, mem)
        print(f"[PAINT] ClearChannel({channel}) ok={ok}")
        return ok

    def _write_brush_settings(self, comp, radius, hardness, opacity):
        try:
            self.pm.write_bytes(comp + 0x0170, struct.pack("<f", radius), 4)
            self.pm.write_bytes(comp + 0x0174, struct.pack("<f", hardness), 4)
            self.pm.write_bytes(comp + 0x0178, struct.pack("<f", opacity), 4)
            return True
        except Exception:
            return False

    def _opaque_image_average(self, bgra_bytes, resolution):
        """Mean RGB of non-transparent texture pixels (for base coat)."""
        rs = gs = bs = n = 0
        for off in range(0, len(bgra_bytes), 4):
            if bgra_bytes[off + 3] < self.MIN_PAINT_ALPHA:
                continue
            rs += bgra_bytes[off + 2]
            gs += bgra_bytes[off + 1]
            bs += bgra_bytes[off]
            n += 1
        if not n:
            return None
        return (rs // n, gs // n, bs // n)

    def _paint_base_override(self, comp, rgb, regions=None):
        """Knock down prior paint with a few large Override stamps (no ClearChannel)."""
        saved = None
        try:
            saved = self.pm.read_bytes(comp + 0x0170, 12)
        except Exception:
            pass
        self._write_brush_settings(comp, 0.48, 1.0, 1.0)
        base = []
        if regions:
            for u0, v0, u1, v1 in regions:
                uc = (u0 + u1) * 0.5
                vc = (v0 + v1) * 0.5
                base.extend([
                    (uc, vc, rgb),
                    (u0 + 0.02, vc, rgb),
                    (u1 - 0.02, vc, rgb),
                    (uc, v0 + 0.02, rgb),
                    (uc, v1 - 0.02, rgb),
                ])
        else:
            base = [
                (0.50, 0.50, rgb),
                (0.02, 0.50, rgb),
                (0.98, 0.50, rgb),
                (0.50, 0.02, rgb),
                (0.50, 0.98, rgb),
            ]
        ok = self._call_paint_pattern(comp, base, channel=0)
        if saved and len(saved) == 12:
            try:
                self.pm.write_bytes(comp + 0x0170, saved, 12)
            except Exception:
                pass
        return ok

    def _apply_uv_paint_points(
        self, actor, points, log_prefix="CAMO", replace=False, brush_grid=None,
        progress_cb=None, brush_opacity=1.0, brush_hardness=1.0, freeze=True,
        base_color=None, base_regions=None, comp=None, fast_mode=False,
    ):
        """
        Paint UV points with correct UV-space brush sizing.

        freeze=True suspends the game's threads for the whole apply so paint
        natives run while the game is visually frozen (avoids render-thread races).
        Pass comp= when the caller already validated RuntimePaintableComponent.
        """
        if not points:
            return False
        paint_comp = comp or self._get_runtime_paint_component(actor, quiet=bool(comp))
        if not paint_comp:
            print(f"[{log_prefix}] No RuntimePaintableComponent — cannot paint")
            return False

        self._prepare_paint_component(paint_comp)

        if brush_grid is not None:
            g = max(1, int(brush_grid))
        else:
            g = max(1, int(round(len(points) ** 0.5)))

        # Brush radius is in UV-space units (0..1 fraction of texture width).
        # For image/preset painting we want each stamp to cover exactly its own cell
        # plus a tiny overlap to seal seams: radius = 0.6 / g  (= 0.6 × cell_size).
        # - Too large (old: 3.5/g = 0.109 for g=32) → each stamp floods many cells →
        #   the last stamp wins everywhere → whole body turns one solid colour.
        # - Too large as pixel units (e.g. 2.4 or 64 UV) → same flooding, much worse.
        # - 0.6/g keeps stamps just slightly overlapping their neighbours only.
        if log_prefix in ("PAINT", "PRESET"):
            # 1.5/g gives ~3× overlap so stamps merge seamlessly into a solid surface.
            # g=256 → radius=0.0059 UV ≈ 6 px diameter on a 1024 texture.
            radius = min(0.45, 1.5 / g)
        else:
            radius = min(0.45, 3.5 / g)   # camo: wide blending is fine (smooth colours)

        hardness = brush_hardness
        if log_prefix in ("PAINT", "PRESET"):
            # 0.95 = near-hard circles that still blend at edges without
            # creating the blurry/smeared "watercolour" look of low hardness.
            hardness = 0.95
        opacity = max(0.0, min(1.0, float(brush_opacity)))
        paint_channel = 4  # EPaintChannel::All — same as F10 camo (Albedo-only is invisible)
        try:
            self._write_brush_settings(paint_comp, radius, hardness, opacity)
            print(f"[{log_prefix}] grid~{g}x{g} brush r={radius:.4f}UV "
                  f"hard={hardness:.2f} op={opacity:.2f} stamps={len(points)} "
                  f"freeze={freeze}")
        except Exception as e:
            print(f"[{log_prefix}] brush settings write failed: {e}")

        def _run_batches():
            import time
            if replace and log_prefix not in ("PAINT", "PRESET"):
                avg = base_color
                if avg is None and points:
                    avg = (
                        sum(p[2][0] for p in points) // len(points),
                        sum(p[2][1] for p in points) // len(points),
                        sum(p[2][2] for p in points) // len(points),
                    )
                if avg and sum(avg) > 30:
                    base_ok = self._paint_base_override(paint_comp, avg, regions=base_regions)
                    if not base_ok:
                        return False
                self._write_brush_settings(paint_comp, radius, hardness, opacity)
            elif replace:
                self._write_brush_settings(paint_comp, radius, hardness, opacity)

            total = len(points)
            if log_prefix in ("PAINT", "PRESET"):
                # Game is frozen for PAINT/PRESET.  Quality-5 fast_mode doubles the
                # batch (256) to halve remote-call overhead; otherwise use default 128.
                batch = self.PAINT_UV_BATCH_FAST if fast_mode else self.PAINT_UV_BATCH_SIZE
            elif freeze:
                batch = self.PAINT_UV_BATCH_SIZE
                if log_prefix == "CAMO" and total > 4000:
                    batch = 64
            else:
                # Live (unfrozen) camo path.  Quality-5 fast_mode: 4× bigger batches
                # and 4× shorter inter-batch pause to spend less time sleeping.
                if fast_mode:
                    batch = 96
                else:
                    batch = self.PAINT_UV_BATCH_SAFE
            live_pause = (
                0.005 if (fast_mode and not freeze)
                else self.PAINT_LIVE_BATCH_PAUSE if not freeze
                else 0.0
            )

            for offset in range(0, total, batch):
                chunk = points[offset:offset + batch]
                batch_comp = paint_comp
                if not comp:
                    batch_comp = self._get_runtime_paint_component(actor, quiet=True)
                if not batch_comp:
                    print(f"[{log_prefix}] paint component lost at offset {offset}")
                    return False
                if not self._call_paint_pattern(batch_comp, chunk, channel=paint_channel):
                    print(f"[{log_prefix}] UV batch failed at offset {offset} — stopping")
                    return False
                done = min(offset + len(chunk), total)
                if progress_cb:
                    try:
                        progress_cb(done, total)
                    except Exception:
                        pass
                if live_pause and offset + batch < total:
                    time.sleep(live_pause)
            return True

        if freeze:
            with self._game_frozen(log_prefix):
                return _run_batches()
        return _run_batches()

    def set_camouflage_screenspace(self, actor, uv_points, brush_opacity=1.0, brush_hardness=0.42, fast_paint=False):
        """
        TRUE chameleon blend: paint each body part with the floor colour visible
        at/around its on-screen position.

        uv_points: list of (u, v, (r, g, b)) — u/v in [0,1] UV space,
        colours 0..255. The UV grid maps proportionally to the body bbox on screen,
        so each texture cell is painted with the floor colour beneath that part of
        the body.

        Calls the native PaintAtUV worker (0x50FCCE0) directly — no screen-space
        physics trace, no PlayerController, thread-safe from a remote thread.
        """
        if not uv_points:
            print("[CAMO] no UV points — cannot apply camo")
            return False

        print(f"[CAMO] UV apply pawn=0x{actor:X} points={len(uv_points)}")

        g = max(8, int(round(len(uv_points) ** 0.5)))
        ok = self._apply_uv_paint_points(
            actor, uv_points, log_prefix="CAMO", brush_grid=g,
            brush_opacity=brush_opacity, brush_hardness=brush_hardness,
            fast_mode=fast_paint,
        )
        if ok:
            print("[CAMO] UV paint executed — body should now match the floor")
            return True
        print("[CAMO] FAILED")
        return False

    # -----------------------------------------------------------------------
    # Custom paint — image import + save/load presets (Export/ImportChannel)
    # -----------------------------------------------------------------------
    @staticmethod
    def ensure_paint_presets_dir():
        os.makedirs(PAINT_PRESETS_DIR, exist_ok=True)
        return PAINT_PRESETS_DIR

    def get_albedo_resolution(self, comp=None, pawn=None):
        """Return the albedo render-target resolution (square, typically 1024)."""
        if not comp:
            pawn = pawn or self._get_local_pawn()
            if not pawn:
                return 1024
            comp = rp(self.pm, pawn + 0x0B68)
        if not comp or comp <= 0x100000:
            return 1024
        try:
            res = struct.unpack("<i", self.pm.read_bytes(comp + 0x00B8, 4))[0]
            if 64 <= res <= 8192:
                return res
        except Exception:
            pass
        return 1024

    def _prepare_paint_component(self, comp):
        try:
            self.pm.write_bytes(comp + 0x01AD, b"\x00", 1)
        except Exception:
            pass

    def _resolve_channel_io_workers(self):
        """Resolve Export/Import native workers; cache after first successful read."""
        if self._channel_io_resolved:
            return self._export_worker_rva, self._import_worker_rva
        self._channel_io_resolved = True
        self._export_worker_rva = self.RVA_EXPORT_CHANNEL_NATIVE
        self._import_worker_rva = self.RVA_IMPORT_CHANNEL_NATIVE
        self._clear_worker_rva = self.RVA_CLEAR_CHANNEL_NATIVE
        base = self._module_base()
        if not base:
            return self._export_worker_rva, self._import_worker_rva
        mod = pymem.process.module_from_name(self.pm.process_handle, self.MODULE_NAME)
        if not mod:
            return self._export_worker_rva, self._import_worker_rva
        text_hi = mod.SizeOfImage
        try:
            uv_code = self.pm.read_bytes(base + self.RVA_PAINT_AT_UV, 0x600)
            ex_code = self.pm.read_bytes(base + self.RVA_EXEC_EXPORT_CHANNEL, 0x600)
            im_code = self.pm.read_bytes(base + self.RVA_EXEC_IMPORT_CHANNEL, 0x600)
        except Exception:
            return self._export_worker_rva, self._import_worker_rva
        uv_tr = self._scan_call_jmp_targets(uv_code, self.RVA_PAINT_AT_UV, 0, text_hi)
        if self._select_worker(uv_tr, self.RVA_PAINT_AT_UV) != self.RVA_PAINT_AT_UV_NATIVE:
            print("[PAINT] channel IO worker self-test failed — using dump RVAs")
            return self._export_worker_rva, self._import_worker_rva
        ex_tr = self._scan_call_jmp_targets(ex_code, self.RVA_EXEC_EXPORT_CHANNEL, 0, text_hi)
        im_tr = self._scan_call_jmp_targets(im_code, self.RVA_EXEC_IMPORT_CHANNEL, 0, text_hi)
        cl_code = self.pm.read_bytes(base + self.RVA_EXEC_CLEAR_CHANNEL, 0x600)
        cl_tr = self._scan_call_jmp_targets(cl_code, self.RVA_EXEC_CLEAR_CHANNEL, 0, text_hi)
        ex_w = self._select_worker(ex_tr, self.RVA_EXEC_EXPORT_CHANNEL)
        im_w = self._select_worker(im_tr, self.RVA_EXEC_IMPORT_CHANNEL)
        cl_w = self._select_worker(cl_tr, self.RVA_EXEC_CLEAR_CHANNEL)
        if ex_w and abs(ex_w - self.RVA_EXEC_EXPORT_CHANNEL) > self.WORKER_FAR_BAND:
            self._export_worker_rva = ex_w
        if im_w and abs(im_w - self.RVA_EXEC_IMPORT_CHANNEL) > self.WORKER_FAR_BAND:
            self._import_worker_rva = im_w
        if cl_w and abs(cl_w - self.RVA_EXEC_CLEAR_CHANNEL) > self.WORKER_FAR_BAND:
            self._clear_worker_rva = cl_w
        print(f"[PAINT] export worker=0x{self._export_worker_rva:X} "
              f"import worker=0x{self._import_worker_rva:X} "
              f"clear worker=0x{self._clear_worker_rva:X}")
        return self._export_worker_rva, self._import_worker_rva

    def _call_export_channel_bytes(self, comp, channel=None):
        """Export one paint channel via the native ExportChannelToBytes worker."""
        channel = self.EPaintChannel_Albedo if channel is None else channel
        export_rva, _ = self._resolve_channel_io_workers()
        if not export_rva:
            return None
        base = self._module_base()
        if not base:
            return None
        fn = base + export_rva
        SC_SIZE = 512
        tarray_off = SC_SIZE
        total = SC_SIZE + 16
        mem = self._remote_alloc(self.pm.process_handle, total)
        if not mem:
            return None
        mem_i = int(mem)
        tarray_ptr = mem_i + tarray_off

        def q(v):
            return struct.pack("<Q", v)

        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x20"
        sc += b"\x48\xB9" + q(comp)
        sc += bytes([0xB2, channel & 0xFF])
        sc += b"\x49\xB8" + q(tarray_ptr)
        sc += b"\x48\xB8" + q(fn) + b"\xFF\xD0"
        sc += b"\x48\x83\xC4\x20\x48\x89\xEC\x5D\xC3"
        sc = sc.ljust(SC_SIZE, b"\x90")

        try:
            self.pm.write_bytes(mem_i, sc + b"\x00" * 16, total)
        except Exception as e:
            print(f"[PAINT] export shellcode write failed: {e}")
            self._remote_free(self.pm.process_handle, mem)
            return None

        self._remote_thread(self.pm.process_handle, mem_i)
        try:
            data_ptr, count, _cap = struct.unpack("<QII", self.pm.read_bytes(tarray_ptr, 16))
        except Exception:
            data_ptr, count = 0, 0
        blob = None
        if count and data_ptr:
            try:
                blob = bytes(self.pm.read_bytes(data_ptr, count))
            except Exception as e:
                print(f"[PAINT] export read failed: {e}")
        self._remote_free(self.pm.process_handle, mem)
        if blob:
            print(f"[PAINT] exported {len(blob)} bytes (channel={channel})")
        return blob

    def _call_import_channel_bytes(self, comp, data, channel=None):
        """Import raw channel bytes via the native ImportChannelFromBytes worker."""
        if not data:
            return False
        channel = self.EPaintChannel_Albedo if channel is None else channel
        _, import_rva = self._resolve_channel_io_workers()
        if not import_rva:
            return False
        base = self._module_base()
        if not base:
            return False
        fn = base + import_rva

        data_mem = self._remote_alloc(self.pm.process_handle, len(data))
        if not data_mem:
            return False
        data_i = int(data_mem)
        try:
            self.pm.write_bytes(data_i, data, len(data))
        except Exception as e:
            print(f"[PAINT] import data write failed: {e}")
            self._remote_free(self.pm.process_handle, data_mem)
            return False

        SC_SIZE = 512
        tarray_off = SC_SIZE
        total = SC_SIZE + 16
        mem = self._remote_alloc(self.pm.process_handle, total)
        if not mem:
            self._remote_free(self.pm.process_handle, data_mem)
            return False
        mem_i = int(mem)
        tarray_ptr = mem_i + tarray_off
        tarray = struct.pack("<QII", data_i, len(data), len(data))

        def q(v):
            return struct.pack("<Q", v)

        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x20"
        sc += b"\x48\xB9" + q(comp)
        sc += bytes([0xB2, channel & 0xFF])
        sc += b"\x49\xB8" + q(tarray_ptr)
        sc += b"\x48\xB8" + q(fn) + b"\xFF\xD0"
        sc += b"\x48\x83\xC4\x20\x48\x89\xEC\x5D\xC3"
        sc = sc.ljust(SC_SIZE, b"\x90")

        try:
            self.pm.write_bytes(mem_i, sc + tarray, total)
        except Exception as e:
            print(f"[PAINT] import shellcode write failed: {e}")
            self._remote_free(self.pm.process_handle, mem)
            self._remote_free(self.pm.process_handle, data_mem)
            return False

        ok = self._remote_thread(
            self.pm.process_handle, mem_i, timeout_ms=30000,
        )
        ok = self._finish_remote_mem(self.pm.process_handle, mem, ok, "ImportChannel")
        if ok:
            self._remote_free(self.pm.process_handle, data_mem)
        else:
            print("[PAINT] import data memory kept allocated after failed thread")
        print(f"[PAINT] import {len(data)} bytes ok={ok}")
        return ok

    def export_paint_albedo(self, pawn=None):
        """Read the current albedo paint texture bytes from the local character."""
        pawn = pawn or self._get_local_pawn()
        if not pawn:
            print("[PAINT] no local pawn")
            return None, 0
        comp = self._get_runtime_paint_component(pawn)
        if not comp:
            return None, 0
        resolution = self.get_albedo_resolution(comp)
        self._prepare_paint_component(comp)
        blob = self._call_export_channel_bytes(comp, self.EPaintChannel_Albedo)
        if not blob:
            return None, resolution
        expected = resolution * resolution * 4
        if len(blob) != expected:
            print(f"[PAINT] export size {len(blob)} (expected ~{expected})")
        self._last_paint_bgra = bytes(blob)
        self._last_paint_resolution = resolution
        return blob, resolution

    def import_paint_albedo(self, pawn, data, resolution=None):
        """Write albedo paint bytes onto the local character."""
        pawn = pawn or self._get_local_pawn()
        if not pawn or not data:
            return False
        comp = self._get_runtime_paint_component(pawn)
        if not comp:
            return False
        if resolution:
            expected = resolution * resolution * 4
            if len(data) != expected:
                print(f"[PAINT] import size mismatch: got {len(data)}, expected {expected}")
        self._prepare_paint_component(comp)
        ok = self._call_import_channel_bytes(comp, data, self.EPaintChannel_Albedo)
        if ok:
            self._last_paint_bgra = bytes(data)
            self._last_paint_resolution = resolution or self.get_albedo_resolution(comp)
        return ok

    @staticmethod
    def _sanitize_preset_name(name):
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (name or "")).strip()
        return safe or "preset"

    def list_paint_presets(self):
        self.ensure_paint_presets_dir()
        out = []
        for fname in os.listdir(PAINT_PRESETS_DIR):
            if fname.endswith(".mechpaint"):
                out.append(fname[:-10])
        return sorted(out, key=str.lower)

    def save_paint_preset(self, name, pawn=None, grid=None):
        """Save texture data to a .mechpaint preset file (disk only — does not touch the game)."""
        grid = self.parse_grid_value(grid or self._last_paint_grid or 32)
        blob = None
        resolution = 0

        if self._last_paint_bgra and self._last_paint_resolution:
            blob = self._last_paint_bgra
            resolution = self._last_paint_resolution
            print(f"[PAINT] saving cached texture ({len(blob)} bytes, grid={grid})")

        if not blob:
            pawn = pawn or self._get_local_pawn()
            if pawn:
                try:
                    blob, resolution = self.export_paint_albedo(pawn)
                except Exception as e:
                    print(f"[PAINT] export failed during save: {e}")
                    blob = None
                if blob:
                    print(f"[PAINT] saving exported paint ({len(blob)} bytes)")

        if not blob:
            return False, (
                "Nothing to save — apply an image, load a preset, or paint first"
            )

        expected = resolution * resolution * 4
        if len(blob) != expected:
            return False, f"Paint data invalid ({len(blob)} bytes, expected {expected})"

        safe = self._sanitize_preset_name(name)
        path = os.path.join(PAINT_PRESETS_DIR, f"{safe}.mechpaint")
        tmp_path = path + ".tmp"
        self.ensure_paint_presets_dir()
        try:
            with open(tmp_path, "wb") as f:
                f.write(PAINT_FILE_MAGIC)
                f.write(struct.pack("<IIII", PAINT_FILE_VERSION, resolution, grid, len(blob)))
                f.write(blob)
            os.replace(tmp_path, path)
            print(f"[PAINT] saved preset '{safe}' ({len(blob)} bytes, {grid}x{grid})")
            return True, path
        except Exception as e:
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            return False, str(e)

    def load_paint_preset(self, name, pawn=None, grid=None, progress_cb=None):
        """Load a .mechpaint preset onto the local character via PaintAtUV."""
        safe = self._sanitize_preset_name(name)
        path = os.path.join(PAINT_PRESETS_DIR, f"{safe}.mechpaint")
        if not os.path.isfile(path):
            return False, f"Preset not found: {safe}"
        try:
            with open(path, "rb") as f:
                magic = f.read(len(PAINT_FILE_MAGIC))
                if magic != PAINT_FILE_MAGIC:
                    return False, "Invalid preset file"
                version = struct.unpack("<I", f.read(4))[0]
                if version == PAINT_FILE_VERSION_V1:
                    resolution, data_len = struct.unpack("<II", f.read(8))
                    file_grid = 32
                elif version == PAINT_FILE_VERSION:
                    resolution, file_grid, data_len = struct.unpack("<III", f.read(12))
                else:
                    return False, f"Unsupported preset version {version}"
                blob = f.read(data_len)
                if len(blob) != data_len:
                    return False, "Preset file truncated"
        except Exception as e:
            return False, str(e)

        expected = resolution * resolution * 4
        if len(blob) != expected:
            return False, (
                f"Preset data size mismatch ({len(blob)} bytes, expected {expected})"
            )

        apply_grid = self.parse_grid_value(grid or file_grid or 32)
        print(f"[PAINT] loading preset '{safe}' res={resolution} grid={apply_grid} "
              f"({len(blob)} bytes from file)")
        ok = self.apply_preset_bgra(
            pawn, blob, resolution, grid=apply_grid, progress_cb=progress_cb
        )
        if ok:
            self._last_paint_grid = apply_grid
            return True, f"Loaded preset '{safe}' ({apply_grid}×{apply_grid} grid)"
        return False, "Failed to apply preset — try again in a match"

    def _get_paint_component(self, pawn):
        """Return RuntimePaintableComponent without noisy logging."""
        comp = rp(self.pm, pawn + 0x0B68)
        if not comp or comp <= 0x100000:
            return 0
        if "RuntimePaintable" not in self.objects.class_name(comp):
            return 0
        return comp

    def _sample_bgra_at_uv(self, bgra_bytes, resolution, u, v, opacity=255):
        px = min(resolution - 1, max(0, int(u * (resolution - 1))))
        py = min(resolution - 1, max(0, int(v * (resolution - 1))))
        off = (py * resolution + px) * 4
        b, g, r = bgra_bytes[off], bgra_bytes[off + 1], bgra_bytes[off + 2]
        if opacity < 255:
            scale = opacity / 255.0
            r = int(r * scale)
            g = int(g * scale)
            b = int(b * scale)
        return r, g, b

    def _image_bgra_to_uv_points(
        self, bgra_bytes, resolution, grid, opacity=255, front_back_only=True,
    ):
        """
        Map a BGRA image to UV paint stamps.

        front_back_only=True paints the full image on the front and back torso
        islands (same picture on each side). False maps across the entire UV atlas.
        """
        grid = self.parse_grid_value(grid)
        if front_back_only:
            return self._build_front_back_image_uv_points(
                bgra_bytes, resolution, grid, opacity,
            )
        points = []
        step = resolution / grid
        regions = ((0.0, 0.0, 1.0, 1.0),)

        for u0, v0, u1, v1 in regions:
            for gy in range(grid):
                for gx in range(grid):
                    px = min(resolution - 1, int((gx + 0.5) * step))
                    py = min(resolution - 1, int((gy + 0.5) * step))
                    off = (py * resolution + px) * 4
                    alpha = bgra_bytes[off + 3]
                    if alpha < self.MIN_PAINT_ALPHA:
                        continue
                    b, g, r = bgra_bytes[off], bgra_bytes[off + 1], bgra_bytes[off + 2]
                    scale = (opacity / 255.0) * (alpha / 255.0)
                    r = int(r * scale)
                    g = int(g * scale)
                    b = int(b * scale)
                    fu = (gx + 0.5) / grid
                    fv = (gy + 0.5) / grid
                    u = u0 + fu * (u1 - u0)
                    v = v0 + fv * (v1 - v0)
                    points.append((u, v, (r, g, b)))
        return points

    def _synthetic_body_uv_bounds_from_screen(self, pawn, cam, screen_w, screen_h):
        """
        Estimate front torso UV bounds from the projected body bbox only.
        No HitTest / screen raycasts — those crash the game from our thread.
        """
        bbox = self.project_body_screen_bbox(pawn, cam, screen_w, screen_h)
        if not bbox:
            return None
        _, y0, _, y1 = bbox
        bh = max(8.0, y1 - y0)
        sh = max(1.0, float(screen_h))
        v_scale = min(1.08, max(0.82, bh / (sh * 0.62)))
        hu = self.PAINT_BODY_HU * v_scale
        hv = self.PAINT_BODY_HV * v_scale
        front_u = self.PAINT_FRONT_U
        vc = self.PAINT_BODY_VC
        return (
            max(0.0, front_u - hu),
            max(0.0, vc - hv),
            min(0.5, front_u + hu),
            min(1.0, vc + hv),
        )

    def _measure_front_body_uv_bounds(self, comp, mesh, pc, rect):
        """Disabled — HitTest from our thread crashes the game."""
        del comp, mesh, pc, rect
        return None

    def _build_front_back_image_uv_hitmapped(
        self, pawn, bgra_bytes, resolution, grid, opacity, screen_w, screen_h,
        bounds=None,
    ):
        """Map image using measured body UV bounds; mirror u+0.5 for the back."""
        grid = self.parse_grid_value(grid)
        if bounds is None:
            world = self._get_world()
            pc = self._get_local_controller(world)
            cam = self.get_camera()
            comp = self._get_runtime_paint_component(pawn)
            mesh, _ = self._get_paint_mesh(pawn, comp)
            if not cam or not pc or not comp or not mesh:
                return self._build_front_back_image_uv_points(
                    bgra_bytes, resolution, grid, opacity,
                )
            if not screen_w or not screen_h:
                screen_w, screen_h = self.get_viewport_size()
            bbox = self.project_body_screen_bbox(pawn, cam, screen_w, screen_h)
            if not bbox:
                return self._build_front_back_image_uv_points(
                    bgra_bytes, resolution, grid, opacity,
                )
            rect = self._body_image_screen_rect(bbox, width_fill=0.62, height_fill=0.96)
            bounds = self._measure_front_body_uv_bounds(comp, mesh, pc, rect)
        if not bounds:
            return self._build_front_back_image_uv_points(
                bgra_bytes, resolution, grid, opacity,
            )
        u0, v0, u1, v1 = bounds
        bu0, bu1 = u0 + 0.5, u1 + 0.5
        points = []
        for gy in range(grid):
            for gx in range(grid):
                rgb = self._sample_bgra_pixel(
                    bgra_bytes, resolution, gx, gy, grid, opacity,
                )
                if not rgb:
                    continue
                fu = (gx + 0.5) / grid
                fv = (gy + 0.5) / grid
                u = u0 + fu * (u1 - u0)
                v = v0 + fv * (v1 - v0)
                points.append((u, v, rgb))
                points.append((bu0 + fu * (bu1 - bu0), v, rgb))
        return points

    def _compose_front_back_texture_measured(
        self, bgra_bytes, img_res, atlas_res, opacity, bounds,
    ):
        """Compose atlas using measured front bounds + u+0.5 mirror for back."""
        u0, v0, u1, v1 = bounds
        bu0, bu1 = u0 + 0.5, u1 + 0.5
        out = bytearray(atlas_res * atlas_res * 4)
        op_scale = opacity / 255.0
        for u_start, u_end in ((u0, u1), (bu0, bu1)):
            x0 = int(u_start * atlas_res)
            x1 = max(x0 + 1, int(u_end * atlas_res))
            y0 = int(v0 * atlas_res)
            y1 = max(y0 + 1, int(v1 * atlas_res))
            rw = x1 - x0
            rh = y1 - y0
            for ry in range(rh):
                for rx in range(rw):
                    ix = min(img_res - 1, int((rx + 0.5) / rw * img_res))
                    iy = min(img_res - 1, int((ry + 0.5) / rh * img_res))
                    src_off = (iy * img_res + ix) * 4
                    alpha = bgra_bytes[src_off + 3]
                    if alpha < self.MIN_PAINT_ALPHA:
                        continue
                    scale = op_scale * (alpha / 255.0)
                    dst_off = ((y0 + ry) * atlas_res + (x0 + rx)) * 4
                    out[dst_off] = int(bgra_bytes[src_off] * scale)
                    out[dst_off + 1] = int(bgra_bytes[src_off + 1] * scale)
                    out[dst_off + 2] = int(bgra_bytes[src_off + 2] * scale)
                    out[dst_off + 3] = int(alpha * op_scale)
        return bytes(out)

    def _compose_front_back_texture_fitted(
        self, bgra_bytes, img_res, atlas_res, opacity, layout, img_aspect=1.0,
    ):
        """Write the image into front and back paint-sphere hemispheres on the atlas."""
        del layout
        border = self.PAINT_UV_BORDER
        out = bytearray(atlas_res * atlas_res * 4)
        op_scale = opacity / 255.0

        for py in range(atlas_res):
            v = (py + 0.5) / atlas_res
            for px in range(atlas_res):
                u = (px + 0.5) / atlas_res
                if u < 0.5 - border * 0.5:
                    fu = (u - border) / max(1e-6, 0.5 - 2.0 * border)
                elif u > 0.5 + border * 0.5:
                    fu = (u - 0.5 - border) / max(1e-6, 0.5 - 2.0 * border)
                else:
                    continue
                fv_img = (v - border) / max(1e-6, 1.0 - 2.0 * border)
                if not (0.0 <= fu <= 1.0 and 0.0 <= fv_img <= 1.0):
                    continue
                ix, iy = self._image_frac_from_paint_frac(fu, fv_img, img_aspect)
                sx = min(img_res - 1, max(0, int(ix * (img_res - 1))))
                sy = min(img_res - 1, max(0, int(iy * (img_res - 1))))
                src_off = (sy * img_res + sx) * 4
                alpha = bgra_bytes[src_off + 3]
                if alpha < self.MIN_PAINT_ALPHA:
                    continue
                scale = op_scale * (alpha / 255.0)
                dst_off = (py * atlas_res + px) * 4
                out[dst_off] = int(bgra_bytes[src_off] * scale)
                out[dst_off + 1] = int(bgra_bytes[src_off + 1] * scale)
                out[dst_off + 2] = int(bgra_bytes[src_off + 2] * scale)
                out[dst_off + 3] = int(alpha * op_scale)
        return bytes(out)

    @staticmethod
    def _centered_square_rect(bx0, by0, bx1, by1, fill=0.90):
        """Return (x0,y0,x1,y1) square centered in bbox, fill= fraction of min side."""
        bc_x = (bx0 + bx1) * 0.5
        bc_y = (by0 + by1) * 0.5
        side = min(max(8.0, bx1 - bx0), max(8.0, by1 - by0)) * fill
        half = side * 0.5
        return bc_x - half, bc_y - half, bc_x + half, bc_y + half

    def _sample_bgra_frac(self, bgra_bytes, resolution, fu, fv, opacity=255):
        """Sample BGRA at normalized texture coordinates (0..1)."""
        px = min(resolution - 1, max(0, int(fu * resolution)))
        py = min(resolution - 1, max(0, int(fv * resolution)))
        off = (py * resolution + px) * 4
        if off + 3 >= len(bgra_bytes):
            return None
        alpha = bgra_bytes[off + 3]
        if alpha < self.MIN_PAINT_ALPHA:
            return None
        scale = (opacity / 255.0) * (alpha / 255.0)
        return (
            int(bgra_bytes[off + 2] * scale),
            int(bgra_bytes[off + 1] * scale),
            int(bgra_bytes[off] * scale),
        )

    @staticmethod
    def _image_v_to_sphere_v(fv_img):
        """Map image row fraction (0=top) to paint-sphere v (1=head, 0=feet)."""
        return max(0.0, min(1.0, 1.0 - fv_img))

    @staticmethod
    def _image_frac_from_paint_frac(fu, fv, img_aspect=1.0):
        """Map paint-cell fraction to image fraction on a letterboxed square canvas."""
        aspect = max(0.05, float(img_aspect))
        if aspect >= 1.0:
            vis = 1.0 / aspect
            off = (1.0 - vis) * 0.5
            return off + fu * vis, fv
        vis = aspect
        off = (1.0 - vis) * 0.5
        return fu, off + fv * vis

    @classmethod
    def _hemisphere_uv_from_image_frac(cls, fu, fv_img, front=True):
        """Map normalized image fractions onto one paint-sphere hemisphere."""
        border = cls.PAINT_UV_BORDER
        if front:
            u0, u1 = border, 0.5 - border
        else:
            u0, u1 = 0.5 + border, 1.0 - border
        v0, v1 = border, 1.0 - border
        fu = max(0.0, min(1.0, float(fu)))
        fv_img = max(0.0, min(1.0, float(fv_img)))
        u = u0 + fu * (u1 - u0)
        v = v0 + fv_img * (v1 - v0)
        return u, v

    @classmethod
    def _fit_image_full_body_layout(cls, img_aspect):
        """Legacy layout tuple for logging / preset compose (full hemispheres)."""
        del img_aspect
        border = cls.PAINT_UV_BORDER
        hu = (0.5 - 2.0 * border) * 0.5
        hv = (1.0 - 2.0 * border) * 0.5
        return cls.PAINT_FRONT_U, cls.PAINT_BACK_U, cls.PAINT_BODY_VC, hu, hv

    @classmethod
    def _fit_image_in_torso_box(cls, img_aspect, v_center=None, max_hu=None, max_hv=None):
        """Legacy torso box fit — prefer _fit_image_full_body_layout for Apply Image."""
        del v_center, max_hu, max_hv
        return cls._fit_image_full_body_layout(img_aspect)

    @classmethod
    def _fit_image_paint_layout(cls, img_aspect, bounds=None):
        """Legacy wrapper — bounds ignored; use fixed front/back torso centers."""
        del bounds
        return cls._fit_image_in_torso_box(img_aspect)

    def _default_torso_max_half_extents(self, pawn, cam, screen_w, screen_h):
        """Scale the max torso paint box from the character height on screen."""
        max_hu = self.PAINT_BODY_HU
        max_hv = self.PAINT_BODY_HV
        if not cam or not screen_w or not screen_h:
            return max_hu, max_hv
        bbox = self.project_body_screen_bbox(pawn, cam, screen_w, screen_h)
        if not bbox:
            return max_hu, max_hv
        _, y0, _, y1 = bbox
        bh = max(8.0, y1 - y0)
        scale = min(1.08, max(0.88, bh / (float(screen_h) * 0.58)))
        return max_hu * scale, max_hv * scale

    def _try_calibrate_torso_uv(self, pawn, screen_w, screen_h):
        """Hit-test torso UV box — disabled during Apply Image (corrupts pawn while frozen)."""
        del pawn, screen_w, screen_h
        return None

    def _resolve_image_paint_layout(
        self, pawn, img_aspect, screen_w=0, screen_h=0, img_w=0, img_h=0,
        torso_cal=None,
    ):
        """Log image layout — stamps use full front/back hemispheres (camo-style)."""
        del pawn, screen_w, screen_h, torso_cal
        if img_w > 0 and img_h > 0:
            img_aspect = img_w / max(1, img_h)
        layout = self._fit_image_full_body_layout(img_aspect)
        border = self.PAINT_UV_BORDER
        print(
            f"[PAINT] layout image={int(img_w)}x{int(img_h)} aspect={img_aspect:.3f} "
            f"mode=full-hemisphere front_u=[{border:.3f},{0.5 - border:.3f}] "
            f"back_u=[{0.5 + border:.3f},{1.0 - border:.3f}] "
            f"v=[{border:.3f},{1.0 - border:.3f}]"
        )
        return layout

    def _sample_bgra_at_paint_frac(
        self, bgra_bytes, resolution, fu, fv, opacity=255, img_aspect=1.0, flip_v=False,
    ):
        if flip_v:
            fv = 1.0 - fv
        ix, iy = self._image_frac_from_paint_frac(fu, fv, img_aspect)
        return self._sample_bgra_frac(bgra_bytes, resolution, ix, iy, opacity)

    def _sample_bgra_pixel(self, bgra_bytes, resolution, gx, gy, grid, opacity=255):
        """Sample BGRA from image grid cell (gx, gy) in a G×G grid."""
        step = resolution / grid
        px = min(resolution - 1, int((gx + 0.5) * step))
        py = min(resolution - 1, int((gy + 0.5) * step))
        off = (py * resolution + px) * 4
        alpha = bgra_bytes[off + 3]
        if alpha < self.MIN_PAINT_ALPHA:
            return None
        scale = (opacity / 255.0) * (alpha / 255.0)
        return (
            int(bgra_bytes[off + 2] * scale),
            int(bgra_bytes[off + 1] * scale),
            int(bgra_bytes[off] * scale),
        )

    @staticmethod
    def _body_image_screen_rect(bbox, width_fill=0.50, height_fill=0.94):
        """Head-to-toe screen rect over the body (narrow width avoids arm/leg corners)."""
        x0, y0, x1, y1 = bbox
        cx = (x0 + x1) * 0.5
        cy = (y0 + y1) * 0.5
        bw = max(8.0, x1 - x0) * width_fill
        bh = max(8.0, y1 - y0) * height_fill
        return cx - bw * 0.5, cy - bh * 0.5, cx + bw * 0.5, cy + bh * 0.5

    @staticmethod
    def _fit_screen_rect_to_image_aspect(rect, img_aspect):
        """Contain-fit an image aspect ratio inside a screen rect, centered."""
        x0, y0, x1, y1 = rect
        rw = max(8.0, x1 - x0)
        rh = max(8.0, y1 - y0)
        cx = (x0 + x1) * 0.5
        cy = (y0 + y1) * 0.5
        aspect = max(0.05, float(img_aspect))
        rect_aspect = rw / rh
        if aspect >= rect_aspect:
            nw = rw
            nh = rw / aspect
        else:
            nh = rh
            nw = rh * aspect
        return cx - nw * 0.5, cy - nh * 0.5, cx + nw * 0.5, cy + nh * 0.5

    @staticmethod
    def _torso_panel_u_distance(u, u_center):
        """Shortest distance on the U wrap to a torso panel center."""
        d = abs(u - u_center)
        return min(d, 1.0 - d)

    def _calibrate_torso_uv_from_screen_rect(self, comp, mesh, pc, rect, u_center=None):
        """
        Hit-test a head-to-toe screen rect and learn torso panel half-extents.
        Filters hits to the front panel so leg/arm UV islands are ignored.
        """
        u_center = self.PAINT_FRONT_U if u_center is None else u_center
        x0, y0, x1, y1 = rect
        cx = (x0 + x1) * 0.5
        cy = (y0 + y1) * 0.5
        probes = (
            (cx, y0), (cx, y1),
            (x0, cy), (x1, cy),
            (cx, cy),
        )
        hits = []
        for sx, sy in probes:
            ok, u, v = self._hit_test_at_screen(comp, mesh, pc, sx, sy)
            if not ok or not math.isfinite(u) or not math.isfinite(v):
                continue
            if self._torso_panel_u_distance(u, u_center) > 0.26:
                continue
            hits.append((u, v))
        if len(hits) < 3:
            print(f"[PAINT] torso UV calibration: only {len(hits)}/5 torso hits")
            return None
        us = [h[0] for h in hits]
        vs = [h[1] for h in hits]
        vc = (min(vs) + max(vs)) * 0.5
        hu = max(0.08, (max(us) - min(us)) * 0.5)
        hv = max(0.12, (max(vs) - min(vs)) * 0.5)
        print(f"[PAINT] torso UV calib vc={vc:.3f} hu={hu:.3f} hv={hv:.3f} "
              f"(hits={len(hits)})")
        return vc, hu, hv

    def _resolve_paint_uv_layout(self, pawn, screen_w, screen_h):
        """Resolve vertical center + panel size for front/back image placement."""
        vc = self.PAINT_BODY_VC
        hu = self.PAINT_BODY_HU
        hv = self.PAINT_BODY_HV

        world = self._get_world()
        pc = self._get_local_controller(world)
        cam = self.get_camera()
        comp = self._get_runtime_paint_component(pawn)
        mesh, mesh_src = self._get_paint_mesh(pawn, comp)
        if not cam or not pc or not comp or not mesh:
            print(f"[PAINT] UV layout defaults (missing context)")
            return vc, hu, hv

        if not screen_w or not screen_h:
            screen_w, screen_h = self.get_viewport_size()

        bbox = self.project_body_screen_bbox(pawn, cam, screen_w, screen_h)
        if not bbox:
            print("[PAINT] UV layout defaults (body off screen)")
            return vc, hu, hv

        rect = self._body_image_screen_rect(bbox)
        cal = self._calibrate_torso_uv_from_screen_rect(comp, mesh, pc, rect)
        if cal:
            vc, hu, hv = cal
        else:
            print(f"[PAINT] UV layout defaults (hit test) mesh={mesh_src}")
        return vc, hu, hv

    def _build_front_back_image_uv_points(
        self, bgra_bytes, resolution, grid, opacity, layout=None, img_aspect=1.0,
        wrap_mode="projector",
    ):
        """
        Map the source image onto the character's UV atlas as a single
        continuous wrap (no duplicate copies on front and back).

        Paint-sphere UV convention:
          v=0 → TOP of body (neck/collar)   v=1 → BOTTOM (feet)
          u∈[0,0.5)  front hemisphere        u∈[0.5,1]  back hemisphere
          u=0.25     chest centre            u=0.75     spine centre

        wrap_mode="projector"  (front→back linear)
          fu_img = u
          Image left edge  → u=0  (side/armpit seam, front edge)
          Chest centre     → fu=0.25
          Spine centre     → fu=0.75
          Image right edge → u=1  (same armpit seam)
          Front shows image columns 0%–50%, back shows 50%–100%.
          The seam is at the body side (least visible location).

        wrap_mode="centered"  (chest-centred outward)
          fu_img = (u + 0.25) % 1.0
          Image centre (50%) lands exactly on the chest (u=0.25).
          The seam (image edges meeting) is at the spine centre (u=0.75).
          Front shows image columns 25%–75% (centre of image on chest),
          back shows 75%–100% + 0%–25% (image edges meet at spine).

        img_aspect=1.0 bypasses letterbox correction — the canvas was already
        stretched to square with Qt.IgnoreAspectRatio so direct (u,v) sampling
        covers the full image.
        """
        del layout
        grid = self.parse_grid_value(grid)
        border = self.PAINT_UV_BORDER
        points = []
        seen = set()

        def add(u, v, rgb):
            if not rgb:
                return
            u = u % 1.0
            v = max(0.0, min(1.0, v))
            key = (round(u * 2048), round(v * 2048))
            if key in seen:
                return
            seen.add(key)
            points.append((u, v, rgb))

        def sample(u, v):
            """Return image colour for UV-atlas position (u, v).

            wrap_mode="projector": fu_img = u  (front→back linear).
            wrap_mode="centered":  fu_img = (u+0.25)%1  (chest-centred).
            img_aspect=1.0 → sample the full stretched canvas without letterboxing.
            """
            if wrap_mode == "centered":
                fu_img = (u + 0.25) % 1.0   # image centre on chest; seam at spine
            else:
                fu_img = u                   # image starts at front-side, ends at back
            fv_img = v   # v=0 → image top (subject head); v=1 → image bottom (feet)
            return self._sample_bgra_at_paint_frac(
                bgra_bytes, resolution, fu_img, fv_img, opacity, 1.0,
            )

        # Main grid — full [0,1]×[0,1] atlas
        for gy in range(grid):
            for gx in range(grid):
                u = (gx + 0.5) / grid
                v = (gy + 0.5) / grid
                add(u, v, sample(u, v))

        # Border stamps — UV edges carry head/arm/shoulder UV islands
        for k in range(grid):
            frac = (k + 0.5) / grid
            for u_b in (border, 1.0 - border):
                add(u_b, frac, sample(u_b, frac))
            for v_b in (border, 1.0 - border):
                add(frac, v_b, sample(frac, v_b))

        print(
            f"[PAINT] wrap points={len(points)} grid={grid} "
            f"mode={wrap_mode} orig-aspect={img_aspect:.2f}"
        )
        return points

    def _calibrate_uv_from_screen_rect(self, comp, mesh, pc, rect):
        """
        Hit-test the corners + center of a screen rect to learn the UV footprint
        of the visible body surface (for mirroring to the opposite side).
        """
        x0, y0, x1, y1 = rect
        probes = (
            (x0, y0), (x1, y0), (x0, y1), (x1, y1),
            ((x0 + x1) * 0.5, (y0 + y1) * 0.5),
        )
        hits = []
        for sx, sy in probes:
            ok, u, v = self._hit_test_at_screen(comp, mesh, pc, sx, sy)
            if ok and math.isfinite(u) and math.isfinite(v):
                hits.append((u, v))
        if len(hits) < 3:
            print(f"[PAINT] UV calibration: only {len(hits)}/5 hit tests succeeded")
            return None
        us = [h[0] for h in hits]
        vs = [h[1] for h in hits]
        uc = (min(us) + max(us)) * 0.5
        vc = (min(vs) + max(vs)) * 0.5
        hu = max(0.04, (max(us) - min(us)) * 0.5)
        hv = max(0.04, (max(vs) - min(vs)) * 0.5)
        print(f"[PAINT] UV calib center=({uc:.3f},{vc:.3f}) half=({hu:.3f},{hv:.3f})")
        return uc, vc, hu, hv

    @staticmethod
    def _map_uv_in_rect(fu, fv, u_center, v_center, half_u, half_v):
        u = u_center - half_u + fu * (2.0 * half_u)
        v = v_center - half_v + fv * (2.0 * half_v)
        u = u % 1.0
        return u, max(0.0, min(1.0, v))

    def _build_screen_image_points(
        self, bgra_bytes, resolution, rect, grid, opacity=255, img_aspect=1.0,
    ):
        """Map image grid to screen positions inside rect (centered on body)."""
        x0, y0, x1, y1 = rect
        points = []
        for gy in range(grid):
            for gx in range(grid):
                fu = (gx + 0.5) / grid
                fv = (gy + 0.5) / grid
                rgb = self._sample_bgra_at_paint_frac(
                    bgra_bytes, resolution, fu, fv, opacity, img_aspect,
                )
                if not rgb:
                    continue
                sx = x0 + fu * (x1 - x0)
                sy = y0 + fv * (y1 - y0)
                points.append((sx, sy, rgb))
        return points

    def _build_body_screen_image_points(
        self, pawn, bgra_bytes, resolution, grid, opacity, img_aspect,
        screen_w, screen_h,
    ):
        """Screen-space stamps on the camera-visible body (aspect-fit, centered)."""
        cam = self.get_camera()
        world = self._get_world()
        pc = self._get_local_controller(world)
        comp = self._get_runtime_paint_component(pawn)
        mesh, _ = self._get_paint_mesh(pawn, comp)
        if not cam or not pc or not comp or not mesh:
            return []
        if not screen_w or not screen_h:
            screen_w, screen_h = self.get_viewport_size()
        bbox = self.project_body_screen_bbox(pawn, cam, screen_w, screen_h)
        if not bbox:
            return []
        body_rect = self._body_image_screen_rect(bbox, width_fill=0.52, height_fill=0.95)
        paint_rect = self._fit_screen_rect_to_image_aspect(body_rect, img_aspect)
        points = self._build_screen_image_points(
            bgra_bytes, resolution, paint_rect, grid, opacity, img_aspect=img_aspect,
        )
        if points:
            x0, y0, x1, y1 = paint_rect
            print(
                f"[PAINT] screen pass points={len(points)} rect="
                f"{int(x1 - x0)}x{int(y1 - y0)} aspect={img_aspect:.3f}"
            )
        return points

    def _build_uv_image_points_for_rect(
        self, bgra_bytes, resolution, grid, opacity, u_center, v_center, half_u, half_v,
        flip_v=False, img_aspect=1.0,
    ):
        """Map image grid into a UV rectangle centered on u_center, v_center."""
        points = []
        for gy in range(grid):
            for gx in range(grid):
                fu = (gx + 0.5) / grid
                fv_img = (gy + 0.5) / grid
                rgb = self._sample_bgra_at_paint_frac(
                    bgra_bytes, resolution, fu, fv_img, opacity, img_aspect, flip_v=False,
                )
                if not rgb:
                    continue
                fv_map = (1.0 - fv_img) if flip_v else fv_img
                u, v = self._map_uv_in_rect(fu, fv_map, u_center, v_center, half_u, half_v)
                points.append((u, v, rgb))
        return points

    def _append_panel_edge_stamps(
        self, points, seen, bgra_bytes, resolution, grid, opacity,
        u_center, v_center, half_u, half_v, img_aspect=1.0,
    ):
        """Extra stamps on panel edges so head/arm UV islands pick up the image border."""

        def add(u, v, rgb):
            if not rgb:
                return
            u = u % 1.0
            v = max(0.0, min(1.0, v))
            key = (round(u * 2048), round(v * 2048))
            if key in seen:
                return
            seen.add(key)
            points.append((u, v, rgb))

        for k in range(grid):
            frac = (k + 0.5) / grid
            rgb_top = self._sample_bgra_at_paint_frac(
                bgra_bytes, resolution, frac, 0.0, opacity, img_aspect,
            )
            rgb_bot = self._sample_bgra_at_paint_frac(
                bgra_bytes, resolution, frac, 1.0, opacity, img_aspect,
            )
            rgb_left = self._sample_bgra_at_paint_frac(
                bgra_bytes, resolution, 0.0, frac, opacity, img_aspect,
            )
            rgb_right = self._sample_bgra_at_paint_frac(
                bgra_bytes, resolution, 1.0, frac, opacity, img_aspect,
            )
            u = u_center - half_u + frac * (2.0 * half_u)
            add(u, v_center + half_v, rgb_top or rgb_left or rgb_right)
            add(u, v_center - half_v, rgb_bot or rgb_left or rgb_right)
            v = v_center - half_v + frac * (2.0 * half_v)
            add(u_center - half_u, v, rgb_left or rgb_top or rgb_bot)
            add(u_center + half_u, v, rgb_right or rgb_top or rgb_bot)

    def _paint_image_centered(self, pawn, bgra_bytes, resolution, grid, opacity, progress_cb):
        """
        Paint the image centered on the character:
          1) Screen-space on the camera-visible side (perfect screen centering)
          2) UV-space on the opposite side (u + 0.5 on the paint sphere/atlas)
        """
        world = self._get_world()
        pc = self._get_local_controller(world)
        cam = self.get_camera()
        comp = self._get_runtime_paint_component(pawn)
        mesh, mesh_src = self._get_paint_mesh(pawn, comp)
        screen_w, screen_h = self.get_viewport_size()

        if not cam or not pc or not comp or not mesh:
            print("[PAINT] missing cam/pc/comp/mesh for screen paint")
            return False

        bbox = self.project_body_screen_bbox(pawn, cam, screen_w, screen_h)
        if not bbox:
            print("[PAINT] body not on screen")
            return False

        rect = self._centered_square_rect(*bbox, fill=0.90)
        screen_worker = self._resolve_paint_screen_worker()
        if not screen_worker:
            print("[PAINT] PaintAtScreenPosition worker unavailable")
            return False

        screen_points = self._build_screen_image_points(
            bgra_bytes, resolution, rect, grid, opacity,
        )
        if not screen_points:
            print("[PAINT] no opaque pixels in image")
            return False

        radius = min(0.5, 1.25 / grid)
        brush_op = max(0.0, min(1.0, opacity / 255.0))
        base_color = self._opaque_image_average(bgra_bytes, resolution)

        def _report(done, total):
            if progress_cb:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass

        total_stamps = len(screen_points)
        opposite_points = []
        u_opp = vc = hu = hv = 0.0

        self._prepare_paint_component(comp)
        cal = self._calibrate_uv_from_screen_rect(comp, mesh, pc, rect)
        if cal:
            uc, vc, hu, hv = cal
            u_opp = (uc + 0.5) % 1.0
        else:
            uc, vc, hu, hv = (
                self.PAINT_FRONT_U, self.PAINT_BODY_VC,
                self.PAINT_BODY_HU, self.PAINT_BODY_HV,
            )
            u_opp = self.PAINT_BACK_U
            print("[PAINT] using fallback opposite UV center")
        opposite_points = self._build_uv_image_points_for_rect(
            bgra_bytes, resolution, grid, opacity, u_opp, vc, hu, hv,
        )
        total_stamps += len(opposite_points)

        self._write_brush_settings(comp, radius, 1.0, brush_op)
        ok_screen = self._call_paint_at_screen(
            comp, mesh, pc, screen_worker, screen_points,
            channel=self.EPaintChannel_Albedo,
        )
        _report(len(screen_points), total_stamps)

        ok_uv = True
        if opposite_points:
            opp_region = (
                max(0.0, u_opp - hu), max(0.0, vc - hv),
                min(1.0, u_opp + hu), min(1.0, vc + hv),
            )
            with self._game_frozen("PAINT"):
                self._prepare_paint_component(comp)
                self._write_brush_settings(comp, radius, 1.0, brush_op)
                ok_uv = self._apply_uv_paint_points(
                    pawn, opposite_points, log_prefix="PAINT", replace=True,
                    brush_grid=grid, brush_opacity=brush_op, brush_hardness=1.0,
                    freeze=False, base_color=base_color,
                    base_regions=(opp_region,),
                )
            _report(total_stamps, total_stamps)

        print(f"[PAINT] screen({mesh_src})={ok_screen} stamps={len(screen_points)} "
              f"opposite_uv={len(opposite_points)} ok={ok_uv}")
        return ok_screen or (bool(opposite_points) and ok_uv)

    def _paint_texture_via_uv(
        self, pawn, bgra_bytes, resolution, grid, opacity=255, progress_cb=None,
        log_prefix="PAINT", base_color=None, front_back_only=True,
    ):
        """Paint a BGRA texture through the UV grid path (used by Apply Image)."""
        grid = self.parse_grid_value(grid)
        if base_color is None:
            base_color = self._opaque_image_average(bgra_bytes, resolution)
        points = self._image_bgra_to_uv_points(
            bgra_bytes, resolution, grid, opacity, front_back_only=front_back_only,
        )
        if not points:
            print(f"[{log_prefix}] no opaque pixels in texture")
            return False
        base_regions = self.PAINT_HEMI_RECTS if front_back_only else None
        return self._apply_uv_paint_points(
            pawn, points, log_prefix=log_prefix, replace=True, brush_grid=grid,
            progress_cb=progress_cb, freeze=True, base_color=base_color,
            base_regions=base_regions,
        )

    def apply_preset_bgra(self, pawn, bgra_bytes, resolution, grid=32, progress_cb=None):
        """
        Restore a .mechpaint preset onto the character.

        Tries direct ImportChannel first (true preset restore), then UV fallback
        using only the bytes read from the preset file — never the Browse image path.
        """
        if not bgra_bytes:
            return False
        expected = resolution * resolution * 4
        if len(bgra_bytes) != expected:
            print(f"[PRESET] data size {len(bgra_bytes)} != {expected}")
            return False
        pawn = pawn or self.wait_for_paintable_pawn()
        if not pawn:
            print("[PRESET] no paintable pawn")
            return False

        comp = self._get_runtime_paint_component(pawn)
        if not comp:
            print("[PRESET] no RuntimePaintableComponent")
            return False

        grid = self.parse_grid_value(grid)
        imported = False
        with self._game_frozen("PRESET"):
            self._prepare_paint_component(comp)
            imported = self._call_import_channel_bytes(
                comp, bgra_bytes, self.EPaintChannel_Albedo
            )
        if imported:
            self._last_paint_bgra = bytes(bgra_bytes)
            self._last_paint_resolution = resolution
            self._last_paint_grid = grid
            print(f"[PRESET] imported {len(bgra_bytes)} bytes via ImportChannel")
            return True

        print("[PRESET] import failed — UV painting from preset file data")
        ok = self._paint_texture_via_uv(
            pawn, bgra_bytes, resolution, grid, opacity=255,
            progress_cb=progress_cb, log_prefix="PRESET", front_back_only=False,
        )
        if ok:
            self._last_paint_bgra = bytes(bgra_bytes)
            self._last_paint_resolution = resolution
            self._last_paint_grid = grid
            print(f"[PRESET] UV applied from preset file ({grid}x{grid})")
        return ok

    def paint_image_bgra(
        self, pawn, bgra_bytes, resolution, opacity=255, grid=32, progress_cb=None,
        screen_w=0, screen_h=0, img_aspect=1.0, img_w=0, img_h=0, fast_paint=False,
        wrap_mode="projector",
    ):
        """
        Apply the same image centered on front (u=0.25) and back (u=0.75) torso panels.

        Image width/height set aspect ratio for contain-fit inside a torso box
        derived from the on-screen body bbox (no HitTest — that corrupts state while frozen).
        """
        if not bgra_bytes:
            return False
        expected = resolution * resolution * 4
        if len(bgra_bytes) != expected:
            print(f"[PAINT] image bytes {len(bgra_bytes)} != {expected}")
            return False
        pawn = pawn or self.wait_for_paintable_pawn()
        if not pawn:
            print("[PAINT] no paintable pawn")
            return False

        comp = self._get_runtime_paint_component(pawn)
        if not comp:
            print("[PAINT] no RuntimePaintableComponent")
            return False

        atlas_res = self.get_albedo_resolution(comp, pawn=pawn)

        # Auto-calculate grid from texture resolution when not specified (grid=0).
        # atlas_res / 4  →  1024 / 4 = 256  →  256×256 = 65 536 stamps total.
        # radius = 1.5/256 = 0.0059 UV = ~6 px on a 1024 texture → high detail.
        if not grid:
            grid = max(64, atlas_res // 4)
        grid = self.parse_grid_value(grid)
        if img_w > 0 and img_h > 0:
            img_aspect = img_w / max(1, img_h)
        img_aspect = max(0.05, float(img_aspect))
        base_color = self._opaque_image_average(bgra_bytes, resolution)

        viewport_w, viewport_h = self.get_viewport_size()
        if (
            screen_w > 100 and screen_h > 100
            and abs(screen_w - viewport_w) <= 8
            and abs(screen_h - viewport_h) <= 8
        ):
            viewport_w, viewport_h = screen_w, screen_h

        layout = self._resolve_image_paint_layout(
            pawn, img_aspect, viewport_w, viewport_h, img_w, img_h, torso_cal=None,
        )
        front_u, back_u, vc, hu, hv = layout
        paint_regions = self.PAINT_HEMI_RECTS

        uv_points = self._build_front_back_image_uv_points(
            bgra_bytes, resolution, grid, opacity,
            layout=layout, img_aspect=img_aspect, wrap_mode=wrap_mode,
        )
        if not uv_points:
            print("[PAINT] no opaque pixels in image")
            return False

        print(
            f"[PAINT] UV stamping {len(uv_points)} points "
            f"({grid}×{grid} per side, batch={self.PAINT_UV_BATCH_SAFE})"
        )

        ok = False
        with self._game_frozen("PAINT"):
            live_comp = rp(self.pm, pawn + 0x0B68)
            if not live_comp or live_comp <= 0x100000:
                print("[PAINT] paint component lost before apply")
                return False
            self._prepare_paint_component(live_comp)

            # ── Step 1: wipe previous paint ───────────────────────────────────
            self._call_clear_paint_channel(live_comp)

            # ── Step 2: white flood with ONE big-brush pass ───────────────────
            # Four stamps placed at the UV quadrant centres (0.25/0.75 × 0.25/0.75)
            # with radius=0.35 each.  At that radius every pair of adjacent stamps
            # overlaps in the middle, so the full [0,1]×[0,1] atlas is covered in
            # a single call — no gaps, no repeated sweeping.
            white = (255, 255, 255)
            white_pts = [
                (u, v, white)
                for u in (0.25, 0.75)
                for v in (0.25, 0.75)
            ]
            self._write_brush_settings(live_comp, 0.35, 1.0, 1.0)   # BIG brush
            self._call_paint_pattern(live_comp, white_pts, channel=4)
            print("[PAINT] white flood done (4 stamps, r=0.35)")

            # ── Step 3: image detail pass — small brush for quality ───────────
            # _apply_uv_paint_points calls _write_brush_settings itself as its
            # first action, setting radius = 1.5/grid (≈0.006 UV for grid=256)
            # and hardness = 0.95.  No need to touch the brush here.
            ok = self._apply_uv_paint_points(
                pawn, uv_points, log_prefix="PAINT", replace=True, brush_grid=grid,
                progress_cb=progress_cb, freeze=False, base_color=base_color,
                base_regions=paint_regions, comp=live_comp, fast_mode=fast_paint,
            )
            if ok:
                print("[PAINT] UV stamp path completed")
            else:
                print("[PAINT] UV stamp path failed")

        if ok and layout:
            composed = self._compose_front_back_texture_fitted(
                bgra_bytes, resolution, atlas_res, opacity, layout, img_aspect,
            )
            self._last_paint_bgra = composed
            self._last_paint_resolution = atlas_res
            self._last_paint_grid = grid
            print(f"[PAINT] front/back apply done ({grid}×{grid} per side)")
        return ok
