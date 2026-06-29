"""Peterhack session logging to C:\\peterhack\\logs."""
import atexit
import datetime
import faulthandler
import os
import sys
import threading
import traceback

LOG_DIR = r"C:\peterhack\logs"

_log_files = []
_session_path = None
_original_stdout = None
_original_stderr = None
_original_excepthook = None
_lock = threading.Lock()


class _TeeStream:
    """Write to the original stream and every open log file."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        if not data:
            return 0
        with _lock:
            for fh in _log_files:
                try:
                    fh.write(data)
                    fh.flush()
                except Exception:
                    pass
            try:
                self._stream.write(data)
                self._stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        with _lock:
            for fh in _log_files:
                try:
                    fh.flush()
                except Exception:
                    pass
            try:
                self._stream.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")


def _log_line(text):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {text}\n"
    with _lock:
        for fh in _log_files:
            try:
                fh.write(line)
                fh.flush()
            except Exception:
                pass
    try:
        sys.__stdout__.write(line)
        sys.__stdout__.flush()
    except Exception:
        pass


def _log_exception(prefix, exc_type, exc, tb):
    _log_line(f"{prefix} {exc_type.__name__}: {exc}")
    formatted = "".join(traceback.format_exception(exc_type, exc, tb))
    with _lock:
        for fh in _log_files:
            try:
                fh.write(formatted)
                fh.flush()
            except Exception:
                pass
    try:
        sys.__stderr__.write(formatted)
        sys.__stderr__.flush()
    except Exception:
        pass


def _sys_excepthook(exc_type, exc, tb):
    _log_exception("UNHANDLED EXCEPTION", exc_type, exc, tb)
    if _original_excepthook:
        _original_excepthook(exc_type, exc, tb)


def _thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    _log_exception(
        f"THREAD EXCEPTION ({getattr(args.thread, 'name', 'unknown')})",
        args.exc_type,
        args.exc_value,
        args.exc_traceback,
    )


def get_log_path():
    return _session_path


def get_log_dir():
    return LOG_DIR


def shutdown_file_logging():
    _log_line("Peterhack logging shutdown")
    global _log_files
    for fh in _log_files:
        try:
            fh.flush()
            fh.close()
        except Exception:
            pass
    _log_files = []


def _purge_old_logs(log_dir: str, max_age_days: int = 1) -> None:
    """Delete *.log files in log_dir that are older than max_age_days.

    latest.log is always skipped since it is the mirror of the current session.
    Errors on individual files are silently ignored so a locked file never
    prevents the app from starting.
    """
    import time
    cutoff = time.time() - max_age_days * 86400
    try:
        for name in os.listdir(log_dir):
            if not name.endswith(".log"):
                continue
            if name == "latest.log":
                continue
            path = os.path.join(log_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    sys.__stderr__.write(f"[LOG] Purged old log: {name}\n")
            except Exception:
                pass
    except Exception:
        pass


def setup_file_logging():
    """Redirect stdout/stderr to session + latest log files under LOG_DIR."""
    global _session_path, _original_stdout, _original_stderr, _original_excepthook
    global _log_files

    primary_dir = LOG_DIR
    log_dir = primary_dir
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        log_dir = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "peterhack", "logs",
        )
        os.makedirs(log_dir, exist_ok=True)
        sys.__stderr__.write(
            f"[LOG] Could not create {primary_dir}: {exc}\n"
            f"[LOG] Using fallback: {log_dir}\n"
        )
    log_dir = LOG_DIR
    _purge_old_logs(log_dir)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    _session_path = os.path.join(log_dir, f"peterhack_{stamp}.log")
    latest_path = os.path.join(log_dir, "latest.log")

    session_fh = open(_session_path, "a", encoding="utf-8", buffering=1)
    latest_fh = open(latest_path, "w", encoding="utf-8", buffering=1)
    _log_files = [session_fh, latest_fh]

    _original_stdout = sys.stdout
    _original_stderr = sys.stderr
    sys.stdout = _TeeStream(_original_stdout)
    sys.stderr = _TeeStream(_original_stderr)

    _original_excepthook = sys.excepthook
    sys.excepthook = _sys_excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook

    try:
        faulthandler.enable(file=session_fh, all_threads=True)
    except Exception:
        pass

    atexit.register(shutdown_file_logging)

    _log_line("=" * 60)
    _log_line("Peterhack session started")
    _log_line(f"Session log: {_session_path}")
    _log_line(f"Latest log:  {latest_path}")
    _log_line(f"PID: {os.getpid()}")
    _log_line(f"Python: {sys.version.replace(chr(10), ' ')}")
    _log_line(f"CWD: {os.getcwd()}")
    _log_line(f"Executable: {sys.executable}")
    _log_line("=" * 60)

    print(f"[LOG] Writing to {_session_path}", flush=True)
    print(f"[LOG] Mirror copy: {latest_path}", flush=True)
    return _session_path
