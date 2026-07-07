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
BRIDGE_QUICK_PING_TIMEOUT = 0.6
BRIDGE_PING_RETRIES = 3
BRIDGE_PORT_TRUST_SEC = 20.0
BRIDGE_FIXED_PORT = 47654
# How many ports above the fixed port to probe when the listener isn't on 47654.
BRIDGE_PORT_SCAN_RANGE = 16


class CamoBridgeMixin:
    """In-game bridge via meccha-xenos-bridge.dll."""

    DLL_NAME = "meccha-xenos-bridge.dll"
    EXE_NAME = "meccha-camouflage.exe"  # legacy bundle artifact — not launched by Peterhack
    INJECTOR_NAME = "meccha-xenos-injector.exe"
    BRIDGE_FOLDER = "bridge"
    BRIDGE_HOST = "127.0.0.1"
    BRIDGE_PORT = BRIDGE_FIXED_PORT
    # Legacy controller sidecars only — not used for inject.
    RUNTIME_DIR = os.path.join(PETERHACK_ROOT, "logs", "bridge")

    @classmethod
    def _repo_root(cls):
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    @classmethod
    def _bridge_dir(cls):
        """Shipped bridge binaries live in <repo>/bridge/ (committed to GitHub)."""
        return os.path.join(cls._repo_root(), cls.BRIDGE_FOLDER)

    @classmethod
    def _embedded_bridge_dir(cls):
        """PyInstaller bundle or legacy meccha_chameleon_tools fallback."""
        if getattr(sys, "frozen", False):
            bundled = os.path.join(sys._MEIPASS, cls.BRIDGE_FOLDER)
            if os.path.isdir(bundled):
                return bundled
            return sys._MEIPASS
        legacy = os.path.dirname(os.path.abspath(__file__))
        bridge = cls._bridge_dir()
        if os.path.isfile(os.path.join(bridge, cls.DLL_NAME)):
            return bridge
        return legacy

    @classmethod
    def _get_bridge_dll_path(cls):
        return os.path.join(cls._bridge_dir(), cls.DLL_NAME)

    @classmethod
    def _get_bridge_injector_path(cls):
        return os.path.join(cls._bridge_dir(), cls.INJECTOR_NAME)

    @classmethod
    def _get_bridge_exe_path(cls):
        return os.path.join(cls._bridge_dir(), cls.EXE_NAME)

    BRIDGE_DLL_MARKERS = (
        "meccha-xenos-bridge",
        "runtime-bridge",
        "xenos-bridge",
    )
    RUNTIME_BRIDGE_MARKERS = ("runtime-bridge",)
    XENOS_BRIDGE_MARKERS = ("meccha-xenos-bridge", "xenos-bridge")
    REQUIRED_BRIDGE_COMMANDS = frozenset({
        "get_skeleton",
        "set_anti_kick",
        "paint_full_route",
        "teleport",
    })

    @classmethod
    def _bridge_file_fingerprint(cls, path):
        try:
            stat = os.stat(path)
            return int(stat.st_size), int(stat.st_mtime)
        except OSError:
            return None, None

    @classmethod
    def _format_bridge_fingerprint(cls, path):
        size, mtime = cls._bridge_file_fingerprint(path)
        if size is None:
            return "missing"
        import datetime as _dt
        stamp = _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        return f"{size} bytes, {stamp}"

    @classmethod
    def _validate_inject_dll_path(cls, dll_path):
        """Peterhack must inject only the stable xenos bridge — never runtime-bridge."""
        if not dll_path:
            return False, "bridge DLL path is empty"
        base = os.path.basename(dll_path)
        if base.lower() != cls.DLL_NAME.lower():
            return False, f"refusing wrong DLL name: {base!r} (expected {cls.DLL_NAME})"
        norm = os.path.normcase(os.path.abspath(dll_path))
        if "runtime-bridge" in norm:
            return False, "refusing runtime-bridge DLL — use meccha-xenos-bridge.dll"
        expected = os.path.normcase(os.path.abspath(cls._get_bridge_dll_path()))
        if norm != expected:
            return False, f"refusing unexpected inject path: {dll_path} (expected {expected})"
        if not os.path.isfile(dll_path):
            return False, f"bridge DLL not found: {dll_path}"
        if os.path.getsize(dll_path) <= 0:
            return False, f"bridge DLL is empty: {dll_path}"
        return True, ""

    def _verify_bridge_identity(self):
        """Confirm TCP bridge is the merged Peterhack xenos DLL, not legacy runtime-bridge."""
        loaded, mod_names, has_xenos, has_runtime = self._bridge_dll_state()
        if has_runtime and not has_xenos:
            return False, "runtime-bridge loaded — quit game and reconnect so Peterhack can inject meccha-xenos-bridge.dll"
        if loaded and not has_xenos:
            return False, f"unknown bridge module(s): {', '.join(mod_names)}"

        resp = self._bridge_request("capabilities", {}, timeout=5)
        if not resp or not resp.get("success"):
            return False, "bridge capabilities probe failed"
        meta = resp.get("metadata") or {}
        route = meta.get("paint_full_route")
        if route != "mesh_first_paint":
            return False, (
                f"wrong bridge build (paint_full_route={route!r}) — "
                "fully quit the game, then restart Peterhack to inject the updated meccha-xenos-bridge.dll"
            )
        cmds = set((meta.get("commands") or []))
        missing = sorted(self.REQUIRED_BRIDGE_COMMANDS - cmds)
        if missing:
            return False, f"bridge missing Peterhack commands: {', '.join(missing)}"
        return True, ""

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
        add(self._read_port_file(self._get_bridge_dll_path() + ".port"))

        sidecars = []
        for pattern in (
            os.path.join(self.RUNTIME_DIR, "native", "*.dll.port"),
            os.path.join(self._bridge_dir(), "*.dll.port"),
        ):
            sidecars.extend(glob.glob(pattern))
        sidecars.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for path in sidecars[:6]:
            add(self._read_port_file(path))

        # Fallback: scan a small range above the fixed port. The DLL normally
        # binds BRIDGE_FIXED_PORT, but if that port was taken at inject time it
        # may have landed on a neighbour — probe those before giving up.
        for offset in range(1, BRIDGE_PORT_SCAN_RANGE + 1):
            add(BRIDGE_FIXED_PORT + offset)

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

    def _resolve_bridge_port(self, fast=False):
        """Find the live bridge TCP port (controller uses dynamic ports)."""
        import time as _t

        now = _t.time()
        cached = getattr(self, "_bridge_port", None)
        trust_until = getattr(self, "_bridge_port_trust_until", 0.0)
        if cached and now < trust_until:
            return cached

        if cached:
            if self._ping_port(cached, timeout=BRIDGE_QUICK_PING_TIMEOUT, quiet=True):
                self._bridge_port_trust_until = now + BRIDGE_PORT_TRUST_SEC
                self._bridge_last_ok_ts = now
                return cached
            self._bridge_port = None
            if fast:
                return None

        for port in self._discover_bridge_ports():
            if self._ping_port(port, timeout=BRIDGE_QUICK_PING_TIMEOUT, quiet=True):
                if getattr(self, "_bridge_port", None) != port:
                    print(f"[CAMO] bridge TCP on 127.0.0.1:{port}", flush=True)
                self._bridge_port = port
                self._bridge_port_trust_until = now + BRIDGE_PORT_TRUST_SEC
                self._bridge_last_ok_ts = now
                return port
        self._bridge_port = None
        self._bridge_port_trust_until = 0.0
        return None

    def _bridge_request(self, command, payload=None, timeout=30):
        port = self._resolve_bridge_port(fast=True)
        if not port:
            port = self._resolve_bridge_port(fast=False)
        if not port:
            return {"success": False}
        resp = self._bridge_request_on_port(port, command, payload, timeout=timeout)
        if resp and self._bridge_response_ok(resp):
            import time as _t
            self._bridge_port_trust_until = _t.time() + BRIDGE_PORT_TRUST_SEC
            self._bridge_last_ok_ts = _t.time()
        elif port == getattr(self, "_bridge_port", None):
            self._bridge_port_trust_until = 0.0
        return resp

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
        """Clean up external helpers on exit.

        Deliberately does NOT send "shutdown" to the in-game bridge: that stops
        the DLL's TCP listener while the DLL stays loaded in the game, which
        bricks reconnection when Peterhack is restarted mid-match (the listener
        is dead and re-injecting can't restart the thread). Leaving the bridge
        running lets a restarted Peterhack reconnect on the same port instantly.
        The bridge dies naturally when the game process closes.
        """
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

    def _ensure_bridge_files(self):
        """Ensure repo bridge/ has DLL, injector, and mesh-profiles (sync from EXE bundle when frozen)."""
        import shutil

        bridge_dir = self._bridge_dir()
        os.makedirs(bridge_dir, exist_ok=True)
        os.makedirs(self.RUNTIME_DIR, exist_ok=True)
        missing = []
        embedded = self._embedded_bridge_dir()

        def _files_match(src_fn, dst_fn):
            if not os.path.isfile(src_fn) or not os.path.isfile(dst_fn):
                return False
            src_size, src_mtime = self._bridge_file_fingerprint(src_fn)
            dst_size, dst_mtime = self._bridge_file_fingerprint(dst_fn)
            return src_size == dst_size and src_mtime == dst_mtime

        def _safe_copy(src_fn, dst_fn, label):
            if not os.path.isfile(src_fn):
                return os.path.isfile(dst_fn)
            if os.path.isfile(dst_fn) and _files_match(src_fn, dst_fn):
                return True
            try:
                os.makedirs(os.path.dirname(dst_fn), exist_ok=True)
                shutil.copy2(src_fn, dst_fn)
                print(
                    f"[CAMO] refreshed {label} → {dst_fn} "
                    f"({self._format_bridge_fingerprint(dst_fn)})",
                    flush=True,
                )
                return True
            except (PermissionError, OSError) as exc:
                winerr = getattr(exc, "winerror", None)
                if os.path.isfile(dst_fn) and os.path.getsize(dst_fn) > 0:
                    if winerr == 32 or isinstance(exc, PermissionError):
                        src_fp = self._format_bridge_fingerprint(src_fn)
                        dst_fp = self._format_bridge_fingerprint(dst_fn)
                        if not _files_match(src_fn, dst_fn):
                            print(
                                f"[CAMO] {label} locked in game — using {dst_fn} ({dst_fp}); "
                                f"bundle has newer {src_fp}. Fully quit the game to pick up updates.",
                                flush=True,
                            )
                        else:
                            print(
                                f"[CAMO] {label} in use — using existing copy ({dst_fp})",
                                flush=True,
                            )
                        return True
                raise

        print(
            f"[CAMO] bridge dir={bridge_dir} "
            f"dll={self._format_bridge_fingerprint(self._get_bridge_dll_path())}",
            flush=True,
        )

        embedded_dll = os.path.join(embedded, self.DLL_NAME)
        embedded_inj = os.path.join(embedded, self.INJECTOR_NAME)
        if not os.path.isfile(embedded_dll):
            embedded_dll = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), self.DLL_NAME,
            )
            embedded_inj = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), self.INJECTOR_NAME,
            )

        for src_fn, dst_fn, label in (
            (embedded_dll, self._get_bridge_dll_path(), self.DLL_NAME),
            (embedded_inj, self._get_bridge_injector_path(), self.INJECTOR_NAME),
        ):
            src_norm = os.path.normcase(os.path.abspath(src_fn))
            dst_norm = os.path.normcase(os.path.abspath(dst_fn))
            if src_norm == dst_norm:
                if not os.path.isfile(dst_fn):
                    missing.append(label)
                continue
            try:
                if not _safe_copy(src_fn, dst_fn, label):
                    missing.append(label)
            except Exception as exc:
                if os.path.isfile(dst_fn) and os.path.getsize(dst_fn) > 0:
                    print(
                        f"[CAMO] could not refresh {label}: {exc} — using existing",
                        flush=True,
                    )
                else:
                    missing.append(label)

        profiles_dst = os.path.join(bridge_dir, "mesh-profiles")
        profiles_src_candidates = [
            os.path.join(embedded, "mesh-profiles"),
            os.path.join(self._repo_root(), "runtime", "resources", "mesh-profiles"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "mesh-profiles"),
        ]
        profiles_src = ""
        for candidate in profiles_src_candidates:
            if os.path.isdir(candidate):
                profiles_src = candidate
                break

        src_norm = os.path.normcase(os.path.abspath(profiles_src)) if profiles_src else ""
        dst_norm = os.path.normcase(os.path.abspath(profiles_dst))
        has_profiles = os.path.isdir(profiles_dst) and any(
            name.endswith(".json") for name in os.listdir(profiles_dst)
        )

        if profiles_src and src_norm != dst_norm:
            try:
                if os.path.isdir(profiles_dst):
                    shutil.rmtree(profiles_dst, ignore_errors=True)
                shutil.copytree(profiles_src, profiles_dst)
                print(f"[CAMO] refreshed mesh-profiles → {profiles_dst}", flush=True)
            except Exception as exc:
                print(f"[CAMO] could not copy mesh-profiles: {exc}", flush=True)
        elif profiles_src and src_norm == dst_norm and has_profiles:
            pass  # already in bridge/
        elif not has_profiles:
            missing.append("mesh-profiles")
            print(
                "[CAMO] error: mesh-profiles missing in bridge/ — "
                "run runtime/scripts/build.ps1 or copy runtime/resources/mesh-profiles",
                flush=True,
            )

        if not missing:
            print(f"[CAMO] bridge files ready in {bridge_dir}", flush=True)
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

    @staticmethod
    def _bridge_stuck_message(module_names):
        mods = ", ".join(module_names) if module_names else "meccha-xenos-bridge.dll"
        return (
            f"Bridge loaded but TCP not responding ({mods}). "
            "ESP and memory exploit features work without the bridge. "
            "Paint and Anti-Kick need the bridge — fully quit the game once to clear "
            "a stuck DLL (after a bridge crash or DLL update only; not required for "
            "Python-only Peterhack changes)."
        )

    def _unload_game_bridge_modules(self, *name_markers):
        """FreeLibrary selected bridge DLLs in the game process."""
        import time as _t

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

        list_modules = getattr(self, "_list_game_modules", None)
        if not callable(list_modules):
            return unloaded

        for round_num in range(8):
            round_unloaded = []
            for name, base in list(list_modules()):
                base = int(base or 0)
                low = (name or "").lower()
                if base <= 0x10000 or not any(m in low for m in markers):
                    continue
                if round_num == 0:
                    print(f"[CAMO] unloading {name} @ 0x{base:X}", flush=True)
                h_thread = kernel32.CreateRemoteThread(
                    h_process, None, 0, free_lib, base, 0, None,
                )
                if h_thread:
                    kernel32.WaitForSingleObject(h_thread, 8000)
                    kernel32.CloseHandle(h_thread)
                    round_unloaded.append(name)
            if round_unloaded:
                unloaded.extend(round_unloaded)
            still_loaded, _, _, _ = self._bridge_dll_state()
            if not still_loaded:
                break
            _t.sleep(0.15)

        if unloaded:
            print(
                f"[CAMO] unloaded {len(unloaded)} module unload(s): {', '.join(unloaded)}",
                flush=True,
            )
        return unloaded

    def _bridge_shutdown(self, quiet=True):
        """Ask the in-game TCP bridge to stop (only when TCP is actually up)."""
        port = self._resolve_bridge_port()
        if not port:
            return False
        resp = self._bridge_request_on_port(port, "shutdown", {}, timeout=3, quiet=quiet)
        return self._bridge_response_ok(resp)

    def _unload_all_bridge_modules(self):
        """FreeLibrary every camouflage bridge DLL in the game process."""
        if self._resolve_bridge_port():
            self._bridge_shutdown(quiet=True)
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
        self._bridge_paint_yaw = (
            meta.get("paint_full_route") != "mesh_first_paint"
            and "camera_yaw_offset" in fields
        )
        if cmds:
            print(f"[CAMO] bridge commands: {', '.join(cmds)}", flush=True)
        if meta.get("paint_full_route") == "mesh_first_paint":
            print("[CAMO] bridge uses official mesh_first_paint pipeline", flush=True)
        elif self._bridge_paint_yaw:
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
                    msg = self._bridge_stuck_message(mod_names)
                    self._camo_last_error = msg
                    self._bridge_recover_give_up = True
                    print(f"[CAMO] {msg}", flush=True)
                    return False
            else:
                print(
                    f"[CAMO] unknown bridge loaded ({', '.join(mod_names)}) — "
                    "restart game before inject",
                    flush=True,
                )
                return False
        inj = self._get_bridge_injector_path()
        dll = self._get_bridge_dll_path()
        ok_path, path_err = self._validate_inject_dll_path(dll)
        if not ok_path:
            print(f"[CAMO] {path_err}", flush=True)
            self._camo_last_error = path_err
            return False
        proc_name = getattr(self, "PROCESS_NAME", "PenguinHotel-Win64-Shipping.exe")
        if not os.path.isfile(inj) or not os.path.isfile(dll):
            print("[CAMO] direct inject skipped — injector or DLL missing", flush=True)
            return False
        self._write_port_sidecar(dll, port)
        print(
            f"[CAMO] injecting {self.DLL_NAME} pid={getattr(self.pm, 'process_id', 0)} "
            f"port={port} path={dll} ({self._format_bridge_fingerprint(dll)})...",
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
        pid = getattr(getattr(self, "pm", None), "process_id", 0) or 0
        if pid != getattr(self, "_bridge_session_pid", 0):
            self._bridge_session_pid = pid
            self._bridge_recover_give_up = False
            self._bridge_preload_started = False
            self._bridge_preload_ok = None
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

    def _ensure_bridge(self, force=False):
        """Ensure meccha-xenos-bridge.dll is loaded — the only in-game bridge we use."""
        lock = getattr(self, "_bridge_ensure_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._bridge_ensure_lock = lock
        if not force:
            acquired = lock.acquire(blocking=False)
            if not acquired:
                return bool(self._resolve_bridge_port(fast=True))
            try:
                return self._ensure_bridge_impl(force=False)
            finally:
                lock.release()
        with lock:
            return self._ensure_bridge_impl(force=True)

    def _ensure_bridge_impl(self, force=False):
        import time as _t

        if not getattr(self, "pm", None) or not self.pm.process_id:
            print("[CAMO] no game pid", flush=True)
            return False

        if getattr(self, "_bridge_recover_give_up", False) and not force:
            return False

        self._stop_legacy_camo_controller()
        if force:
            self._bridge_recover_give_up = False
        self._camo_last_error = None

        loaded, mod_names, has_xenos, has_runtime = self._bridge_dll_state()
        tcp_ok = bool(self._resolve_bridge_port())
        reasons = []

        ready = tcp_ok and has_xenos and not has_runtime
        if ready:
            ok_id, id_err = self._verify_bridge_identity()
            if not ok_id:
                print(f"[CAMO] {id_err}", flush=True)
                self._camo_last_error = id_err
                if not force:
                    return False
                reasons.append("wrong bridge identity")
                ready = False
            else:
                self._bridge_last_ok_ts = _t.time()
                print("[CAMO] meccha-xenos-bridge ready (TCP)", flush=True)
                self._log_bridge_capabilities()
                return True

        if has_runtime:
            print("[CAMO] runtime-bridge detected — unloading before xenos inject", flush=True)
            self._replace_runtime_bridge_with_xenos()
            loaded, mod_names, has_xenos, has_runtime = self._bridge_dll_state()
            tcp_ok = bool(self._resolve_bridge_port())

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
        if has_xenos and not tcp_ok:
            print("[CAMO] bridge DLL in process — waiting for TCP listener...", flush=True)
            if self._wait_for_bridge_tcp("bridge listener", attempts=24, sleep_s=0.25):
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
                msg = self._bridge_stuck_message(still_names)
                self._camo_last_error = msg
                self._bridge_recover_give_up = True
                print(f"[CAMO] {msg}", flush=True)
                return False

        try:
            missing = self._ensure_bridge_files()
            if missing:
                msg = (
                    f"Missing bridge files: {', '.join(missing)}. "
                    f"Run runtime/scripts/build.ps1 or place {self.DLL_NAME} and "
                    f"{self.INJECTOR_NAME} in {self._bridge_dir()}."
                )
                self._camo_last_error = msg
                print(f"[CAMO] {msg}", flush=True)
                return False
        except Exception as exc:
            self._camo_last_error = f"Bridge extract failed: {exc}"
            print(f"[CAMO] extract failed: {exc}", flush=True)
            return False

        self._write_port_sidecar(self._get_bridge_dll_path(), BRIDGE_FIXED_PORT)

        if not self._try_direct_inject(BRIDGE_FIXED_PORT, force=True):
            msg = "Bridge inject failed — run Peterhack as Administrator."
            self._camo_last_error = msg
            print(f"[CAMO] {msg}", flush=True)
            return False

        print("[CAMO] waiting for meccha-xenos-bridge TCP...", flush=True)
        if self._wait_for_bridge_tcp("xenos bridge", attempts=40, sleep_s=0.25):
            ok_id, id_err = self._verify_bridge_identity()
            if not ok_id:
                self._camo_last_error = id_err
                print(f"[CAMO] {id_err}", flush=True)
                return False
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
    # Yaw orbit is in the body forward/right plane. Scene capture sits at
    # body - look*pullback facing look, so +forward (180°) sees spine/back and
    # -forward (0°) sees chest/front — labels match the body side painted.
    CAMO_WRAP_YAW_PASSES = (
        (180, "back"),
        (90, "left"),
        (270, "right"),
        (0, "front"),
    )
    CAMO_DETAIL_PASSES = (
        "head_shoulders",
        "inner_legs",
    )
    CAMO_PAWN_BACK_YAW = 180
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
        if not hasattr(self, "_exploits_anti_clipping") or not hasattr(self, "_find_local_pawn"):
            return
        pawn = self._find_local_pawn()
        cfg = getattr(self, "config", None)
        if not pawn or not cfg:
            return
        user_noclip = bool(getattr(cfg, "exploits_anti_clipping", False))
        note = f" ({label})" if label else ""
        if enable:
            if user_noclip:
                self._camo_noclip_owned = False
                self._camo_noclip_hold = True
                print(f"[CAMO] noclip already on (exploits toggle){note}", flush=True)
                return
            self._exploits_anti_clipping(pawn, cfg, True)
            self._camo_noclip_owned = True
            self._camo_noclip_hold = True
            print(f"[CAMO] noclip on for front pass (held through paint){note}", flush=True)
        elif getattr(self, "_camo_noclip_owned", False) or getattr(self, "_camo_noclip_hold", False):
            self._camo_noclip_hold = False
            if getattr(self, "_camo_noclip_owned", False):
                self._exploits_anti_clipping(pawn, cfg, False)
                self._camo_noclip_owned = False
            print(f"[CAMO] noclip restored after front pass{note}", flush=True)

    @staticmethod
    def _quality_to_mesh_first_tuning(quality):
        """Map Peterhack camo slider 1–20 → official mesh_first_paint tuning.

        Higher quality = smaller brush + tighter coverage (more strokes, sharper camo).
        """
        q = max(1, min(20, int(quality)))
        t = (q - 1) / 19.0  # 0.0 at draft, 1.0 at god mode
        # stroke/coverage: 12 texels (fast/blocky) → 1 texel (extreme detail).
        stroke = round(12.0 - t * 11.0, 2)
        stroke = max(1.0, min(12.0, stroke))
        # Side/front-back source distance in UV — tighter at high quality.
        side_uv = round(0.48 - t * 0.44, 4)  # 0.48 → 0.04
        side_uv = max(0.04, min(0.50, side_uv))
        front_back_uv = round(1.15 - t * 1.0, 4)  # 1.15 → 0.15
        front_back_uv = max(0.08, min(1.20, front_back_uv))
        # Slightly faster batch pacing at low quality; patient at extreme.
        batch_delay_ms = int(round(95 - t * 45))  # 95 ms → 50 ms
        batch_delay_ms = max(50, min(100, batch_delay_ms))
        return {
            "stroke_size_texels": stroke,
            "coverage_step_texels": stroke,
            "side_source_max_uv": side_uv,
            "front_back_source_max_uv": front_back_uv,
            "server_batch_delay_ms": batch_delay_ms,
        }

    def _paint_payload(self, pid):
        """Official MecchaCamouflage mesh_first_paint payload (v1.6.0-beta.4)."""
        cfg = getattr(self, "config", None)
        quality = max(1, min(20, int(getattr(cfg, "paint_quality", 12) if cfg else 12)))
        tuning = self._quality_to_mesh_first_tuning(quality)
        skip_front = bool(getattr(cfg, "camo_skip_front_pass", False)) if cfg else False
        back_only = bool(getattr(cfg, "camo_back_pass_only", False)) if cfg else False
        if back_only:
            front_mode = side_mode = "skip"
            back_mode = "paint"
        elif skip_front:
            front_mode = "skip"
            side_mode = back_mode = "paint"
        else:
            front_mode = side_mode = back_mode = "paint"
        return {
            "native_apply_mode": "mesh_first_paint",
            "route": "f10_mesh_first_paint",
            "server_batch_rpc": "packed",
            "packed_route": "component",
            "preview_only": False,
            "unpreview_only": False,
            "research_artifacts": False,
            "process": {
                "pid": pid,
                "name": getattr(self, "PROCESS_NAME", "PenguinHotel-Win64-Shipping.exe"),
            },
            # Top-level + tuning — bridge parses keys anywhere in the JSON blob.
            "stroke_size_texels": tuning["stroke_size_texels"],
            "coverage_step_texels": tuning["coverage_step_texels"],
            "side_source_max_uv": tuning["side_source_max_uv"],
            "front_back_source_max_uv": tuning["front_back_source_max_uv"],
            "server_batch_delay_ms": tuning["server_batch_delay_ms"],
            "tuning": {
                "stroke_size_texels": tuning["stroke_size_texels"],
                "server_batch_delay_ms": tuning["server_batch_delay_ms"],
                "coverage_step_texels": tuning["coverage_step_texels"],
                "side_source_max_uv": tuning["side_source_max_uv"],
                "front_back_source_max_uv": tuning["front_back_source_max_uv"],
                "auto_material": True,
                "auto_material_properties": True,
                "metallic": 0.0,
                "roughness": 0.65,
                "front_region_mode": front_mode,
                "side_region_mode": side_mode,
                "back_region_mode": back_mode,
                "enable_front_paint": front_mode == "paint",
                "enable_side_paint": side_mode == "paint",
                "enable_back_paint": back_mode == "paint",
                "fill_color": "#FFFFFF",
                "fill_color_r": 1.0,
                "fill_color_g": 1.0,
                "fill_color_b": 1.0,
                "fill_metallic": 0.0,
                "fill_roughness": 0.65,
            },
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

    def camo_apply(self, r=None, g=None, b=None, a=None, full_wrap=True):
        """Environment camo via official mesh_first_paint (single bridge call)."""
        del r, g, b, a, full_wrap
        if not getattr(self, "pm", None) or not self.pm:
            print("[CAMO] no pymem handle", flush=True)
            return False

        self._camo_abort = False
        self._bridge_port = None
        self._camo_noclip_owned = False
        self._camo_noclip_hold = False
        try:
            pid = self.pm.process_id
            cfg = getattr(self, "config", None)
            q = getattr(cfg, "paint_quality", 12) if cfg else 12
            tuning = self._quality_to_mesh_first_tuning(q)
            print(
                f"[CAMO] pid={pid} route=mesh_first_paint quality={q}/20 "
                f"stroke={tuning['stroke_size_texels']}tex side_uv={tuning['side_source_max_uv']} "
                f"front_back_uv={tuning['front_back_source_max_uv']}",
                flush=True,
            )
            if not pid:
                return False

            if not self._ensure_bridge(force=True):
                return False

            payload = self._paint_payload(pid)
            print("[CAMO] sending mesh_first_paint (official camo pipeline)...", flush=True)
            resp = self._bridge_request(
                "paint_full_route",
                payload,
                timeout=self.CAMO_PAINT_ROUTE_TIMEOUT,
            )
            print(f"[CAMO] paint response={resp}", flush=True)
            if not resp or not resp.get("success", False):
                err = resp.get("message") or resp.get("stage") if resp else "no response"
                self._camo_last_error = f"Paint failed: {err}"
                print(f"[CAMO] {self._camo_last_error}", flush=True)
                self._finalize_camo_paint()
                return False

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

    def bridge_set_netconn_watch(self, enabled=True):
        """Arm/disarm the diagnostic NetConnection close watcher (logs to anti_kick.log)."""
        state = "on" if enabled else "off"
        print(f"[CAMO] set_netconn_watch {state}...", flush=True)
        resp = self._bridge_request("set_netconn_watch", {"enabled": bool(enabled)}, timeout=20)
        print(f"[CAMO] set_netconn_watch: {resp}", flush=True)
        return resp

    def bridge_dump_netconn_vtable(self, slots=128):
        """Dump the local UNetConnection vtable to anti_kick.log (diagnostic)."""
        print(f"[CAMO] dump_netconn_vtable slots={slots}...", flush=True)
        resp = self._bridge_request("dump_netconn_vtable", {"slots": int(slots)}, timeout=30)
        print(f"[CAMO] dump_netconn_vtable: {resp}", flush=True)
        return resp

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
