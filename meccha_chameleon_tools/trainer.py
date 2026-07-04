"""In-game exploit toggles and ProcessEvent helpers with debug logging."""
import math
import struct
import threading
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
    OFF_INFINITY_BULLET = 0x0D94
    OFF_MY_PLAYER_STATE = 0x0808
    OFF_IS_HUNTER = 0x0C3A
    # BP_FirstPersonCharacter_cLeon_Character_Survivor_C
    OFF_OVERLAP_CHECK_CAPSULES = 0x0CE0
    OFF_UFUNCTION_PARMS_SIZE = 0x58
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
    OFF_USTRUCT_CHILDREN = 0x48
    OFF_UFIELD_NEXT = 0x28
    OFF_USTRUCT_SUPER = 0x40

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
        self._trainer_last_playable_pawn = 0
        self._cdo_cache = {}
        self._anti_kick_bridge_state = None
        self._anti_kick_bridge_last_attempt = 0.0
        self._anti_kick_blocks_reported = 0
        self._anti_kick_log_seq = 0
        self._anti_kick_watch_pc = 0
        self._anti_kick_watch_ps = 0
        self._anti_kick_watch_conn = 0
        self._anti_kick_watch_pawn = 0
        self._anti_kick_blocked_count = 0
        self._anti_kick_reported_blocked = None
        self._anti_kick_refresh_key = None
        self._anti_kick_partial_retry_key = None
        self._anti_kick_spawn_stable_ts = 0.0
        self._anti_kick_spawn_stable_key = None
        self._anti_kick_defer_log_ts = 0.0
        self._anti_kick_refresh_last = 0.0
        self._magnet_active = False
        self._magnet_key_was_down = False
        self._magnet_hotkey_native = False
        self._rename_lock = threading.Lock()
        self._rename_pending = None
        self._rename_worker_alive = False
        self._rename_notify = None
        self._rename_last_send_ts = 0.0
        self._rename_min_interval = 0.45
        self._trainer_auto_rename_queued_for = None
        self._trainer_rename_bridge_timeout = 10
        self._trainer_loop_started = False
        self._trainer_loop_stop = None
        self._anti_kick_sync_thread = None
        self._anti_kick_sync_pending = None

    def start_trainer_loop(self, config_getter):
        """Run trainer tick on a background thread so overlay paint never blocks."""
        if getattr(self, "_trainer_loop_started", False):
            return
        self._trainer_loop_started = True
        self._trainer_loop_stop = threading.Event()

        def _loop():
            while not self._trainer_loop_stop.is_set():
                try:
                    cfg = config_getter() if callable(config_getter) else config_getter
                    if cfg:
                        self.tick_trainer(cfg)
                except Exception as exc:
                    now_loop = time.monotonic()
                    last_ts = getattr(self, "_trainer_loop_exc_ts", 0.0)
                    last_msg = getattr(self, "_trainer_loop_exc_msg", "")
                    msg = str(exc)
                    if msg != last_msg or now_loop - last_ts >= 5.0:
                        self._trainer_loop_exc_ts = now_loop
                        self._trainer_loop_exc_msg = msg
                        print(f"[TRAINER:LOOP] {exc}", flush=True)
                self._trainer_loop_stop.wait(0.05)

        threading.Thread(target=_loop, daemon=True, name="trainer-loop").start()

    def stop_trainer_loop(self):
        stop = getattr(self, "_trainer_loop_stop", None)
        if stop:
            stop.set()

    def _schedule_anti_kick_bridge_sync(self, enabled, config):
        """Bridge RPCs can take seconds — never run them on the Qt UI thread."""
        key = (bool(enabled),)
        thread = getattr(self, "_anti_kick_sync_thread", None)
        if thread and thread.is_alive() and getattr(self, "_anti_kick_sync_pending", None) == key:
            return
        self._anti_kick_sync_pending = key

        def _worker():
            try:
                self._sync_anti_kick_bridge(enabled, config)
            finally:
                self._anti_kick_sync_pending = None

        self._anti_kick_sync_thread = threading.Thread(
            target=_worker, daemon=True, name="anti-kick-sync",
        )
        self._anti_kick_sync_thread.start()

    def set_rename_notify(self, callback):
        """Optional callable(ok, msg) after a queued rename finishes (may run on worker thread)."""
        self._rename_notify = callback

    def queue_trainer_rename(self, name, config=None, force=False):
        """Queue a rename on a background thread; latest name wins (never blocks UI)."""
        name = (name or "").strip()[:32]
        if not name:
            return False
        if config is None:
            config = getattr(self, "config", None)
        with self._rename_lock:
            self._rename_pending = (name, bool(force), config)
            if not self._rename_worker_alive:
                self._rename_worker_alive = True
                threading.Thread(target=self._rename_worker_loop, daemon=True).start()
        return True

    def _rename_worker_loop(self):
        try:
            while True:
                with self._rename_lock:
                    if not self._rename_pending:
                        break
                    name, force, config = self._rename_pending
                    self._rename_pending = None

                now = time.monotonic()
                wait = self._rename_min_interval - (now - self._rename_last_send_ts)
                if wait > 0:
                    time.sleep(wait)
                    with self._rename_lock:
                        if self._rename_pending:
                            continue

                pawn = self._find_local_pawn()
                ok, msg = self._trainer_apply_rename(pawn, config, name, force=force)
                self._rename_last_send_ts = time.monotonic()

                notify = getattr(self, "_rename_notify", None)
                if notify:
                    try:
                        notify(ok, msg or ("Renamed." if ok else "Rename failed."))
                    except Exception:
                        pass

                with self._rename_lock:
                    if not self._rename_pending:
                        time.sleep(0.05)
                        if not self._rename_pending:
                            break
        finally:
            with self._rename_lock:
                if self._rename_pending:
                    self._rename_worker_alive = True
                    threading.Thread(target=self._rename_worker_loop, daemon=True).start()
                else:
                    self._rename_worker_alive = False

    def _trainer_enabled(self, config):
        return any((
            config.trainer_no_gun_cooldown,
            config.trainer_no_recoil,
            config.trainer_no_decoy_cooldown,
            config.trainer_set_decoy_num,
            config.trainer_anti_clipping,
            config.trainer_anti_detection,
            config.trainer_infinite_bullets,
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

    def _trainer_is_playable_pawn(self, pawn):
        if not pawn or pawn <= 0x100000:
            return False
        cls = self.objects.class_name(pawn) or ""
        return self._is_player_class(cls)

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
            ready = 30.0
            _wdouble(self.pm, pawn + self.OFF_DECOY_COOL_DEFAULT, ready)
            data, count = _read_tarray_ptr(self.pm, pawn + self.OFF_DECOY_COOL_TIMES)
            if data and count > 0 and count < 64:
                for i in range(count):
                    _wdouble(self.pm, data + i * 8, ready)
            elif count == 0:
                self._trainer_log("DECOY-CD", "DecoyCoolTimes empty", config=config, level="debug", interval=10.0)
        except Exception as exc:
            self._trainer_error("DECOY-CD", exc, config)

    def _trainer_anti_detection(self, pawn, config):
        if not self._trainer_is_playable_pawn(pawn):
            return
        if self._read_is_hunter(pawn) is not False:
            return
        try:
            if _wint32(self.pm, pawn + self.OFF_OVERLAP_CHECK_CAPSULES + 8, 0):
                self._trainer_log(
                    "ANTI-DETECT",
                    "cleared OverlapCheckCapsules",
                    config=config,
                    level="debug",
                    interval=8.0,
                )
        except Exception as exc:
            self._trainer_error("ANTI-DETECT", exc, config)

    def _trainer_infinite_bullets(self, pawn, config):
        if not pawn or self._read_is_hunter(pawn) is not True:
            return
        try:
            raw = self.pm.read_bytes(pawn + self.OFF_INFINITY_BULLET, 1)[0]
            if raw == 0:
                _wbyte(self.pm, pawn + self.OFF_INFINITY_BULLET, 1)
                self._trainer_log("INF-BULLETS", "InfinityBullet -> true", config=config, level="debug", interval=8.0)
        except Exception as exc:
            self._trainer_error("INF-BULLETS", exc, config)

    @staticmethod
    def _forward_from_control_rotation(pitch_deg, yaw_deg):
        pr = math.radians(float(pitch_deg))
        yr = math.radians(float(yaw_deg))
        cp = math.cos(pr)
        return (
            math.cos(yr) * cp,
            math.sin(yr) * cp,
            math.sin(pr),
        )

    def _teleport_pawn_pe(self, pawn, x, y, z):
        if not pawn:
            return False
        fn, _ = self._find_ue_function_on_object(pawn, "K2_SetActorLocation")
        if not fn:
            return False
        psize = max(_ru32(self.pm, fn + self.OFF_UFUNCTION_PARMS_SIZE), 0x128)
        buf = bytearray(psize)
        struct.pack_into("<ddd", buf, 0, float(x), float(y), float(z))
        if psize > 24:
            buf[24] = 0
        if psize > 0x120:
            buf[0x120] = 1
        return self._process_event_call(pawn, fn, bytes(buf))

    def _game_hwnd_for_magnet(self):
        from meccha_chameleon_tools.core import MecchaESP
        return MecchaESP._find_game_window_hwnd()

    def toggle_magnet(self, config):
        self._magnet_active = not self._magnet_active
        self._trainer_log(
            "MAGNET",
            f"{'ON' if self._magnet_active else 'OFF'} (key {getattr(config, 'trainer_magnet_key', 'G')})",
            config=config,
            force=True,
        )

    def _trainer_magnet_key_toggle(self, config):
        try:
            if getattr(self, "_magnet_poll_ui", False):
                return
            from meccha_chameleon_tools.ui import vk_from_name
            import ctypes
            allowed = getattr(self, "_magnet_hotkey_allowed", None)
            if callable(allowed):
                if not allowed():
                    vk = vk_from_name(getattr(config, "trainer_magnet_key", "G"))
                    self._magnet_key_was_down = bool(
                        ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
                    )
                    return
            else:
                from meccha_chameleon_tools.ui import is_game_foreground
                game_hwnd = self._game_hwnd_for_magnet()
                if not is_game_foreground(game_hwnd):
                    vk = vk_from_name(getattr(config, "trainer_magnet_key", "G"))
                    self._magnet_key_was_down = bool(
                        ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000
                    )
                    return
            vk = vk_from_name(getattr(config, "trainer_magnet_key", "G"))
            down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not self._magnet_key_was_down:
                self.toggle_magnet(config)
            self._magnet_key_was_down = down
        except Exception as exc:
            self._trainer_error("MAGNET", exc, config)

    def _trainer_magnet(self, hunter_pawn, config):
        if not self._magnet_active or not hunter_pawn:
            return
        if self._read_is_hunter(hunter_pawn) is not True:
            return
        loc = self.get_actor_root_pos(hunter_pawn) if hasattr(self, "get_actor_root_pos") else None
        rot = self.get_control_rotation() if hasattr(self, "get_control_rotation") else None
        if not loc or not rot:
            return
        fx, fy, fz = self._forward_from_control_rotation(rot[0], rot[1])
        depth = 0
        try:
            players = self.get_session_players(force=False) if hasattr(self, "get_session_players") else []
            for pdata in players:
                if pdata.get("is_local"):
                    continue
                if pdata.get("is_hunter") is not False:
                    continue
                actor = pdata.get("pawn") or 0
                if not actor:
                    continue
                spread = depth * 120.0
                dist = 150.0 + spread
                tx = loc[0] + fx * dist
                ty = loc[1] + fy * dist
                tz = loc[2] + fz * dist
                self._teleport_pawn_pe(actor, tx, ty, tz)
                depth += 1
        except Exception as exc:
            self._trainer_error("MAGNET", exc, config)

    def kill_survivor_pawn(self, target_pawn, config=None):
        hunter = self._find_local_pawn()
        if not hunter or not target_pawn:
            return False, "no hunter or target pawn"
        if self._read_is_hunter(hunter) is not True:
            return False, "local player is not hunter"
        if self._read_is_hunter(target_pawn) is not False:
            return False, "target is not a survivor"
        fn, _ = self._find_ue_function_on_object(hunter, "KillPlayer")
        if not fn:
            fn = self._find_ue_function(
                "BP_FirstPersonCharacter_cLeon_Character_Hunter_C", "KillPlayer"
            )
        if not fn:
            return False, "KillPlayer UFunction not found"
        my_ps = _rp(self.pm, hunter + self.OFF_MY_PLAYER_STATE)
        if not my_ps:
            world = self._get_world() if hasattr(self, "_get_world") else 0
            pc = self._get_local_controller(world) if world else 0
            my_ps = _rp(self.pm, pc + self.offsets.get("AController::PlayerState", 0x2B0)) if pc else 0
        params = struct.pack("<QQ", int(target_pawn), int(my_ps))
        ok = self._process_event_call(hunter, fn, params)
        if config is not None:
            self._trainer_log(
                "KILL",
                f"KillPlayer target=0x{target_pawn:X} ok={ok}",
                config=config,
                force=True,
            )
        return ok, "ok" if ok else "ProcessEvent failed"

    def kill_all_survivors(self, config=None):
        killed = 0
        errors = []
        for pdata in self.get_session_players(force=False):
            if pdata.get("is_local"):
                continue
            if pdata.get("is_hunter") is not False:
                continue
            pawn = pdata.get("pawn") or 0
            if not pawn:
                continue
            ok, err = self.kill_survivor_pawn(pawn, config=config)
            if ok:
                killed += 1
            else:
                errors.append(err)
            time.sleep(0.05)
        return killed, errors

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
        want = bool(config.trainer_anti_kick)
        if not want:
            self._anti_kick_bridge_give_up = False
        elif getattr(self, "_anti_kick_bridge_give_up", False):
            return
        now = time.monotonic()
        world = self._get_world()
        pawn = self._find_local_pawn() if world else 0
        ps = self._trainer_local_player_state(pawn) if pawn else 0
        pc = self._get_local_controller(world) if world else 0
        conn = 0
        if world and pc:
            player = _rp(self.pm, pc + self.offsets.get("APlayerController::Player", 0x0348))
            if player:
                conn = _rp(self.pm, player + self.OFF_UPLAYER_CONNECTION)

        if pawn and conn and pc:
            stable_key = (pc, conn, pawn)
            if stable_key != getattr(self, "_anti_kick_spawn_stable_key", None):
                self._anti_kick_spawn_stable_key = stable_key
                self._anti_kick_spawn_stable_ts = now

        ANTI_KICK_SPAWN_STABLE_SEC = 5.0

        if getattr(self, "_anti_kick_bridge_state", None) != want:
            if want:
                if not pawn or not conn or not pc:
                    if now - getattr(self, "_anti_kick_defer_log_ts", 0.0) >= 12.0:
                        self._anti_kick_defer_log_ts = now
                        self._trainer_log(
                            "ANTI-KICK",
                            "waiting until spawned in before installing hook (avoids match-load crash)",
                            config=config,
                            level="debug",
                        )
                    return
                if now - getattr(self, "_anti_kick_spawn_stable_ts", 0.0) < ANTI_KICK_SPAWN_STABLE_SEC:
                    return
            if now - getattr(self, "_anti_kick_bridge_last_attempt", 0.0) >= 2.0:
                self._anti_kick_bridge_last_attempt = now
                self._schedule_anti_kick_bridge_sync(want, config)

        prev = self._trainer_watch
        if prev["world"] and not world:
            self._trainer_log(
                "ANTI-KICK",
                "world lost — disconnected (EOS/platform kick may bypass bridge hook)",
                config=config,
                level="error",
                force=True,
            )
        if prev["conn"] and not conn and world:
            blocks = int(getattr(self, "_anti_kick_log_seq", 0) or 0)
            blocked_n = int(getattr(self, "_anti_kick_blocked_count", 0) or 0)
            if blocks <= 0:
                if blocked_n <= 3:
                    detail = (
                        "no kick RPC was blocked — anti-kick had PARTIAL coverage "
                        f"(only {blocked_n} RPC(s) hooked; NetConnection.Close was likely not hooked). "
                        "Enable after spawning in and wait for auto-refresh, or toggle Anti-Kick off/on in match."
                    )
                else:
                    detail = (
                        "no kick RPC was blocked this session; likely EOS/lobby disconnect or transport drop "
                        "(anti-kick only blocks ProcessEvent RPCs, not platform session kicks)"
                    )
            else:
                detail = (
                    f"last blocked kick seq={blocks}; disconnect may have bypassed ClientWasKicked "
                    "(EOS/session drop or stale hook — restart game to reload bridge)"
                )
            self._trainer_log(
                "ANTI-KICK",
                "NetConnection lost while world active — " + detail,
                config=config,
                level="error",
                force=True,
            )
        if prev["pawn"] and not pawn and world:
            self._trainer_log("ANTI-KICK", "local pawn lost", config=config, level="error", force=True)
        self._trainer_watch = {"world": world, "pawn": pawn, "ps": ps, "conn": conn}
        if want:
            self._anti_kick_watch_pc = pc
            self._anti_kick_watch_ps = ps
            self._anti_kick_watch_conn = conn
            self._anti_kick_watch_pawn = pawn
        if want and getattr(self, "_anti_kick_bridge_state", False):
            self._poll_anti_kick_log(config)

    def _poll_anti_kick_log(self, config):
        if not hasattr(self, "_bridge_request"):
            return
        now = time.monotonic()
        if now - getattr(self, "_anti_kick_log_last_poll", 0.0) < 2.0:
            return
        self._anti_kick_log_last_poll = now
        try:
            resp = self._bridge_request(
                "get_anti_kick_log",
                {"since_seq": int(getattr(self, "_anti_kick_log_seq", 0))},
                timeout=3,
            )
        except Exception:
            return
        if not resp or not resp.get("success"):
            return
        meta = resp.get("metadata") or {}
        entries = meta.get("entries") or []
        for entry in entries:
            func = entry.get("function") or "?"
            owner = entry.get("owner") or "local"
            detail = (entry.get("detail") or "").strip()
            kicker = (entry.get("kicker") or entry.get("host") or "").strip()
            if not kicker and hasattr(self, "get_session_host_name"):
                kicker = (self.get_session_host_name() or "").strip()
            msg = f"blocked kick from {kicker or 'unknown host'} — {owner}.{func}"
            if detail:
                msg += f" — reason: {detail}"
            self._trainer_log("ANTI-KICK", msg, config=config, force=True)
        latest = meta.get("latest_seq")
        if latest is not None:
            self._anti_kick_log_seq = int(latest)

    def _sync_anti_kick_bridge(self, enabled, config):
        if not hasattr(self, "_bridge_request"):
            self._trainer_log(
                "ANTI-KICK",
                "bridge API unavailable — restart Peterhack",
                config=config,
                level="error",
                force=True,
            )
            return False
        if enabled:
            self._anti_kick_bridge_give_up = False
        if enabled and getattr(self, "_bridge_recover_give_up", False):
            if not self._ensure_bridge(force=True):
                err = getattr(self, "_camo_last_error", None) or "bridge not responding"
                if not getattr(self, "_anti_kick_bridge_give_up", False):
                    self._trainer_log(
                        "ANTI-KICK",
                        f"bridge unavailable — {err}",
                        config=config,
                        level="debug",
                    )
                    self._anti_kick_bridge_give_up = True
                return False
        elif enabled and not self._ensure_bridge():
            err = getattr(self, "_camo_last_error", None) or "bridge not ready"
            self._trainer_log("ANTI-KICK", f"waiting for bridge — {err}", config=config, level="debug")
            return False
        try:
            resp = self._bridge_request("set_anti_kick", {"enabled": bool(enabled)}, timeout=8)
        except Exception as exc:
            self._trainer_log("ANTI-KICK", f"bridge request failed: {exc}", config=config, level="error", force=True)
            return False
        ok = bool(resp and resp.get("success"))
        if ok:
            self._anti_kick_bridge_state = bool(enabled)
            meta = resp.get("metadata") or {}
            blocked = meta.get("blocked_functions", "?")
            try:
                self._anti_kick_blocked_count = int(blocked)
            except (TypeError, ValueError):
                self._anti_kick_blocked_count = 0
            hook_mode = meta.get("hook_mode") or ("chained" if meta.get("hook_chained") else "inline")
            monitored = meta.get("monitored_functions") or []
            net_conn = meta.get("net_connection")
            player_state = meta.get("player_state")
            extra = f" [{hook_mode}]" if hook_mode else ""
            msg = f"{'enabled' if enabled else 'disabled'} via bridge (blocked RPCs={blocked}{extra})"
            if enabled and net_conn and net_conn not in ("0x0", "0"):
                msg += f" conn={net_conn}"
            if enabled and monitored:
                preview = ", ".join(str(x) for x in monitored[:8])
                if len(monitored) > 8:
                    preview += f", +{len(monitored) - 8} more"
                msg += f" — watching: {preview}"
                msg += " — log: C:\\peterhack\\logs\\anti_kick.log"
            prev_reported = getattr(self, "_anti_kick_reported_blocked", None)
            if not enabled or prev_reported != self._anti_kick_blocked_count:
                if enabled:
                    self._anti_kick_reported_blocked = self._anti_kick_blocked_count
                self._trainer_log("ANTI-KICK", msg, config=config, force=True)
            if enabled and not getattr(self, "_anti_kick_bridge_state", False):
                self._anti_kick_log_seq = 0
        else:
            err = (resp or {}).get("message") or (resp or {}).get("stage") or "unknown error"
            if enabled:
                self._anti_kick_bridge_give_up = True
                self._anti_kick_bridge_state = False
                err = (
                    f"{err} — toggle Anti-Kick off/on to retry. "
                    "If this repeats, fully quit/relaunch the game or disable UE4SS/other ProcessEvent hooks."
                )
            self._trainer_log(
                "ANTI-KICK",
                f"bridge set_anti_kick failed: {err}",
                config=config,
                level="error",
                force=True,
            )
        return ok

    # ------------------------------------------------------------------
    # Autokick / blocklist (Redpoint KickPlayerController when host)
    # ------------------------------------------------------------------
    RVA_PROCESS_EVENT = 0x15D0B80
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
            outer_name = self.objects.obj_name(outer) if outer else ""
            if owner_class_substr in outer_name:
                self._ue_func_cache[key] = obj
                return obj
        return 0

    def _find_ue_function_on_object(self, obj, func_names):
        """Walk the instance class super chain and match UFunction children."""
        if isinstance(func_names, str):
            func_names = (func_names,)
        want = set(func_names)
        cls = self.objects.obj_class(obj) if obj else 0
        seen = set()
        while cls and cls not in seen:
            seen.add(cls)
            child = _rp(self.pm, cls + self.OFF_USTRUCT_CHILDREN)
            depth = 0
            while child and depth < 4096:
                name = self.objects.obj_name(child)
                if name in want:
                    return child, name
                child = _rp(self.pm, child + self.OFF_UFIELD_NEXT)
                depth += 1
            cls = _rp(self.pm, cls + self.OFF_USTRUCT_SUPER)
        return 0, ""

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
        ok, _msg = self.return_to_main_lobby(config=config, respect_cooldown=True)
        return ok

    def return_to_main_lobby(self, config=None, *, respect_cooldown=False):
        """Leave the current lobby/match and return to the main menu."""
        now = time.monotonic()
        if respect_cooldown and now < self._autokick_leave_until:
            return False, "leave on cooldown"
        world = self._get_world()
        if not world:
            return False, "not in game — join a lobby or match first"
        pc = self._get_local_controller(world)
        if not pc:
            return False, "no PlayerController"

        # Anti-kick hooks block ClientReturnToMainMenu* on the local controller.
        if getattr(self, "_anti_kick_bridge_state", False) and hasattr(self, "_bridge_request"):
            try:
                resp = self._bridge_request("set_anti_kick", {"enabled": False}, timeout=5)
                if resp and resp.get("success"):
                    self._anti_kick_bridge_state = False
                    self._trainer_log(
                        "LOBBY", "disabled anti-kick so return-to-menu can run",
                        config=config, level="debug",
                    )
            except Exception:
                pass

        for func_name in (
            "ClientReturnToMainMenuWithTextReason",
            "ClientReturnToMainMenu",
        ):
            ufunc = self._find_ue_function("PlayerController", func_name)
            if not ufunc:
                continue
            params = b"\x00" * 0x10
            if self._process_event_call(pc, ufunc, params):
                if respect_cooldown:
                    self._autokick_leave_until = now + self.AUTOKICK_LEAVE_COOLDOWN_SEC
                self._trainer_log(
                    "LOBBY",
                    f"return to main menu via {func_name}",
                    config=config,
                    force=True,
                )
                return True, "Returning to main menu..."

        return False, "ClientReturnToMainMenu not found or call failed"

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
            if not sid:
                ps = pdata.get("player_state", 0)
                if ps:
                    sid = self.get_player_steam_id(ps, force=True)
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

    def _trainer_apply_rename(self, pawn, config, name=None, *, force=False):
        """Apply replicated rename via bridge. Returns (ok, message)."""
        ps = self._trainer_local_player_state(pawn)
        if not ps:
            return False, "no PlayerState — join a lobby or match first"

        name = (name if name is not None else (config.trainer_rename_text or "")).strip()[:32]
        if not name:
            return False, "name is empty"

        now = time.monotonic()
        if not force:
            pending = getattr(self, "_trainer_rename_pending_name", None)
            if name != pending:
                self._trainer_rename_pending_name = name
                self._trainer_rename_pending_ts = now
                return False, ""
            if now - self._trainer_rename_pending_ts < 1.5:
                return False, ""
            if name == self._trainer_last_rename and (now - self._trainer_last_rename_ts) < 5.0:
                return False, ""
            cur = self.get_custom_player_name(ps)
            if cur == name and self._trainer_last_rename == name:
                return False, ""
        else:
            cur = self.get_custom_player_name(ps)

        if not hasattr(self, "_bridge_request"):
            self._trainer_log(
                "RENAME",
                "bridge API unavailable — restart Peterhack",
                config=config,
                level="error",
                force=True,
            )
            return False, "bridge API unavailable — restart Peterhack"

        if not self._resolve_bridge_port() and not self._ensure_bridge():
            err = getattr(self, "_camo_last_error", None) or "bridge not ready — wait for auto-inject"
            self._trainer_log("RENAME", err, config=config, level="debug", interval=10.0)
            return False, err

        cls = self.objects.class_name(ps) or "?"
        timeout = getattr(self, "_trainer_rename_bridge_timeout", 10)
        try:
            resp = self._bridge_request("set_player_name", {"name": name}, timeout=timeout)
        except Exception as exc:
            self._trainer_log("RENAME", f"bridge request failed: {exc}", config=config, level="error", force=True)
            return False, str(exc)

        ok = bool(resp and resp.get("success"))
        if ok:
            self._trainer_last_rename = name
            self._trainer_last_rename_ts = now
            if getattr(self, "_trainer_auto_rename_queued_for", None) == name:
                self._trainer_auto_rename_queued_for = None
            new_cur = self.get_custom_player_name(ps)
            msg = f"'{cur or '?'}' -> '{name}' readback='{new_cur or '?'}'"
            self._trainer_log("RENAME", f"SetName {msg} ({cls})", config=config, interval=2.0 if force else 3.0)
            return True, msg

        err = (resp or {}).get("message") or (resp or {}).get("stage") or "unknown error"
        self._trainer_log("RENAME", f"SetName failed via bridge: {err}", config=config, level="error", force=True)
        if getattr(self, "_trainer_auto_rename_queued_for", None) == name:
            self._trainer_auto_rename_queued_for = None
        off = self._custom_player_name_offset(ps)
        if off and self._trainer_write_fstring(ps + off, name):
            self._trainer_log(
                "RENAME",
                f"SetName RPC failed — local FString only @0x{off:X} (not replicated)",
                config=config,
                level="error",
                force=True,
            )
            return False, f"RPC failed ({err}); local FString only (not replicated)"
        return False, err

    def trainer_rename_now(self, name, config=None, force=True):
        """Queue a rename (non-blocking). Returns (True, 'queued') or (False, reason)."""
        config = config or getattr(self, "config", None)
        if not config:
            return False, "config unavailable"
        if self.queue_trainer_rename(name, config, force=force):
            return True, "queued"
        return False, "name is empty"

    def _trainer_auto_rename(self, pawn, config):
        name = (config.trainer_rename_text or "").strip()[:32]
        if not name:
            self._trainer_auto_rename_queued_for = None
            return

        now = time.monotonic()
        pending = getattr(self, "_trainer_rename_pending_name", None)
        if name != pending:
            self._trainer_rename_pending_name = name
            self._trainer_rename_pending_ts = now
            self._trainer_auto_rename_queued_for = None
            return
        if now - self._trainer_rename_pending_ts < 1.5:
            return
        if name == self._trainer_last_rename and (now - self._trainer_last_rename_ts) < 5.0:
            return
        ps = self._trainer_local_player_state(pawn)
        cur = self.get_custom_player_name(ps) if ps else None
        if cur == name and self._trainer_last_rename == name:
            return
        if getattr(self, "_trainer_auto_rename_queued_for", None) == name:
            return
        with self._rename_lock:
            if self._rename_pending and self._rename_pending[0] == name:
                return
        self._trainer_auto_rename_queued_for = name
        self.queue_trainer_rename(name, config, force=False)

    def tick_trainer(self, config):
        """Apply enabled trainer features (throttled — not every overlay frame)."""
        camo_noclip_hold = getattr(self, "_camo_noclip_hold", False)

        if config:
            try:
                if self._game_process_alive():
                    self._trainer_magnet_key_toggle(config)
                    if self._magnet_active:
                        pawn = self._find_local_pawn()
                        if pawn and self._trainer_is_playable_pawn(pawn):
                            self._trainer_magnet(pawn, config)
            except Exception as exc:
                self._trainer_error("MAGNET", exc, config)

        if not config or not self._trainer_any_active(config):
            if self._trainer_anticlip_saved is not None and not camo_noclip_hold:
                self._trainer_anti_clipping(0, config, False)
            if camo_noclip_hold:
                pawn = self._find_local_pawn()
                if pawn and self._trainer_anticlip_saved is None:
                    self._trainer_anti_clipping(pawn, config, True)
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
        playable = self._trainer_is_playable_pawn(pawn)
        prev_playable = getattr(self, "_trainer_last_playable_pawn", 0)
        if prev_playable and (not pawn or pawn != prev_playable) and self._trainer_anticlip_saved is not None:
            self._trainer_anti_clipping(0, config, False)
        if playable:
            self._trainer_last_playable_pawn = pawn
        elif not pawn:
            self._trainer_last_playable_pawn = 0

        if camo_noclip_hold:
            if playable and self._trainer_anticlip_saved is None:
                self._trainer_anti_clipping(pawn, config, True)
        elif config.trainer_anti_clipping:
            if playable:
                self._trainer_anti_clipping(pawn, config, True)
            elif self._trainer_anticlip_saved is not None:
                self._trainer_anti_clipping(pawn, config, False)
        elif self._trainer_anticlip_saved is not None:
            self._trainer_anti_clipping(pawn, config, False)

        if not playable:
            if pawn and self._trainer_tick_count % 120 == 1:
                cls = self.objects.class_name(pawn) or "?"
                self._trainer_log(
                    "TICK",
                    f"spectate/non-playable pawn ({cls}) — trainer memory writes paused",
                    config=config,
                    level="debug",
                )
            if config.trainer_auto_rename:
                self._trainer_auto_rename(0, config)
            if config.trainer_anti_kick:
                self._trainer_anti_kick(config)
            if getattr(config, "autokick_enabled", False):
                self._trainer_autokick(config)
            return

        if not pawn:
            if self._trainer_tick_count % 180 == 1:
                self._trainer_log("TICK", "no local pawn (not in match?)", config=config, level="debug")
            if config.trainer_auto_rename:
                self._trainer_auto_rename(pawn, config)
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
            if config.trainer_anti_detection:
                self._trainer_anti_detection(pawn, config)
            if config.trainer_infinite_bullets:
                self._trainer_infinite_bullets(pawn, config)
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
