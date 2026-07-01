#!/usr/bin/env python3
"""
Core game reading engine for MECCA CHAMELEON (UE5.6) ESP.
Memory primitives, pattern scanning, FName resolution, object array,
offset resolution, and game state reading.
"""
import struct
import math
import os
import re
import ctypes
import threading
import pymem
from contextlib import contextmanager

from meccha_chameleon_tools.camo_bridge import CamoBridgeMixin
from meccha_chameleon_tools.trainer import TrainerMixin

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
class MecchaESP(CamoBridgeMixin, TrainerMixin):
    MAX_ESP_PLAYERS = 24
    PAINT_UV_BATCH_SIZE = 128    # default stamps per remote call (frozen apply)
    PAINT_UV_BATCH_FAST = 256    # larger batches when game is frozen
    PAINT_UV_BATCH_SAFE = 24     # max stamps per shellcode (avoids timeout use-after-free)
    CAMO_MAX_BRUSH_PX = 32.0     # huge brushes corrupt RT / crash on unfreeze flush
    CAMO_MAX_STAMPS = 16384      # paint full grid when possible — subsampling causes gaps
    CAMO_WRAP_MAX_STAMPS = 16384 # wrap uses same dense dot grid as camera-facing
    CAMO_MAX_SCREEN_STAMPS = 4096  # PaintAtScreenPosition raycasts — cap like image paint
    CAMO_DOT_OVERLAP = 0.46      # brush radius ≈ half UV cell → hard dots tile, not streaks
    CAMO_DOT_HARDNESS = 1.0      # crisp circular stamps (no soft smear)
    PAINT_LIVE_BATCH_PAUSE = 0.022  # seconds between live batches (camo unfrozen path)
    MIN_PAINT_ALPHA = 24         # skip transparent PNG pixels (avoid black stamps)
    IMAGE_WRAP_MODE_LABELS = {
        "projector": "Projector (front → back, full-atlas wrap)",
        "centered": "Centered (chest outward, island UV map)",
    }
    # Paint-sphere atlas layout (confirmed via UV diagnostic, Jun 2026):
    #   The atlas is NOT one rectangle per side — head, torso, and legs live in
    #   separate UV islands scattered across u/v space.
    #   Diagnostic quadrant → body part (front view):
    #     GREEN  (u 0.5-1, v 0-0.5)  → head + outer arms
    #     BLUE   (u 0-0.5, v 0.5-1)  → chest / torso (also back spine strip)
    #     YELLOW (u 0.5-1, v 0.5-1)  → legs (front-left), back-right panel
    #     RED    (u 0-0.5, v 0-0.5)  → inner leg / back-head-left
    #   v=0 → head end,  v=1 → feet end  (empirical from diagnostic).
    PAINT_FRONT_HEMI = (0.0, 0.0, 0.5, 1.0)
    PAINT_BACK_HEMI = (0.5, 0.0, 1.0, 1.0)
    PAINT_HEMI_RECTS = (PAINT_FRONT_HEMI, PAINT_BACK_HEMI)
    PAINT_UV_BORDER = 0.01
    PAINT_UV_SEAM = 0.49
    PAINT_FRONT_U = 0.25
    PAINT_BACK_U = 0.75
    PAINT_BODY_VC = 0.50
    PAINT_BODY_HU = 0.22
    PAINT_BODY_HV = 0.38
    # Island rects: (u0, v0, u1, v1, img_y0, img_y1) — calibrated Jun 2026 diagnostic.
    #   GREEN  u∈[0.52,0.98] v∈[0.02,0.48] → front head / back head-R
    #   BLUE   u∈[0.02,0.48] v∈[0.52,0.98] → front chest / back spine
    #   YELLOW u∈[0.52,0.98] v∈[0.52,0.98] → front leg-L / back-right panel
    #   RED    u∈[0.02,0.48] v∈[0.02,0.48] → front leg-R inner / back head-L
    PAINT_FRONT_ISLANDS = (
        (0.52, 0.02, 0.98, 0.48, 0.00, 0.35),   # head + outer arms (green quad)
        (0.02, 0.52, 0.48, 0.98, 0.32, 0.68),   # chest / torso (blue quad)
        (0.52, 0.52, 0.98, 0.98, 0.65, 1.00),   # leg L (yellow quad)
        (0.02, 0.02, 0.48, 0.48, 0.65, 1.00),   # leg R inner (red quad)
    )
    PAINT_BACK_ISLANDS = (
        (0.02, 0.02, 0.48, 0.48, 0.00, 0.35),   # head-left (red quad)
        (0.52, 0.02, 0.98, 0.48, 0.00, 0.35),   # head-right (green quad)
        (0.02, 0.52, 0.48, 0.98, 0.32, 0.68),   # spine (blue quad)
        (0.52, 0.52, 0.98, 0.98, 0.32, 0.68),   # back-right (yellow quad)
        (0.02, 0.42, 0.48, 0.58, 0.65, 1.00),   # leg L lower (u≈0.25 cross)
        (0.52, 0.42, 0.98, 0.58, 0.65, 1.00),   # leg R lower (u≈0.75 cross)
    )

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
    GWORLD_RVA = 0x9C85620

    # APlayerState (Engine 5.6 dump)
    OFF_PLAYER_UNIQUE_ID = 0x02B8          # FUniqueNetIdRepl UniqueID
    OFF_PLAYER_SAVED_NET_ADDR = 0x02F8     # FString SavedNetworkAddress
    OFF_PLAYER_NAME_PRIVATE = 0x0340       # FString PlayerNamePrivate (Steam display name)
    OFF_PC_CACHED_CONNECTION_PLAYER_ID = 0x0700  # APlayerController::CachedConnectionPlayerId
    STEAM64_MIN = 76561197960265728
    STEAM64_MAX = 76561199999999999
    _STEAM64_RE = re.compile(r"7656119\d{10}")

    # ESP session debounce — ignore brief memory-read glitches (overlay ~60 Hz).
    ESP_WORLD_STALE_SEC = 2.0       # reuse last-good UWorld this long on read miss
    ESP_DISCONNECT_SEC = 3.0        # no UWorld this long → treat as left server
    ESP_PA_EMPTY_LIMIT = 90         # ~1.5 s empty PlayerArray before match-end clear
    ESP_SESSION_CHANGE_LIMIT = 30   # ~500 ms new world before cache reset
    ESP_FRESH_MISS_LIMIT = 60       # frames to keep a player missing from a partial read
    ESP_POS_MISS_LIMIT = 30         # frames before dropping a cached player (pos miss)
    ESP_DEAD_STREAK_LIMIT = 4       # consecutive dead reads before hiding a player

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
    FNAMEPOOL_DELTA = 0x11C658   # GObjects − GNames (dump 5.6.1-44394996)

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
        self._paint_at_uv_worker_rva = None    # cached PaintAtUV deep native worker
        self._active_freeze_handles = None     # set while _game_frozen() is active
        self._paint_keep_unfrozen = False      # stay unfrozen after first inject in a session
        self._paint_session_resumed = False    # avoid double-resume in _game_frozen finally
        self._players_cache = []               # ESP player list; brief fallback on empty reads
        self._esp_session_key = None           # UWorld ptr — change clears cache
        self._esp_session_ready = False
        self._esp_last_player_array_count = 0
        self._esp_pa_empty_streak = 0
        self._esp_session_change_streak = 0
        self._esp_pending_session_key = None
        self._esp_world_last_seen = 0.0
        self._last_camera = None
        self._world_last_good = 0
        self._world_last_good_time = 0.0
        self._esp_dead_streak = {}             # pawn -> consecutive dead reads
        self._discord_session_notified = False
        self._steam_id_cache = {}
        self._blocked_steam_ids = set()
        self._ps_pc_cache = {}
        self._ps_pc_cache_ts = 0.0
        self._steam_features_active = False
        self._steam_lazy_ts = 0.0
        self._steam_lazy_ps_queue = []
        self._actor_scan_frame = 0
        self._ue_func_cache = {}
        self._session_players_cache = []
        self._session_players_cache_ts = 0.0
        self._blocklist_cache_ids = set()
        self._blocklist_file_mtime = -1.0
        self._blocklist_cache_ts = 0.0
        try:
            self.refresh_blocklist_cache(force=True)
        except Exception:
            pass
        self._export_worker_rva = None
        self._import_worker_rva = None
        self._clear_worker_rva = None
        self._channel_io_resolved = False
        self._cached_module_base = 0
        self._inject_handle_cache = 0
        self._last_paint_bgra = None
        self._last_paint_resolution = 0
        self._last_paint_grid = 32
        self._bridge_proc = None
        self._bridge_ensure_lock = threading.Lock()
        self._bridge_preload_started = False
        self._bridge_preload_ok = None
        self._bridge_preload_thread = None
        self._camo_abort = False
        self._init_trainer_state()

    def _scan_guobject_array(self):
        scanner = PatternScanner(self.pm, self.MODULE_NAME)
        self._cached_module_base = scanner.base
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

    @classmethod
    def image_wrap_mode_label(cls, wrap_mode):
        """Human-readable Apply Image wrap mode for console output."""
        key = (wrap_mode or "projector").strip().lower()
        return cls.IMAGE_WRAP_MODE_LABELS.get(key, f"Unknown ({key})")

    def _get_world(self):
        import time as _time

        world = 0
        viewport = rp(self.pm, self.gengine + self.offsets["UEngine::GameViewport"])
        if viewport:
            world = rp(self.pm, viewport + self.offsets["UGameViewportClient::World"]) or 0

        if not world or world < 0x100000:
            try:
                base = self._cached_module_base
                if base:
                    world = rp(self.pm, base + self.GWORLD_RVA) or 0
            except Exception:
                world = 0

        if world and world > 0x100000:
            self._world_last_good = world
            self._world_last_good_time = _time.monotonic()
            return world

        last = self._world_last_good
        if last and (_time.monotonic() - self._world_last_good_time) < self.ESP_WORLD_STALE_SEC:
            return last
        return 0

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
                self._last_camera = cam
                return cam

        last_cam = getattr(self, "_last_camera", None)
        if last_cam:
            return last_cam

        # Last resort: controller rotation + acknowledged pawn/root position.
        cr_off = self.offsets.get("AController::ControlRotation", 0x320)
        rot = rvec3(self.pm, pc + cr_off)
        if not all(math.isfinite(v) for v in rot):
            return None
        pawn = rp(self.pm, pc + self.offsets["APlayerController::AcknowledgedPawn"])
        loc = self.get_actor_root_pos(pawn) if pawn else None
        if loc and self._is_valid_world_loc(loc):
            cam = {"loc": loc, "rot": rot, "fov": 90.0}
            self._last_camera = cam
            return cam
        return last_cam if last_cam else None

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

    def set_actor_root_yaw(self, actor, yaw_deg):
        """Set root component yaw (degrees) while keeping pitch/roll."""
        root = rp(self.pm, actor + self.offsets["AActor::RootComponent"])
        if not root:
            return False
        rot = rvec3(self.pm, root + 0x158)
        return wvec3(self.pm, root + 0x158, (rot[0], float(yaw_deg), rot[2]))

    def get_control_rotation(self):
        """Read player controller ControlRotation (pitch, yaw, roll degrees)."""
        world = self._get_world()
        if not world:
            return None
        pc = self._get_local_controller(world)
        if not pc:
            return None
        addr = pc + self.offsets.get("AController::ControlRotation", 0x320)
        rot = rvec3(self.pm, addr)
        if not all(math.isfinite(v) for v in rot):
            return None
        return rot

    def set_control_rotation(self, pitch_deg, yaw_deg, roll_deg):
        """Set full controller ControlRotation (pitch, yaw, roll degrees)."""
        world = self._get_world()
        if not world:
            return False
        pc = self._get_local_controller(world)
        if not pc:
            return False
        addr = pc + self.offsets.get("AController::ControlRotation", 0x320)
        return wvec3(self.pm, addr, (float(pitch_deg), float(yaw_deg), float(roll_deg)))

    def set_control_yaw(self, yaw_deg):
        """Set controller yaw while keeping pitch/roll."""
        world = self._get_world()
        if not world:
            return False
        pc = self._get_local_controller(world)
        if not pc:
            return False
        addr = pc + self.offsets.get("AController::ControlRotation", 0x320)
        rot = rvec3(self.pm, addr)
        return wvec3(self.pm, addr, (rot[0], float(yaw_deg), rot[2]))

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

    def get_custom_player_name(self, ps: int) -> str:
        """In-game display name (CustomPlayerName) — not Steam / PlayerNamePrivate."""
        if not ps:
            return ""
        off = self._custom_player_name_offset(ps)
        if off:
            return self._read_fstring(ps + off)
        for try_off in (0x0388, 0x03F0):
            name = self._read_fstring(ps + try_off)
            if name:
                return name
        return ""

    def _custom_player_name_offset(self, ps: int) -> int:
        """FString offset for CustomPlayerName on this PlayerState class, or 0."""
        if not ps:
            return 0
        cls = self.objects.class_name(ps) or ""
        if "PlayerState_LINK" in cls:
            return 0x03F0
        if "PlayerState_Online" in cls:
            return 0x0388
        return 0x0388  # default online layout

    def get_player_name(self, ps: int) -> str:
        """Display name for ESP — CustomPlayerName first, then PlayerNamePrivate (Steam)."""
        if not ps:
            return ""
        custom = self.get_custom_player_name(ps)
        if custom:
            return custom
        return self._read_fstring(ps + self.OFF_PLAYER_NAME_PRIVATE)

    def _read_is_hunter(self, pawn: int):
        """Return True=Hunter, False=Survivor, None=unreadable. IsHunter @ pawn+0x0C3A."""
        try:
            raw = self.pm.read_bytes(pawn + 0x0C3A, 1)
            return bool(raw[0])
        except Exception:
            return None

    @staticmethod
    def _is_player_enemy(local_is_hunter, target_is_hunter):
        """True when target is on the opposing Hunter/Survivor team."""
        if local_is_hunter is True and target_is_hunter is False:
            return True
        if local_is_hunter is False and target_is_hunter is True:
            return True
        return False

    def _is_visible(self, actor):
        """Approximate visibility — component hidden flag + actor bHidden."""
        if not actor:
            return True
        try:
            root = rp(self.pm, actor + self.offsets.get("AActor::RootComponent", 0x1A0))
            if root:
                vis = ru32(self.pm, root + 0x258)
                if vis == 0:
                    return False
        except Exception:
            pass
        try:
            hidden_off = self.offsets.get("AActor::bHidden", 0x178)
            vis = ru32(self.pm, actor + hidden_off)
            if vis == 1:
                return False
        except Exception:
            pass
        return True

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

    def clear_players_cache(self, reason=""):
        if self._players_cache:
            msg = f"[ESP] player cache cleared"
            if reason:
                msg += f" ({reason})"
            print(msg, flush=True)
        self._players_cache = []
        self._esp_dead_streak.clear()
        self._steam_id_cache.clear()
        self._ps_pc_cache.clear()
        self._ps_pc_cache_ts = 0.0

    def set_blocked_steam_ids(self, ids):
        """Cache blocklist Steam IDs for ESP highlighting."""
        self._blocked_steam_ids = set(ids or [])

    def is_blocked_steam_id(self, steam_id: str) -> bool:
        sid = (steam_id or "").strip()
        return bool(sid and sid in getattr(self, "_blocked_steam_ids", set()))

    def _get_player_controller(self, ps: int, pawn: int = 0) -> int:
        """Resolve APlayerController* via pawn Controller or cached PS→PC map."""
        if not pawn and ps:
            pawn = rp(self.pm, ps + self.offsets["APlayerState::PawnPrivate"])
        if pawn:
            pc = rp(self.pm, pawn + self.offsets.get("APawn::Controller", 0x2D8))
            if pc:
                return pc
        if ps:
            self._refresh_ps_pc_cache()
            return self._ps_pc_cache.get(ps, 0)
        return 0

    def _refresh_ps_pc_cache(self):
        """Map PlayerState → PlayerController (lobby / no-pawn cases). Cached ~5s."""
        import time as _time

        now = _time.monotonic()
        if self._ps_pc_cache and (now - self._ps_pc_cache_ts) < 5.0:
            return
        mapping = {}
        ps_off = self.offsets.get("AController::PlayerState", 0x2B0)
        try:
            for obj in self.objects.iter_objects():
                if len(mapping) >= 32:
                    break
                cls_name = self.objects.class_name(obj) or ""
                if "PlayerController" not in cls_name:
                    continue
                ps = rp(self.pm, obj + ps_off)
                if ps and ps not in mapping:
                    mapping[ps] = obj
        except Exception:
            pass
        self._ps_pc_cache = mapping
        self._ps_pc_cache_ts = now

    def invalidate_session_players_cache(self):
        self._session_players_cache_ts = 0.0

    def refresh_blocklist_cache(self, force=False):
        """Load blocklist from disk at most every few seconds (avoid per-frame disk I/O)."""
        import os
        import time as _time
        from meccha_chameleon_tools.blocklist import load_blocklist, blocklist_ids, BLOCKLIST_FILE

        now = _time.monotonic()
        if not force and (now - self._blocklist_cache_ts) < 5.0:
            return self._blocklist_cache_ids
        try:
            mtime = os.path.getmtime(BLOCKLIST_FILE) if os.path.isfile(BLOCKLIST_FILE) else 0.0
        except Exception:
            mtime = 0.0
        if not force and mtime == self._blocklist_file_mtime:
            self._blocklist_cache_ts = now
            return self._blocklist_cache_ids
        entries = load_blocklist()
        ids = blocklist_ids(entries)
        self._blocklist_cache_ids = ids
        self._blocklist_file_mtime = mtime
        self._blocklist_cache_ts = now
        self.set_blocked_steam_ids(ids)
        return ids

    def get_session_players(self, force=False, resolve_steam=False):
        """All PlayerState entries in the current session (lobby + match). Cached ~2s."""
        import time as _time

        now = _time.monotonic()
        cache_ttl = 2.0 if resolve_steam else 2.5
        if not force and self._session_players_cache and (now - self._session_players_cache_ts) < cache_ttl:
            return list(self._session_players_cache)

        world = self._get_world()
        if not world:
            self._session_players_cache = []
            self._session_players_cache_ts = now
            return []
        gamestate = rp(self.pm, world + self.offsets["UWorld::GameState"])
        if not gamestate:
            self._session_players_cache = []
            self._session_players_cache_ts = now
            return []
        pc = self._get_local_controller(world)
        local_ps = rp(self.pm, pc + self.offsets["AController::PlayerState"]) if pc else 0
        pa_data, pa_count, _ = read_array(
            self.pm, gamestate + self.offsets["AGameStateBase::PlayerArray"]
        )
        if not pa_data or pa_count <= 0:
            self._session_players_cache = []
            self._session_players_cache_ts = now
            return []
        want_steam = resolve_steam or (self._steam_features_active and force)
        players = []
        for i in range(min(pa_count, self.MAX_ESP_PLAYERS)):
            ps = rp(self.pm, pa_data + i * 8)
            if not ps:
                continue
            pawn = rp(self.pm, ps + self.offsets["APlayerState::PawnPrivate"])
            ih = self._read_is_hunter(pawn) if pawn else None
            if want_steam:
                steam_id = self.get_player_steam_id(ps)
            else:
                steam_id = self.peek_steam_id(ps)
            players.append({
                "idx": i,
                "player_state": ps,
                "pawn": pawn,
                "is_local": ps == local_ps,
                "player_name": self.get_player_name(ps),
                "steam_name": self.get_player_steam_name(ps),
                "steam_id": steam_id,
                "is_hunter": ih,
            })
        self._session_players_cache = players
        self._session_players_cache_ts = now
        return list(players)

    @classmethod
    def _extract_steam64(cls, text):
        if not text:
            return ""
        match = cls._STEAM64_RE.search(str(text))
        return match.group(0) if match else ""

    def _read_unique_net_id_bytes(self, repl_base: int) -> bytes:
        """Read FUniqueNetIdRepl.ReplicationBytes at repl_base."""
        if not repl_base:
            return b""
        data_ptr = rp(self.pm, repl_base + 0x20)
        count = ru32(self.pm, repl_base + 0x28)
        if not data_ptr or count <= 0 or count > 512:
            return b""
        try:
            return self.pm.read_bytes(data_ptr, count)
        except Exception:
            return b""

    def _steam_id_from_net_id_repl(self, repl_base: int) -> str:
        raw = self._read_unique_net_id_bytes(repl_base)
        found = self._parse_steam64_from_bytes(raw)
        if found:
            return found
        return self._parse_steam64_from_bytes(self._read_net_id_repl_blob(repl_base))

    def _read_net_id_repl_blob(self, repl_base: int) -> bytes:
        """Read the full 0x30-byte FUniqueNetIdRepl struct."""
        if not repl_base:
            return b""
        try:
            return self.pm.read_bytes(repl_base, 0x30)
        except Exception:
            return b""

    def _scan_steam64_near_player_state(self, ps: int) -> str:
        """Scan UniqueID + SavedNetworkAddress region for an embedded Steam64."""
        if not ps:
            return ""
        try:
            blob = self.pm.read_bytes(ps + self.OFF_PLAYER_UNIQUE_ID, 0x60)
        except Exception:
            return ""
        return self._parse_steam64_from_bytes(blob)

    def _ue_steam_id_for_player_state(self, ps: int) -> str:
        """Last resort: ask the game to decode FUniqueNetIdRepl (Redpoint EOS)."""
        if not ps:
            return ""
        caller = self._find_class_default_object("OnlineHelpers")
        if not caller:
            return ""
        fn_get = self._find_ue_function("OnlineHelpers", "GetPlayerStateUniqueNetId")
        fn_conv = self._find_ue_function("OnlineHelpers", "Conv_FUniqueNetIdReplToString")
        if not fn_get or not fn_conv:
            return ""

        get_params = struct.pack("<Q", int(ps)) + (b"\x00" * 0x30)
        get_out = self._process_event_call_out(caller, fn_get, get_params)
        if not get_out:
            return ""
        net_id = get_out[8:8 + 0x30]
        if not any(net_id):
            return ""

        conv_params = net_id + (b"\x00" * 0x10)
        conv_out = self._process_event_call_out(caller, fn_conv, conv_params)
        if not conv_out:
            return ""
        try:
            data_ptr = struct.unpack("<Q", conv_out[0x30:0x38])[0]
            arr_num = struct.unpack("<I", conv_out[0x38:0x3C])[0]
            if not data_ptr or arr_num <= 0 or arr_num > 256:
                return ""
            raw = self.pm.read_bytes(data_ptr, arr_num * 2)
            text = raw.decode("utf-16-le", errors="ignore").rstrip("\x00")
        except Exception:
            return ""
        return self._extract_steam64(text)

    def _parse_steam64_from_bytes(self, raw: bytes) -> str:
        if not raw:
            return ""
        for enc in ("utf-8", "utf-16-le", "latin-1"):
            try:
                text = raw.decode(enc, errors="ignore")
                found = self._extract_steam64(text)
                if found:
                    return found
                for part in re.split(r"[:|\\|]", text):
                    part = part.strip()
                    if self._STEAM64_RE.fullmatch(part):
                        return part
            except Exception:
                continue
        for size in (8, 4):
            if len(raw) < size:
                continue
            for off in range(max(0, len(raw) - size + 1)):
                chunk = raw[off:off + size]
                val = int.from_bytes(chunk, "little", signed=False)
                if self.STEAM64_MIN <= val <= self.STEAM64_MAX:
                    return str(val)
        return ""

    def _read_player_steam_id(self, ps: int, use_ue_fallback=False) -> str:
        """Return Steam64 id string from PlayerState / controller, or '' if unavailable."""
        if not ps:
            return ""

        found = self._steam_id_from_net_id_repl(ps + self.OFF_PLAYER_UNIQUE_ID)
        if found:
            return found

        found = self._scan_steam64_near_player_state(ps)
        if found:
            return found

        saved = self._read_fstring(ps + self.OFF_PLAYER_SAVED_NET_ADDR)
        found = self._extract_steam64(saved)
        if found:
            return found

        pc = self._get_player_controller(ps)
        if pc:
            found = self._steam_id_from_net_id_repl(
                pc + self.OFF_PC_CACHED_CONNECTION_PLAYER_ID,
            )
            if found:
                return found

        if use_ue_fallback:
            found = self._ue_steam_id_for_player_state(ps)
            if found:
                return found

        return ""

    def set_steam_features_active(self, active: bool):
        """When False, ESP hot path never reads Steam IDs from memory."""
        self._steam_features_active = bool(active)

    def peek_steam_id(self, ps: int) -> str:
        """Return cached Steam64 only — no memory reads (safe on overlay hot path)."""
        if not ps:
            return ""
        cached = self._steam_id_cache.get(ps)
        if cached is None:
            return ""
        if isinstance(cached, tuple):
            return cached[0] or ""
        return cached or ""

    def refresh_steam_ids_lazy(self, player_states=None, max_resolve=3):
        """Background Steam ID resolution — at most a few lookups per second."""
        import time as _time

        if not self._steam_features_active:
            return
        now = _time.monotonic()
        if now - self._steam_lazy_ts < 1.0:
            return
        self._steam_lazy_ts = now

        ps_list = []
        seen = set()
        for p in self._players_cache:
            ps = p.get("player_state", 0)
            if ps and ps not in seen and not self.peek_steam_id(ps):
                ps_list.append(ps)
                seen.add(ps)
        for p in getattr(self, "_session_players_cache", []):
            ps = p.get("player_state", 0)
            if ps and ps not in seen and not self.peek_steam_id(ps):
                ps_list.append(ps)
                seen.add(ps)

        resolved = 0
        for ps in ps_list:
            if resolved >= max_resolve:
                break
            if self.peek_steam_id(ps):
                continue
            self.get_player_steam_id(ps)
            resolved += 1

    def _esp_steam_id(self, ps: int) -> str:
        """Cached Steam ID for ESP — never triggers memory reads on the overlay path."""
        return self.peek_steam_id(ps)

    def get_player_steam_id(self, ps: int, force: bool = False) -> str:
        """Cached Steam64 lookup for a PlayerState (retries while ID is replicating)."""
        import time as _time

        if not ps:
            return ""
        now = _time.monotonic()
        cached = self._steam_id_cache.get(ps)
        if cached is not None and not force:
            if isinstance(cached, tuple):
                steam_id, cached_at = cached
            else:
                steam_id, cached_at = cached, 0.0
            if steam_id:
                return steam_id
            if now - cached_at < 3.0:
                return ""

        steam_id = self._read_player_steam_id(ps, use_ue_fallback=force)
        self._steam_id_cache[ps] = (steam_id, now)
        return steam_id

    def get_player_steam_name(self, ps: int) -> str:
        """Steam/platform name from PlayerNamePrivate (not in-game display name)."""
        if not ps:
            return ""
        return self._read_fstring(ps + self.OFF_PLAYER_NAME_PRIVATE)

    def _maybe_notify_discord_session(self):
        """Post one Discord webhook when local player is identified in a match."""
        if self._discord_session_notified:
            return
        config = getattr(self, "_webhook_config", None)
        if config is None:
            return

        world = self._get_world()
        if not world:
            return
        pc = self._get_local_controller(world)
        if not pc:
            return
        ps = rp(self.pm, pc + self.offsets.get("AController::PlayerState", 0x2B0))
        pawn = rp(self.pm, pc + self.offsets["APlayerController::AcknowledgedPawn"])
        if not ps and not pawn:
            return

        display = self.get_custom_player_name(ps) if ps else ""
        steam_name = self.get_player_steam_name(ps) if ps else ""
        steam_id = self.get_player_steam_id(ps) if ps else ""
        if not display and not steam_name and not steam_id and not pawn:
            return

        ih = self._read_is_hunter(pawn) if pawn else None
        if ih is True:
            team = "Hunter"
        elif ih is False:
            team = "Survivor"
        else:
            team = "Unknown"

        from meccha_chameleon_tools.webhook import notify_peterhack_in_match

        notify_peterhack_in_match(
            config,
            display_name=display or steam_name or "?",
            steam_name=steam_name or "?",
            steam_id=steam_id or "?",
            team=team,
            game_pid=getattr(self.pm, "process_id", 0),
        )
        self._discord_session_notified = True

    def _is_pawn_alive(self, pawn, player_state=0):
        """False for confirmed dead pawns. Dead flag only on FirstPersonCharacter."""
        if not pawn:
            return False
        cls_name = self.objects.class_name(pawn) or ""
        if "BP_FirstPersonCharacter" in cls_name:
            try:
                return self.pm.read_bytes(pawn + self.PAWN_OFF_DEAD, 1)[0] == 0
            except Exception:
                return True
        try:
            hp, _sh = self.get_health(pawn, player_state)
            if hp is not None and hp <= 0.0:
                return False
        except Exception:
            pass
        return True

    def _pawn_alive_for_esp(self, pawn, player_state=0):
        """Debounced alive check — ignores one-frame dead/health glitches."""
        if not pawn:
            return False
        if self._is_pawn_alive(pawn, player_state):
            self._esp_dead_streak.pop(pawn, None)
            return True
        streak = self._esp_dead_streak.get(pawn, 0) + 1
        self._esp_dead_streak[pawn] = streak
        return streak < self.ESP_DEAD_STREAK_LIMIT

    def _esp_session_snapshot(self):
        """Return (world_ptr, gamestate_ptr, player_array_count) for cache invalidation."""
        world = self._get_world()
        if not world:
            return 0, 0, 0
        gs = rp(self.pm, world + self.offsets["UWorld::GameState"]) or 0
        pa_count = 0
        if gs:
            _data, pa_count, _ = read_array(
                self.pm, gs + self.offsets["AGameStateBase::PlayerArray"],
            )
        return world, gs, pa_count or 0

    def _tick_esp_session(self):
        """
        Clear ESP cache when leaving a server or when the match ends.

        Transient read failures (empty PlayerArray, null UWorld for a frame)
        are debounced so ESP does not flicker off mid-match.
        """
        import time as _time

        world, gs, pa_count = self._esp_session_snapshot()
        now = _time.monotonic()

        if world:
            self._esp_world_last_seen = now
        elif not self._esp_world_last_seen:
            self._esp_world_last_seen = now

        if now - self._esp_world_last_seen >= self.ESP_DISCONNECT_SEC:
            if self._players_cache:
                self.clear_players_cache("left world / disconnected")
            self._discord_session_notified = False
            self._esp_session_key = None
            self._esp_session_ready = False
            self._esp_last_player_array_count = 0
            self._esp_pa_empty_streak = 0
            self._esp_session_change_streak = 0
            self._esp_pending_session_key = None
            return False

        session_world = world or self._world_last_good or 0
        if (
            session_world
            and self._esp_session_ready
            and self._esp_session_key
            and session_world != self._esp_session_key
        ):
            if self._esp_pending_session_key != session_world:
                self._esp_pending_session_key = session_world
                self._esp_session_change_streak = 1
            else:
                self._esp_session_change_streak += 1
            if self._esp_session_change_streak >= self.ESP_SESSION_CHANGE_LIMIT:
                self.clear_players_cache("server or map changed")
                self._esp_session_change_streak = 0
                self._esp_pending_session_key = None
        else:
            self._esp_session_change_streak = 0
            self._esp_pending_session_key = None

        prev_pa = self._esp_last_player_array_count
        if self._esp_session_ready and world and prev_pa >= 2 and pa_count == 0:
            self._esp_pa_empty_streak += 1
            if self._esp_pa_empty_streak >= self.ESP_PA_EMPTY_LIMIT:
                self.clear_players_cache("match ended (PlayerArray empty)")
                self._esp_pa_empty_streak = 0
        else:
            self._esp_pa_empty_streak = 0

        if session_world:
            self._esp_session_key = session_world
            self._esp_session_ready = True
        if world:
            self._esp_last_player_array_count = pa_count
        return True

    def _merge_players_cache(self, fresh, cap):
        """Merge a partial fresh read with cache — never drop players on one bad frame."""
        fresh_actors = set()
        merged = []
        for p in fresh:
            actor = p.get("actor")
            if not actor:
                continue
            fresh_actors.add(actor)
            entry = dict(p)
            entry["_fresh_miss"] = 0
            entry["_pos_miss"] = 0
            if not entry.get("steam_id"):
                sid = self.peek_steam_id(entry.get("player_state", 0))
                if sid:
                    entry["steam_id"] = sid
            merged.append(entry)

        for p in self._players_cache:
            if len(merged) >= cap:
                break
            actor = p.get("actor")
            if not actor or actor in fresh_actors:
                continue
            ps = p.get("player_state", 0)
            if not self._pawn_alive_for_esp(actor, ps):
                continue
            miss = int(p.get("_fresh_miss", 0)) + 1
            if miss >= self.ESP_FRESH_MISS_LIMIT:
                continue
            entry = dict(p)
            entry["_fresh_miss"] = miss
            merged.append(entry)

        return merged[:cap]

    def _refresh_cached_players(self, cap):
        """Keep last-known players alive through brief empty reads / pos glitches."""
        updated = []
        for p in self._players_cache[:cap]:
            actor = p.get("actor")
            if not actor:
                continue
            ps = p.get("player_state", 0)
            if not self._pawn_alive_for_esp(actor, ps):
                continue
            pos = self.get_actor_root_pos(actor)
            entry = dict(p)
            if pos:
                entry["pos"] = pos
                entry["_pos_miss"] = 0
            else:
                miss = int(p.get("_pos_miss", 0)) + 1
                if miss >= self.ESP_POS_MISS_LIMIT:
                    continue
                entry["_pos_miss"] = miss
            updated.append(entry)
        self._players_cache = updated
        return self._players_cache[:cap]

    def get_players(self, include_local=False, team_filter=False, enemy_only=False):
        """Return up to MAX_ESP_PLAYERS entries.

        Uses a sticky cache when reads fail or return fewer players than before.
        Dead pawns are excluded. Cache clears on disconnect, server change, or match end.
        """
        if not self._tick_esp_session():
            return self._players_cache[: self.MAX_ESP_PLAYERS]

        self._maybe_notify_discord_session()

        cap = self.MAX_ESP_PLAYERS
        try:
            fresh = list(self.iter_players(
                include_local=include_local,
                team_filter=team_filter,
                enemy_only=enemy_only,
            ))
        except Exception:
            fresh = []

        if fresh:
            self._players_cache = self._merge_players_cache(fresh, cap)
        elif self._players_cache:
            return self._refresh_cached_players(cap)

        return self._players_cache[:cap]

    def iter_players(self, include_local=False, team_filter=False, enemy_only=False):
        world = self._get_world()
        if not world:
            return
        gamestate = rp(self.pm, world + self.offsets["UWorld::GameState"])
        pc = self._get_local_controller(world)
        local_pawn = rp(self.pm, pc + self.offsets["APlayerController::AcknowledgedPawn"]) if pc else 0
        local_ps   = rp(self.pm, pc + self.offsets["AController::PlayerState"]) if pc else 0
        local_is_hunter = self._read_is_hunter(local_pawn) if local_pawn else None
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
                    "steam_id": self._esp_steam_id(local_ps),
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
                    if not self._pawn_alive_for_esp(pawn, ps):
                        continue
                    target_is_hunter = self._read_is_hunter(pawn)
                    if enemy_only and not self._is_player_enemy(local_is_hunter, target_is_hunter):
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
                        "steam_id": self._esp_steam_id(ps),
                    }

        if total >= self.MAX_ESP_PLAYERS:
            return

        # Supplemental actor scan — throttled fallback (was every frame, caused lag).
        self._actor_scan_frame = getattr(self, "_actor_scan_frame", 0) + 1
        if total > 0 and self._actor_scan_frame % 30 != 0:
            return
        if total == 0 and self._actor_scan_frame % 10 != 0:
            return

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
                cap = min(actors_count, 512)
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
                    if not self._pawn_alive_for_esp(actor, 0):
                        continue
                    target_is_hunter = self._read_is_hunter(actor)
                    if enemy_only and not self._is_player_enemy(local_is_hunter, target_is_hunter):
                        continue
                    pos = self.get_actor_root_pos(actor)
                    if not pos:
                        continue
                    seen_pawns.add(actor)
                    total += 1
                    ps_actor = rp(
                        self.pm,
                        actor + self.offsets.get("APawn::PlayerState", 0x2C0),
                    )
                    yield {
                        "is_local": False,
                        "pos": pos,
                        "idx": i,
                        "actor": actor,
                        "player_state": ps_actor or 0,
                        "is_hunter": self._read_is_hunter(actor),
                        "player_name": "",   # actor-scan fallback: no PlayerState
                        "steam_id": self._esp_steam_id(ps_actor),
                    }


    # Camouflage — offsets from Dumper-7 dump
    # C:\dumper-7\5.6.1-44394996+++UE5+Release-5.6-Chameleon (2026-07-01 game update)
    #
    # Globals (OffsetsInfo.json / Basic.hpp):
    #   GObjects 0x09F3C6D0  GNames 0x09E20078  GWorld 0x09C85620  ProcessEvent 0x015D0AD0
    #   FNAMEPOOL_DELTA 0x11C658  AppendString 0x013B3110  ProcessEventIdx 0x4C
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

    PAWN_OFF_IS_PAINT_MODE = 0x0B79
    PAWN_OFF_IS_BRUSHING = 0x0BF8

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

    # Mesh / actor render flags — hide local body during camo screen capture (client only).
    PAWN_OFF_DEAD = 0x05AA                    # ABP_FirstPersonCharacter_Main_C::Dead
    PAWN_OFF_SPHERE_MESH = 0x0B60
    PAWN_OFF_HIDE_BLOCK = 0x0C60          # local-only bool — not replicated
    PAWN_OFF_ACTOR_FLAGS = 0x0058         # AActor::bHidden is bit 7
    ACTOR_HIDDEN_BIT = 7
    SCENE_COMP_VISIBLE_OFF = 0x01A0        # USceneComponent — bVisible is bit 5
    SCENE_VISIBLE_BIT = 5
    SCENE_COMP_RENDER_FLAGS_OFF = 0x01A1   # USceneComponent — bHiddenInGame is bit 3
    SCENE_HIDDEN_IN_GAME_BIT = 3

    def _read_scene_flag(self, mesh, byte_off, bit):
        b = self.pm.read_bytes(mesh + byte_off, 1)[0]
        return bool(b & (1 << bit))

    def _write_scene_flag(self, mesh, byte_off, bit, on):
        addr = mesh + byte_off
        b = self.pm.read_bytes(addr, 1)[0]
        if on:
            b |= 1 << bit
        else:
            b &= ~(1 << bit)
        self.pm.write_bytes(addr, bytes([b]), 1)

    def _read_mesh_hidden_in_game(self, mesh):
        return self._read_scene_flag(
            mesh, self.SCENE_COMP_RENDER_FLAGS_OFF, self.SCENE_HIDDEN_IN_GAME_BIT,
        )

    def _write_mesh_hidden_in_game(self, mesh, hidden):
        self._write_scene_flag(
            mesh, self.SCENE_COMP_RENDER_FLAGS_OFF, self.SCENE_HIDDEN_IN_GAME_BIT, hidden,
        )

    def _read_mesh_visible(self, mesh):
        return self._read_scene_flag(mesh, self.SCENE_COMP_VISIBLE_OFF, self.SCENE_VISIBLE_BIT)

    def _write_mesh_visible(self, mesh, visible):
        self._write_scene_flag(mesh, self.SCENE_COMP_VISIBLE_OFF, self.SCENE_VISIBLE_BIT, visible)

    def _read_actor_hidden(self, pawn):
        return self._read_scene_flag(pawn, self.PAWN_OFF_ACTOR_FLAGS, self.ACTOR_HIDDEN_BIT)

    def _write_actor_hidden(self, pawn, hidden):
        self._write_scene_flag(pawn, self.PAWN_OFF_ACTOR_FLAGS, self.ACTOR_HIDDEN_BIT, hidden)

    def _enumerate_camo_body_meshes(self, pawn):
        """Body meshes that can appear in the camo screen-capture region."""
        meshes = []
        seen = set()

        def add(label, mesh, allow_widget=False):
            if mesh and mesh > 0x100000 and mesh not in seen:
                cn = self.objects.class_name(mesh)
                if (
                    "Mesh" in cn
                    or "MeshComponent" in cn
                    or (allow_widget and "Widget" in cn)
                ):
                    seen.add(mesh)
                    meshes.append((label, mesh, cn))

        comp = rp(self.pm, pawn + 0x0B68)
        if comp:
            mesh, _ = self._get_paint_mesh(pawn, comp)
            add("paint_target", mesh)
        add("sphere", rp(self.pm, pawn + self.PAWN_OFF_SPHERE_MESH))
        for off, label in (
            (0x0418, "body_sk"),
            (0x04C8, "fp_mesh"),
            (0x0490, "hand_bone"),
        ):
            add(label, rp(self.pm, pawn + off))
        add("nameplate", rp(self.pm, pawn + 0x0B48), allow_widget=True)
        sk = self.get_skeletal_mesh(pawn)
        add("skeletal", sk)
        return meshes

    def hide_local_character_for_camo(self, pawn):
        """
        Hide local body meshes while sampling screen pixels for camouflage.

        Uses bHiddenInGame + bVisible on scene components, actor bHidden, and the
        local-only HideBlock flag.  Does NOT touch BodyVisibility (replicated).
        """
        token = {
            "pawn": pawn,
            "meshes": [],
            "actor_hidden": None,
            "hide_block": None,
        }
        if not pawn:
            return token
        try:
            token["actor_hidden"] = self._read_actor_hidden(pawn)
            self._write_actor_hidden(pawn, True)
        except Exception as e:
            print(f"[CAMO-HIDE] actor bHidden failed: {e}")
        try:
            token["hide_block"] = bool(self.pm.read_bytes(pawn + self.PAWN_OFF_HIDE_BLOCK, 1)[0])
            self.pm.write_bytes(pawn + self.PAWN_OFF_HIDE_BLOCK, b"\x01", 1)
        except Exception as e:
            print(f"[CAMO-HIDE] HideBlock failed: {e}")

        for label, mesh, cn in self._enumerate_camo_body_meshes(pawn):
            try:
                prev_h = self._read_mesh_hidden_in_game(mesh)
                prev_v = self._read_mesh_visible(mesh)
                token["meshes"].append((label, mesh, prev_h, prev_v))
                self._write_mesh_hidden_in_game(mesh, True)
                self._write_mesh_visible(mesh, False)
                print(f"[CAMO-HIDE] {label} 0x{mesh:X} class={cn}")
            except Exception as e:
                print(f"[CAMO-HIDE] {label} 0x{mesh:X} failed: {e}")
        print(f"[CAMO-HIDE] hid {len(token['meshes'])} local mesh(es) for sampling")
        return token

    def restore_local_character_after_camo(self, token):
        if not token:
            return
        for label, mesh, prev_h, prev_v in token.get("meshes", []):
            try:
                self._write_mesh_hidden_in_game(mesh, prev_h)
                self._write_mesh_visible(mesh, prev_v)
            except Exception as e:
                print(f"[CAMO-HIDE] restore {label} failed: {e}")
        pawn = token.get("pawn")
        if pawn:
            if token.get("hide_block") is not None:
                try:
                    self.pm.write_bytes(
                        pawn + self.PAWN_OFF_HIDE_BLOCK,
                        b"\x01" if token["hide_block"] else b"\x00",
                        1,
                    )
                except Exception as e:
                    print(f"[CAMO-HIDE] restore HideBlock failed: {e}")
            if token.get("actor_hidden") is not None:
                try:
                    self._write_actor_hidden(pawn, token["actor_hidden"])
                except Exception as e:
                    print(f"[CAMO-HIDE] restore actor bHidden failed: {e}")
        if token.get("meshes"):
            print("[CAMO-HIDE] restored local body meshes")

    @contextmanager
    def camo_sampling_hide_local(self, pawn, enabled=True, settle_ms=200, pump_frame=None):
        """
        Hide the local body so screen sampling skips the player model.

        pump_frame: optional callable (e.g. QApplication.processEvents) invoked
        between short sleeps so the game can render a frame without the body.
        """
        token = None
        if enabled and pawn:
            token = self.hide_local_character_for_camo(pawn)
            import time
            base = max(0.05, settle_ms / 1000.0)
            time.sleep(base * 0.4)
            for _ in range(6):
                if pump_frame:
                    try:
                        pump_frame()
                    except Exception:
                        pass
                time.sleep(max(0.025, base / 6.0))
        try:
            yield
        finally:
            if token is not None:
                self.restore_local_character_after_camo(token)

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
    # RVAs from Dumper-7 Dumpspace/FunctionsInfo.json — build 44394996
    # (re-dumped 2026-06-28; FunctionsInfo natives in 0x50E1xxx band unchanged vs 06-27)
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
    # FunctionsInfo lists UFUNCTION entry points in the 0x50E1xxx band; the *deep*
    # workers that accept register-style remote calls sit ~0x1AC40–0x1E000 ahead.
    # Always call deep worker RVAs (register-style native entry points) — never the
    # FunctionsInfo / exec thunks directly.  Re-verify prologues at runtime because
    # game patches shift these by small deltas (calling a stale RVA lands mid-function
    # and corrupts the heap — "ClearChannel ok=True" then crash / inject failure).
    RVA_PAINT_AT_UV_DUMP          = 0x50E5300   # FunctionsInfo PaintAtUV exec
    RVA_PAINT_AT_UV_LEGACY        = 0x5101000   # deep PaintAtUV worker (RCX=comp RDX=&UV R8=&ChannelData R9=channel)
    RVA_EXEC_PAINT_AT_UV          = 0x50E5300   # exec anchor for scan / self-test
    RVA_CLEAR_CHANNEL_LEGACY      = 0x50F6C60   # deep ClearChannel native (RCX=comp, DL=channel)
    RVA_EXPORT_CHANNEL_LEGACY     = 0x50F94C0   # deep ExportChannelToBytes worker
    RVA_IMPORT_CHANNEL_LEGACY     = 0x50FCE50   # Import dispatch wrapper
    RVA_EXEC_IMPORT_CHANNEL       = 0x50E3D90   # exec ImportChannelFromBytes (scan anchor)
    RVA_CLEAR_CHANNEL_NATIVE      = 0x50E35E0   # FunctionsInfo ClearChannel exec
    RVA_EXPORT_CHANNEL_NATIVE     = 0x50E36A0   # FunctionsInfo ExportChannelToBytes exec
    RVA_IMPORT_CHANNEL_NATIVE     = 0x50E3D90   # FunctionsInfo ImportChannelFromBytes exec
    RVA_BEGIN_STROKE_NATIVE       = 0x50E35A0   # FunctionsInfo BeginStroke exec
    RVA_END_STROKE_NATIVE         = 0x50E3680   # FunctionsInfo EndStroke exec
    RVA_BEGIN_STROKE_LEGACY       = 0x50F24E0   # deep BeginStroke worker
    RVA_END_STROKE_LEGACY         = 0x50F8DF0   # deep EndStroke worker
    RVA_IMPORT_RT_NATIVE          = 0x5106890   # copies TArray bytes into AlbedoRenderTarget
    RVA_EXEC_REQUEST_TEXTURE_SYNC = 0x50D19C0   # FunctionsInfo RequestFullTextureSync (do NOT call)
    RVA_APPLY_PAINT_TO_MATERIAL   = 0x5105AD0   # internal RT→material worker (do NOT call directly)
    OFF_PAINT_CHANNELS_DIRTY      = 0x016B      # DISABLED — overlaps DynamicMaterialInstance @ +0x0168
    RVA_REQUEST_TEXTURE_SYNC        = 0x50FF610   # DO NOT call from Peterhack — delayed AV crash
    RVA_PAINT_AT_SCREEN_NATIVE    = 0x50E50C0   # PaintAtScreenPosition exec (FunctionsInfo)
    RVA_HITTEST_AT_SCREEN_NATIVE  = 0x50E3BD0   # HitTestAtScreenPosition exec (FunctionsInfo)
    RVA_GET_PAINT_MESH_NATIVE     = 0x50E3950   # GetInitializedPaintMesh exec (FunctionsInfo)
    RVA_EXEC_PAINT_AT_SCREEN      = 0x50E50C0   # PaintAtScreenPosition scan anchor
    RVA_EXEC_HITTEST_AT_SCREEN    = 0x50E3BD0   # HitTestAtScreenPosition scan anchor
    EPaintChannel_Albedo = 0
    EPaintChannel_All = 4

    # ---- Screen-space camouflage (true chameleon blend) -------------------
    # PaintAtScreenPosition raycasts a screen point onto the body mesh and paints
    # the correct UV automatically — giving real spatial correspondence.

    def _module_base(self):
        try:
            mod = pymem.process.module_from_name(self.pm.process_handle, self.MODULE_NAME)
            if mod and mod.lpBaseOfDll:
                self._cached_module_base = int(mod.lpBaseOfDll)
                return self._cached_module_base
        except Exception:
            pass
        if self._cached_module_base:
            return self._cached_module_base
        return 0

    def _inject_handle(self):
        """Process handle with full access for VirtualAllocEx / CreateRemoteThread."""
        if self._inject_handle_cache:
            return self._inject_handle_cache
        import ctypes
        k32 = ctypes.windll.kernel32
        access = 0x001F0FFF  # PROCESS_ALL_ACCESS
        h = k32.OpenProcess(access, False, int(self.pm.process_id))
        if h:
            self._inject_handle_cache = int(h)
            return self._inject_handle_cache
        return int(self.pm.process_handle)

    def _game_process_alive(self):
        """True while the attached game process is still running."""
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
            ]
            if not k32.GetExitCodeProcess(int(self.pm.process_handle), ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return False

    @staticmethod
    def _flush_remote_code(handle, addr, size):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.FlushInstructionCache(handle, ctypes.c_void_p(int(addr)), int(size))

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
        return k32.VirtualAllocEx(int(handle), 0, int(size), 0x3000, 0x40)

    @staticmethod
    def _remote_free(handle, addr):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.VirtualFreeEx.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_uint32,
        ]
        k32.VirtualFreeEx(int(handle), int(addr), 0, 0x8000)

    @staticmethod
    def _remote_thread_nt_impl(handle, addr, timeout_ms=5000):
        """Fallback when CreateRemoteThread is blocked (err=0x5)."""
        import ctypes
        from ctypes import wintypes
        ntdll = ctypes.windll.ntdll
        k32 = ctypes.windll.kernel32
        h_thread = wintypes.HANDLE()
        nt = ntdll.NtCreateThreadEx
        nt.restype = wintypes.LONG
        nt.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.ULONG,
            ctypes.c_size_t,
            ctypes.c_size_t,
            ctypes.c_size_t,
            wintypes.LPVOID,
        ]
        status = nt(
            ctypes.byref(h_thread),
            0x1FFFFF,
            None,
            int(handle),
            ctypes.c_void_p(int(addr)),
            None,
            0x4,  # skip thread attach — safer for injected stubs
            0, 0, 0, None,
        )
        if status != 0:
            print(f"[REMOTE] NtCreateThreadEx failed (status=0x{status & 0xFFFFFFFF:X})")
            return False, False
        wait = k32.WaitForSingleObject(h_thread, int(timeout_ms))
        k32.CloseHandle(h_thread)
        if wait != 0:
            print(f"[REMOTE] NtCreateThreadEx wait failed (code=0x{wait & 0xFFFFFFFF:X}, "
                  f"timeout={timeout_ms}ms)")
            return False, True
        return True, True

    @staticmethod
    def _remote_thread_impl(handle, addr, timeout_ms=5000):
        import ctypes
        import time
        k32 = ctypes.windll.kernel32
        k32.CreateRemoteThread.restype = ctypes.c_void_p
        k32.CreateRemoteThread.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
        h = int(handle)
        a = int(addr)
        for attempt in range(3):
            th = k32.CreateRemoteThread(h, None, 0, a, None, 0, None)
            if th:
                wait = k32.WaitForSingleObject(th, int(timeout_ms))
                k32.CloseHandle(th)
                if wait != 0:
                    print(f"[REMOTE] thread wait failed (code=0x{wait & 0xFFFFFFFF:X}, "
                          f"timeout={timeout_ms}ms)")
                    return False, True
                return True, True
            err = k32.GetLastError()
            if attempt < 2 and err == 5:
                time.sleep(0.05 * (attempt + 1))
                continue
            print(f"[REMOTE] CreateRemoteThread failed (err=0x{err:X}, addr=0x{a:X})")
            if err == 5:
                ok, started = MecchaESP._remote_thread_nt_impl(h, a, timeout_ms)
                if ok or started:
                    if ok:
                        print("[REMOTE] NtCreateThreadEx fallback ok")
                    return ok, started
            return False, False
        return False, False

    @staticmethod
    def _remote_thread(handle, addr, timeout_ms=5000):
        ok, _started = MecchaESP._remote_thread_impl(handle, addr, timeout_ms)
        return ok

    def _remote_thread_resilient(self, handle, addr, timeout_ms=5000):
        """Inject while game is frozen — resume briefly so natives run on live threads."""
        handle = self._inject_handle()
        handles = self._active_freeze_handles
        if handles:
            self._resume_game_threads(handles)
            self._active_freeze_handles = None
            if self._paint_keep_unfrozen:
                self._paint_session_resumed = True
            try:
                ok, _started = self._remote_thread_impl(handle, addr, timeout_ms)
                return ok
            finally:
                if not self._paint_keep_unfrozen:
                    new_handles = self._suspend_game_threads()
                    self._active_freeze_handles = new_handles if new_handles else None
        ok, _ = self._remote_thread_impl(handle, addr, timeout_ms)
        return ok

    def _run_remote_shellcode(self, handle, addr, timeout_ms=5000):
        """
        Run injected shellcode in the target process.

        While _game_frozen is active, inject directly into the suspended process
        (CreateRemoteThread still works — do NOT resume/suspend per batch; that
        caused CreateRemoteThread err=0x5 on the second inject).
        """
        handle = self._inject_handle()
        if getattr(self, "_active_freeze_handles", None):
            ok, _ = self._remote_thread_impl(handle, int(addr), timeout_ms)
            return ok
        return self._remote_thread_resilient(handle, int(addr), timeout_ms=timeout_ms)

    def _sync_paint_to_render_target(self, comp, label="PAINT", settle_ms=350):
        """
        After stamp injects return, resume briefly so the render thread can apply
        strokes.  Do NOT re-suspend — _game_frozen must exit with threads running.
        """
        import time
        del comp
        handles = self._active_freeze_handles
        if handles:
            self._resume_game_threads(handles)
            self._active_freeze_handles = None
        print(f"[{label}] stamp inject complete — syncing render target ({settle_ms}ms)...")
        time.sleep(max(0.05, settle_ms / 1000.0))
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

    def _resume_all_process_threads(self, label="PAINT"):
        """Resume every thread in the game process (clears stale suspend counts)."""
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
        resumed = 0
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snap in (-1, 0xFFFFFFFF):
            return resumed
        try:
            te = THREADENTRY32()
            te.dwSize = ctypes.sizeof(THREADENTRY32)
            if not k32.Thread32First(snap, ctypes.byref(te)):
                return resumed
            while True:
                if te.th32OwnerProcessID == pid:
                    th = k32.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                    if th:
                        try:
                            while True:
                                prev = k32.ResumeThread(th)
                                if prev <= 1:
                                    if prev == 1:
                                        resumed += 1
                                    break
                        finally:
                            k32.CloseHandle(th)
                if not k32.Thread32Next(snap, ctypes.byref(te)):
                    break
        finally:
            k32.CloseHandle(snap)
        if resumed:
            print(f"[{label}] safety resume ({resumed} threads were still suspended)")
        return resumed

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
    def _paint_live(self, label="PAINT"):
        """Run paint injects without suspending game threads (inject-safe on current build)."""
        print(f"[{label}] live paint session (no thread suspend)")
        yield True

    @contextmanager
    def _game_frozen(self, label="PAINT"):
        """
        Freeze the game while paint natives run (same idea as in-game paint mods).
        Existing game threads are suspended; our CreateRemoteThread worker still runs.
        """
        handles = self._suspend_game_threads()
        self._active_freeze_handles = handles if handles else None
        if handles:
            print(f"[{label}] froze game ({len(handles)} threads)")
        else:
            print(f"[{label}] freeze failed — painting anyway")
        try:
            yield bool(handles)
        finally:
            pending = self._active_freeze_handles
            self._active_freeze_handles = None
            if pending:
                self._resume_game_threads(pending)
                print(f"[{label}] unfroze game ({len(pending)} threads)")
            elif handles and not self._paint_keep_unfrozen:
                self._resume_game_threads(handles)
                print(f"[{label}] unfroze game ({len(handles)} threads)")
            elif handles and self._paint_keep_unfrozen:
                if not self._paint_session_resumed:
                    self._resume_game_threads(handles)
                print(f"[{label}] paint session ended (threads running)")
            self._resume_all_process_threads(label)

    def _emergency_unfreeze_game(self, label="CAMO"):
        """Resume all game threads — safe to call from Stop / abort paths."""
        pending = getattr(self, "_active_freeze_handles", None)
        if pending:
            try:
                self._resume_game_threads(pending)
                print(f"[{label}] emergency unfreeze ({len(pending)} suspended handles)")
            except Exception as exc:
                print(f"[{label}] emergency unfreeze error: {exc}")
            self._active_freeze_handles = None
        n = self._resume_all_process_threads(label)
        if n:
            print(f"[{label}] cleared {n} stale thread suspend(s)")

    def _call_paint_at_uv_grid(self, comp, r_lin, g_lin, b_lin):
        """
        Paint the albedo render target at 5 UV grid points (centre + 4 quadrants)
        in a single remote thread.

        IMPORTANT: we call the *real native* PaintAtUV worker (resolved at runtime
        via _resolve_paint_at_uv_worker — NOT the FunctionsInfo dump address).
        The execPaintAtUV thunk parses params from FFrame bytecode; calling it with
        register-style args corrupts the heap and crashes the game.

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
        worker = self._resolve_paint_at_uv_worker()
        if not worker:
            return False
        fn = base + worker

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

    def _call_paint_pattern(self, comp, points, channel=4, quiet=False):
        """
        Call the native PaintAtUV worker once per point in a single remote thread.
        channel: EPaintChannel — 0=Albedo, 4=All (default for camo).
        """
        base = self._module_base()
        if not base:
            print("[CAMO] PaintAtUV: module base unavailable")
            return False
        worker = self._resolve_paint_at_uv_worker()
        if not worker:
            print("[CAMO] PaintAtUV worker unresolved — refusing to paint")
            return False
        fn = base + worker
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

        inj = self._inject_handle()
        mem = self._remote_alloc(inj, total)
        if not mem:
            if not self._game_process_alive():
                print("[CAMO] VirtualAllocEx failed — game process exited")
            else:
                import ctypes
                err = ctypes.windll.kernel32.GetLastError()
                print(f"[CAMO] VirtualAllocEx failed (GetLastError={err})")
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
            sc += b'\x41\xB1' + bytes([channel & 0xFF])  # mov r9b, channel
            sc += b'\x48\xB8' + q(fn)              # mov rax, fn
            sc += b'\xFF\xD0'                      # call rax

        sc += b'\x48\x83\xC4\x20'          # add  rsp, 0x20
        sc += b'\x48\x89\xEC'              # mov  rsp, rbp
        sc += b'\x5D'                       # pop  rbp
        sc += b'\xC3'                       # ret

        if len(sc) > SC_SIZE:
            self._remote_free(inj, mem)
            print(f"[CAMO] shellcode {len(sc)} exceeds SC_SIZE {SC_SIZE}")
            return False

        sc = sc.ljust(SC_SIZE, b'\x90')
        payload = (sc + cd_block + uv_block).ljust(total, b'\x00')

        try:
            self.pm.write_bytes(mem_i, payload, len(payload))
            self._flush_remote_code(inj, mem_i, len(payload))
        except Exception as e:
            print(f"[CAMO] write shellcode failed: {e}")
            self._remote_free(inj, mem)
            return False

        ok = self._run_remote_shellcode(
            inj, mem_i,
            timeout_ms=max(60000, n * 100),
        )
        self._remote_free(inj, mem)
        if not quiet or not ok:
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

    # Native UFUNCTION implementations from the dump sit in a contiguous band
    # around 0x50E1xxx. Older builds used separate exec thunks + far workers;
    # runtime disassembly fallback is kept for forward compatibility.
    WORKER_FAR_BAND = 0x4000

    _NATIVE_PROLOGUE_BYTES = (0x40, 0x41, 0x48, 0x4C, 0x53, 0x55, 0x56, 0x57, 0x66, 0x80)

    def _native_rva_ok(self, rva):
        """True when `rva` points at what looks like a function entry in the live module."""
        if not rva:
            return False
        base = self._module_base()
        if not base:
            return False
        try:
            return self.pm.read_bytes(base + rva, 1)[0] in self._NATIVE_PROLOGUE_BYTES
        except Exception:
            return False

    @staticmethod
    def _select_worker(transfers, exec_rva, log=False):
        """Pick the native member-function worker the exec thunk dispatches to.

        Observed thunk layout (verified on execPaintAtUV):
          * shared UE runtime helpers (FFrame::Step / FProperty handling) are
            huge BACKWARD calls into the 0x15xxxxx region and usually repeat
            (n > 1);
          * the per-UFUNCTION native worker sits just AFTER the exec-thunk
            cluster, reached by a single FORWARD call/jmp (positive distance,
            e.g. execPaintAtUV -> deep worker ~+0x1AC40 from exec thunk).

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

    def _select_worker_prologue(self, transfers, exec_rva):
        """Pick a native worker with a valid prologue; prefer far forward calls over jmp tails."""
        if not transfers:
            return 0
        from collections import Counter
        cnt = Counter(t for _, _, t in transfers)
        band = self.WORKER_FAR_BAND
        for _pos, kind, tgt in transfers:
            if (
                kind == "call"
                and cnt[tgt] == 1
                and (tgt - exec_rva) > band
                and self._native_rva_ok(tgt)
            ):
                return tgt
        worker = self._select_worker(transfers, exec_rva)
        if worker and self._native_rva_ok(worker):
            return worker
        for _pos, kind, tgt in reversed(transfers):
            if cnt[tgt] == 1 and (tgt - exec_rva) > band and self._native_rva_ok(tgt):
                print(f"[PAINT] worker prologue fallback 0x{tgt:X} ({kind} @ exec 0x{exec_rva:X})")
                return tgt
        if worker:
            print(f"[PAINT] rejected worker 0x{worker:X} (bad prologue @ exec 0x{exec_rva:X})")
        return 0

    def _align_exec_entry(self, anchor_rva, search=0x80):
        """
        FunctionsInfo exec RVAs can point at a prior function's epilogue (ret/int3)
        instead of the real UFUNCTION entry. Scan forward for a normal prologue.
        """
        if self._native_rva_ok(anchor_rva):
            return anchor_rva
        base = self._module_base()
        if not base:
            return anchor_rva
        try:
            blob = self.pm.read_bytes(base + anchor_rva, search)
        except Exception:
            return anchor_rva
        for pat in (
            b"\x48\x89\x5C\x24",  # mov [rsp+N], rbx
            b"\x48\x89\x4C\x24",  # mov [rsp+N], rcx
            b"\x40\x53\x48\x83",  # push rbx; sub rsp, …
            b"\x48\x8B\xC4",      # mov rax, rsp
        ):
            idx = blob.find(pat)
            if idx >= 0:
                aligned = anchor_rva + idx
                if aligned != anchor_rva:
                    print(
                        f"[PAINT] exec anchor 0x{anchor_rva:X} "
                        f"aligned -> 0x{aligned:X} (+0x{idx:X})",
                        flush=True,
                    )
                return aligned
        return anchor_rva

    def _scan_deep_worker(self, anchor_rva, read_size=0x900):
        """Disassemble an exec/dump anchor and return the far native worker RVA."""
        anchor_rva = self._align_exec_entry(anchor_rva)
        base = self._module_base()
        if not base:
            return 0
        try:
            mod = pymem.process.module_from_name(self.pm.process_handle, self.MODULE_NAME)
            text_hi = mod.SizeOfImage if mod else 0x10000000
            code = self.pm.read_bytes(base + anchor_rva, read_size)
        except Exception as exc:
            print(f"[PAINT] worker scan read @0x{anchor_rva:X} failed: {exc}")
            return 0
        transfers = self._scan_call_jmp_targets(code, anchor_rva, 0, text_hi)
        return self._select_worker_prologue(transfers, anchor_rva)

    def _resolve_paint_at_uv_worker(self):
        """Return the deep PaintAtUV native for the current build."""
        if self._paint_at_uv_worker_rva:
            return self._paint_at_uv_worker_rva
        expected = self.RVA_PAINT_AT_UV_LEGACY
        worker = 0
        if self._native_rva_ok(expected):
            worker = expected
        if not worker:
            worker = self._scan_deep_worker(self.RVA_PAINT_AT_UV_DUMP)
        if not worker:
            worker = self._scan_deep_worker(self.RVA_EXEC_PAINT_AT_UV)
        if not worker or not self._native_rva_ok(worker):
            print("[PAINT] PaintAtUV worker unresolved — refusing to paint")
            self._paint_at_uv_worker_rva = 0
            return 0
        if worker != expected:
            print(
                f"[PAINT] PaintAtUV using scan worker 0x{worker:X} "
                f"(expected 0x{expected:X})",
            )
        self._paint_at_uv_worker_rva = worker
        return worker

    def _resolve_paint_screen_worker(self):
        """
        Return PaintAtScreenPosition native RVA.  Gated by a PaintAtUV worker
        self-test: execPaintAtUV scan must agree with _resolve_paint_at_uv_worker
        before we trust execPaintAtScreenPosition disassembly.
        """
        if self._paint_screen_worker_rva is not None:
            return self._paint_screen_worker_rva
        self._paint_screen_worker_rva = 0

        uv_worker = self._resolve_paint_at_uv_worker()
        if not uv_worker:
            return 0

        base = self._module_base()
        if not base:
            print("[CAMO] no module base — cannot resolve worker")
            return 0
        mod = pymem.process.module_from_name(self.pm.process_handle, self.MODULE_NAME)
        if not mod:
            return 0
        text_hi = mod.SizeOfImage

        # Self-test: execPaintAtUV scan must reproduce the UV worker we will call.
        try:
            uv_code = self.pm.read_bytes(base + self.RVA_EXEC_PAINT_AT_UV, 0x800)
        except Exception as e:
            print(f"[CAMO] execPaintAtUV read failed: {e}")
            return 0
        uv_tr = self._scan_call_jmp_targets(uv_code, self.RVA_EXEC_PAINT_AT_UV, 0, text_hi)
        uv_scan = self._select_worker(uv_tr, self.RVA_EXEC_PAINT_AT_UV)
        if uv_scan and uv_scan != uv_worker:
            print("[CAMO] --- execPaintAtUV transfers (self-test) ---")
            self._select_worker(uv_tr, self.RVA_EXEC_PAINT_AT_UV, log=True)
            print(f"[CAMO] UV worker self-test FAILED: scan->0x{uv_scan:X} "
                  f"expected 0x{uv_worker:X}. Refusing PaintAtScreenPosition.")
            return 0
        if uv_scan:
            print(f"[CAMO] UV worker self-test PASSED: execPaintAtUV -> 0x{uv_worker:X}")

        # Prefer dump native when prologue looks valid (same band as ClearChannel).
        dump_rva = self.RVA_PAINT_AT_SCREEN_NATIVE
        if self._native_rva_ok(dump_rva):
            print(f"[CAMO] PaintAtScreenPosition native RVA=0x{dump_rva:X} (dump)")
            self._paint_screen_worker_rva = dump_rva
            return dump_rva

        try:
            sc_code = self.pm.read_bytes(base + self.RVA_EXEC_PAINT_AT_SCREEN, 0x800)
        except Exception as e:
            print(f"[CAMO] execPaintAtScreen read failed: {e}")
            return 0

        print("[CAMO] --- execPaintAtScreenPosition transfers ---")
        sc_tr = self._scan_call_jmp_targets(
            sc_code, self.RVA_EXEC_PAINT_AT_SCREEN, 0, text_hi,
        )
        worker = self._select_worker(sc_tr, self.RVA_EXEC_PAINT_AT_SCREEN, log=True)
        if not worker or not self._native_rva_ok(worker):
            print("[CAMO] could not locate PaintAtScreenPosition worker")
            return 0
        dist = abs(worker - self.RVA_EXEC_PAINT_AT_SCREEN)
        if dist <= self.WORKER_FAR_BAND:
            print(f"[CAMO] rejected worker 0x{worker:X}: only 0x{dist:X} from thunk")
            return 0
        print(f"[CAMO] PaintAtScreenPosition worker RVA=0x{worker:X} (dist=0x{dist:X})")
        self._paint_screen_worker_rva = worker
        return worker

    def _resolve_hittest_screen_worker(self):
        """Return HitTestAtScreenPosition native RVA (dump-first, scan fallback)."""
        if self._hittest_screen_worker_rva is not None:
            return self._hittest_screen_worker_rva
        self._hittest_screen_worker_rva = 0
        dump_rva = self.RVA_HITTEST_AT_SCREEN_NATIVE
        if self._native_rva_ok(dump_rva):
            print(f"[PAINT] HitTest native RVA=0x{dump_rva:X} (dump)")
            self._hittest_screen_worker_rva = dump_rva
            return dump_rva
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
            ht_code = self.pm.read_bytes(base + dump_rva, 0x600)
        except Exception as e:
            print(f"[PAINT] HitTest native read failed: {e}")
            return 0
        ht_tr = self._scan_call_jmp_targets(
            ht_code, dump_rva, 0, text_hi,
        )
        worker = self._select_worker(ht_tr, dump_rva)
        if not worker or not self._native_rva_ok(worker):
            print("[PAINT] could not locate HitTestAtScreenPosition worker")
            return 0
        dist = abs(worker - dump_rva)
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

        # sub rsp, 0x40: 0x20 shadow + 0x20 for the two stack args at [rsp+0x20/+0x28].
        # Using 0x20 here writes PlayerController into the saved-rbp slot and corrupts
        # the thread stack, crashing the game (identical fix to _call_paint_at_screen).
        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x40"
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
        Call the resolved PaintAtUV deep native worker for each (u, v, (r,g,b)) point.
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
        worker = self._resolve_paint_at_uv_worker()
        if not worker:
            return False
        fn = base + worker

        n = len(uv_points)
        # per-call: 5 × mov-rXX imm64 (10 bytes each) + call rax (2) = 52 bytes
        SC_SIZE = (32 + n * 64 + 16) & ~15
        uv_off  = SC_SIZE            # FVector2D blocks (16 B each)
        cd_off  = uv_off + n * 16   # FPaintChannelData blocks (32 B each)
        total   = cd_off + n * 32

        mem = self._remote_alloc(self.pm.process_handle, total)
        if not mem:
            if not self._game_process_alive():
                print("[PAINT] VirtualAllocEx failed — game process is dead")
            else:
                print("[PAINT] VirtualAllocEx failed")
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
            sc += b"\x41\xB1\x04"          # mov r9b, 4 (EPaintChannel::All)
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

        ok = self._remote_thread_resilient(
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

        mem = self._remote_alloc(self._inject_handle(), len(sc))
        if not mem:
            return False
        inj = self._inject_handle()
        try:
            self.pm.write_bytes(int(mem), sc, len(sc))
            self._flush_remote_code(inj, int(mem), len(sc))
        except Exception:
            self._remote_free(inj, mem)
            return False
        ok = self._run_remote_shellcode(inj, int(mem), timeout_ms=8000)
        if ok:
            self._remote_free(inj, mem)
        else:
            print(f"[PAINT] ClearChannel remote thread failed — mem left allocated")
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

    def _paint_brush_radius_pixels(self, comp, grid, overlap=0.85):
        """Brush radius in texture pixels — matches the working camo path."""
        res = self.get_albedo_resolution(comp=comp)
        g = max(1, int(grid))
        return (res / g) * overlap

    def _white_flood_atlas(self, comp, grid=2, channel=4):
        """Prime the atlas with a few white stamps before image painting."""
        grid = max(2, int(grid))
        white = (255, 255, 255)
        pts = [
            ((gx + 0.5) / grid, (gy + 0.5) / grid, white)
            for gy in range(grid)
            for gx in range(grid)
        ]
        # Legacy sessions used brush radius ≈0.35 here; huge pixel radii crash PaintAtUV.
        self._write_brush_settings(comp, 0.35, 1.0, 1.0)
        ok = self._call_paint_pattern_batched(
            comp, pts, channel=channel, batch=self.PAINT_UV_BATCH_SAFE, log_prefix="PAINT",
        )
        print(f"[PAINT] white flood done ({len(pts)} stamps, r=0.35) ok={ok}")
        return ok

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
        res = self.get_albedo_resolution(comp=comp)
        base_r = min(max(32.0, res * 0.22), 64.0)
        self._write_brush_settings(comp, base_r, 1.0, 1.0)
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
        ok = self._call_paint_pattern(comp, base, channel=self.EPaintChannel_All)
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
        record_strokes=None, auto_flush=None, brush_overlap=None,
        subsampling_stride=1, brush_radius_px=None,
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

        if record_strokes is None:
            record_strokes = log_prefix not in ("PAINT", "PRESET")
        if auto_flush is None:
            auto_flush = log_prefix in ("PAINT", "PRESET")
        self._prepare_paint_component(
            paint_comp, record_strokes=record_strokes, auto_flush=auto_flush,
        )
        if log_prefix in ("PAINT", "PRESET"):
            print(f"[{log_prefix}] direct UV flush mode record={record_strokes} auto_flush={auto_flush}")

        if brush_grid is not None:
            g = max(1, int(brush_grid))
        else:
            g = max(1, int(round(len(points) ** 0.5)))

        # FRuntimeBrushSettings::Radius is in texture pixels (see UV diagnostic + backup camo).
        overlap = 0.85 if brush_overlap is None else float(brush_overlap)
        if log_prefix in ("PAINT", "PRESET"):
            overlap = 0.90 if brush_overlap is None else float(brush_overlap)
        atlas_res = self.get_albedo_resolution(comp=paint_comp)
        if brush_radius_px is not None:
            radius = max(1.0, min(float(brush_radius_px), self.CAMO_MAX_BRUSH_PX))
            radius_label = f"{radius:.1f}px"
        elif log_prefix in ("PAINT", "PRESET", "CAMO") and brush_grid is not None:
            radius = self._paint_brush_radius_pixels(paint_comp, g, overlap=overlap)
            if log_prefix == "CAMO":
                radius = min(radius, self.CAMO_MAX_BRUSH_PX)
            radius_label = f"{radius:.1f}px"
        else:
            radius = self._paint_brush_radius_pixels(paint_comp, g, overlap=overlap)
            radius_label = f"{radius:.1f}px"

        hardness = brush_hardness
        if log_prefix in ("PAINT", "PRESET"):
            hardness = 0.95
        elif log_prefix == "CAMO":
            hardness = max(0.98, min(float(brush_hardness), 1.0))
        opacity = max(0.0, min(1.0, float(brush_opacity)))
        paint_channel = 4  # EPaintChannel::All — same as F10 camo (Albedo-only is invisible)
        try:
            atlas_res = self.get_albedo_resolution(comp=paint_comp)
            self._write_brush_settings(paint_comp, radius, hardness, opacity)
            print(f"[{log_prefix}] grid~{g}x{g} brush r={radius_label} "
                  f"(atlas={atlas_res}) hard={hardness:.2f} op={opacity:.2f} "
                  f"stamps={len(points)} freeze={freeze}")
        except Exception as e:
            print(f"[{log_prefix}] brush settings write failed: {e}")

        def _run_batches():
            import time
            if replace and log_prefix not in ("PAINT", "PRESET", "CAMO"):
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
            already_frozen = bool(getattr(self, "_active_freeze_handles", None))
            if log_prefix in ("PAINT", "PRESET"):
                # Always small batches — 128-stamp injects fail with err=0x5 after probe.
                batch = self.PAINT_UV_BATCH_SAFE
            elif log_prefix == "CAMO":
                if already_frozen or freeze:
                    # Frozen injects are stable at 64 stamps; live injects crash UE.
                    batch = 64 if total > 4000 else self.PAINT_UV_BATCH_SIZE
                else:
                    batch = self.PAINT_UV_BATCH_SAFE
            elif freeze:
                batch = self.PAINT_UV_BATCH_SIZE
                if log_prefix == "CAMO" and total > 4000:
                    batch = 64
            else:
                if fast_mode:
                    batch = 96
                else:
                    batch = self.PAINT_UV_BATCH_SAFE
            live_pause = (
                0.005 if (fast_mode and not freeze)
                else self.PAINT_LIVE_BATCH_PAUSE if not freeze
                else 0.0
            )
            n_batches = (total + batch - 1) // batch
            quiet_batches = log_prefix in ("PAINT", "PRESET") and n_batches > 8
            if log_prefix in ("PAINT", "PRESET"):
                print(f"[{log_prefix}] UV batches: {n_batches}×{batch} stamps "
                      f"(frozen={bool(already_frozen or freeze)})")

            for bi, offset in enumerate(range(0, total, batch)):
                if log_prefix == "CAMO" and self._camo_aborted():
                    print(f"[CAMO] aborted at batch {bi + 1}/{n_batches}")
                    return False
                chunk = points[offset:offset + batch]
                batch_comp = paint_comp
                if log_prefix == "CAMO" or not comp:
                    fresh = self._get_runtime_paint_component(actor, quiet=True)
                    if fresh and fresh > 0x100000:
                        batch_comp = fresh
                if not batch_comp or not self._paint_comp_ready(batch_comp):
                    print(f"[{log_prefix}] paint component not ready at offset {offset}")
                    return False
                if not self._call_paint_pattern(
                    batch_comp, chunk, channel=paint_channel, quiet=quiet_batches,
                ):
                    if not self._game_process_alive():
                        print(f"[{log_prefix}] UV batch failed — game crashed or exited")
                    else:
                        print(f"[{log_prefix}] UV batch failed at offset {offset} — stopping")
                    return False
                if (
                    log_prefix in ("PAINT", "PRESET")
                    and bi == 0
                    and batch_comp
                    and record_strokes
                ):
                    n0 = self._read_recorded_stroke_count(batch_comp)
                    print(f"[{log_prefix}] strokes after first batch: {n0}")
                if quiet_batches and ((bi + 1) % 64 == 0 or bi + 1 == n_batches):
                    done = min(offset + len(chunk), total)
                    print(f"[{log_prefix}] progress {done}/{total} stamps "
                          f"(batch {bi + 1}/{n_batches})")
                elif (
                    log_prefix == "CAMO"
                    and ((bi + 1) % 8 == 0 or bi + 1 == n_batches)
                ):
                    done = min(offset + len(chunk), total)
                    print(f"[CAMO] progress {done}/{total} (batch {bi + 1}/{n_batches})")
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

    def _silence_pawn_paint_tick(self, pawn):
        """Stop blueprint PaintTick from repainting one averaged CurrentPaintColor."""
        if not pawn:
            return
        try:
            self.pm.write_bytes(pawn + self.PAWN_OFF_IS_PAINT_MODE, b"\x00", 1)
            self.pm.write_bytes(pawn + self.PAWN_OFF_IS_BRUSHING, b"\x00", 1)
            self.pm.write_bytes(pawn + 0x0BA8, struct.pack("<ffff", 0.0, 0.0, 0.0, 0.0), 16)
        except Exception:
            pass

    def _force_quiesce_camo_paint(self, pawn=None, comp=None, label="CAMO", quiet=True):
        """Stop native + network paint loops on pawn and RuntimePaintableComponent."""
        pawn = pawn or self._get_local_pawn()
        if pawn:
            self._silence_pawn_paint_tick(pawn)
        if comp and comp > 0x100000:
            self._quiesce_paint_component(comp, label=label)
        elif pawn:
            comp = self._get_runtime_paint_component(pawn, quiet=True)
            if comp:
                self._quiesce_paint_component(comp, label=label)
        if not quiet:
            print(f"[{label}] force-quiesced paint flags on pawn/comp", flush=True)
        return True

    def _prepare_paint_component(self, comp, record_strokes=True, auto_flush=False):
        try:
            self.pm.write_bytes(comp + 0x01AC, b"\x01" if record_strokes else b"\x00", 1)
            self.pm.write_bytes(comp + 0x01AD, b"\x01" if auto_flush else b"\x00", 1)
        except Exception:
            pass

    def _reset_paint_component_flags(self, comp):
        """Turn off record_strokes/auto_flush so the game does not keep repainting."""
        self._prepare_paint_component(comp, record_strokes=False, auto_flush=False)

    def _quiesce_paint_component(self, comp, label="CAMO"):
        """Stop stroke recording, auto-flush, and live network paint sync."""
        self._reset_paint_component_flags(comp)
        try:
            self.pm.write_bytes(comp + 0x0125, b"\x00", 1)  # bRealtimeNetworkSync
        except Exception:
            pass

    def _list_game_modules(self):
        """Yield (name_lower, base_addr) for modules in the attached game process."""
        h_process = getattr(getattr(self, "pm", None), "process_handle", None)
        if not h_process:
            return

        import ctypes
        from ctypes import wintypes

        psapi = ctypes.windll.psapi
        kernel32 = ctypes.windll.kernel32
        h_process = ctypes.c_void_p(int(h_process) & 0xFFFFFFFFFFFFFFFF)
        hmods = (ctypes.c_void_p * 1024)()
        cb_needed = wintypes.DWORD()

        if psapi.EnumProcessModules(
            h_process,
            ctypes.byref(hmods),
            ctypes.sizeof(hmods),
            ctypes.byref(cb_needed),
        ):
            count = cb_needed.value // ctypes.sizeof(ctypes.c_void_p)
            name_buf = ctypes.create_unicode_buffer(512)
            for i in range(count):
                mod = hmods[i]
                base = int(mod or 0)
                if base <= 0x10000:
                    continue
                mod_handle = ctypes.c_void_p(base)
                name = ""
                if psapi.GetModuleBaseNameW(h_process, mod_handle, name_buf, 512):
                    name = name_buf.value
                elif psapi.GetModuleFileNameExW(h_process, mod_handle, name_buf, 512):
                    name = os.path.basename(name_buf.value)
                if name:
                    yield name.lower(), base
            return

        # Toolhelp32 fallback when psapi fails (older pymem / permissions).
        TH32CS_SNAPMODULE = 0x00000008
        TH32CS_SNAPMODULE32 = 0x00000010
        pid = kernel32.GetProcessId(h_process)
        if not pid:
            return

        class MODULEENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
                ("modBaseSize", wintypes.DWORD),
                ("hModule", wintypes.HMODULE),
                ("szModule", wintypes.WCHAR * 256),
                ("szExePath", wintypes.WCHAR * 260),
            ]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snap in (-1, 0xFFFFFFFF):
            return
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(MODULEENTRY32W)
            if not kernel32.Module32FirstW(snap, ctypes.byref(entry)):
                return
            while True:
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                if base > 0x10000 and entry.szModule:
                    yield entry.szModule.lower(), int(base)
                if not kernel32.Module32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snap)

    def _unload_bridge_dll_from_game(self):
        """Eject meccha-xenos-bridge.dll — F9 alone does not stop its 16 ms paint tick."""
        unloaded = []
        if not getattr(self, "pm", None):
            return unloaded

        modules = []
        try:
            import pymem.process
            if hasattr(pymem.process, "list_modules"):
                modules = pymem.process.list_modules(self.pm.process_handle)
        except Exception:
            modules = []

        fragments = (
            "meccha-xenos-bridge", "xenos-bridge", "meccha_camouflage",
            "meccha-camouflage", "camouflage", "xenos_bridge",
        )
        suspicious = ("bridge", "xenos", "camo", "inject")
        kernel32 = ctypes.windll.kernel32
        h_process = self.pm.process_handle
        free_lib = kernel32.GetProcAddress(
            kernel32.GetModuleHandleW("Kernel32.dll"), b"FreeLibrary",
        )
        if not free_lib:
            return unloaded

        seen_bases = set()

        def _try_unload(name, base):
            base = int(base or 0)
            if base <= 0x10000 or base in seen_bases:
                return
            if not any(frag in (name or "").lower() for frag in fragments):
                return
            seen_bases.add(base)
            print(f"[CAMO] unloading in-game DLL {name} @ 0x{base:X}", flush=True)
            h_thread = kernel32.CreateRemoteThread(
                h_process, None, 0, free_lib, base, 0, None,
            )
            if h_thread:
                kernel32.WaitForSingleObject(h_thread, 8000)
                kernel32.CloseHandle(h_thread)
                unloaded.append(name)

        if modules:
            for mod in modules:
                _try_unload(getattr(mod, "name", ""), getattr(mod, "lpBaseOfDll", 0))
        else:
            for name, base in self._list_game_modules():
                _try_unload(name, base)
                low = (name or "").lower()
                if any(s in low for s in suspicious):
                    print(f"[CAMO] in-game module (not unloaded): {name} @ 0x{base:X}", flush=True)

        if unloaded:
            print(f"[CAMO] unloaded {len(unloaded)} bridge DLL(s): {', '.join(unloaded)}", flush=True)
        return unloaded

    def _validate_paint_ready(self, comp, pawn, label="PAINT"):
        """Ensure RT, material, and mesh exist before native PaintAtUV (null+0x18 AV otherwise)."""
        issues = []
        rt = rp(self.pm, comp + 0x0148)
        if not rt or rt <= 0x100000:
            issues.append("AlbedoRenderTarget null")
        dmi = rp(self.pm, comp + 0x0168)
        if not dmi or dmi <= 0x100000:
            issues.append("DynamicMaterialInstance null")
        mesh, src = self._get_paint_mesh(pawn, comp)
        if not mesh:
            issues.append("TargetMeshComponent unresolved")
        if issues:
            print(f"[{label}] paint readiness FAILED: {', '.join(issues)}")
            return False
        print(
            f"[{label}] paint ready RT=0x{rt:X} DMI=0x{dmi:X} "
            f"mesh=0x{mesh:X} ({src})"
        )
        return True

    def _probe_paint_uv(self, comp, label="PAINT"):
        """Single-stamp sanity check before a large UV apply."""
        probe = [(0.25, 0.75, (255, 32, 32))]
        if not self._call_paint_pattern(comp, probe, channel=4):
            if not self._game_process_alive():
                print(f"[{label}] PaintAtUV probe crashed the game — aborting")
            else:
                print(f"[{label}] PaintAtUV probe failed — aborting full apply")
            return False
        print(f"[{label}] PaintAtUV probe ok (1 stamp)")
        return True

    def _read_recorded_stroke_count(self, comp):
        try:
            return struct.unpack("<i", self.pm.read_bytes(comp + 0x01F8, 4))[0]
        except Exception:
            return -1

    def _get_albedo_render_target_size(self, comp):
        """Read SizeX/SizeY from the live AlbedoRenderTarget (for ImportChannel validation)."""
        try:
            rt = rp(self.pm, comp + 0x0148)
            if not rt or rt <= 0x100000:
                return 0, 0
            sx = struct.unpack("<i", self.pm.read_bytes(rt + 0x0148, 4))[0]
            sy = struct.unpack("<i", self.pm.read_bytes(rt + 0x014C, 4))[0]
            if 64 <= sx <= 8192 and 64 <= sy <= 8192:
                return sx, sy
        except Exception:
            pass
        res = self.get_albedo_resolution(comp=comp)
        return res, res

    def _call_comp_void_native(self, comp, worker_rva, label="PAINT", timeout_ms=8000):
        """Call a void native(this) on RuntimePaintableComponent via remote thread."""
        base = self._module_base()
        if not base or not comp or not worker_rva:
            return False

        def q(v):
            return struct.pack("<Q", v)

        fn = base + worker_rva
        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x20"
        sc += b"\x48\xB9" + q(comp)
        sc += b"\x48\xB8" + q(fn) + b"\xFF\xD0"
        sc += b"\x48\x83\xC4\x20\x48\x89\xEC\x5D\xC3"
        sc = sc.ljust(256, b"\x90")

        inj = self._inject_handle()
        mem = self._remote_alloc(inj, len(sc))
        if not mem:
            return False
        try:
            self.pm.write_bytes(int(mem), sc, len(sc))
            self._flush_remote_code(inj, int(mem), len(sc))
        except Exception:
            self._remote_free(inj, mem)
            return False
        ok = self._run_remote_shellcode(inj, int(mem), timeout_ms=timeout_ms)
        if ok:
            self._remote_free(inj, mem)
        print(f"[{label}] comp+void 0x{worker_rva:X} ok={ok}")
        return ok

    def _call_request_texture_sync_live(self, comp, label="PAINT", timeout_ms=20000):
        """
        DISABLED — same as _call_request_texture_sync.  Calling 0x50FCDB0 from an
        external injector thread crashes the UE render/material pipeline even when
        the remote thread returns ok=True.
        """
        del comp, label, timeout_ms
        return False

    def _nudge_paint_visibility(self, comp, label="PAINT", auto_flush=False):
        """
        A few PaintAtUV stamps to wake the game's paint pipeline after import.
        Keep auto_flush=False for CAMO — it leaves a continuous repaint loop.
        """
        if not comp or not self._game_process_alive():
            return False
        self._prepare_paint_component(comp, record_strokes=False, auto_flush=auto_flush)
        self._write_brush_settings(comp, radius=2.0, hardness=1.0, opacity=0.01)
        grey = (128, 128, 128)
        # Front + back hemisphere probes — material may only refresh one side otherwise.
        probes = [
            (0.12, 0.30, grey),
            (0.25, 0.50, grey),
            (0.37, 0.70, grey),
            (0.62, 0.30, grey),
            (0.75, 0.50, grey),
            (0.87, 0.70, grey),
        ]
        ok = self._call_paint_pattern(comp, probes, channel=self.EPaintChannel_All)
        print(f"[{label}] visibility UV nudge x{len(probes)} ok={ok}")
        return ok

    def _camo_log_rt_coverage(self, comp, label="CAMO"):
        """Export albedo after apply — diagnostic only (export often fails while frozen)."""
        try:
            blob = self._call_export_channel_bytes(
                comp, self.EPaintChannel_Albedo, timeout_ms=6000,
            )
            if not blob:
                print(
                    f"[{label}] RT export empty (diagnostic — "
                    "export can fail even when PaintAtUV wrote pixels)",
                )
                return
            n = self._count_opaque_bgra(blob)
            total = len(blob) // 4
            print(f"[{label}] RT opaque pixels~{n}/{total} (diagnostic)")
        except Exception as exc:
            print(f"[{label}] RT export diagnostic failed: {exc}")

    def _sync_paint_to_material(self, comp, label="CAMO", end_stroke=True):
        """
        Push albedo RT to the visible material while the game is still frozen.

        CAMO: EndStroke only — grey UV nudges retrigger continuous repaint spam.
        Post-unfreeze nudge crashes UE after large applies.
        """
        if not comp or not self._game_process_alive():
            return False
        import time
        end_ok = False
        if end_stroke:
            self._prepare_paint_component(comp, record_strokes=False, auto_flush=False)
            end_ok = self._call_end_stroke(comp, label=label)
            time.sleep(0.08)
        if label == "CAMO":
            self._reset_paint_component_flags(comp)
            print(
                f"[{label}] material sync (frozen) end={end_ok}",
                flush=True,
            )
            return bool(end_ok)
        self._prepare_paint_component(comp, record_strokes=False, auto_flush=True)
        nudge_ok = self._nudge_paint_visibility(comp, label=label, auto_flush=True)
        time.sleep(0.08)
        print(
            f"[{label}] material sync (frozen) end={end_ok} nudge={nudge_ok}",
            flush=True,
        )
        self._reset_paint_component_flags(comp)
        return bool(end_ok or nudge_ok)

    def _post_unfreeze_paint_finalize(self, comp, label="PAINT", verify_export=False, atlas_verified=False, used_import=False):
        """
        Brief settle after paint.  Never call RequestFullTextureSync or scribble on
        comp memory — both crash the render pipeline on this build.

        CAMO: sync already ran while frozen; never inject after unfreeze.
        """
        import time
        if not comp or not self._game_process_alive():
            return False
        if label == "CAMO":
            time.sleep(0.35)
            alive = self._game_process_alive()
            if alive and comp:
                self._quiesce_paint_component(comp)
                self._force_quiesce_camo_paint(
                    self._get_local_pawn(), comp, label=label, quiet=True,
                )
            print(f"[{label}] finalize settle ok={alive}", flush=True)
            return alive
        time.sleep(0.2)
        nudged = self._nudge_paint_visibility(comp, label=label, auto_flush=True)
        time.sleep(0.1)
        settled = self._wait_paint_settle(comp, label=label, settle_ms=200)
        if not self._game_process_alive():
            print(f"[{label}] game exited during material finalize")
            return False
        visible = atlas_verified or nudged or settled
        print(
            f"[{label}] finalize nudge={nudged} settle={settled} "
            f"verified={atlas_verified} visible={visible}"
        )
        return visible

    def _pick_stroke_worker(self, native_rva, legacy_rva):
        """Prefer dump Final|Native entry; fall back to deep worker from older layouts."""
        if self._native_rva_ok(native_rva):
            return native_rva
        if self._native_rva_ok(legacy_rva):
            print(f"[PAINT] stroke worker fallback legacy 0x{legacy_rva:X} "
                  f"(native 0x{native_rva:X} failed prologue)")
            return legacy_rva
        return legacy_rva

    def _call_begin_stroke(self, comp, label="PAINT"):
        worker = self._pick_stroke_worker(
            self.RVA_BEGIN_STROKE_NATIVE, self.RVA_BEGIN_STROKE_LEGACY,
        )
        return self._call_comp_void_native(
            comp, worker, label=f"{label}/BeginStroke",
        )

    def _call_end_stroke(self, comp, label="PAINT"):
        """Flush recorded UV stamps to the albedo render target."""
        n = self._read_recorded_stroke_count(comp)
        if n >= 0:
            print(f"[{label}] recorded strokes before EndStroke: {n}")
        worker = self._pick_stroke_worker(
            self.RVA_END_STROKE_NATIVE, self.RVA_END_STROKE_LEGACY,
        )
        return self._call_comp_void_native(
            comp, worker, label=f"{label}/EndStroke",
        )

    def _flush_uv_paint_session(self, comp, label="PAINT", settle_ms=500):
        """Enable auto-flush, EndStroke, then wait for the game to flush stamps."""
        self._prepare_paint_component(comp, record_strokes=True, auto_flush=True)
        self._call_end_stroke(comp, label=label)
        return self._wait_paint_settle(comp, label=label, settle_ms=settle_ms)

    def _start_paint_stroke_session(self, comp, label="PAINT", clear=True):
        """Clear prior paint, then open a stroke session (Clear must NOT run after Begin)."""
        self._prepare_paint_component(comp, record_strokes=True, auto_flush=False)
        if clear:
            self._call_clear_paint_channel(comp)
            import time
            time.sleep(0.08)
        self._call_begin_stroke(comp, label=label)
        self._prepare_paint_component(comp, record_strokes=True, auto_flush=False)
        try:
            stroking = self.pm.read_bytes(comp + 0x0210, 2)
            print(f"[{label}] stroke session open stroking={stroking.hex()}")
        except Exception:
            pass

    def _paint_comp_ready(self, comp):
        """True when the paint component and albedo RT look usable."""
        if not comp or comp <= 0x100000:
            return False
        try:
            rt = rp(self.pm, comp + 0x0148)
            return bool(rt and rt > 0x100000)
        except Exception:
            return False

    def _mark_paint_channels_dirty(self, comp):
        """No-op — guessed dirty-flag offsets (+0x016A/+0x016B) overlap DMI and caused AV."""
        del comp
        return True

    @staticmethod
    def _count_opaque_bgra(blob, min_alpha=8):
        if not blob:
            return 0
        return sum(1 for i in range(0, len(blob), 4) if blob[i + 3] > min_alpha)

    def _verify_import_rt_opaque(self, comp, label="PAINT", min_opaque=4096):
        """Export albedo while frozen — confirms ImportChannel actually wrote pixels."""
        if self._camo_aborted():
            return False
        blob = self._call_export_channel_bytes(comp, self.EPaintChannel_Albedo, timeout_ms=8000)
        n = self._count_opaque_bgra(blob)
        total = len(blob) // 4 if blob else 0
        print(f"[{label}] frozen RT verify opaque~{n}/{total}")
        return n >= min_opaque

    def _try_import_atlas(self, comp, atlas_blob, label="PAINT", progress_cb=None, verify=False):
        """
        Write a composed atlas via ImportChannel in one inject.

        Returns (ok, verified) when verify=True; legacy callers treat as bool via tuple truth.
        """
        import time
        if not atlas_blob:
            return (False, False) if verify else False
        if progress_cb:
            try:
                progress_cb(1, 100)
            except Exception:
                pass
        self._prepare_paint_component(comp, record_strokes=False, auto_flush=False)
        if not self._call_clear_paint_channel(comp):
            print(f"[{label}] ClearChannel failed before import")
            return (False, False) if verify else False
        time.sleep(0.05)
        if not self._game_process_alive():
            print(f"[{label}] game exited before import")
            return (False, False) if verify else False
        print(f"[{label}] ImportChannel one-shot ({len(atlas_blob)} bytes)")
        native_ok = self._call_import_channel_bytes(comp, atlas_blob, self.EPaintChannel_Albedo)
        verified = False
        if native_ok:
            time.sleep(0.08)
            if verify:
                if self._camo_aborted():
                    native_ok = False
                else:
                    verified = self._verify_import_rt_opaque(comp, label=label)
                    if not verified:
                        print(
                            f"[{label}] export verify unavailable while frozen — "
                            "trusting native import (skipping UV stamp fallback)",
                            flush=True,
                        )
            else:
                print(f"[{label}] import complete — RT verify skipped")
        ok = native_ok
        if progress_cb:
            try:
                progress_cb(100, 100)
            except Exception:
                pass
        if verify:
            return ok, verified
        return ok

    def _call_request_texture_sync(self, comp, label="PAINT", timeout_ms=12000):
        """
        DISABLED — RequestFullTextureSync / ApplyToMaterial (0x50FCDB0 / 0x5103270)
        cannot be called from an external injector thread. Doing so causes delayed
        EXCEPTION_ACCESS_VIOLATION crashes in the UE render/material pipeline.
        Visible paint must go through PaintAtUV + EndStroke instead.
        """
        del comp, label, timeout_ms
        return False

    def _wait_paint_settle(self, comp, label="PAINT", settle_ms=350):
        """Brief pause so EndStroke / auto-flush can finish on the game thread."""
        self._mark_paint_channels_dirty(comp)
        import time
        print(f"[{label}] waiting for stroke flush ({settle_ms}ms)...")
        time.sleep(max(0.1, settle_ms / 1000.0))
        return True

    def _defer_paint_material_sync(self, comp, label="PAINT", settle_ms=350, sync=False):
        """Legacy alias — never calls material-sync natives."""
        del sync
        return self._wait_paint_settle(comp, label=label, settle_ms=settle_ms)

    def _call_apply_paint_to_material(self, comp, label="PAINT"):
        """Disabled — see _call_request_texture_sync."""
        del comp, label
        return False

    def _verify_paint_build_offsets(self):
        """Log dump vs live prologue checks for the current game build."""
        dump_globals = (
            ("GObjects", 0x09F3C6D0),
            ("GNames", 0x09E20078),
            ("GWorld", 0x09C85620),
            ("ProcessEvent", 0x015D0AD0),
        )
        workers = (
            ("PaintAtUV", self.RVA_PAINT_AT_UV_LEGACY),
            ("ClearChannel", self.RVA_CLEAR_CHANNEL_LEGACY),
            ("ExportChannel", self.RVA_EXPORT_CHANNEL_LEGACY),
            ("ImportDispatch", self.RVA_IMPORT_CHANNEL_LEGACY),
            ("ImportRT", self.RVA_IMPORT_RT_NATIVE),
            ("ApplyToMaterial", self.RVA_APPLY_PAINT_TO_MATERIAL),
            ("BeginStroke", self.RVA_BEGIN_STROKE_NATIVE),
            ("BeginStrokeLegacy", self.RVA_BEGIN_STROKE_LEGACY),
            ("EndStroke", self.RVA_END_STROKE_NATIVE),
            ("EndStrokeLegacy", self.RVA_END_STROKE_LEGACY),
        )
        parts = [f"{n}=0x{v:X}" for n, v in dump_globals]
        print(f"[PAINT] dump globals: {', '.join(parts)}")
        ok_n = sum(1 for _, rva in workers if self._native_rva_ok(rva))
        bad = [n for n, rva in workers if not self._native_rva_ok(rva)]
        print(f"[PAINT] worker prologues {ok_n}/{len(workers)} ok"
              + (f" FAILED: {', '.join(bad)}" if bad else ""))

    def _verify_import_on_comp(self, comp, expected_len, label="PAINT"):
        """After import, export albedo and log whether non-clear pixels came back."""
        blob = self._call_export_channel_bytes(comp, self.EPaintChannel_Albedo)
        if not blob:
            print(f"[{label}] post-import export failed — cannot verify RT")
            return False
        if len(blob) != expected_len:
            print(f"[{label}] post-import export size {len(blob)} (expected {expected_len})")
        opaque = sum(1 for i in range(0, min(len(blob), expected_len), 4) if blob[i + 3] > 8)
        print(f"[{label}] post-import export opaque-ish pixels~{opaque}/{expected_len // 4}")
        return opaque > 64

    def _resolve_channel_io_workers(self):
        """Resolve deep channel I/O workers (never call dump thunks directly)."""
        if self._channel_io_resolved:
            return self._export_worker_rva, self._import_worker_rva

        def _pick(fallback, anchor):
            if self._native_rva_ok(fallback):
                return fallback
            scanned = self._scan_deep_worker(anchor)
            return scanned if scanned else fallback

        self._clear_worker_rva = _pick(
            self.RVA_CLEAR_CHANNEL_LEGACY, self.RVA_CLEAR_CHANNEL_NATIVE,
        )
        self._export_worker_rva = _pick(
            self.RVA_EXPORT_CHANNEL_LEGACY, self.RVA_EXPORT_CHANNEL_NATIVE,
        )
        self._import_worker_rva = _pick(
            self.RVA_IMPORT_CHANNEL_LEGACY, self.RVA_EXEC_IMPORT_CHANNEL,
        )
        for label, rva in (
            ("clear", self._clear_worker_rva),
            ("export", self._export_worker_rva),
            ("import", self._import_worker_rva),
        ):
            if not self._native_rva_ok(rva):
                print(f"[PAINT] {label} worker 0x{rva:X} FAILED prologue check — abort I/O")
                self._clear_worker_rva = self._export_worker_rva = self._import_worker_rva = 0
                break

        self._channel_io_resolved = True
        print(f"[PAINT] export worker=0x{self._export_worker_rva:X} "
              f"import worker=0x{self._import_worker_rva:X} "
              f"clear worker=0x{self._clear_worker_rva:X}")
        return self._export_worker_rva, self._import_worker_rva

    def _call_export_channel_bytes(self, comp, channel=None, timeout_ms=60000):
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
            self._flush_remote_code(self._inject_handle(), mem_i, total)
        except Exception as e:
            print(f"[PAINT] export shellcode write failed: {e}")
            self._remote_free(self._inject_handle(), mem)
            return None

        inj = self._inject_handle()
        thread_ok = self._run_remote_shellcode(inj, mem_i, timeout_ms=timeout_ms)
        blob = None
        if thread_ok:
            try:
                data_ptr, count, _cap = struct.unpack(
                    "<QII", self.pm.read_bytes(tarray_ptr, 16),
                )
            except Exception:
                data_ptr, count = 0, 0
            if count and data_ptr:
                try:
                    blob = bytes(self.pm.read_bytes(data_ptr, count))
                except Exception as e:
                    print(f"[PAINT] export read failed: {e}")
        else:
            print("[PAINT] export thread failed or timed out — not freeing remote mem")
        if thread_ok:
            self._remote_free(inj, mem)
        if blob:
            print(f"[PAINT] exported {len(blob)} bytes (channel={channel})")
        return blob

    def _call_import_channel_bytes(self, comp, data, channel=None):
        """Import raw channel bytes via the native ImportChannelFromBytes worker."""
        if not data:
            return False
        channel = self.EPaintChannel_Albedo if channel is None else channel
        self._resolve_channel_io_workers()
        sx, sy = self._get_albedo_render_target_size(comp)
        expected = sx * sy * 4
        if expected > 0 and len(data) != expected:
            print(
                f"[PAINT] import size mismatch: got {len(data)} bytes, "
                f"RT expects {sx}x{sy} ({expected} bytes)"
            )
            return False
        base = self._module_base()
        if not base:
            return False
        inj = self._inject_handle()
        rt_ptr = rp(self.pm, comp + 0x0148)
        if not rt_ptr or rt_ptr <= 0x100000:
            print("[PAINT] import aborted — no AlbedoRenderTarget on component")
            return False

        data_mem = self._remote_alloc(inj, len(data))
        if not data_mem:
            return False
        data_i = int(data_mem)
        try:
            self.pm.write_bytes(data_i, data, len(data))
        except Exception as e:
            print(f"[PAINT] import data write failed: {e}")
            self._remote_free(inj, data_mem)
            return False

        SC_SIZE = 512
        tarray_off = SC_SIZE
        result_off = SC_SIZE + 16
        total = SC_SIZE + 24
        mem = self._remote_alloc(inj, total)
        if not mem:
            self._remote_free(inj, data_mem)
            return False
        mem_i = int(mem)
        tarray_ptr = mem_i + tarray_off
        result_ptr = mem_i + result_off
        tarray = struct.pack("<QII", data_i, len(data), len(data))

        def q(v):
            return struct.pack("<Q", v)

        dispatch_fn = base + self.RVA_IMPORT_CHANNEL_LEGACY

        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x20"
        sc += b"\x48\xB9" + q(comp)
        sc += bytes([0xB2, channel & 0xFF])
        sc += b"\x49\xB8" + q(tarray_ptr)
        sc += b"\x48\xB8" + q(dispatch_fn) + b"\xFF\xD0"
        sc += b"\x48\xB8" + q(result_ptr)
        sc += b"\x88\x00"
        sc += b"\x48\x83\xC4\x20\x48\x89\xEC\x5D\xC3"
        sc = sc.ljust(SC_SIZE, b"\x90")

        try:
            self.pm.write_bytes(mem_i, sc + tarray + b"\x00" * 8, total)
        except Exception as e:
            print(f"[PAINT] import shellcode write failed: {e}")
            self._remote_free(inj, mem)
            self._remote_free(inj, data_mem)
            return False

        thread_ok = self._run_remote_shellcode(inj, mem_i, timeout_ms=30000)
        native_ok = False
        if thread_ok:
            try:
                native_ok = bool(self.pm.read_bytes(result_ptr, 1)[0])
            except Exception:
                native_ok = False
        self._finish_remote_mem(inj, mem, thread_ok, "ImportChannel")
        if not thread_ok:
            self._remote_free(inj, data_mem)
            print("[PAINT] import thread failed")
            return False
        if not native_ok:
            self._remote_free(inj, data_mem)
            diag = ""
            try:
                rt_flags = struct.unpack("<I", self.pm.read_bytes(rt_ptr + 8, 4))[0]
                diag = f", RT+8=0x{rt_flags:08X}"
            except Exception:
                pass
            print(
                f"[PAINT] import rejected by game (native returned False, "
                f"RT={sx}x{sy}, bytes={len(data)}{diag})"
            )
            return False
        print(f"[PAINT] import {len(data)} bytes ok=True (RT={sx}x{sy})")
        self._remote_free(inj, data_mem)
        return True

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
            self._wait_paint_settle(comp)
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
                # Run the export WITHOUT a freeze — ExportChannelToBytes internally
                # dispatches to render/job threads.  Freezing would deadlock those
                # threads and crash the game with a WAIT_TIMEOUT.
                try:
                    comp = self._get_runtime_paint_component(pawn)
                    if comp:
                        self._prepare_paint_component(comp)
                        raw = self._call_export_channel_bytes(
                            comp, self.EPaintChannel_Albedo
                        )
                        if raw:
                            res_exp = self.get_albedo_resolution(comp)
                            if len(raw) == res_exp * res_exp * 4:
                                blob = bytes(raw)
                                resolution = res_exp
                                self._last_paint_bgra = blob
                                self._last_paint_resolution = resolution
                                print(f"[PAINT] saving exported paint ({len(blob)} bytes)")
                            else:
                                print(f"[PAINT] export size mismatch: {len(raw)} bytes, "
                                      f"expected {res_exp * res_exp * 4}")
                except Exception as e:
                    print(f"[PAINT] export failed during save: {e}")
                    blob = None

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

    def _calibrate_body_uv_vertical(self, pawn):
        """
        Returns (v_head, v_feet, u_front) for _build_front_back_image_uv_points,
        or None to fall back to full-range defaults.

        The previous implementation called the native HitTestAtScreenPosition
        worker (resolved at runtime via exec-thunk scan) to ray-cast screen points onto the paint mesh
        and read back UV coordinates.  That worker has a different ABI from what
        we assumed — it dereferences its 6th argument (passed as null) at
        offset +0x38, causing EXCEPTION_ACCESS_VIOLATION reading 0x38 every time,
        which kills the game before a single stamp is sent.

        Until the correct ABI is confirmed, we use the safe geometry-only path:
        read the character's root position, project the body AABB corners to
        screen space, and derive head/feet UV estimates from the UV border
        constants we already use for stamping.  No native calls → no crash.
        """
        try:
            cam = self.get_camera()
            sw, sh = self.get_viewport_size()
            if not cam or not sw or not sh:
                return None

            bbox = self.project_body_screen_bbox(pawn, cam, sw, sh)
            if not bbox:
                print("[PAINT] UV vertical calib skipped (body off screen)")
                return None

            # The paint atlas stamps use v in [PAINT_UV_BORDER, 1-PAINT_UV_BORDER].
            # Treat that range as [head, feet] — no native call needed.
            border = self.PAINT_UV_BORDER
            v_head = border
            v_feet = 1.0 - border
            u_front = self.PAINT_FRONT_U
            print(
                f"[PAINT] UV vertical calib (geometry)  "
                f"v_head={v_head:.3f}  v_feet={v_feet:.3f}  u_front={u_front:.3f}"
            )
            return v_head, v_feet, u_front
        except Exception as e:
            print(f"[PAINT] UV vertical calib error: {e}")
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
        wrap_mode="projector", v_head=None, v_feet=None,
    ):
        """
        Map the source image onto the character's UV atlas as a single
        continuous wrap (no duplicate copies on front and back).

        Paint-sphere UV convention:
          u∈[0,0.5)  front hemisphere        u∈[0.5,1]  back hemisphere
          u=0.25     chest centre            u=0.75     spine centre

        v_head / v_feet are calibrated UV v-coordinates for the character's
        head and feet obtained by pre-freeze camera hit-testing.  Using them:
          fv_img = (v - v_head) / (v_feet - v_head)
        correctly maps image_top→head and image_bottom→feet regardless of
        which direction the atlas uses for v (0=head or 0=feet).
        Falls back to full [0,1] range when not provided.

        wrap_mode="projector"  (front→back linear)
          fu_img = u
          Image left edge  → u=0  (side/armpit seam, front edge)
          Chest centre     → fu=0.25
          Spine centre     → fu=0.75
          Image right edge → u=1  (same armpit seam)

        wrap_mode="centered"  (chest-centred outward)
          fu_img = (u + 0.25) % 1.0
          Image centre (50%) lands exactly on the chest (u=0.25).
          The seam (image edges meeting) is at the spine centre (u=0.75).

        img_aspect=1.0 bypasses letterbox correction — the canvas was already
        stretched to square with Qt.IgnoreAspectRatio so direct (u,v) sampling
        covers the full image.
        """
        del layout
        grid = self.parse_grid_value(grid)
        border = self.PAINT_UV_BORDER
        points = []
        seen = set()

        # Calibrated vertical range — maps atlas v to image fraction [0,1].
        # (v_feet - v_head) is signed; the formula handles both v-directions.
        _v_head = v_head if v_head is not None else 0.0
        _v_feet = v_feet if v_feet is not None else 1.0
        _v_range = _v_feet - _v_head  # signed
        if abs(_v_range) < 0.05:     # degenerate — fall back to full range
            _v_head = 0.0
            _v_range = 1.0

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
            fv_img uses calibrated v_head/v_feet so the image top always maps
            to the character's head and the bottom to the feet, regardless of
            the atlas v-direction.
            """
            if wrap_mode == "centered":
                fu_img = (u + 0.25) % 1.0   # image centre on chest; seam at spine
            else:
                fu_img = u                   # image starts at front-side, ends at back
            fv_img = (v - _v_head) / _v_range
            fv_img = max(0.0, min(1.0, fv_img))
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

        cal_str = (
            f"v_head={_v_head:.3f} v_feet={_v_head + _v_range:.3f}"
            if (v_head is not None or v_feet is not None)
            else "v=default[0,1]"
        )
        print(
            f"[PAINT] wrap points={len(points)} grid={grid} "
            f"mode={wrap_mode} {cal_str} orig-aspect={img_aspect:.2f}"
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

    def _build_island_image_uv_points(
        self, bgra_bytes, resolution, grid, opacity,
        u0, v0, u1, v1, img_y0, img_y1, img_aspect=1.0,
    ):
        """
        Map an image vertical slice [img_y0, img_y1] onto a UV island rect [u0,v0]-[u1,v1].
        Used for centered mode where head/torso/legs occupy separate atlas islands.
        """
        points = []
        seen = set()
        img_y0 = max(0.0, min(1.0, img_y0))
        img_y1 = max(0.0, min(1.0, img_y1))
        if img_y1 <= img_y0 + 0.01:
            return points

        for gy in range(grid):
            for gx in range(grid):
                fu = (gx + 0.5) / grid
                fv_uv = (gy + 0.5) / grid
                u = u0 + fu * (u1 - u0)
                v = v0 + fv_uv * (v1 - v0)
                img_fy = img_y0 + fv_uv * (img_y1 - img_y0)
                rgb = self._sample_bgra_at_paint_frac(
                    bgra_bytes, resolution, fu, img_fy, opacity, img_aspect,
                )
                if not rgb:
                    continue
                key = (round(u * 2048), round(v * 2048))
                if key in seen:
                    continue
                seen.add(key)
                points.append((u, v, rgb))
        return points

    def _compose_centered_island_texture(
        self, bgra_bytes, img_res, atlas_res, opacity, img_aspect=1.0,
    ):
        """Build a full atlas BGRA buffer for ImportChannel (centered island layout)."""
        import math
        out = bytearray(atlas_res * atlas_res * 4)
        all_islands = list(self.PAINT_FRONT_ISLANDS) + list(self.PAINT_BACK_ISLANDS)
        pixels = 0
        for u0, v0, u1, v1, iy0, iy1 in all_islands:
            x0 = int(u0 * atlas_res)
            x1 = min(atlas_res, max(x0 + 1, int(math.ceil(u1 * atlas_res))))
            y0 = int(v0 * atlas_res)
            y1 = min(atlas_res, max(y0 + 1, int(math.ceil(v1 * atlas_res))))
            for py in range(y0, y1):
                v_atlas = (py + 0.5) / atlas_res
                fv_island = (v_atlas - v0) / max(1e-6, v1 - v0)
                img_fy = iy0 + fv_island * (iy1 - iy0)
                for px in range(x0, x1):
                    u_atlas = (px + 0.5) / atlas_res
                    fu_island = (u_atlas - u0) / max(1e-6, u1 - u0)
                    ix, iy = self._image_frac_from_paint_frac(
                        fu_island, img_fy, img_aspect,
                    )
                    sx = min(img_res - 1, max(0, int(ix * (img_res - 1))))
                    sy = min(img_res - 1, max(0, int(iy * (img_res - 1))))
                    off = (sy * img_res + sx) * 4
                    alpha = bgra_bytes[off + 3]
                    if alpha < self.MIN_PAINT_ALPHA:
                        continue
                    dst_a = int(alpha * (opacity / 255.0))
                    if dst_a < self.MIN_PAINT_ALPHA:
                        continue
                    scale = dst_a / 255.0
                    dst = (py * atlas_res + px) * 4
                    out[dst] = int(bgra_bytes[off] * scale)
                    out[dst + 1] = int(bgra_bytes[off + 1] * scale)
                    out[dst + 2] = int(bgra_bytes[off + 2] * scale)
                    out[dst + 3] = dst_a
                    pixels += 1
        print(f"[PAINT] composed centered atlas {atlas_res}x{atlas_res} pixels={pixels}")
        return bytes(out)

    def _compose_projector_wrap_texture(
        self, bgra_bytes, img_res, atlas_res, opacity, v_head=None, v_feet=None,
        img_aspect=1.0,
    ):
        """Build full atlas BGRA for projector wrap (matches _build_front_back_image_uv_points)."""
        _v_head = v_head if v_head is not None else 0.0
        _v_feet = v_feet if v_feet is not None else 1.0
        _v_range = _v_feet - _v_head
        if abs(_v_range) < 0.05:
            _v_head = 0.0
            _v_range = 1.0
        out = bytearray(atlas_res * atlas_res * 4)
        op_scale = opacity / 255.0
        pixels = 0
        for py in range(atlas_res):
            v = (py + 0.5) / atlas_res
            fv_img = max(0.0, min(1.0, (v - _v_head) / _v_range))
            for px in range(atlas_res):
                u = (px + 0.5) / atlas_res
                ix, iy = self._image_frac_from_paint_frac(u, fv_img, img_aspect)
                sx = min(img_res - 1, max(0, int(ix * (img_res - 1))))
                sy = min(img_res - 1, max(0, int(iy * (img_res - 1))))
                off = (sy * img_res + sx) * 4
                alpha = bgra_bytes[off + 3]
                if alpha < self.MIN_PAINT_ALPHA:
                    continue
                dst_a = int(alpha * op_scale)
                if dst_a < self.MIN_PAINT_ALPHA:
                    continue
                scale = dst_a / 255.0
                dst = (py * atlas_res + px) * 4
                out[dst] = int(bgra_bytes[off] * scale)
                out[dst + 1] = int(bgra_bytes[off + 1] * scale)
                out[dst + 2] = int(bgra_bytes[off + 2] * scale)
                out[dst + 3] = dst_a
                pixels += 1
        print(f"[PAINT] composed projector atlas {atlas_res}x{atlas_res} pixels={pixels}")
        return bytes(out)

    def _build_centered_island_uv_points(
        self, bgra_bytes, resolution, grid, opacity, img_aspect=1.0,
    ):
        """Build UV stamps for centered mode using diagnostic-calibrated island map."""
        points = []
        seen = set()
        for u0, v0, u1, v1, iy0, iy1 in self.PAINT_FRONT_ISLANDS:
            chunk = self._build_island_image_uv_points(
                bgra_bytes, resolution, grid, opacity,
                u0, v0, u1, v1, iy0, iy1, img_aspect=img_aspect,
            )
            for pt in chunk:
                key = (round(pt[0] * 2048), round(pt[1] * 2048))
                if key not in seen:
                    seen.add(key)
                    points.append(pt)
        for u0, v0, u1, v1, iy0, iy1 in self.PAINT_BACK_ISLANDS:
            chunk = self._build_island_image_uv_points(
                bgra_bytes, resolution, grid, opacity,
                u0, v0, u1, v1, iy0, iy1, img_aspect=img_aspect,
            )
            for pt in chunk:
                key = (round(pt[0] * 2048), round(pt[1] * 2048))
                if key not in seen:
                    seen.add(key)
                    points.append(pt)
        print(
            f"[PAINT] centered island points={len(points)} grid={grid} "
            f"front_islands={len(self.PAINT_FRONT_ISLANDS)} "
            f"back_islands={len(self.PAINT_BACK_ISLANDS)}"
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

    def _paint_image_centered(
        self, pawn, bgra_bytes, resolution, grid, opacity, progress_cb, img_aspect=1.0,
    ):
        """
        Screen-space paint centered on the character's visible body — no UV guessing.

        Front side:  PaintAtScreenPosition raycasts each screen point to the mesh UV,
                     so the image is perfectly centered on whatever face is visible.
                     No knowledge of the UV atlas layout is required.
        Back side:   UV paint at the known spine centre (u=0.75) using the same image.
                     Falls back to hardcoded UV extents — no hit-test calls that crash.

        _calibrate_uv_from_screen_rect / _hit_test_at_screen are intentionally NOT
        called here: the native HitTestAtScreenPosition worker has an ABI mismatch
        (dereferences its 6th arg at +0x38 when we pass null) → EXCEPTION_ACCESS_
        VIOLATION reading 0x38 → game crash.
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

        screen_worker = self._resolve_paint_screen_worker()
        if not screen_worker:
            print("[PAINT] PaintAtScreenPosition worker unavailable")
            return False

        # Screen-space grid is capped at 64 (4 096 raycasts).
        # PaintAtScreenPosition does a full raycast per point, so fewer stamps
        # are needed vs UV painting — the game's raycast handles UV naturally.
        # Using the full image-quality grid (128-256) packs 16k-65k calls into
        # one remote shellcode blob and times out after 5 s, hanging the UI.
        screen_grid = min(grid, 64)

        # Build a screen rect that fits the image aspect ratio inside the body bbox.
        body_rect = self._body_image_screen_rect(bbox, width_fill=0.52, height_fill=0.95)
        paint_rect = self._fit_screen_rect_to_image_aspect(body_rect, img_aspect)

        screen_points = self._build_screen_image_points(
            bgra_bytes, resolution, paint_rect, screen_grid, opacity, img_aspect=img_aspect,
        )
        if not screen_points:
            print("[PAINT] no opaque pixels in image")
            return False

        # Back face: UV paint centred at the spine (u=0.75) with fixed extents.
        vc    = self.PAINT_BODY_VC
        hu    = self.PAINT_BODY_HU
        hv    = self.PAINT_BODY_HV
        u_opp = self.PAINT_BACK_U
        opposite_points = self._build_uv_image_points_for_rect(
            bgra_bytes, resolution, grid, opacity, u_opp, vc, hu, hv,
            img_aspect=img_aspect,
        )

        radius   = min(0.5, 1.25 / screen_grid)
        brush_op = max(0.0, min(1.0, opacity / 255.0))
        base_color = self._opaque_image_average(bgra_bytes, resolution)
        total_stamps = len(screen_points) + len(opposite_points)

        def _report(done, total):
            if progress_cb:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass

        self._prepare_paint_component(comp)
        self._write_brush_settings(comp, radius, 1.0, brush_op)

        # Front: batched screen-space calls (PAINT_UV_BATCH_SAFE = 24 per remote
        # thread).  Each batch has its own 5-second timeout — no global hang.
        batch_sz = self.PAINT_UV_BATCH_SAFE
        ok_screen = True
        sent = 0
        for off in range(0, len(screen_points), batch_sz):
            chunk = screen_points[off:off + batch_sz]
            if not self._call_paint_at_screen(
                comp, mesh, pc, screen_worker, chunk, channel=4,
            ):
                print(f"[PAINT] screen batch failed at offset {off}")
                ok_screen = False
                break
            sent += len(chunk)
            _report(sent, total_stamps)

        print(
            f"[PAINT] screen({mesh_src}) ok={ok_screen} stamps={len(screen_points)} "
            f"grid={screen_grid} rect={int(paint_rect[2]-paint_rect[0])}"
            f"x{int(paint_rect[3]-paint_rect[1])}"
        )

        # Back: UV paint under freeze (safe — no hit-test)
        ok_uv = True
        if opposite_points:
            opp_region = (
                max(0.0, u_opp - hu), max(0.0, vc - hv),
                min(1.0, u_opp + hu), min(1.0, vc + hv),
            )
            with self._game_frozen("PAINT-BACK"):
                self._prepare_paint_component(comp)
                self._write_brush_settings(comp, radius, 1.0, brush_op)
                ok_uv = self._apply_uv_paint_points(
                    pawn, opposite_points, log_prefix="PAINT", replace=True,
                    brush_grid=grid, brush_opacity=brush_op, brush_hardness=1.0,
                    freeze=False, base_color=base_color,
                    base_regions=(opp_region,), comp=comp,
                )
            _report(total_stamps, total_stamps)
            print(f"[PAINT] back UV ok={ok_uv} stamps={len(opposite_points)}")

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
        with self._paint_live("PRESET"):
            self._prepare_paint_component(comp)
            imported = self._call_import_channel_bytes(
                comp, bgra_bytes, self.EPaintChannel_Albedo,
            )
            if imported:
                self._wait_paint_settle(comp, label="PRESET")
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

    # ──────────────────────────────────────────────────────────────────────────
    # UV Diagnostic — multiple overlay modes for ongoing atlas calibration
    # ──────────────────────────────────────────────────────────────────────────
    UV_DIAG_MODES = ("quadrants", "islands", "grid", "slices", "full")

    UV_DIAG_BATCH = 64
    UV_DIAG_FILL_SEG = 10
    UV_DIAG_ISLAND_SEG = 12
    UV_DIAG_BRUSH_OVERLAP = 0.90

    # Distinct fill colours per paint island (front then back, matches PAINT_*_ISLANDS order).
    UV_DIAG_ISLAND_COLORS = (
        (255,  60, 200),   # F1 head
        (  0, 200, 255),   # F2 chest
        (255, 140,   0),   # F3 leg L
        (160,  60, 255),   # F4 leg R
        (255, 120, 150),   # B1 head L
        (180, 255,   0),   # B2 head R
        (100, 180, 255),   # B3 spine
        (255, 210,   0),   # B4 back R
        (255, 255, 255),   # B5 leg L lower
        (  0, 230, 230),   # B6 leg R lower
    )
    UV_DIAG_ISLAND_NAMES = (
        "F-head", "F-chest", "F-legL", "F-legR",
        "B-headL", "B-headR", "B-spine", "B-backR", "B-legLl", "B-legRl",
    )
    UV_DIAG_SLICE_COLORS = (
        (255,  50, 150),   # image head third
        (  0, 180, 180),   # image torso third
        (255, 120,   0),   # image legs third
    )
    UV_DIAG_QUAD_COLORS = (
        (220,  40,  40),   # u0-0.5 v0-0.5  RED
        ( 40, 200,  40),   # u0.5-1  v0-0.5  GREEN
        ( 40,  80, 220),   # u0-0.5 v0.5-1  BLUE
        (220, 200,   0),   # u0.5-1  v0.5-1  YELLOW
    )

    @classmethod
    def _diag_fill_rect(cls, pts, u0, v0, u1, v1, color, seg=6):
        for i in range(seg):
            for j in range(seg):
                u = u0 + (u1 - u0) * (i + 0.5) / seg
                v = v0 + (v1 - v0) * (j + 0.5) / seg
                pts.append((u, v, color))

    @classmethod
    def _diag_stroke_rect(cls, pts, u0, v0, u1, v1, color, steps=10):
        for i in range(steps):
            t = (i + 0.5) / steps
            pts.append((u0 + t * (u1 - u0), v0, color))
            pts.append((u0 + t * (u1 - u0), v1, color))
            pts.append((u0, v0 + t * (v1 - v0), color))
            pts.append((u1, v0 + t * (v1 - v0), color))

    @classmethod
    def _diag_cross(cls, pts, u, v, color, arm=0.05):
        for du in (-arm, 0.0, arm):
            pts.append((u + du, v, color))
        for dv in (-arm, 0.0, arm):
            pts.append((u, v + dv, color))

    @classmethod
    def _diag_grid_lines(cls, pts, values=(0.25, 0.5, 0.75), steps=14):
        line_c = (255, 255, 255)
        for val in values:
            for i in range(steps):
                t = (i + 0.5) / steps
                pts.append((val, t, line_c))
                pts.append((t, val, line_c))

    @classmethod
    def _diag_slice_color(cls, iy0, iy1):
        mid = (iy0 + iy1) * 0.5
        if mid < 0.40:
            return cls.UV_DIAG_SLICE_COLORS[0]
        if mid < 0.66:
            return cls.UV_DIAG_SLICE_COLORS[1]
        return cls.UV_DIAG_SLICE_COLORS[2]

    def _build_uv_diagnostic_points(self, mode="full"):
        """Build stamp list for the requested UV diagnostic overlay mode."""
        mode = (mode or "full").lower()
        if mode not in self.UV_DIAG_MODES:
            mode = "full"
        pts = []
        border = self.PAINT_UV_BORDER

        if mode in ("quadrants", "full"):
            quads = (
                ((border, 0.5 - border), (border, 0.5 - border), self.UV_DIAG_QUAD_COLORS[0]),
                ((0.5 + border, 1.0 - border), (border, 0.5 - border), self.UV_DIAG_QUAD_COLORS[1]),
                ((border, 0.5 - border), (0.5 + border, 1.0 - border), self.UV_DIAG_QUAD_COLORS[2]),
                ((0.5 + border, 1.0 - border), (0.5 + border, 1.0 - border), self.UV_DIAG_QUAD_COLORS[3]),
            )
            for (u0, u1), (v0, v1), color in quads:
                self._diag_fill_rect(pts, u0, v0, u1, v1, color, seg=self.UV_DIAG_FILL_SEG)

        if mode == "grid":
            grid_n = 12
            for gy in range(grid_n):
                for gx in range(grid_n):
                    u = (gx + 0.5) / grid_n
                    v = (gy + 0.5) / grid_n
                    pts.append((
                        u, v,
                        (int(u * 255), int(v * 255), 128),
                    ))
            self._diag_grid_lines(pts)

        if mode in ("islands", "slices"):
            all_islands = list(self.PAINT_FRONT_ISLANDS) + list(self.PAINT_BACK_ISLANDS)
            for idx, island in enumerate(all_islands):
                u0, v0, u1, v1, iy0, iy1 = island
                if mode == "slices":
                    color = self._diag_slice_color(iy0, iy1)
                else:
                    color = self.UV_DIAG_ISLAND_COLORS[idx % len(self.UV_DIAG_ISLAND_COLORS)]
                self._diag_fill_rect(
                    pts, u0, v0, u1, v1, color, seg=self.UV_DIAG_ISLAND_SEG,
                )
                self._diag_stroke_rect(pts, u0, v0, u1, v1, (255, 255, 255), steps=12)
                uc = (u0 + u1) * 0.5
                vc = (v0 + v1) * 0.5
                self._diag_cross(pts, uc, vc, (0, 0, 0), arm=0.035)
                label = self.UV_DIAG_ISLAND_NAMES[idx] if idx < len(self.UV_DIAG_ISLAND_NAMES) else f"I{idx}"
                print(
                    f"[DIAG] island {label}  "
                    f"u=[{u0:.2f},{u1:.2f}] v=[{v0:.2f},{v1:.2f}] "
                    f"img_y=[{iy0:.2f},{iy1:.2f}]"
                )

        if mode == "full":
            # Quadrants already filled above — overlay white island borders only.
            all_islands = list(self.PAINT_FRONT_ISLANDS) + list(self.PAINT_BACK_ISLANDS)
            for idx, island in enumerate(all_islands):
                u0, v0, u1, v1, iy0, iy1 = island
                self._diag_stroke_rect(pts, u0, v0, u1, v1, (255, 255, 255), steps=14)
                label = self.UV_DIAG_ISLAND_NAMES[idx] if idx < len(self.UV_DIAG_ISLAND_NAMES) else f"I{idx}"
                print(
                    f"[DIAG] island {label}  "
                    f"u=[{u0:.2f},{u1:.2f}] v=[{v0:.2f},{v1:.2f}] "
                    f"img_y=[{iy0:.2f},{iy1:.2f}]"
                )

        if mode in ("quadrants", "full"):
            self._diag_cross(pts, 0.25, 0.50, (255, 255, 255), arm=0.05)
            self._diag_cross(pts, 0.75, 0.50, (0, 230, 230), arm=0.05)
            self._diag_cross(pts, 0.50, 0.25, (255, 255, 0), arm=0.04)
            self._diag_cross(pts, 0.50, 0.75, (255, 128, 0), arm=0.04)

        if mode in ("full", "grid"):
            self._diag_grid_lines(pts, steps=12)

        if mode == "slices":
            self._diag_grid_lines(pts, values=(0.34, 0.68), steps=12)

        print(f"[DIAG] mode={mode} stamps={len(pts)}")
        return pts

    def _call_paint_pattern_batched(self, comp, points, channel=4, batch=None, log_prefix="DIAG"):
        if batch is None:
            batch = (
                self.PAINT_UV_BATCH_SAFE if log_prefix in ("PAINT", "PRESET")
                else self.UV_DIAG_BATCH
            )
        total = len(points)
        n_batches = (total + batch - 1) // batch
        quiet = log_prefix in ("PAINT", "PRESET") and n_batches > 1
        for bi, off in enumerate(range(0, total, batch)):
            chunk = points[off:off + batch]
            if not self._call_paint_pattern(
                comp, chunk, channel=channel, quiet=quiet,
            ):
                print(f"[{log_prefix}] batch {bi + 1}/{n_batches} failed at offset {off}")
                return False
        print(f"[{log_prefix}] batched {total} stamps in {n_batches} calls (batch={batch})")
        return True

    def paint_uv_diagnostic(self, mode="full", progress_cb=None):
        """
        Paint a UV atlas diagnostic overlay onto the character.

        Modes (see uv_diag_mode in config):
          quadrants — 4 coloured atlas quadrants + reference crosses
          islands   — each centered-mode paint island in a unique colour
          grid      — 12×12 UV grid (R=u, G=v) + reference lines
          slices    — image head/torso/legs thirds shown on island rects
          full      — full-bright quadrants + white island border overlays (default)
        """
        pawn = self._find_local_pawn()
        if not pawn:
            print("[DIAG] no local pawn found")
            return False

        comp = rp(self.pm, pawn + 0x0B68)
        if not comp or comp <= 0x100000:
            print("[DIAG] no paint component at pawn+0x0B68")
            return False

        mode = (mode or "full").lower()
        if mode not in self.UV_DIAG_MODES:
            mode = "full"

        pts = self._build_uv_diagnostic_points(mode)
        if not pts:
            print("[DIAG] no diagnostic stamps generated")
            return False

        radius = self._paint_brush_radius_pixels(
            comp, self.UV_DIAG_FILL_SEG if mode != "grid" else 12,
            overlap=self.UV_DIAG_BRUSH_OVERLAP,
        )
        print(f"[DIAG] painting mode={mode} stamps={len(pts)} radius={radius:.1f}px")
        if progress_cb:
            progress_cb(0, f"UV diagnostic ({mode}): {len(pts)} stamps…")

        try:
            with self._game_frozen("DIAG"):
                live_comp = rp(self.pm, pawn + 0x0B68)
                if not live_comp or live_comp <= 0x100000:
                    print("[DIAG] paint component lost before apply")
                    return False
                self._prepare_paint_component(live_comp)
                self._call_clear_paint_channel(live_comp)
                self._write_brush_settings(live_comp, radius, 1.0, 1.0)
                ok = self._call_paint_pattern_batched(live_comp, pts, channel=4)
            if ok:
                print("[DIAG] done — screenshot front & back, check log for island coords")
                if progress_cb:
                    progress_cb(100, f"UV diagnostic ({mode}) done — screenshot now!")
            return ok
        except Exception as e:
            import traceback
            print(f"[DIAG] error: {e}")
            traceback.print_exc()
            return False

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
        import time
        mode_key = (wrap_mode or "projector").strip().lower()
        mode_label = self.image_wrap_mode_label(mode_key)
        print(f"[PAINT] Apply Image mode: {mode_label}  (wrap_mode={mode_key})")
        print(
            f"[PAINT] paint_image_bgra start grid={grid} "
            f"res={resolution} opacity={opacity} fast={fast_paint}"
        )
        pawn = pawn or self.wait_for_paintable_pawn()
        if not pawn:
            print("[PAINT] no paintable pawn")
            return False

        comp = self._get_runtime_paint_component(pawn)
        if not comp:
            print("[PAINT] no RuntimePaintableComponent")
            return False

        # Resolve native workers before painting — clears stale cached RVAs.
        self._paint_at_uv_worker_rva = None
        self._resolve_paint_at_uv_worker()
        self._resolve_channel_io_workers()
        self._verify_paint_build_offsets()

        atlas_res = self.get_albedo_resolution(comp, pawn=pawn)
        rt_w, rt_h = self._get_albedo_render_target_size(comp)
        if rt_w > 0 and rt_h > 0:
            if rt_w != rt_h:
                print(f"[PAINT] non-square RT {rt_w}x{rt_h} — using max dimension for atlas")
            atlas_res = max(rt_w, rt_h)

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

        # ── Centered mode: island-based UV mapping (diagnostic-calibrated) ─────
        # The UV atlas has separate islands for head, torso, and legs — not one
        # rectangle per side.  Diagnostic (Jun 2026) showed:
        #   front head  → GREEN zone  (u 0.5-1, v 0-0.5)
        #   front chest → BLUE zone   (u 0-0.5, v 0.5-1)
        #   front legs  → YELLOW/RED  (u 0.5-1 / 0-0.5, v 0.5-1)
        # Image is split into vertical thirds mapped onto each island.
        if wrap_mode == "centered":
            print(f"[PAINT] using Centered island path ({mode_label})")
            atlas_blob = self._compose_centered_island_texture(
                bgra_bytes, resolution, atlas_res, opacity, img_aspect=img_aspect,
            )

            ok = False
            applied_comp = 0
            uv_points = None
            try:
                with self._game_frozen("PAINT"):
                    live_comp = rp(self.pm, pawn + 0x0B68)
                    if not live_comp or live_comp <= 0x100000:
                        print("[PAINT] paint component lost before apply")
                        return False
                    if not self._validate_paint_ready(live_comp, pawn, label="PAINT"):
                        return False

                    print("[PAINT] apply method: ImportChannel (one-shot)")
                    ok = self._try_import_atlas(
                        live_comp, atlas_blob, label="PAINT", progress_cb=progress_cb,
                    )
                    if ok:
                        applied_comp = live_comp
                        print("[PAINT] centered import apply done")
                    elif uv_points is None:
                        uv_points = self._build_centered_island_uv_points(
                            bgra_bytes, resolution, grid, opacity, img_aspect=img_aspect,
                        )
                    if not ok and uv_points:
                        batch_sz = self.PAINT_UV_BATCH_SAFE
                        n_batches = (len(uv_points) + batch_sz - 1) // batch_sz
                        print(
                            f"[PAINT] apply method: UV stamps (fallback) "
                            f"{len(uv_points)} stamps ~{n_batches}×{batch_sz}"
                        )
                        self._call_clear_paint_channel(live_comp)
                        time.sleep(0.08)
                        if not self._probe_paint_uv(live_comp, label="PAINT"):
                            return False
                        ok = self._apply_uv_paint_points(
                            pawn, uv_points, log_prefix="PAINT", replace=True, brush_grid=grid,
                            progress_cb=progress_cb, freeze=False, base_color=base_color,
                            base_regions=self.PAINT_HEMI_RECTS,
                            comp=live_comp, fast_mode=False,
                            record_strokes=False, auto_flush=False,
                        )
                        if ok:
                            applied_comp = live_comp
                            time.sleep(0.15)
            finally:
                pass

            if ok and not self._game_process_alive():
                print("[PAINT] game exited during apply")
                return False
            if ok and applied_comp:
                if not self._post_unfreeze_paint_finalize(applied_comp, label="PAINT"):
                    print("[PAINT] import wrote RT but material did not update")
                    ok = False

            if ok:
                self._last_paint_bgra = bytes(bgra_bytes)
                self._last_paint_resolution = resolution
                self._last_paint_grid = grid
            return ok

        # ── Projector mode: UV wrap across the full atlas ──────────────────────
        print(f"[PAINT] using Projector full-atlas path ({mode_label})")
        # The image is stretched across u=[0,1] so it wraps continuously from the
        # front-side seam, over the chest, across the back, and back to the seam.
        uv_cal = self._calibrate_body_uv_vertical(pawn)
        v_head_cal = uv_cal[0] if uv_cal else None
        v_feet_cal = uv_cal[1] if uv_cal else None

        atlas_blob = self._compose_projector_wrap_texture(
            bgra_bytes, resolution, atlas_res, opacity,
            v_head=v_head_cal, v_feet=v_feet_cal, img_aspect=img_aspect,
        )

        ok = False
        applied_comp = 0
        try:
            with self._game_frozen("PAINT"):
                live_comp = rp(self.pm, pawn + 0x0B68)
                if not live_comp or live_comp <= 0x100000:
                    print("[PAINT] paint component lost before apply")
                    return False
                if not self._validate_paint_ready(live_comp, pawn, label="PAINT"):
                    return False

                print("[PAINT] apply method: ImportChannel (one-shot)")
                ok = self._try_import_atlas(
                    live_comp, atlas_blob, label="PAINT", progress_cb=progress_cb,
                )
                if ok:
                    applied_comp = live_comp
                if not ok:
                    uv_points = self._build_front_back_image_uv_points(
                        bgra_bytes, resolution, grid, opacity,
                        layout=layout, img_aspect=img_aspect, wrap_mode="projector",
                        v_head=v_head_cal, v_feet=v_feet_cal,
                    )
                    if not uv_points:
                        print("[PAINT] no opaque pixels in image")
                        return False
                    batch_sz = self.PAINT_UV_BATCH_SAFE
                    n_batches = (len(uv_points) + batch_sz - 1) // batch_sz
                    print(
                        f"[PAINT] apply method: UV stamps (fallback) "
                        f"{len(uv_points)} stamps ~{n_batches}×{batch_sz}"
                    )
                    self._call_clear_paint_channel(live_comp)
                    time.sleep(0.08)
                    if not self._probe_paint_uv(live_comp, label="PAINT"):
                        return False
                    ok = self._apply_uv_paint_points(
                        pawn, uv_points, log_prefix="PAINT", replace=True, brush_grid=grid,
                        progress_cb=progress_cb, freeze=False, base_color=base_color,
                        base_regions=paint_regions, comp=live_comp, fast_mode=False,
                        record_strokes=False, auto_flush=False,
                    )
                    if ok:
                        applied_comp = live_comp
                        time.sleep(0.15)
                        print("[PAINT] projector UV fallback done")
                else:
                    print("[PAINT] projector import apply done")
        finally:
            pass

        if ok and not self._game_process_alive():
            print("[PAINT] game exited during apply")
            return False
        if ok and applied_comp:
            if not self._post_unfreeze_paint_finalize(applied_comp, label="PAINT"):
                print("[PAINT] paint data written but material did not update")
                ok = False

        if ok:
            self._last_paint_bgra = bytes(atlas_blob) if atlas_blob else bytes(bgra_bytes)
            self._last_paint_resolution = atlas_res
            self._last_paint_grid = grid
            print(f"[PAINT] projector apply done ({grid}×{grid} grid setting)")
        return ok
