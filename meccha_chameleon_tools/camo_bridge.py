"""
SilentJMA / Meccha-Chameleon-Tools v1.7.4.1 bridge camouflage (TCP + bundled EXE).

Uses meccha-camouflage.exe + xenos injector in meccha_chameleon_tools/camo/
for in-process template_brush_paint (game-thread safe).
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time


class CamoBridgeMixin:
    """Bridge camouflage — port of SilentJMA Meccha-Chameleon-Tools v1.7.4.1."""

    DLL_NAME = "meccha-xenos-bridge.dll"
    EXE_NAME = "meccha-camouflage.exe"
    INJECTOR_NAME = "meccha-xenos-injector.exe"
    BRIDGE_HOST = "127.0.0.1"
    BRIDGE_PORT = 47654

    @staticmethod
    def _camo_bundle_dir():
        """Bundled camo binaries live beside this module in camo/ (shippable with Peterhack)."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "camo")

    @classmethod
    def _camo_bin_path(cls, name):
        return os.path.join(cls._camo_bundle_dir(), name)

    def camo_exe_running(self):
        proc = getattr(self, "_bridge_proc", None)
        return bool(proc and proc.poll() is None)

    def camo_launch_exe(self):
        """Start meccha-camouflage.exe from the bundled camo/ folder."""
        if self.camo_exe_running():
            print("[CAMO] meccha-camouflage.exe already running")
            return True
        exe_path = self._camo_bin_path(self.EXE_NAME)
        if not os.path.isfile(exe_path):
            print(f"[CAMO] EXE not found at {exe_path}")
            print(f"[CAMO] place {self.EXE_NAME}, {self.DLL_NAME}, {self.INJECTOR_NAME} in camo/")
            return False
        print(f"[CAMO] starting meccha-camouflage.exe: {exe_path}")
        print("[CAMO] NOTE: Peterhack must NOT register F10 globally — camo EXE needs that hotkey")
        try:
            self._bridge_proc = subprocess.Popen(
                [exe_path],
                cwd=os.path.dirname(exe_path),
            )
        except Exception as exc:
            print(f"[CAMO] failed to start EXE: {exc}")
            self._bridge_proc = None
            return False
        time.sleep(0.5)
        return self.camo_exe_running()

    def camo_stop_exe(self):
        """Stop meccha-camouflage.exe."""
        proc = getattr(self, "_bridge_proc", None)
        if proc and proc.poll() is None:
            try:
                print("[CAMO] stopping meccha-camouflage.exe")
                proc.terminate()
                proc.wait(3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._bridge_proc = None
        return True

    def camo_kill_bridge_competition(self):
        """
        Stop any bridge EXE / in-flight template_brush_paint before Peterhack UV apply.

        meccha-camouflage.exe listens for F10 and fills the body with a single
        template colour on a 16 ms tick — if it runs alongside Peterhack paint,
        the body flickers between solid colours.
        """
        try:
            if self._bridge_ping(quiet=True).get("success"):
                resp = self._bridge_request(
                    "cancel_paint", {}, timeout=2, quiet=True,
                )
                print(f"[CAMO] cancel_paint (pre-apply) response={resp}")
        except Exception:
            pass
        self.camo_stop_exe()
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            r = subprocess.run(
                ["taskkill", "/IM", self.EXE_NAME, "/F"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=flags,
            )
            if r.returncode == 0:
                print(f"[CAMO] taskkill {self.EXE_NAME} ok")
        except Exception as exc:
            print(f"[CAMO] taskkill {self.EXE_NAME} skipped: {exc}")
        return True

    @classmethod
    def _bridge_request(cls, command, payload=None, timeout=30, quiet=False, abort_check=None):
        import socket as _socket

        msg = json.dumps({
            "type": command,
            "request_id": f"{os.urandom(8).hex()}{int(time.time())}",
            "timestamp_utc": int(time.time()),
            "payload": payload or {},
        }) + "\n"
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(min(5.0, timeout) if timeout else 5.0)
        try:
            if abort_check and abort_check():
                return {"success": False, "aborted": True}
            sock.connect((cls.BRIDGE_HOST, cls.BRIDGE_PORT))
            sock.sendall(msg.encode())
            raw = b""
            deadline = time.monotonic() + timeout
            while b"\n" not in raw:
                if abort_check and abort_check():
                    return {"success": False, "aborted": True}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"success": False, "timeout": True}
                sock.settimeout(min(2.0, remaining))
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
            line = raw.split(b"\n")[0]
            return json.loads(line) if line else {"success": False}
        except Exception as exc:
            if abort_check and abort_check():
                return {"success": False, "aborted": True}
            if not quiet:
                print(f"[CAMO] bridge {command} failed: {exc}")
            return {"success": False}
        finally:
            sock.close()

    def _camo_aborted(self):
        return bool(getattr(self, "_camo_abort", False))

    def _bridge_ping(self, quiet=True):
        return self._bridge_request("ping", timeout=1.5, quiet=quiet)

    def _try_direct_injection(self):
        inj = self._camo_bin_path(self.INJECTOR_NAME)
        dll = self._camo_bin_path(self.DLL_NAME)
        if not os.path.isfile(inj) or not os.path.isfile(dll):
            return False
        print(f"[CAMO] direct inject via {self.INJECTOR_NAME} -> {self.PROCESS_NAME}")
        try:
            result = subprocess.run(
                [inj, self.PROCESS_NAME, dll],
                cwd=os.path.dirname(inj),
                timeout=60,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                print(f"[CAMO] injector stdout: {result.stdout.strip()[:300]}")
            if result.stderr:
                print(f"[CAMO] injector stderr: {result.stderr.strip()[:300]}")
            print(f"[CAMO] injector exit={result.returncode}")
            ok = result.returncode == 0
            self._bridge_dll_injected = ok
            return ok
        except Exception as exc:
            print(f"[CAMO] injector failed: {exc}")
            self._bridge_dll_injected = False
            return False

    def _send_f10_pulse(self):
        user32 = ctypes.windll.user32
        vk_f10 = 0x79
        user32.keybd_event(vk_f10, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(vk_f10, 0, 2, 0)

    def camo_cleanup(self):
        self.camo_stop_exe()

    def _ensure_camo_bridge(self):
        """
        Bring up TCP bridge to meccha-xenos-bridge.dll.

        IMPORTANT: Each F10 pulse triggers an in-game template_brush_paint on the
        camera-visible body (back in 3rd person).  Do NOT spam F10 while waiting
        for TCP — that repaints the back repeatedly without Peterhack control.
        """
        pid = self.pm.process_id if getattr(self, "pm", None) else 0
        if not pid:
            return False
        if self._camo_aborted():
            print("[CAMO] bridge setup aborted")
            return False
        if self._bridge_ping().get("success"):
            return True
        if not self.camo_launch_exe():
            return False
        for _ in range(20):
            if self._camo_aborted():
                print("[CAMO] bridge setup aborted during EXE startup")
                return False
            time.sleep(0.1)
        if self._bridge_ping().get("success"):
            print("[CAMO] bridge ready after EXE launch")
            return True

        self._bridge_dll_injected = False
        try:
            injected = self._try_direct_injection()
            if injected:
                print("[CAMO] waiting for TCP after inject (no F10 spam)...")
                for i in range(60):
                    if self._camo_aborted():
                        print("[CAMO] bridge setup aborted while waiting")
                        return False
                    time.sleep(0.5)
                    if self._bridge_ping().get("success"):
                        print(f"[CAMO] bridge ready after inject ({(i + 1) * 0.5:.1f}s)")
                        return True
                print("[CAMO] TCP still down — one F10 to wake bridge (not repeated)")
                self._send_f10_pulse()
                time.sleep(2.5)
                if self._bridge_ping().get("success"):
                    print("[CAMO] bridge ready after single F10")
                    return True
            else:
                print("[CAMO] inject failed — one F10 for EXE hook path")
                self._send_f10_pulse()
                time.sleep(2.0)
                if self._bridge_ping().get("success"):
                    print("[CAMO] bridge ready after F10")
                    return True
        except Exception as exc:
            print(f"[CAMO] bridge setup failed: {exc}")

        print(
            "[CAMO] bridge not ready — press F10 once in-game (focused), then retry",
        )
        return False

    def _build_paint_payload(self, pid, full_wrap=False):
        payload = {
            "native_apply_mode": "template_brush_paint",
            "route": "f10_template_brush_paint",
            "process": {
                "pid": pid,
                "name": self.PROCESS_NAME,
            },
            "max_paints_per_tick": 256,
            "paint_tick_budget_ms": 16,
            "brush_radius": 4.0,
            "template_min_direct_points": 5000,
            "auto_flush_during_paint": True,
        }
        if full_wrap:
            payload.update({
                "two_pass_enabled": True,
                "template_fill_enabled": True,
                "template_min_direct_points": 8000,
                "capture_requested_texture_width": 1024,
                "capture_requested_texture_height": 1024,
            })
        return payload

    def _run_paint_full_route(self, pid, full_wrap=False, timeout=120):
        payload = self._build_paint_payload(pid, full_wrap=full_wrap)
        print(
            f"[CAMO] paint_full_route wrap={full_wrap} keys={sorted(payload.keys())}",
        )
        return self._bridge_request(
            "paint_full_route",
            payload,
            timeout=timeout,
            abort_check=self._camo_aborted,
        )

    def camo_apply(self, r=None, g=None, b=None, a=None):
        """Apply SilentJMA template-brush camouflage via TCP bridge."""
        del r, g, b, a
        if not getattr(self, "pm", None):
            print("[CAMO] no pymem handle")
            return False
        self._camo_abort = False
        try:
            pid = self.pm.process_id
            print(f"[CAMO] bridge apply pid={pid}")
            if not pid:
                return False
            if self._camo_aborted():
                return False
            if not self._ensure_camo_bridge():
                return False
            if self._camo_aborted():
                return False

            full_wrap = bool(getattr(self, "_camo_full_body_wrap", False))
            print("[CAMO] sending paint_full_route (template_brush_paint)...")
            resp = self._run_paint_full_route(
                pid,
                full_wrap=full_wrap,
                timeout=180 if full_wrap else 120,
            )
            print(f"[CAMO] paint response={resp}")
            ok = bool(resp.get("success"))
            if ok and full_wrap:
                try:
                    cancel = self._bridge_request(
                        "cancel_paint", {}, timeout=3, quiet=True,
                    )
                    print(f"[CAMO] cancel_paint after bridge back-pass={cancel}")
                except Exception:
                    pass
            return ok
        except Exception as exc:
            import traceback
            print(f"[CAMO] exception: {exc}")
            traceback.print_exc()
            return False

    def camo_stop(self):
        """Cancel bridge setup or paint, then stop meccha-camouflage.exe."""
        self._camo_abort = True
        if hasattr(self, "_emergency_unfreeze_game"):
            self._emergency_unfreeze_game("CAMO-STOP")
        try:
            if self._bridge_ping(quiet=True).get("success"):
                resp = self._bridge_request("cancel_paint", {}, timeout=2, quiet=True)
                print(f"[CAMO] cancel_paint response={resp}")
            else:
                print("[CAMO] bridge not connected — skip cancel_paint")
        except Exception as exc:
            print(f"[CAMO] cancel_paint failed: {exc}")
        self.camo_stop_exe()
        return True
