"""
Peterhack bridge camouflage (TCP + bundled camo EXE).

Uses meccha-camouflage.exe + injector in meccha_chameleon_tools/camo/
for in-process template_brush_paint (game-thread safe).

The EXE registers a global F10 hotkey, injects the bridge DLL on F10,
then opens TCP on 127.0.0.1:47654.  Peterhack must NOT inject the DLL itself —
that leaves the EXE stuck at "waiting for F10" with no TCP server.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from ctypes import wintypes


class CamoBridgeMixin:
    """Bridge camouflage — TCP control of in-game template brush paint."""

    DLL_NAME = "meccha-xenos-bridge.dll"
    EXE_NAME = "meccha-camouflage.exe"
    INJECTOR_NAME = "meccha-xenos-injector.exe"
    BRIDGE_HOST = "127.0.0.1"
    BRIDGE_PORT = 47654
    CAMO_DIR = os.path.join(os.environ.get("APPDATA", "."), "PeterhackCamo")

    @staticmethod
    def _camo_bundle_dir():
        """Bundled camo binaries live beside this module in camo/ (shippable with Peterhack)."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "camo")

    @classmethod
    def _camo_bin_path(cls, name):
        return os.path.join(cls._camo_bundle_dir(), name)

    @classmethod
    def _stable_camo_path(cls, name):
        return os.path.join(cls.CAMO_DIR, name)

    def _ensure_stable_camo_files(self):
        """Copy bundled camo binaries to %APPDATA%\\PeterhackCamo."""
        try:
            os.makedirs(self.CAMO_DIR, exist_ok=True)
            for name in (self.EXE_NAME, self.DLL_NAME, self.INJECTOR_NAME):
                src = self._camo_bin_path(name)
                dst = self._stable_camo_path(name)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                elif not os.path.isfile(dst):
                    print(f"[CAMO] missing camo binary: {name}")
                    return False
            return True
        except Exception as exc:
            print(f"[CAMO] stable camo extract failed: {exc}")
            return False

    def _stop_all_camo_exes(self):
        """Kill every meccha-camouflage.exe — guarantees a single bridge instance."""
        proc = getattr(self, "_bridge_proc", None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._bridge_proc = None
        pids = self._find_camo_exe_pids()
        if pids:
            print(f"[CAMO] stopping all {self.EXE_NAME} instances pids={pids}")
        try:
            subprocess.run(
                ["taskkill", "/IM", self.EXE_NAME, "/F"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=self._subprocess_flags(),
            )
        except Exception as exc:
            print(f"[CAMO] taskkill {self.EXE_NAME} skipped: {exc}")
        time.sleep(0.35)

    def _start_bridge_exe_log_reader(self, proc):
        """Mirror bridge EXE stdout into Peterhack log for diagnosis."""
        if not proc or not proc.stdout:
            return

        def _reader():
            try:
                for line in proc.stdout:
                    text = (line or "").strip()
                    if not text:
                        continue
                    print(f"[CAMO-EXE] {text}", flush=True)
                    low = text.lower()
                    if "f10 trigger" in low or "f10 triggered" in low:
                        ev = getattr(self, "_bridge_f10_seen", None)
                        if ev is not None:
                            ev.set()
                    if "paint started" in low:
                        self._bridge_probe_tcp_async("paint-started")
                    if "inject done" in low or "injection completed" in low:
                        self._bridge_probe_tcp_async("post-inject")
                    if "done]" in low and "paint dispatched" in low:
                        ev = getattr(self, "_bridge_hotkey_paint_done", None)
                        if ev is not None:
                            ev.set()
                            print("[CAMO] hotkey back-pass finished (EXE DONE)", flush=True)
            except Exception:
                pass

        threading.Thread(target=_reader, daemon=True).start()

    @classmethod
    def _subprocess_flags(cls):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _find_camo_exe_pids(self):
        """Return PIDs of any running meccha-camouflage.exe (ours or orphaned)."""
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {self.EXE_NAME}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=self._subprocess_flags(),
            )
        except Exception:
            return []
        pids = []
        for line in (result.stdout or "").strip().splitlines():
            if self.EXE_NAME.lower() not in line.lower():
                continue
            parts = line.split(",")
            if len(parts) < 2:
                continue
            try:
                pids.append(int(parts[1].strip().strip('"')))
            except ValueError:
                pass
        return pids

    def camo_exe_running(self):
        proc = getattr(self, "_bridge_proc", None)
        if proc and proc.poll() is None:
            return True
        return bool(self._find_camo_exe_pids())

    def _reset_bridge_state(self, reason=""):
        if reason:
            print(f"[CAMO] reset bridge state: {reason}")
        self._bridge_dll_injected = False
        self._bridge_tcp_ready = False
        self._bridge_game_pid = 0

    def camo_launch_exe(self, force_fresh=False):
        """Start exactly one meccha-camouflage.exe from stable %APPDATA% path."""
        pids = self._find_camo_exe_pids()
        if not force_fresh and len(pids) == 1:
            print(f"[CAMO] meccha-camouflage.exe already running pid={pids[0]}")
            return True
        if len(pids) > 1:
            print(f"[CAMO] multiple bridge EXEs pids={pids} — restarting clean")
            force_fresh = True

        if force_fresh or pids:
            self._stop_all_camo_exes()

        if not self._ensure_stable_camo_files():
            return False

        exe_path = self._stable_camo_path(self.EXE_NAME)
        if not os.path.isfile(exe_path):
            print(f"[CAMO] EXE not found at {exe_path}")
            return False

        print(f"[CAMO] starting meccha-camouflage.exe: {exe_path}")
        print("[CAMO] NOTE: Peterhack must NOT register F10 globally — camo EXE needs that hotkey")
        try:
            self._bridge_proc = subprocess.Popen(
                [exe_path],
                cwd=os.path.dirname(exe_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            print(f"[CAMO] failed to start EXE: {exc}")
            self._bridge_proc = None
            return False
        self._start_bridge_exe_log_reader(self._bridge_proc)
        time.sleep(2.0)
        return self.camo_exe_running()

    def camo_stop_exe(self):
        """Stop all meccha-camouflage.exe instances."""
        self._stop_all_camo_exes()
        return True

    def stop_injected_bridge_paint(self, pulses=5):
        """
        Halt template_brush_paint inside the game if a prior bridge session injected
        the DLL.  Killing the EXE alone does not unload the DLL — F9 stops the tick.

        The DLL averages the viewport to one colour every ~16 ms (solid flicker).
        """
        try:
            self._stop_all_camo_exes()
        except Exception:
            pass
        try:
            unloaded = self._unload_bridge_dll_from_game()
            if not unloaded:
                print(
                    f"[CAMO] F9 x{pulses} — stop in-game bridge paint loop (if any)",
                    flush=True,
                )
                for i in range(max(1, int(pulses))):
                    self._send_f9_pulse()
                    time.sleep(0.35 if i + 1 < pulses else 0.15)
        except Exception as exc:
            print(f"[CAMO] bridge stop failed: {exc}", flush=True)
        self._reset_bridge_state("stop injected paint")
        return True

    def camo_kill_bridge_competition(self):
        """
        Stop bridge EXE and any in-game template_brush_paint tick before UV apply.

        The injected DLL keeps repainting one averaged colour every ~16 ms even after
        the EXE is closed — F9 stops that loop.
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
        return self.stop_injected_bridge_paint()

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

    def _ensure_camo_bridge_tcp(self, post_inject_wait=15.0):
        """Try to connect Peterhack TCP to the bridge (no F10 — F10 paints back only)."""
        pid = self.pm.process_id if getattr(self, "pm", None) else 0
        if not pid:
            return False
        if self._camo_aborted():
            return False

        ev_tcp = getattr(self, "_bridge_tcp_event", None)
        if ev_tcp is not None:
            ev_tcp.clear()

        bridge_pid = getattr(self, "_bridge_game_pid", 0)
        if bridge_pid and bridge_pid != pid:
            self.camo_stop_exe()
            self._reset_bridge_state("game restarted")

        if self._bridge_tcp_up(log_fail=True):
            self._bridge_game_pid = pid
            return True

        if not self.camo_launch_if_needed():
            return False

        t0 = time.monotonic()
        while time.monotonic() - t0 < post_inject_wait:
            if self._camo_aborted():
                return False
            if self._bridge_tcp_up():
                self._bridge_game_pid = pid
                self._camo_status("Bridge connected")
                return True
            if ev_tcp is not None and ev_tcp.wait(timeout=0.15):
                if self._bridge_tcp_up():
                    self._bridge_game_pid = pid
                    self._camo_status("Bridge connected")
                    return True
            time.sleep(0.1)

        print(
            "[CAMO] bridge TCP not available on "
            f"{self.BRIDGE_HOST}:{self.BRIDGE_PORT} — will use F10 hotkey fallback",
            flush=True,
        )
        return False

    def _ensure_camo_bridge(self, allow_f10_wake=True):
        """Try TCP bridge only — F10 does not open TCP (it paints back via hotkey)."""
        del allow_f10_wake
        return self._ensure_camo_bridge_tcp()

    def camo_apply_back_pass(self, timeout=90.0):
        """
        Paint the camera-visible back (physical back in 3rd person).

        Waits for either bridge TCP paint_full_route OR one F10 hotkey cycle.
        F10 during EXE startup counts — we do not ask for a second press.
        """
        if not getattr(self, "pm", None):
            print("[CAMO] no pymem handle")
            return False
        self._camo_abort = False
        pid = self.pm.process_id
        if not pid:
            return False
        if self._camo_aborted():
            return False

        self._camo_full_body_wrap = False
        print(f"[CAMO] back-pass pid={pid}")

        bridge_pid = getattr(self, "_bridge_game_pid", 0)
        if bridge_pid and bridge_pid != pid:
            self.camo_stop_exe()
            self._reset_bridge_state("game restarted")

        hotkey_ev = getattr(self, "_bridge_hotkey_paint_done", None)
        if hotkey_ev is not None:
            hotkey_ev.clear()

        if not self.camo_launch_if_needed():
            return False

        self._camo_status(
            "Press F10 once in the game — paints your BACK (do not press again)",
        )
        print(
            f"[CAMO] waiting for F10 hotkey or TCP back-pass (timeout={timeout}s)",
            flush=True,
        )

        self._camo_bridge_waiting = True
        t0 = time.monotonic()
        last_status = 0.0
        tcp_attempted = False
        try:
            while time.monotonic() - t0 < timeout:
                if self._camo_aborted():
                    return False
                proc = getattr(self, "_bridge_proc", None)
                if proc and proc.poll() is not None:
                    print("[CAMO] bridge EXE exited during back-pass", flush=True)
                    return False

                if hotkey_ev is not None and hotkey_ev.is_set():
                    print("[CAMO] back pass complete via F10 hotkey", flush=True)
                    self._camo_status("Back camo applied (F10)")
                    return True

                if not tcp_attempted and self._bridge_tcp_up():
                    self._bridge_game_pid = pid
                    tcp_attempted = True
                    try:
                        self._bridge_request("cancel_paint", {}, timeout=2, quiet=True)
                    except Exception:
                        pass
                    self._camo_status("Painting back (bridge TCP)…")
                    resp = self._run_paint_full_route(
                        pid, full_wrap=False, timeout=120,
                    )
                    print(f"[CAMO] back-pass TCP response={resp}", flush=True)
                    if bool(resp.get("success")):
                        self._camo_status("Back camo applied (TCP)")
                        return True

                elapsed = time.monotonic() - t0
                if elapsed - last_status >= 8.0:
                    self._camo_status(
                        f"Waiting for F10… ({int(elapsed)}s) — click game, press once",
                    )
                    last_status = elapsed
                time.sleep(0.15)

            print("[CAMO] back-pass timed out (no F10 / TCP)", flush=True)
            self._camo_status("Back pass failed — press F10 once when asked")
            return False
        finally:
            self._camo_bridge_waiting = False

    def _send_f9_pulse(self):
        """Stop in-game bridge template paint (same as meccha-camouflage F9)."""
        user32 = ctypes.windll.user32
        vk_f9 = 0x78
        hwnd = self._find_game_window_hwnd()
        if hwnd:
            try:
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.15)
            except Exception:
                pass
        self._send_vk_key(vk_f9)

    def _send_vk_key(self, vk):
        """Send a key via SendInput (more reliable for global RegisterHotKey hooks)."""
        user32 = ctypes.windll.user32
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            class _INPUTUNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            _anonymous_ = ("u",)
            _fields_ = [
                ("type", wintypes.DWORD),
                ("u", _INPUTUNION),
            ]

        scan = user32.MapVirtualKeyW(vk, 0)
        extra = ctypes.c_ulong(0)
        inputs = (INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].ki = KEYBDINPUT(vk, scan, 0, 0, ctypes.pointer(extra))
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].ki = KEYBDINPUT(vk, scan, KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))
        sent = user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        if sent != 2:
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.05)
            user32.keybd_event(vk, 0, 2, 0)

    def camo_stop_paint_loop(self, keep_bridge=True):
        """
        Stop the in-game template_brush_paint tick without tearing down TCP.

        keep_bridge=True leaves meccha-camouflage.exe running so the next
        camo apply reconnects instantly instead of waiting for F10 again.
        """
        stopped = False
        try:
            if self._bridge_ping(quiet=True).get("success"):
                resp = self._bridge_request(
                    "cancel_paint", {}, timeout=2, quiet=True,
                )
                stopped = bool(resp.get("success"))
                print(f"[CAMO] cancel_paint (stop loop)={resp}")
        except Exception:
            pass
        if not stopped and self._bridge_ping(quiet=True).get("success"):
            try:
                print("[CAMO] F9 pulse — stop in-game bridge paint loop")
                self._send_f9_pulse()
                time.sleep(0.15)
            except Exception as exc:
                print(f"[CAMO] F9 pulse failed: {exc}")
        if not keep_bridge:
            self.camo_stop_exe()
            self._reset_bridge_state("stop paint + close EXE")
        return True

    def camo_stop_injected_paint(self):
        """Halt DLL template_brush_paint loop (F9) and close bridge EXE."""
        return self.camo_stop_paint_loop(keep_bridge=False)

    def _send_f10_pulse(self):
        """Wake the bridge EXE — it injects the DLL and opens TCP on F10."""
        user32 = ctypes.windll.user32
        vk_f10 = 0x79
        hwnd = self._find_game_window_hwnd()
        if hwnd:
            try:
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.25)
            except Exception:
                pass
        print("[CAMO] sending F10 wake pulse to bridge EXE")
        self._send_vk_key(vk_f10)

    def _camo_status(self, msg):
        print(f"[CAMO] {msg}", flush=True)
        cb = getattr(self, "_camo_status_cb", None)
        if callable(cb):
            try:
                cb(msg)
            except Exception:
                pass

    def _focus_game_window(self):
        user32 = ctypes.windll.user32
        hwnd = self._find_game_window_hwnd()
        if not hwnd:
            return False
        try:
            fg = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg, None)
            our_tid = user32.GetCurrentThreadId()
            if fg_tid != our_tid:
                user32.AttachThreadInput(our_tid, fg_tid, True)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            if fg_tid != our_tid:
                user32.AttachThreadInput(our_tid, fg_tid, False)
            time.sleep(0.3)
            return user32.GetForegroundWindow() == hwnd
        except Exception:
            return False

    def _bridge_probe_tcp_async(self, label):
        """Background TCP probe when EXE signals inject/paint (port may be brief)."""

        def _probe():
            for i in range(80):
                if self._bridge_tcp_up(log_fail=(i == 0)):
                    print(f"[CAMO] bridge TCP up ({label})", flush=True)
                    ev = getattr(self, "_bridge_tcp_event", None)
                    if ev is not None:
                        ev.set()
                    return
                time.sleep(0.1)

        threading.Thread(target=_probe, daemon=True).start()

    def _bridge_tcp_up(self, log_fail=False):
        resp = self._bridge_ping(quiet=not log_fail)
        if resp.get("success"):
            self._bridge_tcp_ready = True
            return True
        return False

    def camo_launch_if_needed(self):
        """Ensure bridge EXE is running (does not wait for TCP or F10)."""
        if self.camo_exe_running():
            return True
        self._camo_status("Starting bridge EXE…")
        return self.camo_launch_exe(force_fresh=False)

    def camo_wait_hotkey_back_pass(self, timeout=90):
        """
        Wait for one F10 hotkey paint cycle (EXE paints camera-facing back only).

        F10 does NOT open Peterhack TCP — it runs the bridge's built-in back pass.
        """
        if self._bridge_hotkey_paint_done.is_set():
            print("[CAMO] hotkey back-pass already finished", flush=True)
            return True
        return self.camo_apply_back_pass(timeout=timeout)

    def camo_cleanup(self):
        self.camo_stop_exe()
        self._reset_bridge_state("cleanup")

    def _ensure_bridge_ready(self, post_inject_wait=45.0):
        """
        Launch bridge EXE, pulse F10 to inject DLL, wait for TCP.
        """
        pid = self.pm.process_id if getattr(self, "pm", None) else 0
        if not pid:
            return False
        if self._camo_aborted():
            return False

        if self._bridge_tcp_up():
            self._bridge_game_pid = pid
            return True

        if not self._ensure_stable_camo_files():
            print(
                "[CAMO] bridge binaries missing — build runtime/ or copy "
                "meccha-camouflage.exe + DLL into meccha_chameleon_tools/camo/",
                flush=True,
            )
            return False

        if not self.camo_launch_exe(force_fresh=False):
            return False

        import time as _time
        _time.sleep(2.0)
        user32 = ctypes.windll.user32
        vk_f10 = 0x79
        self._focus_game_window()
        for attempt in range(5):
            if self._camo_aborted():
                return False
            self._send_vk_key(vk_f10)
            _time.sleep(0.15)
            if self._bridge_ping(quiet=True).get("success"):
                print(f"[CAMO] bridge TCP ready after F10 attempt {attempt + 1}", flush=True)
                self._bridge_game_pid = pid
                return True

        deadline = _time.monotonic() + post_inject_wait
        i = 0
        while _time.monotonic() < deadline:
            if self._camo_aborted():
                return False
            proc = getattr(self, "_bridge_proc", None)
            if proc and proc.poll() is not None:
                print(f"[CAMO] bridge EXE exited code={proc.poll()}", flush=True)
                return False
            if self._bridge_ping(quiet=True).get("success"):
                print(f"[CAMO] bridge TCP ready after {i * 0.25 + 2:.1f}s", flush=True)
                self._bridge_game_pid = pid
                return True
            if i > 0 and i % 40 == 39:
                print(f"[CAMO] retry F10 for bridge inject ({i // 40 + 1})", flush=True)
                self._focus_game_window()
                self._send_vk_key(vk_f10)
            if i % 16 == 15:
                print(f"[CAMO] waiting for bridge TCP… ({(i + 1) // 16}/10)", flush=True)
            _time.sleep(0.25)
            i += 1

        print("[CAMO] bridge never came alive — click game and press F10 once", flush=True)
        return False

    def _build_paint_payload_single_pass(self, pid):
        """One template_brush_paint pass via bridge TCP."""
        return {
            "native_apply_mode": "template_brush_paint",
            "route": "f10_template_brush_paint",
            "process": {"pid": pid, "name": self.PROCESS_NAME},
            "max_paints_per_tick": 256,
            "paint_tick_budget_ms": 16,
            "brush_radius": 4.0,
            "template_min_direct_points": 1000,
            "auto_flush_during_paint": True,
            "single_pass_enabled": True,
            "two_pass_enabled": False,
        }

    def camo_apply_multi_angle(self):
        """
        Full-body wrap: front pass (yaw 0), rotate +180° via K2_SetActorRotation,
        wait 1.5s, back pass.  Requires bundled bridge EXE + injected DLL (game thread).
        """
        if not getattr(self, "pm", None):
            print("[CAMO] no pymem handle")
            return False
        self._camo_abort = False
        pid = self.pm.process_id
        if not pid:
            return False

        print(f"[CAMO] bridge multi-angle wrap pid={pid}", flush=True)
        self._camo_status("Starting bridge wrap…")

        if not self._ensure_bridge_ready():
            self._camo_status("Bridge failed — press F10 in game once")
            return False

        try:
            self._bridge_request("cancel_paint", {}, timeout=2, quiet=True)
        except Exception:
            pass

        for pass_idx, yaw in enumerate([0, 180]):
            if self._camo_aborted():
                return False
            if yaw != 0:
                print(f"[CAMO] rotate +{yaw}° (K2_SetActorRotation)", flush=True)
                self._camo_status(f"Rotating {yaw}° for back pass…")
                rot = self._bridge_request("rotate", {"yaw": float(yaw)}, timeout=10)
                if not rot.get("success"):
                    print(f"[CAMO] rotate failed: {rot}", flush=True)
                    return False
                time.sleep(1.5)

            label = "front" if yaw == 0 else "back"
            self._camo_status(f"Painting {label} pass…")
            print(f"[CAMO] paint_full_route pass={label}", flush=True)
            payload = self._build_paint_payload_single_pass(pid)
            resp = self._bridge_request(
                "paint_full_route",
                payload,
                timeout=120,
                abort_check=self._camo_aborted,
            )
            print(f"[CAMO] {label} pass response success={resp.get('success')}", flush=True)
            if not resp.get("success"):
                print(f"[CAMO] {label} pass failed: {resp}", flush=True)
                return False

        self._camo_status("Wrap camo complete")
        return True

    def camo_apply_full_body_wrap(self):
        """Public entry — 360° multi-angle bridge camo."""
        return self.camo_apply_multi_angle()

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
            "template_min_direct_points": 8000 if full_wrap else 5000,
            "auto_flush_during_paint": True,
        }
        if full_wrap:
            payload.update({
                "single_pass_enabled": False,
                "two_pass_enabled": True,
                "template_fill_enabled": True,
                "white_base_enabled": True,
                "capture_requested_texture_width": 1024,
                "capture_requested_texture_height": 1024,
            })
        else:
            payload["single_pass_enabled"] = True
            payload["two_pass_enabled"] = False
        return payload

    def _run_paint_full_route(self, pid, full_wrap=False, timeout=120):
        payload = self._build_paint_payload(pid, full_wrap=full_wrap)
        print(
            f"[CAMO] paint_full_route wrap={full_wrap} "
            f"two_pass={payload.get('two_pass_enabled')} "
            f"keys={sorted(payload.keys())}",
        )
        return self._bridge_request(
            "paint_full_route",
            payload,
            timeout=timeout,
            abort_check=self._camo_aborted,
        )

    def camo_apply(self, r=None, g=None, b=None, a=None):
        """Apply template-brush camouflage via bridge TCP (or F10 hotkey fallback)."""
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
            return self.camo_apply_back_pass()
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
        self._reset_bridge_state("camo_stop")
        return True
