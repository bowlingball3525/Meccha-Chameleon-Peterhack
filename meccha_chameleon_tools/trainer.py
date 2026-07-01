"""In-game exploit toggles and ProcessEvent helpers with debug logging."""
import struct
import time
import traceback

_OFF_UOBJECT_OUTER = 0x20  # UObjectBase::OuterPrivate


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
    # APlayerState — Steam / platform replicated name (do not use for in-game rename)
    OFF_PLAYER_NAME_PRIVATE = 0x0340
    # CustomPlayerName — in-game display name the player chooses in lobby
    OFF_CUSTOM_PLAYER_NAME_ONLINE = 0x0388
    OFF_CUSTOM_PLAYER_NAME_LINK = 0x03F0
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
        self._ue_func_cache = {}
        self._autokick_last_attempt = {}
        self._autokick_leave_until = 0.0
        self._autokick_last_scan = 0.0
        self._trainer_last_tick_ts = 0.0
        self._cdo_cache = {}

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

    def _trainer_any_active(self, config):
        return self._trainer_enabled(config) or bool(getattr(config, "autokick_enabled", False))

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

    # ------------------------------------------------------------------
    # Autokick / blocklist (Redpoint KickPlayerController when host)
    # ------------------------------------------------------------------
    RVA_PROCESS_EVENT = 0x015D0950
    AUTOKICK_COOLDOWN_SEC = 12.0
    AUTOKICK_LEAVE_COOLDOWN_SEC = 45.0

    def _find_ue_function(self, owner_class_substr, func_name):
        key = f"{owner_class_substr}::{func_name}"
        cached = self._ue_func_cache.get(key)
        if cached:
            return cached
        func_meta = self.objects.find_class("Function")
        if not func_meta:
            return 0
        for obj in self.objects.iter_objects():
            if self.objects.obj_class(obj) != func_meta:
                continue
            if self.objects.obj_name(obj) != func_name:
                continue
            outer = _rp(self.pm, obj + _OFF_UOBJECT_OUTER)
            outer_name = self.objects.class_name(outer) if outer else ""
            if owner_class_substr in outer_name:
                self._ue_func_cache[key] = obj
                return obj
        return 0

    def _find_class_default_object(self, class_substr):
        cached = self._cdo_cache.get(class_substr)
        if cached:
            return cached
        for obj in self.objects.iter_objects():
            name = self.objects.obj_name(obj)
            if name.startswith("Default__") and class_substr in name:
                self._cdo_cache[class_substr] = obj
                return obj
        return 0

    def _process_event_call_out(self, caller_obj, ufunc, params_bytes, timeout_ms=8000):
        """ProcessEvent; returns the params struct read back from game memory."""
        if not caller_obj or not ufunc or not params_bytes:
            return None
        base = self._module_base()
        if not base:
            return None
        pe = base + self.RVA_PROCESS_EVENT
        inj = self._inject_handle()
        if not inj:
            return None

        params_mem = self._remote_alloc(inj, len(params_bytes))
        if not params_mem:
            return None

        def q(v):
            return struct.pack("<Q", v)

        try:
            self.pm.write_bytes(int(params_mem), params_bytes, len(params_bytes))
        except Exception:
            self._remote_free(inj, params_mem)
            return None

        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x20"
        sc += b"\x48\xB9" + q(int(caller_obj))
        sc += b"\x48\xBA" + q(int(ufunc))
        sc += b"\x49\xB8" + q(int(params_mem))
        sc += b"\x48\xB8" + q(int(pe)) + b"\xFF\xD0"
        sc += b"\x48\x83\xC4\x20\x48\x89\xEC\x5D\xC3"
        sc = sc.ljust(256, b"\x90")

        code_mem = self._remote_alloc(inj, len(sc))
        if not code_mem:
            self._remote_free(inj, params_mem)
            return None
        try:
            self.pm.write_bytes(int(code_mem), sc, len(sc))
            self._flush_remote_code(inj, int(code_mem), len(sc))
        except Exception:
            self._remote_free(inj, code_mem)
            self._remote_free(inj, params_mem)
            return None

        ok = self._run_remote_shellcode(inj, int(code_mem), timeout_ms=timeout_ms)
        out = None
        if ok:
            try:
                out = self.pm.read_bytes(int(params_mem), len(params_bytes))
            except Exception:
                out = None
        self._remote_free(inj, code_mem)
        self._remote_free(inj, params_mem)
        return out if ok else None

    def _process_event_call(self, caller_obj, ufunc, params_bytes, timeout_ms=8000):
        """Invoke UObject::ProcessEvent(caller, ufunc, params) in the game process."""
        return self._process_event_call_out(caller_obj, ufunc, params_bytes, timeout_ms) is not None

    def native_set_control_rotation(self, pitch_deg, yaw_deg, roll_deg):
        """Set controller view via ProcessEvent SetControlRotation."""
        world = self._get_world() if hasattr(self, "_get_world") else 0
        pc = self._get_local_controller(world) if world and hasattr(self, "_get_local_controller") else 0
        fn_cr = self._find_ue_function("Controller", "SetControlRotation")
        if not pc or not fn_cr:
            return False
        cr_params = struct.pack("<ddd", float(pitch_deg), float(yaw_deg), float(roll_deg))
        return self._process_event_call(pc, fn_cr, cr_params)

    def native_rotate_yaw_delta(self, yaw_delta_deg):
        """Rotate controller view yaw only (scene capture reads ControlRotation, not pawn)."""
        delta = float(yaw_delta_deg)
        if abs(delta) < 0.01:
            return True
        world = self._get_world() if hasattr(self, "_get_world") else 0
        pc = self._get_local_controller(world) if world and hasattr(self, "_get_local_controller") else 0
        fn_cr = self._find_ue_function("Controller", "SetControlRotation")
        if not pc or not fn_cr or not hasattr(self, "get_control_rotation"):
            return False
        rot = self.get_control_rotation()
        if not rot:
            return False
        pitch, yaw, roll = rot
        cr_params = struct.pack("<ddd", pitch, yaw + delta, roll)
        return self._process_event_call(pc, fn_cr, cr_params)

    def _kick_player_controller(self, target_pc, config=None):
        """Host-only Redpoint KickPlayerController."""
        if not target_pc:
            return False
        ufunc = self._find_ue_function("RedpointFrameworkBlueprintLibrary", "KickPlayerController")
        caller = self._find_class_default_object("RedpointFrameworkBlueprintLibrary")
        if not ufunc or not caller:
            self._trainer_log(
                "AUTOKICK", "KickPlayerController UFunction not found",
                config=config, level="debug", interval=30.0,
            )
            return False
        params = struct.pack("<Q", int(target_pc)) + (b"\x00" * 16) + (b"\x00" * 8)
        base = self._module_base()
        if not base:
            return False
        pe = base + self.RVA_PROCESS_EVENT
        inj = self._inject_handle()
        if not inj:
            return False
        params_mem = self._remote_alloc(inj, len(params))
        if not params_mem:
            return False

        def q(v):
            return struct.pack("<Q", v)

        try:
            self.pm.write_bytes(int(params_mem), params, len(params))
        except Exception:
            self._remote_free(inj, params_mem)
            return False

        sc = b"\x55\x48\x89\xE5\x48\x83\xE4\xF0\x48\x83\xEC\x20"
        sc += b"\x48\xB9" + q(int(caller))
        sc += b"\x48\xBA" + q(int(ufunc))
        sc += b"\x49\xB8" + q(int(params_mem))
        sc += b"\x48\xB8" + q(int(pe)) + b"\xFF\xD0"
        sc += b"\x48\x83\xC4\x20\x48\x89\xEC\x5D\xC3"
        sc = sc.ljust(256, b"\x90")
        code_mem = self._remote_alloc(inj, len(sc))
        if not code_mem:
            self._remote_free(inj, params_mem)
            return False
        try:
            self.pm.write_bytes(int(code_mem), sc, len(sc))
            self._flush_remote_code(inj, int(code_mem), len(sc))
        except Exception:
            self._remote_free(inj, code_mem)
            self._remote_free(inj, params_mem)
            return False

        thread_ok = self._run_remote_shellcode(inj, int(code_mem), timeout_ms=8000)
        kicked = False
        if thread_ok:
            try:
                kicked = self.pm.read_bytes(int(params_mem) + 0x18, 1)[0] != 0
            except Exception:
                kicked = False
        if thread_ok:
            self._remote_free(inj, code_mem)
            self._remote_free(inj, params_mem)
        self._trainer_log(
            "AUTOKICK",
            f"KickPlayerController pc=0x{target_pc:X} ok={kicked}",
            config=config,
            force=True,
        )
        return kicked

    def _leave_match(self, config=None):
        """Leave lobby/match when a blocked player is present (non-host fallback)."""
        now = time.monotonic()
        if now < self._autokick_leave_until:
            return False
        world = self._get_world()
        if not world:
            return False
        pc = self._get_local_controller(world)
        if not pc:
            return False
        ufunc = self._find_ue_function("PlayerController", "ClientReturnToMainMenuWithTextReason")
        if not ufunc:
            self._trainer_log(
                "AUTOKICK", "ClientReturnToMainMenuWithTextReason not found",
                config=config, level="error", force=True,
            )
            return False
        params = b"\x00" * 16
        ok = self._process_event_call(pc, ufunc, params)
        if ok:
            self._autokick_leave_until = now + self.AUTOKICK_LEAVE_COOLDOWN_SEC
            self._trainer_log("AUTOKICK", "leaving lobby (blocked player)", config=config, force=True)
        return ok

    def _trainer_autokick(self, config):
        now = time.monotonic()
        if now - self._autokick_last_scan < 2.5:
            return
        self._autokick_last_scan = now

        blocked = self.refresh_blocklist_cache()
        if not blocked:
            return
        for pdata in self.get_session_players():
            if pdata.get("is_local"):
                continue
            sid = (pdata.get("steam_id") or "").strip()
            if not sid or sid not in blocked:
                continue
            last = self._autokick_last_attempt.get(sid, 0.0)
            if now - last < self.AUTOKICK_COOLDOWN_SEC:
                continue
            self._autokick_last_attempt[sid] = now
            label = pdata.get("player_name") or pdata.get("steam_name") or sid
            ps = pdata.get("player_state", 0)
            pawn = pdata.get("pawn", 0)
            target_pc = self._get_player_controller(ps, pawn)
            kicked = self._kick_player_controller(target_pc, config=config) if target_pc else False
            if kicked:
                continue
            if getattr(config, "autokick_leave_on_block", True):
                self._leave_match(config=config)
            else:
                self._trainer_log(
                    "AUTOKICK",
                    f"blocked player in lobby: {label} ({sid}) — not host, kick skipped",
                    config=config,
                    level="debug",
                    interval=15.0,
                )

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
        off = self._custom_player_name_offset(ps)
        cur = self.get_custom_player_name(ps)
        if cur == name and self._trainer_last_rename == name:
            return
        ok = self._trainer_write_fstring(ps + off, name)
        if ok:
            self._trainer_last_rename = name
            self._trainer_last_rename_ts = now
            cls = self.objects.class_name(ps) or "?"
            self._trainer_log(
                "RENAME",
                f"CustomPlayerName @0x{off:X} '{cur or '?'}' -> '{name}' ({cls})",
                config=config,
                force=True,
            )
        else:
            self._trainer_log("RENAME", "CustomPlayerName FString write failed", config=config, level="error", force=True)

    def tick_trainer(self, config):
        """Apply enabled trainer features (throttled — not every overlay frame)."""
        if not config or not self._trainer_any_active(config):
            if self._trainer_anticlip_saved is not None:
                self._trainer_anti_clipping(0, config, False)
            return

        now = time.monotonic()
        if now - self._trainer_last_tick_ts < 0.05:
            return
        self._trainer_last_tick_ts = now

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
            if getattr(config, "autokick_enabled", False):
                self._trainer_autokick(config)
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
            if getattr(config, "autokick_enabled", False):
                self._trainer_autokick(config)
        except Exception as exc:
            self._trainer_error("TICK", exc, config)
