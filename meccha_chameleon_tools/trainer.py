"""Trainer features ported from Desktop/trainer with debug logging."""
import struct
import time
import traceback


def _rp(pm, addr):
    try:
        return struct.unpack("<Q", pm.read_bytes(addr, 8))[0]
    except Exception:
        return 0


def _ru32(pm, addr):
    try:
        return struct.unpack("<I", pm.read_bytes(addr, 4))[0]
    except Exception:
        return 0


def _rfloat(pm, addr):
    try:
        return struct.unpack("<f", pm.read_bytes(addr, 4))[0]
    except Exception:
        return 0.0


def _read_tarray_ptr(pm, addr):
    try:
        data = _rp(pm, addr)
        count = _ru32(pm, addr + 8)
        return data, count
    except Exception:
        return 0, 0


def _wfloat(pm, addr, value):
    try:
        pm.write_bytes(addr, struct.pack("<f", float(value)), 4)
        return True
    except Exception:
        return False


def _wdouble(pm, addr, value):
    try:
        pm.write_bytes(addr, struct.pack("<d", float(value)), 8)
        return True
    except Exception:
        return False


def _wint32(pm, addr, value):
    try:
        pm.write_bytes(addr, struct.pack("<i", int(value)), 4)
        return True
    except Exception:
        return False


def _wbyte(pm, addr, value):
    try:
        pm.write_bytes(addr, bytes([value & 0xFF]), 1)
        return True
    except Exception:
        return False


