#!/usr/bin/env python3
"""
Camouflage bridge for Peterhack.

Injects meccha-xenos-bridge.dll and talks over localhost TCP (paint, rotate, cancel).
"""
import json
import math
import os
import sys
import glob
import ctypes
import threading
import subprocess as _subprocess

from meccha_chameleon_tools.log_util import PETERHACK_ROOT

CREATE_NO_WINDOW = 0x08000000
BRIDGE_PING_TIMEOUT = 2.5
BRIDGE_PING_RETRIES = 3
BRIDGE_FIXED_PORT = 47654


class CamoBridgeMixin:
    """In-game bridge via meccha-xenos-bridge.dll."""

    DLL_NAME = "meccha-xenos-bridge.dll"
    EXE_NAME = "meccha-camouflage.exe"  # legacy bundle artifact — not launched by Peterhack
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
    RUNTIME_BRIDGE_MARKERS = ("runtime-bridge",)
    XENOS_BRIDGE_MARKERS = ("meccha-xenos-bridge", "xenos-bridge")

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

    def _bridge_request_on_port(self, port, command, payload=None, timeout=30, quiet=False):
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
            return json.loads(line) if line else {"success": False, "message": "empty bridge response"}
        except Exception as exc:
            if not quiet:
                print(f"[CAMO] bridge {command} failed: {exc}", flush=True)
            return {"success": False, "message": str(exc)}
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

    def _ping_port(self, port, timeout=BRIDGE_PING_TIMEOUT, quiet=False):
        resp = self._bridge_request_on_port(port, "ping", timeout=timeout, quiet=quiet)
        return self._bridge_response_ok(resp)

    def _resolve_bridge_port(self):
        """Find the live bridge TCP port (controller uses dynamic ports)."""
        import time as _t

        cached = getattr(self, "_bridge_port", None)
        if cached:
            for attempt in range(BRIDGE_PING_RETRIES):
                if self._ping_port(cached, quiet=attempt > 0):
                    return cached
                if attempt + 1 < BRIDGE_PING_RETRIES:
                    _t.sleep(0.25)
        for port in self._discover_bridge_ports():
            if cached and port == cached:
                continue
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
        self._stop_legacy_camo_controller()

    def _stop_legacy_camo_controller(self):
        """Kill stale meccha-camouflage.exe — it injects runtime-bridge and locks camo files."""
        import time as _t

        stopped = []
        try:
            result = _subprocess.run(
                ["taskkill", "/F", "/IM", self.EXE_NAME, "/T"],
                capture_output=True,
                timeout=8,
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                stopped.append(self.EXE_NAME)
            elif result.returncode == 128:
                pass  # process not found
            else:
                err = (result.stderr or b"").decode(errors="replace").strip()
                if err and "not found" not in err.lower():
                    print(f"[CAMO] could not stop {self.EXE_NAME}: {err}", flush=True)
        except Exception as exc:
            print(f"[CAMO] taskkill {self.EXE_NAME}: {exc}", flush=True)

        proc = getattr(self, "_bridge_proc", None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(3)
                stopped.append("bridge_proc")
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._bridge_proc = None

        if stopped:
            print(f"[CAMO] stopped legacy controller ({', '.join(stopped)})", flush=True)
            _t.sleep(0.5)
        self._purge_stale_runtime_bridge_files()
        return bool(stopped)

    @classmethod
    def _purge_stale_runtime_bridge_files(cls):
        """Remove runtime-bridge sidecars left by meccha-camouflage.exe."""
        import glob as _glob

        native_dir = os.path.join(cls.RUNTIME_DIR, "native")
        if not os.path.isdir(native_dir):
            return
        removed = 0
        for pattern in ("runtime-bridge*.dll", "runtime-bridge*.dll.port"):
            for path in _glob.glob(os.path.join(native_dir, pattern)):
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    pass
        if removed:
            print(f"[CAMO] purged {removed} stale runtime-bridge file(s)", flush=True)

    def _extract_stable_camo_files(self):
        import shutil

        os.makedirs(self.CAMO_DIR, exist_ok=True)
        os.makedirs(self.RUNTIME_DIR, exist_ok=True)
        missing = []

        def _safe_copy(src_fn, dst_fn):
            if not os.path.isfile(src_fn):
                return os.path.isfile(dst_fn)
            try:
                shutil.copy2(src_fn, dst_fn)
                return True
            except (PermissionError, OSError) as exc:
                winerr = getattr(exc, "winerror", None)
                if os.path.isfile(dst_fn) and os.path.getsize(dst_fn) > 0:
                    if winerr == 32 or isinstance(exc, PermissionError):
                        print(
                            f"[CAMO] {os.path.basename(dst_fn)} in use — "
                            "using existing copy",
                            flush=True,
                        )
                        return True
                raise

        for src_fn, dst_fn, label in (
            (self._get_dll_path(), self._get_stable_dll_path(), self.DLL_NAME),
            (self._get_injector_path(), self._get_stable_injector_path(), self.INJECTOR_NAME),
        ):
            try:
                if not _safe_copy(src_fn, dst_fn):
                    missing.append(label)
            except Exception as exc:
                if os.path.isfile(dst_fn) and os.path.getsize(dst_fn) > 0:
                    print(
                        f"[CAMO] could not refresh {label}: {exc} — using existing",
                        flush=True,
                    )
                else:
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
        has_xenos = any(any(m in n for m in self.XENOS_BRIDGE_MARKERS) for n in names)
        has_runtime = any(any(m in n for m in self.RUNTIME_BRIDGE_MARKERS) for n in names)
        return True, names, has_xenos, has_runtime

    def _unload_game_bridge_modules(self, *name_markers):
        """FreeLibrary selected bridge DLLs in the game process."""
        unloaded = []
        if not getattr(self, "pm", None):
            return unloaded
        markers = tuple(m.lower() for m in name_markers if m)
        if not markers:
            return unloaded

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
            low = (name or "").lower()
            if base <= 0x10000 or base in seen_bases:
                return
            if not any(m in low for m in markers):
                return
            seen_bases.add(base)
            print(f"[CAMO] unloading {name} @ 0x{base:X}", flush=True)
            h_thread = kernel32.CreateRemoteThread(
                h_process, None, 0, free_lib, base, 0, None,
            )
            if h_thread:
                kernel32.WaitForSingleObject(h_thread, 8000)
                kernel32.CloseHandle(h_thread)
                unloaded.append(name)

        list_modules = getattr(self, "_list_game_modules", None)
        if callable(list_modules):
            for name, base in list_modules():
                _try_unload(name, base)
        if unloaded:
            print(
                f"[CAMO] unloaded {len(unloaded)} module(s): {', '.join(unloaded)}",
                flush=True,
            )
        return unloaded

    def _bridge_shutdown(self):
        """Ask the in-game TCP bridge to stop (best-effort before unload)."""
        try:
            port = getattr(self, "_bridge_port", None)
            if port:
                self._bridge_request("shutdown", {}, timeout=3)
            else:
                for port in self._discover_bridge_ports()[:3]:
                    self._bridge_request_on_port(port, "shutdown", {}, timeout=3)
        except Exception:
            pass

    def _unload_all_bridge_modules(self):
        """FreeLibrary every camouflage bridge DLL in the game process."""
        self._bridge_shutdown()
        return self._unload_game_bridge_modules(*self.BRIDGE_DLL_MARKERS)

    def _replace_runtime_bridge_with_xenos(self):
        """Drop embedded runtime-bridge so the full xenos DLL can load."""
        import time as _t

        self._stop_legacy_camo_controller()
        self._bridge_shutdown()
        _t.sleep(0.4)
        self._bridge_port = None
        removed = self._unload_game_bridge_modules(*self.RUNTIME_BRIDGE_MARKERS)
        _t.sleep(0.3)
        return bool(removed)

    def _log_bridge_capabilities(self):
        resp = self._bridge_request("capabilities", {}, timeout=5)
        if not resp or not resp.get("success"):
            self._bridge_commands = set()
            return
        cmds = (resp.get("metadata") or {}).get("commands") or []
        self._bridge_commands = set(cmds)
        meta = resp.get("metadata") or {}
        fields = meta.get("paint_full_route_fields") or []
        self._bridge_paint_yaw = "camera_yaw_offset" in fields
        if cmds:
            print(f"[CAMO] bridge commands: {', '.join(cmds)}", flush=True)
        if self._bridge_paint_yaw:
            print("[CAMO] wrap yaw via paint_full_route camera_yaw_offset", flush=True)
        elif cmds and "rotate" not in cmds:
            print(
                "[CAMO] bridge DLL is outdated (no rotate / paint yaw) — "
                "update bridge binaries and restart the game",
                flush=True,
            )

    def _bridge_has_rotate(self):
        cached = getattr(self, "_bridge_commands", None)
        if cached is not None:
            return "rotate" in cached
        resp = self._bridge_request("capabilities", {}, timeout=5)
        if resp and resp.get("success"):
            cmds = (resp.get("metadata") or {}).get("commands") or []
            self._bridge_commands = set(cmds)
            return "rotate" in cmds
        probe = self._bridge_request("rotate", {"yaw": 0}, timeout=5)
        return bool(probe and probe.get("success"))

    def _try_direct_inject(self, port=BRIDGE_FIXED_PORT, force=False):
        """Inject meccha-xenos-bridge.dll (full TCP command set)."""
        loaded, mod_names, has_xenos, has_runtime = self._bridge_dll_state()
        tcp_ok = bool(self._resolve_bridge_port()) if has_xenos else False

        if loaded and has_xenos and not force and tcp_ok:
            print(
                f"[CAMO] meccha-xenos-bridge already loaded ({', '.join(mod_names)})",
                flush=True,
            )
            return True

        if loaded and not force:
            if has_runtime or has_xenos:
                print(
                    f"[CAMO] stale bridge in game ({', '.join(mod_names)}) — "
                    "unloading before inject",
                    flush=True,
                )
                self._unload_all_bridge_modules()
                import time as _t
                _t.sleep(0.4)
                loaded, mod_names, has_xenos, has_runtime = self._bridge_dll_state()
                if loaded:
                    print(
                        "[CAMO] bridge DLL(s) still loaded after unload — "
                        "restart the game and retry Paint Now.",
                        flush=True,
                    )
                    return False
            else:
                print(
                    f"[CAMO] unknown bridge loaded ({', '.join(mod_names)}) — "
                    "restart game before inject",
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

    def start_bridge_preload(self):
        """Inject bridge DLL in the background when Peterhack attaches to the game."""
        if getattr(self, "_bridge_preload_started", False):
            return
        self._bridge_preload_started = True
        self._bridge_preload_ok = None

        def _worker():
            print("[CAMO] auto-injecting bridge on connect...", flush=True)
            ok = self._ensure_bridge()
            self._bridge_preload_ok = ok
            if ok:
                print("[CAMO] bridge preload ready", flush=True)
            else:
                err = getattr(self, "_camo_last_error", None) or "unknown error"
                print(f"[CAMO] bridge preload failed: {err}", flush=True)

        self._bridge_preload_thread = threading.Thread(
            target=_worker, daemon=True, name="bridge-preload",
        )
        self._bridge_preload_thread.start()

    def _ensure_bridge(self):
        """Ensure meccha-xenos-bridge.dll is loaded — the only in-game bridge we use."""
        lock = getattr(self, "_bridge_ensure_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._bridge_ensure_lock = lock
        with lock:
            return self._ensure_bridge_impl()

    def _ensure_bridge_impl(self):
        import time as _t

        if not getattr(self, "pm", None) or not self.pm.process_id:
            print("[CAMO] no game pid", flush=True)
            return False

        self._stop_legacy_camo_controller()
        self._camo_last_error = None

        loaded, mod_names, has_xenos, has_runtime = self._bridge_dll_state()
        tcp_ok = bool(self._resolve_bridge_port())

        ready = tcp_ok and has_xenos and not has_runtime
        if ready:
            self._bridge_last_ok_ts = _t.time()
            print("[CAMO] meccha-xenos-bridge ready (TCP)", flush=True)
            self._log_bridge_capabilities()
            return True

        # Sidecar runtime-bridge (e.g. from old meccha-camouflage.exe) — reuse if TCP works.
        if tcp_ok and loaded and not has_xenos:
            resp = self._bridge_request("capabilities", {}, timeout=5)
            if resp and resp.get("success"):
                cmds = (resp.get("metadata") or {}).get("commands") or []
                if "paint_full_route" in cmds:
                    print(
                        f"[CAMO] reusing in-game bridge TCP ({', '.join(mod_names)})",
                        flush=True,
                    )
                    self._log_bridge_capabilities()
                    return True

        reasons = []
        if has_runtime:
            reasons.append("runtime-bridge loaded")
        if has_xenos and not tcp_ok:
            reasons.append("xenos TCP dead")
        if loaded and not has_xenos:
            reasons.append("unknown bridge")
        last_ok = getattr(self, "_bridge_last_ok_ts", 0.0)
        if has_xenos and not tcp_ok and last_ok and (_t.time() - last_ok) < 8.0:
            print("[CAMO] bridge TCP dropped briefly — waiting for listener...", flush=True)
            if self._wait_for_bridge_tcp("bridge reconnect", attempts=20, sleep_s=0.25):
                self._bridge_last_ok_ts = _t.time()
                self._log_bridge_capabilities()
                return True
        print(
            f"[CAMO] bridge reset ({', '.join(reasons) or 'not loaded'}) — "
            f"injecting {self.DLL_NAME}...",
            flush=True,
        )

        if loaded:
            self._unload_all_bridge_modules()
            _t.sleep(0.5)
            for attempt in range(3):
                still_loaded, still_names, _, _ = self._bridge_dll_state()
                if not still_loaded:
                    break
                if attempt < 2:
                    print(
                        f"[CAMO] bridge still loaded — retry unload ({attempt + 2}/3)...",
                        flush=True,
                    )
                    self._bridge_shutdown()
                    _t.sleep(0.6)
                    self._unload_all_bridge_modules()
                    _t.sleep(0.5)
            still_loaded, still_names, _, _ = self._bridge_dll_state()
            if still_loaded:
                msg = (
                    f"Stale bridge stuck in game ({', '.join(still_names)}). "
                    "Fully quit the game (not just menu), relaunch, join a match, "
                    "then Paint Now. Do not run meccha-camouflage.exe separately."
                )
                self._camo_last_error = msg
                print(f"[CAMO] {msg}", flush=True)
                return False

        try:
            missing = self._extract_stable_camo_files()
            if missing:
                msg = (
                    f"Missing bridge files: {', '.join(missing)}. "
                    f"Copy meccha-xenos-bridge.dll and meccha-xenos-injector.exe into "
                    f"{self._camo_bundle_dir()} or download the latest Peterhack release."
                )
                self._camo_last_error = msg
                print(f"[CAMO] {msg}", flush=True)
                return False
        except Exception as exc:
            self._camo_last_error = f"Bridge extract failed: {exc}"
            print(f"[CAMO] extract failed: {exc}", flush=True)
            return False

        self._write_port_sidecar(self._get_stable_dll_path(), BRIDGE_FIXED_PORT)

        if not self._try_direct_inject(BRIDGE_FIXED_PORT, force=True):
            msg = "Bridge inject failed — run Peterhack as Administrator."
            self._camo_last_error = msg
            print(f"[CAMO] {msg}", flush=True)
            return False

        print("[CAMO] waiting for meccha-xenos-bridge TCP...", flush=True)
        if self._wait_for_bridge_tcp("xenos bridge", attempts=80):
            self._bridge_last_ok_ts = _t.time()
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
        """Snapshot controller view for wrap restore (camera only — pawn stays fixed)."""
        state = {}
        if hasattr(self, "get_control_rotation"):
            rot = self.get_control_rotation()
            if rot:
                state["control"] = rot
        return state

    def _camo_apply_view_yaw_delta(self, state, yaw_offset):
        """Rotate camera to yaw_offset° from saved start (ControlRotation only)."""
        if not state or yaw_offset is None:
            return False
        if "control" not in state or not hasattr(self, "set_control_yaw"):
            return False
        pitch, yaw, roll = state["control"]
        return self.set_control_yaw(self._normalize_yaw_deg(yaw + yaw_offset))

    def _camo_restore_view_rotation(self, state):
        if not state or "control" not in state:
            return
        pitch, yaw, roll = state["control"]
        if hasattr(self, "native_set_control_rotation"):
            self.native_set_control_rotation(pitch, yaw, roll)
        elif hasattr(self, "set_control_rotation"):
            self.set_control_rotation(pitch, yaw, roll)
        elif hasattr(self, "set_control_yaw"):
            self.set_control_yaw(yaw)

    @staticmethod
    def _camo_normalize_pitch_deg(pitch):
        pitch = float(pitch) % 360.0
        if pitch > 180.0:
            pitch -= 360.0
        return pitch

    @staticmethod
    def _camo_rotation_to_axes(rot):
        """FRotator (pitch, yaw, roll degrees) → forward, right, up unit axes."""
        pitch, yaw, roll = [math.radians(float(x)) for x in rot]
        sp, cp = math.sin(pitch), math.cos(pitch)
        sy, cy = math.sin(yaw), math.cos(yaw)
        sr, cr = math.sin(roll), math.cos(roll)
        forward = (cp * cy, cp * sy, sp)
        right = (sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp)
        up = (-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp)
        return forward, right, up

    @staticmethod
    def _camo_direction_to_rotation(dx, dy, dz):
        """World look direction → UE ControlRotation (pitch, yaw, roll)."""
        mag = math.hypot(dx, dy, dz)
        if mag < 1e-9:
            return 0.0, 0.0, 0.0
        dx, dy, dz = dx / mag, dy / mag, dz / mag
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, dz))))
        yaw = math.degrees(math.atan2(dy, dx))
        return pitch, yaw, 0.0

    def _camo_get_body_rotation(self):
        """Pawn root FRotator (pitch, yaw, roll) — reference frame for wrap orbit."""
        if not hasattr(self, "_find_local_pawn") or not hasattr(self, "get_actor_root_rotation"):
            return None
        pawn = self._find_local_pawn()
        if not pawn:
            return None
        rot = self.get_actor_root_rotation(pawn)
        if not rot or not all(math.isfinite(float(v)) for v in rot):
            return None
        return (float(rot[0]), float(rot[1]), float(rot[2]))

    def _camo_is_emote_pose(self, control_rot=None, body_rot=None):
        """True when pawn root or controller view suggests emote/prone."""
        if body_rot:
            bp = self._camo_normalize_pitch_deg(body_rot[0])
            br = float(body_rot[2])
            if abs(bp) > 35.0 or abs(br) > 25.0:
                return True
        rot = control_rot
        if rot is None and hasattr(self, "get_control_rotation"):
            rot = self.get_control_rotation()
        if rot:
            cp = self._camo_normalize_pitch_deg(rot[0])
            cr = float(rot[2])
            if abs(cp) > 35.0 or abs(cr) > 25.0:
                return True
        return False

    @staticmethod
    def _camo_neg(v):
        return (-v[0], -v[1], -v[2])

    @staticmethod
    def _camo_normalize_vec(v):
        mag = math.hypot(v[0], v[1], v[2])
        if mag < 1e-9:
            return (0.0, 0.0, 0.0)
        return (v[0] / mag, v[1] / mag, v[2] / mag)

    @staticmethod
    def _camo_blend_dirs(a, b, wa, wb):
        return CamoBridgeMixin._camo_normalize_vec((
            a[0] * wa + b[0] * wb,
            a[1] * wa + b[1] * wb,
            a[2] * wa + b[2] * wb,
        ))

    def _camo_root_is_tilted(self, body_rot):
        if not body_rot:
            return False
        bp = self._camo_normalize_pitch_deg(body_rot[0])
        br = float(body_rot[2])
        return abs(bp) > 20.0 or abs(br) > 20.0

    def _camo_dynamic_body_axes(self, control_rot, body_rot):
        """Body forward/right/up for orbit — uses root rotation or view-inferred frame."""
        if body_rot and self._camo_root_is_tilted(body_rot):
            return self._camo_rotation_to_axes(body_rot)

        body_yaw = float(body_rot[1]) if body_rot else 0.0
        if hasattr(self, "_camo_get_body_forward_yaw"):
            by = self._camo_get_body_forward_yaw()
            if by is not None:
                body_yaw = float(by)

        if control_rot:
            cp = self._camo_normalize_pitch_deg(control_rot[0])
            cr = float(control_rot[2])
            if abs(cp) > 20.0 or abs(cr) > 20.0:
                rad = math.radians(body_yaw)
                forward = (math.cos(rad), math.sin(rad), 0.0)
                right = (-forward[1], forward[0], 0.0)
                up = (0.0, 0.0, 1.0)
                if abs(cr) > 20.0:
                    crad = math.radians(cr)
                    cos_r, sin_r = math.cos(crad), math.sin(crad)
                    forward = (
                        forward[0] * cos_r + up[0] * sin_r,
                        forward[1] * cos_r + up[1] * sin_r,
                        forward[2] * cos_r + up[2] * sin_r,
                    )
                    right = (
                        right[0] * cos_r + up[0] * sin_r,
                        right[1] * cos_r + up[1] * sin_r,
                        right[2] * cos_r + up[2] * sin_r,
                    )
                return forward, right, up

        eff = body_rot if body_rot else (0.0, body_yaw, 0.0)
        return self._camo_rotation_to_axes(eff)

    def _camo_orbit_basis(self, forward, right, up, control_rot=None, root_tilted=False):
        """Pick orbit plane (two axes) from body orientation + current view."""
        forward = self._camo_normalize_vec(forward)
        right = self._camo_normalize_vec(right)
        up = self._camo_normalize_vec(up)
        up_align = abs(up[2])
        fwd_horiz = math.hypot(forward[0], forward[1])

        cp = 0.0
        if control_rot:
            cp = self._camo_normalize_pitch_deg(control_rot[0])
        view_tilted = abs(cp) > 35.0

        # Lay-down / look-down emote with upright root — front/back vertical, sides horizontal.
        if view_tilted and not root_tilted:
            return up, right, "dynamic-flat"

        if root_tilted:
            if up_align > 0.55 and fwd_horiz > 0.35:
                return forward, right, "dynamic-tilted-upright"
            return up, right, "dynamic-tilted"

        # Standing / normal locomotion — yaw orbit in horizontal plane.
        if up_align > 0.65 and fwd_horiz > 0.35:
            return forward, right, "dynamic-upright"

        # On-back root or ambiguous — vertical front/back.
        return up, right, "dynamic-supine"

    def _camo_detail_look_direction(self, forward, right, up, label, orbit_mode):
        """Extra camera aim for UV islands missed by horizontal side passes."""
        neg_up = self._camo_neg(up)
        if label == "head_shoulders":
            # Downward front tilt — head crown + shoulder tops (green/red head islands).
            if orbit_mode == "dynamic-flat":
                look = self._camo_blend_dirs(forward, neg_up, 0.30, 0.95)
            else:
                look = self._camo_blend_dirs(forward, neg_up, 0.42, 0.91)
        elif label == "inner_legs":
            # Upward front tilt — crotch + inner thighs (red leg-inner island).
            if orbit_mode == "dynamic-flat":
                look = self._camo_blend_dirs(right, up, 0.50, 0.82)
            else:
                look = self._camo_blend_dirs(forward, up, 0.52, 0.80)
        else:
            return (0.0, 0.0, 0.0)
        return look

    def _camo_plan_wrap_orbit(self, control_rot=None):
        """Compute wrap + detail camera passes from body axes — any pose, one code path."""
        body_rot = self._camo_get_body_rotation()
        if not body_rot:
            body_yaw = 0.0
            if control_rot:
                body_yaw = float(control_rot[1])
            body_rot = (0.0, body_yaw, 0.0)

        root_tilted = self._camo_root_is_tilted(body_rot)
        emote = self._camo_is_emote_pose(control_rot, body_rot)

        self._camo_body_yaw = float(body_rot[1])
        self._camo_body_rot = body_rot
        self._camo_emote_pose = emote

        forward, right, up = self._camo_dynamic_body_axes(control_rot, body_rot)
        axis_a, axis_b, orbit_mode = self._camo_orbit_basis(
            forward, right, up, control_rot, root_tilted
        )
        self._camo_orbit_mode = orbit_mode

        planned = []
        for yaw_offset, label in self.CAMO_WRAP_YAW_PASSES:
            ang = math.radians(float(yaw_offset))
            ox = axis_a[0] * math.cos(ang) + axis_b[0] * math.sin(ang)
            oy = axis_a[1] * math.cos(ang) + axis_b[1] * math.sin(ang)
            oz = axis_a[2] * math.cos(ang) + axis_b[2] * math.sin(ang)
            look = self._camo_normalize_vec((ox, oy, oz))
            if look == (0.0, 0.0, 0.0):
                continue
            cam = self._camo_direction_to_rotation(*look)
            planned.append((cam, label, int(yaw_offset)))

        for detail_idx, label in enumerate(self.CAMO_DETAIL_PASSES):
            look = self._camo_detail_look_direction(
                forward, right, up, label, orbit_mode
            )
            if look == (0.0, 0.0, 0.0):
                continue
            cam = self._camo_direction_to_rotation(*look)
            planned.append((cam, label, -100 - detail_idx))

        return planned, emote, body_rot

    def _camo_is_prone_or_emote(self, control_rot=None):
        """True when pitch/roll suggest lay-down emote or prone."""
        return self._camo_is_emote_pose(control_rot, self._camo_get_body_rotation())

    def _camo_set_camera_rotation(self, pitch, yaw, roll):
        if hasattr(self, "native_set_control_rotation"):
            return bool(self.native_set_control_rotation(pitch, yaw, roll))
        if hasattr(self, "set_control_rotation"):
            return bool(self.set_control_rotation(pitch, yaw, roll))
        return False

    def _camo_rotate_camera(self, yaw_delta, label=""):
        """Rotate controller view yaw (ControlRotation) via bridge."""
        import time as _t

        note = f" ({label})" if label else ""
        print(f"[CAMO] camera rotate{note} delta={yaw_delta:.0f}°...", flush=True)
        resp = self._bridge_request(
            "rotate",
            {"yaw": float(yaw_delta), "target": "camera"},
            timeout=10,
        )
        print(f"[CAMO] camera rotate response={resp}", flush=True)
        if resp and resp.get("success"):
            _t.sleep(self.CAMO_CAMERA_SETTLE_MS / 1000.0)
            return True
        if hasattr(self, "native_rotate_yaw_delta") and self.native_rotate_yaw_delta(yaw_delta):
            _t.sleep(self.CAMO_CAMERA_SETTLE_MS / 1000.0)
            return True
        return False

    def _camo_pass_camera_from_control(self, control, yaw_offset=0):
        if not control:
            return None
        pitch, yaw, roll = control
        return (
            float(pitch),
            self._normalize_yaw_deg(float(yaw) + float(yaw_offset)),
            float(roll),
        )

    def _camo_get_body_forward_yaw(self):
        """Pawn root yaw — camo pass angles are relative to body facing, not world north."""
        if not hasattr(self, "_find_local_pawn") or not hasattr(self, "get_actor_root_rotation"):
            return None
        pawn = self._find_local_pawn()
        if not pawn:
            return None
        rot = self.get_actor_root_rotation(pawn)
        if not rot:
            return None
        yaw = float(rot[1])
        return yaw if math.isfinite(yaw) else None

    def _camo_reset_to_default_view(self):
        """Align camera to pawn forward (chest view) before camouflage."""
        body_yaw = self._camo_get_body_forward_yaw()
        if body_yaw is None:
            body_yaw = 0.0
            if hasattr(self, "get_control_rotation"):
                rot = self.get_control_rotation()
                if rot:
                    body_yaw = float(rot[1])
        self._camo_body_yaw = body_yaw
        pitch, roll = 0.0, 0.0
        if hasattr(self, "native_set_control_rotation"):
            if self.native_set_control_rotation(pitch, body_yaw, roll):
                return True
        if hasattr(self, "set_control_rotation"):
            if self.set_control_rotation(pitch, body_yaw, roll):
                return True
        return False

    def _camo_rotate_pawn(self, yaw_delta, label=""):
        """Rotate local pawn yaw via bridge K2_SetActorRotation (SilentJMA back-pass)."""
        import time as _t

        note = f" ({label})" if label else ""
        print(f"[CAMO] pawn rotate{note} delta={yaw_delta:.0f}°...", flush=True)
        resp = self._bridge_request(
            "rotate",
            {"yaw": float(yaw_delta), "target": "pawn"},
            timeout=10,
        )
        print(f"[CAMO] pawn rotate response={resp}", flush=True)
        if resp and resp.get("success"):
            _t.sleep(self.CAMO_CAMERA_SETTLE_MS / 1000.0)
            return True
        return False

    def _camo_body_pass_rotation(self, yaw_offset):
        """Legacy standing-only yaw orbit — prefer _camo_plan_wrap_orbit()."""
        base_yaw = getattr(self, "_camo_body_yaw", None)
        if base_yaw is None:
            base_yaw = self._camo_get_body_forward_yaw() or 0.0
        return (
            0.0,
            self._normalize_yaw_deg(float(base_yaw) + float(yaw_offset)),
            0.0,
        )

    # Pass order: front → sides → back → detail (head/shoulders, inner legs).
    # Yaw orbit is in the body forward/right plane. Bridge scene capture places the
    # camera at body - look*pullback facing look, so +forward views the spine/back
    # and -forward views the chest/front (not the raw axis label).
    CAMO_WRAP_YAW_PASSES = (
        (180, "front"),
        (90, "left"),
        (270, "right"),
        (0, "back"),
    )
    CAMO_DETAIL_PASSES = (
        "head_shoulders",
        "inner_legs",
    )
    CAMO_PAWN_BACK_YAW = 0
    CAMO_CAMERA_SETTLE_MS = 2000
    CAMO_DEFAULT_SETTLE_SEC = 0.5

    def _camo_rotate_to_yaw(self, target_yaw, label):
        """Rotate to yaw offset from session start — bridge delta, then ProcessEvent, then memory."""
        import time as _t

        current = getattr(self, "_camo_yaw_offset", 0)
        delta = int(target_yaw) - int(current)
        if not delta:
            return True

        bridge_cmds = getattr(self, "_bridge_commands", None)
        if bridge_cmds is None or "rotate" in bridge_cmds:
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

        if hasattr(self, "native_rotate_yaw_delta"):
            print(
                f"[CAMO] rotate {label} delta={delta}° (camera ProcessEvent)...",
                flush=True,
            )
            if self.native_rotate_yaw_delta(delta):
                verify = ""
                if hasattr(self, "get_control_rotation"):
                    rot = self.get_control_rotation()
                    if rot:
                        verify = f" control_yaw={rot[1]:.1f}°"
                print(f"[CAMO] native rotate ok{verify}", flush=True)
                _t.sleep(1.5)
                self._camo_yaw_offset = target_yaw
                return True

        state = getattr(self, "_camo_view_state", None)
        if state:
            print("[CAMO] ProcessEvent rotate failed — memory fallback...", flush=True)
            if self._camo_apply_view_yaw_delta(state, target_yaw):
                _t.sleep(1.5)
                self._camo_yaw_offset = target_yaw
                return True

        return False

    CAMO_PAINT_ROUTE_TIMEOUT = 300

    def _camo_set_collision_free(self, enable, label=""):
        """Disable pawn collision (noclip) — used on front pass only."""
        if not hasattr(self, "_trainer_anti_clipping") or not hasattr(self, "_find_local_pawn"):
            return
        pawn = self._find_local_pawn()
        cfg = getattr(self, "config", None)
        if not pawn or not cfg:
            return
        user_noclip = bool(getattr(cfg, "trainer_anti_clipping", False))
        note = f" ({label})" if label else ""
        if enable:
            if user_noclip:
                self._camo_noclip_owned = False
                self._camo_noclip_hold = True
                print(f"[CAMO] noclip already on (trainer toggle){note}", flush=True)
                return
            self._trainer_anti_clipping(pawn, cfg, True)
            self._camo_noclip_owned = True
            self._camo_noclip_hold = True
            print(f"[CAMO] noclip on for front pass (held through paint){note}", flush=True)
        elif getattr(self, "_camo_noclip_owned", False) or getattr(self, "_camo_noclip_hold", False):
            self._camo_noclip_hold = False
            if getattr(self, "_camo_noclip_owned", False):
                self._trainer_anti_clipping(pawn, cfg, False)
                self._camo_noclip_owned = False
            print(f"[CAMO] noclip restored after front pass{note}", flush=True)

    def _paint_payload(
        self,
        pid,
        camera_yaw_offset=0,
        camera_rotation=None,
        camera_body_anchor=False,
        camera_body_pullback=150.0,
    ):
        cfg = getattr(self, "config", None)
        quality = max(1, min(20, int(getattr(cfg, "paint_quality", 12) if cfg else 12)))
        min_points = {
            1: 2500, 5: 6000, 8: 9000, 10: 12000, 12: 15000,
            14: 18000, 16: 22000, 18: 26000, 20: 30000,
        }
        keys = sorted(min_points)
        floor = min_points[keys[0]]
        for k in keys:
            if quality >= k:
                floor = min_points[k]
        payload = {
            "native_apply_mode": "template_brush_paint",
            "route": "f10_template_brush_paint",
            "process": {
                "pid": pid,
                "name": getattr(self, "PROCESS_NAME", "PenguinHotel-Win64-Shipping.exe"),
            },
            "max_paints_per_tick": 256,
            "paint_tick_budget_ms": 16,
            "paint_quality": quality,
            "template_min_direct_points": floor,
            "auto_flush_during_paint": True,
        }
        if camera_rotation:
            pitch, yaw, roll = camera_rotation
            payload["camera_rotation_absolute"] = True
            payload["camera_pitch"] = float(pitch)
            payload["camera_yaw"] = float(yaw)
            payload["camera_roll"] = float(roll)
            if camera_body_anchor:
                payload["camera_use_body_anchor"] = True
                payload["camera_body_pullback"] = float(camera_body_pullback)
        elif camera_yaw_offset:
            payload["camera_yaw_offset"] = int(camera_yaw_offset)
        payload["camera_settle_ms"] = int(
            getattr(self, "CAMO_CAMERA_SETTLE_MS", 1500)
        )
        return payload

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

    def camo_apply(self, r=None, g=None, b=None, a=None, full_wrap=True):
        """Environment camo — 6-pass wrap (4 orbit + 2 detail) via paint_full_route."""
        del r, g, b, a
        full_wrap = True
        if not getattr(self, "pm", None) or not self.pm:
            print("[CAMO] no pymem handle", flush=True)
            return False

        self._camo_abort = False
        self._bridge_port = None
        self._camo_noclip_owned = False
        self._camo_noclip_hold = False
        try:
            pid = self.pm.process_id
            print(f"[CAMO] pid={pid} wrap={full_wrap}", flush=True)
            cfg = getattr(self, "config", None)
            q = getattr(cfg, "paint_quality", 12) if cfg else 12
            print(f"[CAMO] paint quality={q}/20", flush=True)
            if not pid:
                return False

            if not self._ensure_bridge():
                return False

            import time as _t

            self._camo_view_state = self._camo_save_view_rotation() if full_wrap else None
            saved_control = (
                self._camo_view_state.get("control") if self._camo_view_state else None
            )

            wrap_passes, emote_pose, body_rot = self._camo_plan_wrap_orbit(saved_control)
            if not full_wrap:
                wrap_passes = wrap_passes[:1]
            skip_front = bool(getattr(cfg, "camo_skip_front_pass", False)) if cfg else False
            back_only = bool(getattr(cfg, "camo_back_pass_only", False)) if cfg else False
            if back_only and full_wrap:
                before = len(wrap_passes)
                wrap_passes = [p for p in wrap_passes if p[1] == "back"]
                if len(wrap_passes) < before:
                    print("[CAMO] back pass only mode", flush=True)
            elif skip_front and full_wrap:
                before = len(wrap_passes)
                wrap_passes = [p for p in wrap_passes if p[1] != "front"]
                if len(wrap_passes) < before:
                    print("[CAMO] front pass disabled (flat map option)", flush=True)
            if not wrap_passes:
                print("[CAMO] orbit plan empty — cannot paint", flush=True)
                self._camo_last_error = "Could not compute camo camera orbit"
                return False

            body_pitch = self._camo_normalize_pitch_deg(body_rot[0])
            body_roll = float(body_rot[2])
            orbit_mode = getattr(self, "_camo_orbit_mode", "dynamic")
            print(
                f"[CAMO] single camo_apply: {len(wrap_passes)}-pass wrap "
                f"(4 orbit + 2 detail, {orbit_mode}, "
                f"body pitch={body_pitch:.0f}° roll={body_roll:.0f}°)",
                flush=True,
            )
            self._camo_last_error = None

            upright_orbit = orbit_mode == "dynamic-upright"
            if full_wrap and upright_orbit:
                body_yaw = self._camo_get_body_forward_yaw()
                print(
                    f"[CAMO] aligning camera to pawn forward "
                    f"(body yaw={body_yaw if body_yaw is not None else '?'}°)...",
                    flush=True,
                )
                if not self._camo_reset_to_default_view():
                    print("[CAMO] camera align failed — continuing", flush=True)
                else:
                    print(
                        f"[CAMO] camera aligned (body yaw={getattr(self, '_camo_body_yaw', 0):.0f}°)",
                        flush=True,
                    )
                _t.sleep(self.CAMO_DEFAULT_SETTLE_SEC)

            for idx, (cam, label, yaw_offset) in enumerate(wrap_passes):
                pitch, yaw, roll = cam
                print(
                    f"[CAMO] orbit plan {idx + 1}/{len(wrap_passes)} "
                    f"({label}, offset={yaw_offset}°, "
                    f"cam pitch={pitch:.0f}° yaw={yaw:.0f}° roll={roll:.0f}°)",
                    flush=True,
                )

            try:
                for pass_idx, (cam, label, yaw_offset) in enumerate(wrap_passes):
                    if self._camo_aborted():
                        print("[CAMO] apply aborted", flush=True)
                        return False
                    pitch, yaw, roll = cam
                    front_pass = label == "front"
                    steep_up = label == "inner_legs" and pitch > 40.0
                    use_body_anchor = front_pass or steep_up
                    print(
                        f"[CAMO] paint pass {pass_idx + 1}/{len(wrap_passes)} "
                        f"({label}, offset={yaw_offset}°, "
                        f"cam pitch={pitch:.0f}° yaw={yaw:.0f}° roll={roll:.0f}°, "
                        f"settle={self.CAMO_CAMERA_SETTLE_MS}ms)"
                        f"{' [noclip]' if front_pass else ''}...",
                        flush=True,
                    )
                    if front_pass:
                        self._camo_set_collision_free(True, label="front")
                    try:
                        payload = self._paint_payload(
                            pid,
                            camera_rotation=cam,
                            camera_body_anchor=use_body_anchor,
                            camera_body_pullback=280.0
                            if (use_body_anchor and not upright_orbit)
                            else 150.0,
                        )
                        resp = self._bridge_request(
                            "paint_full_route",
                            payload,
                            timeout=self.CAMO_PAINT_ROUTE_TIMEOUT,
                        )
                        print(f"[CAMO] paint response={resp}", flush=True)
                        if not resp or not resp.get("success", False):
                            err = resp.get("message") or resp.get("stage") if resp else "no response"
                            self._camo_last_error = f"Paint pass {label} failed: {err}"
                            print(f"[CAMO] {self._camo_last_error}", flush=True)
                            self._finalize_camo_paint()
                            return False
                    finally:
                        if front_pass:
                            self._camo_set_collision_free(False, label="front")
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
                self._camo_set_collision_free(False)
                self._camo_noclip_hold = False
                self._finalize_camo_paint()
            except Exception:
                pass
            return False

    def camo_stop(self):
        """Cancel active camouflage paint."""
        self._camo_abort = True
        self._camo_set_collision_free(False, label="stop")
        self._camo_noclip_hold = False
        print("[CAMO] sending cancel_paint...", flush=True)
        resp = self._bridge_request("cancel_paint", {}, timeout=5)
        print(f"[CAMO] cancel response={resp}", flush=True)
        if hasattr(self, "_force_quiesce_camo_paint"):
            try:
                self._force_quiesce_camo_paint(label="CAMO-STOP", quiet=False)
            except Exception:
                pass
        return bool(resp and resp.get("success", False))

    def bridge_teleport(self, x, y, z):
        """Teleport local pawn via bridge K2_SetActorLocation."""
        print(f"[CAMO] teleport ({x:.1f}, {y:.1f}, {z:.1f})...", flush=True)
        resp = self._bridge_request("teleport", {"x": float(x), "y": float(y), "z": float(z)}, timeout=30)
        ok = bool(resp and resp.get("success"))
        print(f"[CAMO] teleport {'ok' if ok else 'failed'}: {resp}", flush=True)
        return ok

    def bridge_set_fov(self, fov):
        """Override camera FOV via bridge."""
        print(f"[CAMO] set_fov {fov}...", flush=True)
        resp = self._bridge_request("set_fov", {"fov": float(fov)}, timeout=30)
        ok = bool(resp and resp.get("success"))
        print(f"[CAMO] set_fov {'ok' if ok else 'failed'}: {resp}", flush=True)
        return ok

    def bridge_kill(self, enemies=False):
        """Kill local player via bridge (enemies=True not implemented in bridge)."""
        target = "enemies" if enemies else "self"
        print(f"[CAMO] kill {target}...", flush=True)
        resp = self._bridge_request("kill", {"enemies": bool(enemies)}, timeout=30)
        ok = bool(resp and resp.get("success"))
        print(f"[CAMO] kill {'ok' if ok else 'failed'}: {resp}", flush=True)
        return ok

    def bridge_set_anti_kick(self, enabled=True):
        """Enable/disable in-game kick RPC blocking via bridge ProcessEvent hook."""
        state = "on" if enabled else "off"
        print(f"[CAMO] set_anti_kick {state}...", flush=True)
        resp = self._bridge_request("set_anti_kick", {"enabled": bool(enabled)}, timeout=20)
        ok = bool(resp and resp.get("success"))
        print(f"[CAMO] set_anti_kick {'ok' if ok else 'failed'}: {resp}", flush=True)
        return ok

    def bridge_get_anti_kick_log(self, since_seq=0):
        """Fetch kick RPC blocks logged by the bridge anti-kick hook."""
        resp = self._bridge_request("get_anti_kick_log", {"since_seq": int(since_seq)}, timeout=10)
        return resp if resp and resp.get("success") else None

    def bridge_set_player_name(self, name):
        """Replicated rename via SetName(Server) on the game thread."""
        name = (name or "").strip()[:32]
        if not name:
            return False
        print(f"[CAMO] set_player_name '{name}'...", flush=True)
        resp = self._bridge_request("set_player_name", {"name": name}, timeout=30)
        ok = bool(resp and resp.get("success"))
        print(f"[CAMO] set_player_name {'ok' if ok else 'failed'}: {resp}", flush=True)
        return ok

    def bridge_sdk_probe(self, deep=False):
        """Pre-flight SDK validation via bridge (no paint)."""
        cmd = "sdk_deep_probe" if deep else "sdk_probe"
        resp = self._bridge_request(cmd, {"type": cmd}, timeout=30)
        ok = bool(resp and resp.get("success"))
        print(f"[CAMO] {cmd} {'ok' if ok else 'failed'}: {resp}", flush=True)
        return resp
