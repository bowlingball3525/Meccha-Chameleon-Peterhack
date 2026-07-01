#!/usr/bin/env python3
"""
Camouflage bridge — SilentJMA Meccha-Chameleon-Tools v1.8.0.1.

Fully automatic: launch bridge EXE → inject DLL → TCP paint_full_route.
The controller picks a dynamic TCP port (not always 47654) — we discover it.
"""
import json
import os
import sys
import glob
import subprocess as _subprocess

from meccha_chameleon_tools.log_util import PETERHACK_ROOT

CREATE_NO_WINDOW = 0x08000000
BRIDGE_PING_TIMEOUT = 1.0
BRIDGE_FIXED_PORT = 47654


class CamoBridgeMixin:
    """Bridge EXE + DLL camouflage (v1.8.0.1)."""

    DLL_NAME = "meccha-xenos-bridge.dll"
    EXE_NAME = "meccha-camouflage.exe"
    INJECTOR_NAME = "meccha-xenos-injector.exe"
    BRIDGE_HOST = "127.0.0.1"
    BRIDGE_PORT = BRIDGE_FIXED_PORT
    CAMO_DIR = os.path.join(PETERHACK_ROOT, "camo")
    RUNTIME_DIR = os.path.join(CAMO_DIR, "runtime")

    @staticmethod
    def _camo_bundle_dir():
        if getattr(sys, "frozen", False):
            return sys._MEIPASS
        return os.path.dirname(os.path.abspath(__file__))

    @classmethod
    def _get_dll_path(cls):
        return os.path.join(cls._camo_bundle_dir(), cls.DLL_NAME)

    @classmethod
    def _get_exe_path(cls):
        return os.path.join(cls._camo_bundle_dir(), cls.EXE_NAME)

    @classmethod
    def _get_injector_path(cls):
        return os.path.join(cls._camo_bundle_dir(), cls.INJECTOR_NAME)

    @staticmethod
    def _get_stable_exe_path():
        return os.path.join(CamoBridgeMixin.CAMO_DIR, CamoBridgeMixin.EXE_NAME)

    @staticmethod
    def _get_stable_dll_path():
        return os.path.join(CamoBridgeMixin.CAMO_DIR, CamoBridgeMixin.DLL_NAME)

    @staticmethod
    def _get_stable_injector_path():
        return os.path.join(CamoBridgeMixin.CAMO_DIR, CamoBridgeMixin.INJECTOR_NAME)

    BRIDGE_DLL_MARKERS = (
        "meccha-xenos-bridge",
        "runtime-bridge",
        "xenos-bridge",
    )

    def _bridge_dll_loaded(self):
        """Return (loaded, module_names) for camouflage bridge DLLs in the game process."""
        if not getattr(self, "pm", None):
            return False, []
        list_modules = getattr(self, "_list_game_modules", None)
        if not callable(list_modules):
            return False, []
        loaded = []
        try:
            for name, _base in list_modules():
                nl = (name or "").lower()
                if any(marker in nl for marker in self.BRIDGE_DLL_MARKERS):
                    loaded.append(name)
        except Exception as exc:
            print(f"[CAMO] module scan failed: {exc}", flush=True)
        return bool(loaded), loaded

    def _bridge_status_for_pid(self, pid):
        """Read controller status when it matches the current game pid."""
        status = self._read_last_status()
        proc = status.get("process") or {}
        bridge = status.get("bridge") or {}
        if proc.get("pid") != pid:
            return {}
        return bridge

    def _wait_for_bridge_tcp(self, label="bridge TCP", attempts=120, sleep_s=0.5):
        """Poll discovered ports until bridge responds — no inject, no controller launch."""
        import time as _t

        pid = getattr(self.pm, "process_id", 0)
        for i in range(attempts):
            if self._camo_aborted():
                return False
            if self._resolve_bridge_port():
                print(f"[CAMO] {label} ready ({(i + 1) * sleep_s:.1f}s)", flush=True)
                return True
            bridge = self._bridge_status_for_pid(pid)
            if (
                bridge.get("state") == "ready"
                and bridge.get("port")
                and bridge.get("message") == "pong"
            ):
                port = int(bridge["port"])
                self._bridge_port = port
                print(f"[CAMO] {label} ready on port {port} (controller verified)", flush=True)
                return True
            if bridge.get("state") == "ready" and bridge.get("port"):
                port = int(bridge["port"])
                if self._ping_port(port):
                    self._bridge_port = port
                    print(f"[CAMO] {label} ready on port {port} (from status)", flush=True)
                    return True
            if i > 0 and i % 8 == 7:
                tried = self._discover_bridge_ports()
                print(
                    f"[CAMO] waiting for {label}... {(i + 1) // 8}/15 "
                    f"(probing ports: {tried[:5]})",
                    flush=True,
                )
            _t.sleep(sleep_s)
        return bool(self._resolve_bridge_port())

    def _read_last_status(self):
        path = os.path.join(self.RUNTIME_DIR, "last_status.json")
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
        except Exception:
            pass
        return {}

    @staticmethod
    def _read_port_file(path):
        try:
            if os.path.isfile(path):
                raw = open(path, encoding="utf-8").read().strip()
                port = int(raw.split()[0])
                if 1024 <= port <= 65535:
                    return port
        except Exception:
            pass
        return None

    def _discover_bridge_ports(self):
        """Collect TCP ports — status/recent sidecars first, avoid stale port spam."""
        ports = []
        seen = set()

        def add(port):
            if port and 1024 <= int(port) <= 65535:
                p = int(port)
                if p not in seen:
                    seen.add(p)
                    ports.append(p)

        status = self._read_last_status()
        bridge = status.get("bridge") or {}
        add(bridge.get("port"))
        add(getattr(self, "_bridge_port", None))
        add(BRIDGE_FIXED_PORT)
        add(self._read_port_file(self._get_stable_dll_path() + ".port"))

        sidecars = []
        for pattern in (
            os.path.join(self.RUNTIME_DIR, "native", "*.dll.port"),
            os.path.join(self.CAMO_DIR, "*.dll.port"),
        ):
            sidecars.extend(glob.glob(pattern))
        sidecars.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for path in sidecars[:6]:
            add(self._read_port_file(path))

        return ports

    @staticmethod
    def _bridge_encode_request(command, payload=None):
        import time as _time

        # Bridge parses with substring search for compact JSON (no spaces after ':').
        return json.dumps(
            {
                "type": command,
                "request_id": f"{os.urandom(8).hex()}{int(_time.time())}",
                "timestamp_utc": int(_time.time()),
                "payload": payload or {},
            },
            separators=(",", ":"),
        ) + "\n"

    def _bridge_request_on_port(self, port, command, payload=None, timeout=30):
        import socket as _socket

        connect_timeout = min(1.5, max(0.4, float(timeout) * 0.15))
        msg = self._bridge_encode_request(command, payload)
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(connect_timeout)
        try:
            sock.connect((self.BRIDGE_HOST, int(port)))
            sock.settimeout(timeout)
            sock.sendall(msg.encode())
            raw = b""
            while b"\n" not in raw:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
            line = raw.split(b"\n")[0]
            return json.loads(line) if line else {"success": False}
        except Exception:
            return {"success": False}
        finally:
            sock.close()

    @staticmethod
    def _bridge_response_ok(response):
        if not response:
            return False
        if response.get("success"):
            return True
        return (
            response.get("stage") == "ping"
            and response.get("message") == "pong"
        )

    def _ping_port(self, port, timeout=BRIDGE_PING_TIMEOUT):
        resp = self._bridge_request_on_port(port, "ping", timeout=timeout)
        return self._bridge_response_ok(resp)

    def _resolve_bridge_port(self):
        """Find the live bridge TCP port (controller uses dynamic ports)."""
        for port in self._discover_bridge_ports():
            if self._ping_port(port):
                if getattr(self, "_bridge_port", None) != port:
                    print(f"[CAMO] bridge TCP on 127.0.0.1:{port}", flush=True)
                self._bridge_port = port
                return port
        self._bridge_port = None
        return None

    def _bridge_request(self, command, payload=None, timeout=30):
        port = self._resolve_bridge_port()
        if not port:
            return {"success": False}
        return self._bridge_request_on_port(port, command, payload, timeout=timeout)

    def _bridge_ping(self, timeout=BRIDGE_PING_TIMEOUT):
        return self._bridge_request("ping", timeout=timeout)

    def _camo_aborted(self):
        return bool(getattr(self, "_camo_abort", False))

    def cleanup(self):
        proc = getattr(self, "_bridge_proc", None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._bridge_proc = None

    def camo_cleanup(self):
        """Stop bridge EXE on application exit."""
        try:
            if self._resolve_bridge_port():
                self._bridge_request("shutdown", {}, timeout=3)
        except Exception:
            pass
        self.cleanup()

    def _extract_stable_camo_files(self):
        import shutil

        os.makedirs(self.CAMO_DIR, exist_ok=True)
        os.makedirs(self.RUNTIME_DIR, exist_ok=True)
        missing = []
        for src_fn, dst_fn, label in (
            (self._get_exe_path(), self._get_stable_exe_path(), self.EXE_NAME),
            (self._get_dll_path(), self._get_stable_dll_path(), self.DLL_NAME),
            (self._get_injector_path(), self._get_stable_injector_path(), self.INJECTOR_NAME),
        ):
            if os.path.isfile(src_fn):
                shutil.copy2(src_fn, dst_fn)
            elif not os.path.isfile(dst_fn):
                missing.append(label)
        if not missing:
            print(f"[CAMO] bridge files ready in {self.CAMO_DIR}", flush=True)
        return missing

    @staticmethod
    def _write_port_sidecar(dll_path, port):
        try:
            with open(dll_path + ".port", "w", encoding="utf-8") as fh:
                fh.write(f"{int(port)}\n")
        except Exception as exc:
            print(f"[CAMO] could not write port file: {exc}", flush=True)

    def _log_bridge_diagnostics(self):
        status = self._read_last_status()
        bridge = status.get("bridge") or {}
        proc = status.get("process") or {}
        err = status.get("last_error")
        print(
            f"[CAMO] controller: process pid={proc.get('pid')} "
            f"bridge_state={bridge.get('state')} bridge_port={bridge.get('port')} "
            f"msg={bridge.get('message')!r}",
            flush=True,
        )
        if err:
            print(f"[CAMO] controller last_error: {err}", flush=True)

    def _bridge_dll_state(self):
        """Return (any_loaded, module_names, has_xenos_bridge, has_runtime_bridge)."""
        loaded, names = self._bridge_dll_loaded()
        if not loaded:
            return False, [], False, False
        has_xenos = any("meccha-xenos-bridge" in n for n in names)
        has_runtime = any("runtime-bridge" in n for n in names)
        return True, names, has_xenos, has_runtime

    def _log_bridge_capabilities(self):
        resp = self._bridge_request("capabilities", {}, timeout=5)
        if not resp or not resp.get("success"):
            return
        cmds = (resp.get("metadata") or {}).get("commands") or []
        if cmds:
            print(f"[CAMO] bridge commands: {', '.join(cmds)}", flush=True)

    def _bridge_has_rotate(self):
        resp = self._bridge_request("capabilities", {}, timeout=5)
        if resp and resp.get("success"):
            cmds = (resp.get("metadata") or {}).get("commands") or []
            return "rotate" in cmds
        probe = self._bridge_request("rotate", {"yaw": 0}, timeout=5)
        return bool(probe and probe.get("success"))

    def _try_direct_inject(self, port=BRIDGE_FIXED_PORT, force=False):
        """Inject SilentJMA meccha-xenos-bridge.dll (full TCP command set)."""
        loaded, mod_names, has_xenos, _has_runtime = self._bridge_dll_state()
        if loaded and has_xenos and not force:
            print(
                f"[CAMO] meccha-xenos-bridge already loaded ({', '.join(mod_names)})",
                flush=True,
            )
            return True
        if loaded and not force:
            print(
                f"[CAMO] other bridge loaded ({', '.join(mod_names)}) — "
                f"restart game before inject",
                flush=True,
            )
            return False
        inj = self._get_stable_injector_path()
        dll = self._get_stable_dll_path()
        proc_name = getattr(self, "PROCESS_NAME", "PenguinHotel-Win64-Shipping.exe")
        if not os.path.isfile(inj) or not os.path.isfile(dll):
            print("[CAMO] direct inject skipped — injector or DLL missing", flush=True)
            return False
        self._write_port_sidecar(dll, port)
        print(
            f"[CAMO] injecting {self.DLL_NAME} pid={getattr(self.pm, 'process_id', 0)} "
            f"port={port}...",
            flush=True,
        )
        try:
            result = _subprocess.run(
                [inj, proc_name, dll],
                cwd=os.path.dirname(inj),
                capture_output=True,
                timeout=45,
                creationflags=CREATE_NO_WINDOW,
            )
            out = (result.stdout or b"").decode(errors="replace").strip()
            err = (result.stderr or b"").decode(errors="replace").strip()
            if out:
                print(f"[CAMO] inject: {out}", flush=True)
            if err:
                print(f"[CAMO] inject err: {err}", flush=True)
            if result.returncode == 0:
                return True
            print(f"[CAMO] inject failed rc={result.returncode}", flush=True)
        except Exception as exc:
            print(f"[CAMO] direct inject exception: {exc}", flush=True)
        return False

    def _ensure_bridge(self):
        """Inject SilentJMA xenos bridge DLL, then launch controller (TCP command server)."""
        import time as _t

        if not getattr(self, "pm", None) or not self.pm.process_id:
            print("[CAMO] no game pid", flush=True)
            return False

        pid = self.pm.process_id
        loaded, mod_names, has_xenos, has_runtime = self._bridge_dll_state()

        if self._resolve_bridge_port():
            if has_runtime and not has_xenos:
                print(
                    "[CAMO] TCP up but embedded runtime-bridge is loaded (no rotate). "
                    "Restart the game so Peterhack can inject meccha-xenos-bridge.dll.",
                    flush=True,
                )
                return False
            print("[CAMO] bridge already up (TCP)", flush=True)
            self._log_bridge_capabilities()
            return True

        if loaded and has_runtime and not has_xenos:
            print(
                f"[CAMO] wrong bridge in game ({', '.join(mod_names)}) — "
                "restart the game, then Paint Now (needs meccha-xenos-bridge.dll).",
                flush=True,
            )
            return False

        if loaded and has_xenos:
            print(
                f"[CAMO] {self.DLL_NAME} in game — waiting for TCP...",
                flush=True,
            )
            if self._wait_for_bridge_tcp("xenos bridge"):
                self._log_bridge_capabilities()
                return True
            print(
                "[CAMO] xenos bridge loaded but TCP dead — restart the game.",
                flush=True,
            )
            return False

        try:
            missing = self._extract_stable_camo_files()
            if missing:
                print(
                    f"[CAMO] missing bridge binaries: {', '.join(missing)} "
                    f"(expected in {self._camo_bundle_dir()})",
                    flush=True,
                )
                return False
        except Exception as exc:
            print(f"[CAMO] extract failed: {exc}", flush=True)
            return False

        dll = self._get_stable_dll_path()
        self._write_port_sidecar(dll, BRIDGE_FIXED_PORT)

        exe_path = self._get_stable_exe_path()
        if not os.path.isfile(exe_path):
            print(f"[CAMO] EXE not found at {exe_path}", flush=True)
            return False

        proc = getattr(self, "_bridge_proc", None)
        if proc and proc.poll() is None:
            print("[CAMO] restarting bridge controller (not responding on TCP)", flush=True)
            self.cleanup()

        # Inject full SilentJMA bridge BEFORE controller (controller skips inject if TCP up).
        if not self._try_direct_inject(BRIDGE_FIXED_PORT):
            print(
                "[CAMO] inject failed — run Peterhack as administrator.",
                flush=True,
            )
            return False

        for _ in range(30):
            if self._resolve_bridge_port():
                break
            _t.sleep(0.5)

        print(
            f"[CAMO] launching bridge controller (port {BRIDGE_FIXED_PORT})...",
            flush=True,
        )
        print(f"[CAMO] runtime log dir: {self.RUNTIME_DIR}", flush=True)
        try:
            self._bridge_proc = _subprocess.Popen(
                [
                    exe_path,
                    "--bridge-port", str(BRIDGE_FIXED_PORT),
                    "--bridge-host", self.BRIDGE_HOST,
                    "--log-dir", self.RUNTIME_DIR,
                ],
                cwd=os.path.dirname(exe_path),
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            print(f"[CAMO] failed to launch controller: {exc}", flush=True)
            if self._resolve_bridge_port():
                self._log_bridge_capabilities()
                return True
            return False

        print("[CAMO] waiting for bridge TCP...", flush=True)
        if self._wait_for_bridge_tcp("bridge"):
            self._log_bridge_capabilities()
            return True

        self._log_bridge_diagnostics()
        print(
            "[CAMO] failed to communicate with bridge DLL — "
            "Run Peterhack as administrator and retry Paint Now.",
            flush=True,
        )
        return False

    @staticmethod
    def _normalize_yaw_deg(yaw):
        yaw = float(yaw) % 360.0
        if yaw > 180.0:
            yaw -= 360.0
        return yaw

    def _camo_save_view_rotation(self):
        """Snapshot controller + pawn yaw for wrap restore."""
        state = {}
        if hasattr(self, "get_control_rotation"):
            rot = self.get_control_rotation()
            if rot:
                state["control"] = rot
        pawn = self._find_local_pawn() if hasattr(self, "_find_local_pawn") else 0
        if pawn and hasattr(self, "get_actor_root_rotation"):
            rot = self.get_actor_root_rotation(pawn)
            if rot:
                state["pawn"] = pawn
                state["actor"] = rot
        return state

    def _camo_apply_view_yaw_delta(self, state, yaw_offset):
        """Rotate camera + pawn to yaw_offset° from saved start (bridge uses ControlRotation)."""
        if not state or yaw_offset is None:
            return False
        ok = False
        if "control" in state and hasattr(self, "set_control_yaw"):
            pitch, yaw, roll = state["control"]
            ok = self.set_control_yaw(self._normalize_yaw_deg(yaw + yaw_offset)) or ok
        pawn = state.get("pawn")
        if pawn and "actor" in state and hasattr(self, "set_actor_root_yaw"):
            pitch, yaw, roll = state["actor"]
            ok = self.set_actor_root_yaw(
                pawn, self._normalize_yaw_deg(yaw + yaw_offset),
            ) or ok
        return ok

    def _camo_restore_view_rotation(self, state):
        if not state:
            return
        if "control" in state and hasattr(self, "set_control_yaw"):
            _pitch, yaw, _roll = state["control"]
            self.set_control_yaw(yaw)
        pawn = state.get("pawn")
        if pawn and "actor" in state and hasattr(self, "set_actor_root_yaw"):
            _pitch, yaw, _roll = state["actor"]
            self.set_actor_root_yaw(pawn, yaw)

    # Full wrap: sides first, then front (180°), then back (0° — ends facing forward).
    CAMO_WRAP_YAW_PASSES = (
        (90, "left"),
        (270, "right"),
        (180, "front"),
        (0, "back"),
    )

    def _camo_rotate_to_yaw(self, target_yaw, label):
        """Rotate to yaw offset from session start — bridge uses delta, native uses absolute."""
        import time as _t

        current = getattr(self, "_camo_yaw_offset", 0)
        delta = int(target_yaw) - int(current)
        if not delta:
            return True

        print(
            f"[CAMO] rotate {label} target={target_yaw}° delta={delta}° (bridge)...",
            flush=True,
        )
        rot_resp = self._bridge_request("rotate", {"yaw": delta}, timeout=10)
        print(f"[CAMO] rotate response={rot_resp}", flush=True)
        if rot_resp and rot_resp.get("success"):
            _t.sleep(1.5)
            self._camo_yaw_offset = target_yaw
            return True

        state = getattr(self, "_camo_view_state", None)
        if state:
            print("[CAMO] bridge rotate failed — native camera fallback...", flush=True)
            if self._camo_apply_view_yaw_delta(state, target_yaw):
                _t.sleep(1.5)
                self._camo_yaw_offset = target_yaw
                return True

        return False

    def _paint_payload(self, pid):
        return {
            "native_apply_mode": "template_brush_paint",
            "route": "f10_template_brush_paint",
            "process": {
                "pid": pid,
                "name": getattr(self, "PROCESS_NAME", "PenguinHotel-Win64-Shipping.exe"),
            },
            "max_paints_per_tick": 256,
            "paint_tick_budget_ms": 16,
            "brush_radius": 4.0,
            "template_min_direct_points": 5000,
            "auto_flush_during_paint": True,
        }

    def _finalize_camo_paint(self):
        """Stop template_brush_paint tick loops after apply."""
        import time as _t

        print("[CAMO] stopping paint loop...", flush=True)
        self._bridge_request("cancel_paint", {}, timeout=5)
        _t.sleep(0.2)
        if hasattr(self, "_force_quiesce_camo_paint"):
            try:
                self._force_quiesce_camo_paint(label="CAMO", quiet=False)
            except Exception:
                pass

    def camo_apply(self, r=None, g=None, b=None, a=None, full_wrap=False):
        """Environment camouflage via bridge paint_full_route."""
        del r, g, b, a
        if not getattr(self, "pm", None) or not self.pm:
            print("[CAMO] no pymem handle", flush=True)
            return False

        self._camo_abort = False
        self._bridge_port = None
        try:
            pid = self.pm.process_id
            print(f"[CAMO] pid={pid} wrap={full_wrap}", flush=True)
            if not pid:
                return False

            if not self._ensure_bridge():
                return False

            import time as _t

            payload = self._paint_payload(pid)
            passes = (
                list(self.CAMO_WRAP_YAW_PASSES) if full_wrap else [(0, "front")]
            )

            self._camo_yaw_offset = 0
            self._camo_view_state = self._camo_save_view_rotation() if full_wrap else None
            try:
                for pass_idx, (target_yaw, label) in enumerate(passes):
                    if self._camo_aborted():
                        print("[CAMO] apply aborted", flush=True)
                        return False
                    if target_yaw != self._camo_yaw_offset:
                        if not self._camo_rotate_to_yaw(target_yaw, label):
                            print(
                                f"[CAMO] {label} pass rotate failed — aborting wrap "
                                f"(earlier passes already applied)",
                                flush=True,
                            )
                            self._finalize_camo_paint()
                            return False

                    print(
                        f"[CAMO] paint pass {pass_idx + 1}/{len(passes)} "
                        f"({label}, port={self._bridge_port})...",
                        flush=True,
                    )
                    resp = self._bridge_request("paint_full_route", payload, timeout=120)
                    print(f"[CAMO] paint response={resp}", flush=True)
                    if not resp or not resp.get("success", False):
                        self._finalize_camo_paint()
                        return False
            finally:
                if self._camo_view_state:
                    self._camo_restore_view_rotation(self._camo_view_state)
                    self._camo_view_state = None
                    _t.sleep(0.3)

            self._finalize_camo_paint()
            print("[CAMO] apply complete", flush=True)
            return True
        except Exception as exc:
            import traceback

            print(f"[CAMO] exception: {exc}", flush=True)
            traceback.print_exc()
            try:
                self._finalize_camo_paint()
            except Exception:
                pass
            return False

    def camo_stop(self):
        """Cancel active camouflage paint."""
        self._camo_abort = True
        print("[CAMO] sending cancel_paint...", flush=True)
        resp = self._bridge_request("cancel_paint", {}, timeout=5)
        print(f"[CAMO] cancel response={resp}", flush=True)
        if hasattr(self, "_force_quiesce_camo_paint"):
            try:
                self._force_quiesce_camo_paint(label="CAMO-STOP", quiet=False)
            except Exception:
                pass
        return bool(resp and resp.get("success", False))
