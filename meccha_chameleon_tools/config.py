#!/usr/bin/env python3
"""Config dataclass with JSON save/load persistence."""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Tuple, List

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esp_config.json")


@dataclass
class Config:
    # ESP basics
    enabled: bool = True
    dot_esp: bool = True
    box_esp: bool = False
    corner_box: bool = False
    skeleton_esp: bool = False
    show_local: bool = True
    show_names: bool = True
    show_steam_id: bool = False
    show_distance: bool = True
    snap_lines: bool = True
    team_filter: bool = False
    enemy_only: bool = False
    show_roles: bool = True
    oof_arrow_radius: int = 0   # 0 = screen edge; otherwise px from center
    oof_show_names: bool = True
    oof_show_distance: bool = True
    oof_show_health: bool = False

    # Colors
    enemy_color: Tuple[int, int, int] = (255, 0, 0)
    local_color: Tuple[int, int, int] = (0, 255, 0)
    hunter_color: Tuple[int, int, int] = (255, 0, 0)
    survivor_color: Tuple[int, int, int] = (0, 255, 0)
    skeleton_color: Tuple[int, int, int] = (0, 255, 255)
    box_color: Tuple[int, int, int] = (255, 255, 255)
    radar_color: Tuple[int, int, int] = (255, 255, 255)
    visible_color: Tuple[int, int, int] = (0, 255, 0)
    not_visible_color: Tuple[int, int, int] = (128, 0, 128)

    # Sizing
    dot_radius: int = 8
    box_height_world: float = 100.0
    box_y_offset: int = 0
    esp_screen_y_offset: int = 28  # extra pixels down on screen (positive = lower)

    # Distance scaling
    distance_scaling: bool = True
    scale_reference_dist: float = 1500.0

    # Health bar
    health_bar: bool = True
    shield_bar: bool = True

    # Aimbot
    aimbot_enabled: bool = False
    aimbot_key: str = "MB5"
    aimbot_fov: int = 150
    aimbot_smooth: float = 0.30
    aimbot_target_offset: float = 60.0
    aimbot_show_fov: bool = True
    aimbot_visible_check: bool = False

    # Exploits / trainer toggles
    trainer_debug: bool = True
    trainer_no_gun_cooldown: bool = False
    trainer_no_recoil: bool = False
    trainer_no_decoy_cooldown: bool = False
    trainer_set_decoy_num: bool = False
    trainer_decoy_count: int = 5
    trainer_anti_clipping: bool = False
    trainer_anti_detection: bool = False
    trainer_infinite_bullets: bool = False
    trainer_magnet_key: str = "G"
    trainer_anti_kick: bool = False
    trainer_auto_rename: bool = False
    trainer_rename_text: str = "Player"
    autokick_enabled: bool = False
    autokick_leave_on_block: bool = True

    # Radar
    radar_enabled: bool = False
    radar_size: int = 180
    radar_range: float = 5000.0
    radar_opacity: int = 160

    # Camouflage / Paint
    camouflage_enabled: bool = True
    auto_update: bool = True  # check GitHub main on startup and apply newer source
    discord_webhook_url: str = ""  # optional override; else DEFAULT_WEBHOOK_URL in webhook.py
    camouflage_sample_size: int = 32   # legacy — now driven by paint_quality
    camouflage_quality: int = 2        # legacy — now driven by paint_quality
    camouflage_opacity: int = 200
    camouflage_hide_local_body: bool = True  # hide local mesh during screen sampling
    camo_full_body_wrap: bool = True         # always 360° wrap (left/right/front/back)
    camo_skip_front_pass: bool = False       # skip front paint pass (flat maps only)
    camo_back_pass_only: bool = False        # paint only the back orbit pass
    paint_image_path: str = ""
    paint_image_opacity: int = 255
    paint_image_grid: int = 32
    preset_paint_grid: int = 32
    # Quality level 1-5 for F10 Camouflage (grid size / samples per cell).
    paint_quality: int = 12             # camo quality 1-20; 12=High+
    # Quality level 1-5 for Apply Image to Character (stamp grid density).
    image_quality: int = 3
    # Wrap mode for Apply Image to Character.
    # "projector" → image starts at front-side, finishes at back (fu = u).
    # "centered"  → image center on chest, top→head, bottom→feet (fu = (u+0.25)%1).
    image_wrap_mode: str = "projector"
    # UV diagnostic overlay mode: quadrants | islands | grid | slices | full
    uv_diag_mode: str = "full"
    # Chameleon character skeleton (see chameleonEsp skeleton.hpp)
    bone_indices: dict = field(default_factory=lambda: {
        "head": 6, "neck_01": 5, "spine_03": 4,
        "spine_02": 3, "spine_01": 2, "pelvis": 1,
        "clavicle_l": 8, "upperarm_l": 9, "lowerarm_l": 10, "hand_l": 11,
        "clavicle_r": 13, "upperarm_r": 14, "lowerarm_r": 15, "hand_r": 16,
        "thigh_l": 18, "calf_l": 19, "foot_l": 21,
        "thigh_r": 23, "calf_r": 24, "foot_r": 26,
    })


_COLOR_KEYS = ("enemy_color", "local_color", "hunter_color", "survivor_color",
               "skeleton_color", "box_color", "radar_color",
               "visible_color", "not_visible_color")


def config_to_dict(config: Config) -> dict:
    d = asdict(config)
    for key in _COLOR_KEYS:
        d[key] = list(d[key])
    return d


def config_from_dict(d: dict) -> Config:
    import dataclasses
    for key in _COLOR_KEYS:
        if key in d and isinstance(d[key], list):
            d[key] = tuple(d[key])
    # Flatten bone_indices if stored as list of pairs
    if "bone_indices" in d and isinstance(d["bone_indices"], list):
        d["bone_indices"] = {k: v for k, v in d["bone_indices"]}
    # Strip unknown keys so stale JSON fields never crash the dataclass constructor.
    valid = {f.name for f in dataclasses.fields(Config)}
    d = {k: v for k, v in d.items() if k in valid}
    return Config(**d)


def save_config(config: Config, path: str = CONFIG_FILE):
    try:
        d = config_to_dict(config)
        with open(path, "w") as f:
            json.dump(d, f, indent=2)
        return True
    except Exception:
        return False


def load_config(path: str = CONFIG_FILE) -> Config:
    config = Config()
    if not os.path.exists(path):
        return config
    try:
        with open(path) as f:
            d = json.load(f)
        return config_from_dict(d)
    except Exception:
        return config