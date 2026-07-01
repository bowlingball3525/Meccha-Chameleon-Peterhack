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
    CAMO_DIR = os.path.join(os.environ.get("APPDATA", "."), "MecchaCamouflage")
    RUNTIME_DIR = os.path.join(
        os.environ.get("LOCALAPPDATA", "."), "MecchaCamouflage", "runtime",
    )

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

    def _try_direct_inject(self, port=BRIDGE_FIXED_PORT):
        """Fallback inject — writes .port sidecar so DLL listens on known port."""
        loaded, mod_names = self._bridge_dll_loaded()
        if loaded:
            print(
                f"[CAMO] direct inject skipped — bridge already loaded "
                f"({', '.join(mod_names)})",
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
            f"[CAMO] direct inject pid={getattr(self.pm, 'process_id', 0)} "
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
        """Launch bridge controller, wait for inject + TCP (auto port discovery)."""
        import time as _t

        if not getattr(self, "pm", None) or not self.pm.process_id:
            print("[CAMO] no game pid", flush=True)
            return False

        pid = self.pm.process_id

        if self._resolve_bridge_port():
            print("[CAMO] bridge already up (TCP)", flush=True)
            return True

        dll_loaded, mod_names = self._bridge_dll_loaded()
        if dll_loaded:
            print(
                f"[CAMO] bridge DLL already in game ({', '.join(mod_names)}) "
                f"— skipping inject",
                flush=True,
            )
            print("[CAMO] waiting for existing bridge TCP...", flush=True)
            if self._wait_for_bridge_tcp("existing bridge"):
                return True
            self._log_bridge_diagnostics()
            print(
                "[CAMO] bridge DLL is loaded but TCP is not responding — "
                "will not inject again (restart the game, then retry Paint Now).",
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
        except Exception as exc:
            print(f"[CAMO] extract failed: {exc}", flush=True)

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

        print(
            f"[CAMO] launching bridge controller (fixed port {BRIDGE_FIXED_PORT})...",
            flush=True,
        )
        try:
            self._bridge_proc = _subprocess.Popen(
                [
                    exe_path,
                    "--bridge-port", str(BRIDGE_FIXED_PORT),
                    "--bridge-host", self.BRIDGE_HOST,
                ],
                cwd=os.path.dirname(exe_path),
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            print(f"[CAMO] failed to launch controller: {exc}", flush=True)
            return False

        print("[CAMO] waiting for DLL inject + TCP...", flush=True)
        inject_tried = False

        for i in range(120):
            if self._camo_aborted():
                return False
            _t.sleep(0.5)

            if self._resolve_bridge_port():
                print(f"[CAMO] bridge ready ({(i + 1) * 0.5:.1f}s)", flush=True)
                return True

            proc = getattr(self, "_bridge_proc", None)
            if proc and proc.poll() is not None:
                print(f"[CAMO] controller exited rc={proc.poll()}", flush=True)
                break

            status = self._read_last_status()
            bridge = status.get("bridge") or {}
            proc_info = status.get("process") or {}
            if (
                bridge.get("state") == "ready"
                and bridge.get("port")
                and proc_info.get("pid") == pid
                and bridge.get("message") == "pong"
            ):
                port = int(bridge["port"])
                self._bridge_port = port
                print(f"[CAMO] bridge ready on port {port} (controller verified)", flush=True)
                return True
            if bridge.get("state") == "ready" and bridge.get("port"):
                port = int(bridge["port"])
                if self._ping_port(port):
                    self._bridge_port = port
                    print(f"[CAMO] bridge ready on port {port} (from status)", flush=True)
                    return True

            if i == 39 and not inject_tried:
                inject_tried = True
                last_err = status.get("last_error")
                loaded_now, _ = self._bridge_dll_loaded()
                if loaded_now:
                    print(
                        "[CAMO] bridge DLL loaded by controller — skipping direct inject",
                        flush=True,
                    )
                elif bridge.get("state") != "ready" and not last_err:
                    self._try_direct_inject(BRIDGE_FIXED_PORT)

            if i > 0 and i % 8 == 7:
                tried = self._discover_bridge_ports()
                print(
                    f"[CAMO] waiting for bridge TCP... {(i + 1) // 8}/15 "
                    f"(probing ports: {tried[:5]})",
                    flush=True,
                )

        if self._resolve_bridge_port():
            return True

        self._log_bridge_diagnostics()
        print(
            "[CAMO] failed to communicate with bridge DLL — "
            "Peterhack could not reach the TCP server. "
            "Run Peterhack as administrator (right-click Peterhack.bat → Run as administrator) "
            "and retry Paint Now.",
            flush=True,
        )
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
            passes = [(0, "front")]
            if full_wrap:
                passes.append((180, "back"))

            for pass_idx, (yaw, label) in enumerate(passes):
                if self._camo_aborted():
                    print("[CAMO] apply aborted", flush=True)
                    return False
                if yaw != 0:
                    print(f"[CAMO] rotate {label} yaw={yaw}...", flush=True)
                    rot_resp = self._bridge_request("rotate", {"yaw": yaw}, timeout=10)
                    print(f"[CAMO] rotate response={rot_resp}", flush=True)
                    _t.sleep(1.5)

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