class TrainerMixin:
    """Memory-based trainer toggles (external Peterhack)."""

    # BP_FirstPersonCharacter_cLeon_Character_Hunter_C
    OFF_GUN_COOL_TIME = 0x0D20
    # ABP_FirstPersonCharacter_cLeon_Character_C
    OFF_DECOY_COOL_TIMES = 0x0CA0
    OFF_DECOY_COOL_DEFAULT = 0x0CB0
    OFF_RUNTIME_PAINTABLE = 0x0B68
    # URuntimePaintableComponent
    OFF_MAX_DECOY_SPAWN = 0x01A8
    # BP_FirstPersonCharacter_Main_C collision components
    OFF_OVERLAP_COLLISION = 0x0410
    OFF_BODY_MESH = 0x0418
    OFF_BODY_CAPSULE = 0x0420
    # UPrimitiveComponent::BodyInstance + FBodyInstance::CollisionEnabled
    OFF_BODY_INSTANCE = 0x0378
    OFF_COLLISION_ENABLED = 0x0017
    # AActor
    OFF_ACTOR_FLAGS = 0x005D
    ACTOR_COLLISION_BIT = 0x01
    # APlayerCameraManager
    OFF_CACHED_CAMERA_SHAKE = 0x2760
    # UCameraModifier
    OFF_CAM_MOD_ALPHA = 0x0040
    # APlayerState / online variant
    OFF_PLAYER_NAME = 0x0340
    OFF_CUSTOM_PLAYER_NAME = 0x0388
    # UPlayer::Connection (via APlayerController::Player @ resolver)
    OFF_UPLAYER_CONNECTION = 0x0028

    ECollision_NoCollision = 0
    ECollision_QueryAndPhysics = 3

    def _init_trainer_state(self):
        self._trainer_log_ts = {}
        self._trainer_errors = {}
        self._trainer_last_decoy_num = None
        self._trainer_last_rename = None
        self._trainer_last_rename_ts = 0.0
        self._trainer_anticlip_saved = None
        self._trainer_watch = {"world": 0, "pawn": 0, "ps": 0, "conn": 0}
        self._trainer_tick_count = 0

    def _trainer_enabled(self, config):
        return any((
            config.trainer_no_gun_cooldown,
            config.trainer_no_recoil,
            config.trainer_no_decoy_cooldown,
            config.trainer_set_decoy_num,
            config.trainer_anti_clipping,
            config.trainer_anti_kick,
            config.trainer_auto_rename,
        ))

    def _trainer_log(self, tag, msg, *, config=None, level="info", interval=3.0, force=False):
        if config is not None and not config.trainer_debug and level != "error":
            return
        key = f"{tag}:{level}:{msg[:72]}"
        now = time.monotonic()
        if not force and level != "error":
            last = self._trainer_log_ts.get(key, 0.0)
            if now - last < interval:
                return
        self._trainer_log_ts[key] = now
        prefix = "ERROR" if level == "error" else "DEBUG" if level == "debug" else "INFO"
        print(f"[TRAINER:{tag}] {prefix}: {msg}", flush=True)

    def _trainer_error(self, tag, exc, config=None):
        key = f"{tag}:{type(exc).__name__}"
        count = self._trainer_errors.get(key, 0) + 1
        self._trainer_errors[key] = count
        if count <= 5 or count % 120 == 0:
            self._trainer_log(
                tag,
                f"{type(exc).__name__}: {exc} (count={count})",
                config=config,
                level="error",
                force=True,
            )
            if count == 1 and config and config.trainer_debug:
                tb = traceback.format_exc()
                if tb and "NoneType: None" not in tb:
                    print(f"[TRAINER:{tag}] {tb}", flush=True)

    def _trainer_is_hunter(self, pawn):
        if not pawn:
            return False
        ih = self._read_is_hunter(pawn)
        if ih is True:
            return True
        cls = self.objects.class_name(pawn) or ""
        return "Hunter" in cls

    def _trainer_local_player_state(self, pawn):
        world = self._get_world()
        if not world:
            return 0
        pc = self._get_local_controller(world)
        if not pc:
            return 0
        ps = _rp(self.pm, pc + self.offsets.get("AController::PlayerState", 0x2B0))
        if ps and ps > 0x100000:
            return ps
        if pawn:
            ps2 = _rp(self.pm, pawn + self.offsets.get("APawn::PlayerState", 0x2C0))
            if ps2 and ps2 > 0x100000:
                return ps2
        return 0

    def _trainer_paintable_comp(self, pawn):
        if not pawn:
            return 0
        comp = _rp(self.pm, pawn + self.OFF_RUNTIME_PAINTABLE)
        if comp and comp > 0x100000:
            return comp
        return 0

    def _trainer_write_fstring(self, addr, text, max_len=32):
        text = (text or "").strip()[:max_len]
        if not text or not addr:
            return False
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        char_count = len(text) + 1
        data_ptr = _rp(self.pm, addr)
        arr_max = _ru32(self.pm, addr + 16)
        if data_ptr and data_ptr > 0x100000 and arr_max >= char_count:
            self.pm.write_bytes(data_ptr, encoded, len(encoded))
            _wint32(self.pm, addr + 8, char_count)
            return True
        new_mem = self._remote_alloc(self._inject_handle(), len(encoded))
        if not new_mem:
            return False
        try:
            self.pm.write_bytes(new_mem, encoded, len(encoded))
            self.pm.write_bytes(addr, struct.pack("<Q", new_mem), 8)
            _wint32(self.pm, addr + 8, char_count)
            _wint32(self.pm, addr + 16, char_count)
            return True
        except Exception:
            return False

    def _trainer_no_gun_cooldown(self, pawn, config):
        if not self._trainer_is_hunter(pawn):
            return
        addr = pawn + self.OFF_GUN_COOL_TIME
        try:
            cur = struct.unpack("<d", self.pm.read_bytes(addr, 8))[0]
        except Exception as exc:
            self._trainer_error("NO-CD", exc, config)
            return
        if cur != 0.0:
            if not _wdouble(self.pm, addr, 0.0):
                self._trainer_log("NO-CD", f"write failed @ 0x{addr:X}", config=config, level="error", force=True)
                return
            self._trainer_log("NO-CD", f"GunCoolTime {cur:.4f} -> 0.0", config=config)

    def _trainer_no_recoil(self, config):
        world = self._get_world()
        if not world:
            return
        pc = self._get_local_controller(world)
        if not pc:
            return
        cam_mgr = _rp(self.pm, pc + self.offsets["APlayerController::PlayerCameraManager"])
        if not cam_mgr:
            self._trainer_log("NO-RECOIL", "no PlayerCameraManager", config=config, level="debug", interval=8.0)
            return
        mod = _rp(self.pm, cam_mgr + self.OFF_CACHED_CAMERA_SHAKE)
        if not mod or mod <= 0x100000:
            self._trainer_log("NO-RECOIL", "CachedCameraShakeMod not ready", config=config, level="debug", interval=8.0)
            return
        try:
            alpha = _rfloat(self.pm, mod + self.OFF_CAM_MOD_ALPHA)
            if alpha != 0.0:
                if not _wfloat(self.pm, mod + self.OFF_CAM_MOD_ALPHA, 0.0):
                    self._trainer_log("NO-RECOIL", "Alpha write failed", config=config, level="error", force=True)
                    return
                self._trainer_log("NO-RECOIL", f"shake mod alpha {alpha:.3f} -> 0", config=config)
        except Exception as exc:
            self._trainer_error("NO-RECOIL", exc, config)

    def _trainer_no_decoy_cooldown(self, pawn, config):
        if not pawn:
            return
        try:
            _wdouble(self.pm, pawn + self.OFF_DECOY_COOL_DEFAULT, 0.0)
            data, count = _read_tarray_ptr(self.pm, pawn + self.OFF_DECOY_COOL_TIMES)
            if data and count > 0 and count < 64:
                for i in range(count):
                    _wdouble(self.pm, data + i * 8, 0.0)
            elif count == 0:
                self._trainer_log("DECOY-CD", "DecoyCoolTimes empty", config=config, level="debug", interval=10.0)
        except Exception as exc:
            self._trainer_error("DECOY-CD", exc, config)

    def _trainer_set_decoy_num(self, pawn, config):
        comp = self._trainer_paintable_comp(pawn)
        if not comp:
            self._trainer_log("DECOY-NUM", "no RuntimePaintable", config=config, level="debug", interval=8.0)
            return
        target = max(0, min(int(config.trainer_decoy_count), 99))
        if self._trainer_last_decoy_num == target:
            return
        addr = comp + self.OFF_MAX_DECOY_SPAWN
        try:
            cur = _ru32(self.pm, addr)
            if cur == target:
                self._trainer_last_decoy_num = target
                return
            if not _wint32(self.pm, addr, target):
                self._trainer_log("DECOY-NUM", f"write failed @ 0x{addr:X}", config=config, level="error", force=True)
                return
            self._trainer_last_decoy_num = target
            self._trainer_log("DECOY-NUM", f"MaxDecoySpawnCount {cur} -> {target}", config=config, force=True)
        except Exception as exc:
            self._trainer_error("DECOY-NUM", exc, config)

    def _trainer_collision_off(self, comp):
        if not comp or comp <= 0x100000:
            return None
        col_addr = comp + self.OFF_BODY_INSTANCE + self.OFF_COLLISION_ENABLED
        try:
            prev = self.pm.read_bytes(col_addr, 1)[0]
            _wbyte(self.pm, col_addr, self.ECollision_NoCollision)
            return (comp, col_addr, prev)
        except Exception:
            return None

    def _trainer_collision_restore(self, saved):
        if not saved:
            return
        for comp, col_addr, prev in saved:
            try:
                _wbyte(self.pm, col_addr, prev)
            except Exception:
                pass

    def _trainer_anti_clipping(self, pawn, config, enable):
        if enable:
            if self._trainer_anticlip_saved is not None:
                return
            saved = []
            for off, label in (
                (self.OFF_BODY_CAPSULE, "capsule"),
                (self.OFF_BODY_MESH, "mesh"),
                (self.OFF_OVERLAP_COLLISION, "overlap"),
            ):
                comp = _rp(self.pm, pawn + off) if pawn else 0
                entry = self._trainer_collision_off(comp)
                if entry:
                    saved.append(entry)
            actor_flag = None
            if pawn:
                try:
                    flag_addr = pawn + self.OFF_ACTOR_FLAGS
                    b = self.pm.read_bytes(flag_addr, 1)[0]
                    actor_flag = (flag_addr, b)
                    _wbyte(self.pm, flag_addr, b & ~self.ACTOR_COLLISION_BIT)
                except Exception:
                    actor_flag = None
            self._trainer_anticlip_saved = {"cols": saved, "actor": actor_flag}
            self._trainer_log(
                "NO-CLIP",
                f"disabled collision on {len(saved)} component(s)",
                config=config,
                force=True,
            )
        else:
            if self._trainer_anticlip_saved is None:
                return
            snap = self._trainer_anticlip_saved
            self._trainer_collision_restore(snap.get("cols"))
            actor = snap.get("actor")
            if actor:
                flag_addr, prev = actor
                try:
                    _wbyte(self.pm, flag_addr, prev)
                except Exception:
                    pass
            self._trainer_anticlip_saved = None
            self._trainer_log("NO-CLIP", "restored collision", config=config, force=True)

    def _trainer_anti_kick(self, config):
        world = self._get_world()
        pawn = self._find_local_pawn() if world else 0
        ps = self._trainer_local_player_state(pawn) if pawn else 0
        conn = 0
        if world:
            pc = self._get_local_controller(world)
            if pc:
                player = _rp(self.pm, pc + self.offsets.get("APlayerController::Player", 0x0348))
                if player:
                    conn = _rp(self.pm, player + self.OFF_UPLAYER_CONNECTION)
        prev = self._trainer_watch
        if prev["world"] and not world:
            self._trainer_log(
                "ANTI-KICK",
                "world lost — possible kick/disconnect (external tool cannot block NetClient Kick RPC)",
                config=config,
                level="error",
                force=True,
            )
        if prev["conn"] and not conn and world:
            self._trainer_log(
                "ANTI-KICK",
                "NetConnection lost while world active",
                config=config,
                level="error",
                force=True,
            )
        if prev["pawn"] and not pawn and world:
            self._trainer_log("ANTI-KICK", "local pawn lost", config=config, level="error", force=True)
        self._trainer_watch = {"world": world, "pawn": pawn, "ps": ps, "conn": conn}

    def _trainer_auto_rename(self, pawn, config):
        ps = self._trainer_local_player_state(pawn)
        if not ps:
            self._trainer_log("RENAME", "no PlayerState", config=config, level="debug", interval=10.0)
            return
        name = (config.trainer_rename_text or "").strip()
        if not name:
            return
        now = time.monotonic()
        if name == self._trainer_last_rename and (now - self._trainer_last_rename_ts) < 5.0:
            return
        cur = self.get_player_name(ps)
        if cur == name and self._trainer_last_rename == name:
            return
        ok_private = self._trainer_write_fstring(ps + self.OFF_PLAYER_NAME, name)
        ok_custom = self._trainer_write_fstring(ps + self.OFF_CUSTOM_PLAYER_NAME, name)
        if ok_private or ok_custom:
            self._trainer_last_rename = name
            self._trainer_last_rename_ts = now
            self._trainer_log(
                "RENAME",
                f"'{cur or '?'}' -> '{name}' (private={ok_private} custom={ok_custom})",
                config=config,
                force=True,
            )
        else:
            self._trainer_log("RENAME", "FString write failed", config=config, level="error", force=True)

    def tick_trainer(self, config):
        """Apply enabled trainer features once per overlay frame."""
        if not config or not self._trainer_enabled(config):
            if self._trainer_anticlip_saved is not None:
                self._trainer_anti_clipping(0, config, False)
            return
        self._trainer_tick_count += 1
        try:
            if not self._game_process_alive():
                self._trainer_log("TICK", "game process not alive", config=config, level="debug", interval=5.0)
                return
        except Exception as exc:
            self._trainer_error("TICK", exc, config)
            return

        pawn = self._find_local_pawn()
        if config.trainer_anti_clipping:
            self._trainer_anti_clipping(pawn, config, True)
        elif self._trainer_anticlip_saved is not None:
            self._trainer_anti_clipping(pawn, config, False)

        if not pawn:
            if self._trainer_tick_count % 180 == 1:
                self._trainer_log("TICK", "no local pawn (not in match?)", config=config, level="debug")
            if config.trainer_anti_kick:
                self._trainer_anti_kick(config)
            return

        try:
            if config.trainer_no_gun_cooldown:
                self._trainer_no_gun_cooldown(pawn, config)
            if config.trainer_no_recoil:
                self._trainer_no_recoil(config)
            if config.trainer_no_decoy_cooldown:
                self._trainer_no_decoy_cooldown(pawn, config)
            if config.trainer_set_decoy_num:
                self._trainer_set_decoy_num(pawn, config)
            elif self._trainer_last_decoy_num is not None:
                self._trainer_last_decoy_num = None
            if config.trainer_auto_rename:
                self._trainer_auto_rename(pawn, config)
            if config.trainer_anti_kick:
                self._trainer_anti_kick(config)
        except Exception as exc:
            self._trainer_error("TICK", exc, config)
