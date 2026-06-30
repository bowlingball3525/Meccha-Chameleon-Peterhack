#!/usr/bin/env python3
"""
Peterhack auto-updater — tracks GitHub main branch (no releases required).

Runs before the main UI loads. Supports:
  - git clone installs  → git fetch + reset
  - ZIP / Code download → download main.zip and merge into install folder
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

REPO = "bowlingball3525/Meccha-Chameleon-Peterhack"
BRANCH = "main"
API_URL = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
USER_AGENT = "Peterhack-Updater/1.0"

# Never overwrite user settings / local presets during zip merge.
PRESERVE_FILES = (
    os.path.join("meccha_chameleon_tools", "esp_config.json"),
)

# Skip copying these directory names from the update package.
SKIP_DIR_NAMES = {
    ".git", "__pycache__", "runtime", "backup", "logs",
    ".venv", "venv", "env", ".idea", ".vscode",
}


def install_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def version_file_path() -> str:
    return os.path.join(install_root(), "VERSION")


def write_version_file(sha: str) -> None:
    sha = (sha or "").strip().lower()
    if not sha:
        return
    with open(version_file_path(), "w", encoding="utf-8") as f:
        f.write(sha + "\n")


def get_local_sha() -> str:
    root = install_root()
    git_dir = os.path.join(root, ".git")
    if os.path.isdir(git_dir):
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if out.returncode == 0:
                sha = out.stdout.strip().lower()
                if sha:
                    return sha
        except Exception:
            pass

    vf = version_file_path()
    if os.path.isfile(vf):
        try:
            with open(vf, encoding="utf-8") as f:
                line = f.read().strip().lower()
            if line:
                return line.split()[0]
        except Exception:
            pass
    return ""


def fetch_remote_sha() -> str:
    req = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("sha") or "").strip().lower()


def _sha_match(local: str, remote: str) -> bool:
    if not local or not remote:
        return False
    local = local.lower()
    remote = remote.lower()
    return local == remote or local.startswith(remote[:7]) or remote.startswith(local[:7])


def _auto_update_enabled() -> bool:
    cfg_path = os.path.join(install_root(), "meccha_chameleon_tools", "esp_config.json")
    if not os.path.isfile(cfg_path):
        return True
    try:
        with open(cfg_path, encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("auto_update", True))
    except Exception:
        return True


def _is_git_repo(root: str) -> bool:
    return os.path.isdir(os.path.join(root, ".git"))


def _git_on_path() -> bool:
    return shutil.which("git") is not None


def _pip_install_requirements(root: str) -> None:
    req = os.path.join(root, "requirements.txt")
    if not os.path.isfile(req):
        return
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", req, "-q"],
            cwd=root,
            timeout=300,
            check=False,
        )
    except Exception as exc:
        print(f"[UPDATE] pip install skipped: {exc}", flush=True)


def _relaunch_peterhack(root: str) -> None:
    bat = os.path.join(root, "Peterhack.bat")
    try:
        if os.path.isfile(bat):
            subprocess.Popen(
                ["cmd", "/c", bat],
                cwd=root,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
                close_fds=True,
            )
            return
    except Exception:
        pass
    subprocess.Popen(
        [sys.executable, "-m", "meccha_chameleon_tools"],
        cwd=root,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008),
        close_fds=True,
    )


def update_via_git(root: str, remote_sha: str) -> bool:
    subprocess.run(
        ["git", "fetch", "origin", BRANCH],
        cwd=root,
        check=True,
        timeout=120,
        capture_output=True,
    )
    subprocess.run(
        ["git", "reset", "--hard", remote_sha],
        cwd=root,
        check=True,
        timeout=60,
        capture_output=True,
    )
    write_version_file(remote_sha)
    _pip_install_requirements(root)
    print(f"[UPDATE] git updated to {remote_sha[:7]}", flush=True)
    return True


def _download_zip(dest_path: str) -> None:
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        with open(dest_path, "wb") as out:
            shutil.copyfileobj(resp, out)


def _extract_zip_root(zip_path: str, dest_dir: str) -> str:
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    for name in os.listdir(dest_dir):
        if name.lower().endswith(".zip"):
            continue
        path = os.path.join(dest_dir, name)
        if os.path.isdir(path) and name != "__MACOSX":
            return path
    raise RuntimeError("unexpected GitHub zip layout")


def _copy_tree_merge(src_root: str, dst_root: str) -> None:
    """Copy update files into install dir; preserve user config files."""
    preserved = {}
    for rel in PRESERVE_FILES:
        dst = os.path.join(dst_root, rel)
        if os.path.isfile(dst):
            try:
                with open(dst, "rb") as f:
                    preserved[rel] = f.read()
            except Exception:
                pass

    presets_rel = os.path.join("meccha_chameleon_tools", "paint_presets")
    presets_dst = os.path.join(dst_root, presets_rel)
    presets_backup = None
    if os.path.isdir(presets_dst):
        presets_backup = tempfile.mkdtemp(prefix="peterhack-presets-")
        shutil.copytree(presets_dst, os.path.join(presets_backup, "paint_presets"))

    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        rel_dir = os.path.relpath(dirpath, src_root)
        if rel_dir == ".":
            rel_dir = ""

        for fname in filenames:
            rel = os.path.join(rel_dir, fname) if rel_dir else fname
            rel_norm = rel.replace("\\", "/")
            if rel_norm in PRESERVE_FILES:
                continue
            src_file = os.path.join(dirpath, fname)
            dst_file = os.path.join(dst_root, rel)
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            shutil.copy2(src_file, dst_file)

    for rel, data in preserved.items():
        dst = os.path.join(dst_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)

    if presets_backup and os.path.isdir(presets_dst):
        for name in os.listdir(os.path.join(presets_backup, "paint_presets")):
            s = os.path.join(presets_backup, "paint_presets", name)
            d = os.path.join(presets_dst, name)
            if os.path.isdir(s):
                if not os.path.exists(d):
                    shutil.copytree(s, d)
            elif not os.path.exists(d):
                shutil.copy2(s, d)
        shutil.rmtree(presets_backup, ignore_errors=True)


def _write_zip_apply_script(root: str, staging_dir: str, remote_sha: str) -> str:
    tag = remote_sha[:8]
    py_path = os.path.join(tempfile.gettempdir(), f"peterhack-apply-{tag}.py")
    bat_path = os.path.join(tempfile.gettempdir(), f"peterhack-apply-{tag}.bat")
    with open(py_path, "w", encoding="utf-8") as f:
        f.write(
            "import sys\n"
            f"sys.path.insert(0, {root!r})\n"
            "from meccha_chameleon_tools.updater import apply_staged_update\n"
            f"apply_staged_update({staging_dir!r}, {root!r}, {remote_sha!r})\n"
        )
    py_q = py_path.replace('"', '""')
    root_q = root.replace('"', '""')
    bat_q = os.path.join(root, "Peterhack.bat").replace('"', '""')
    py_exe = sys.executable.replace('"', '""')
    lines = [
        "@echo off",
        "setlocal EnableExtensions",
        "timeout /t 2 /nobreak >nul",
        f'"{py_exe}" "{py_q}"',
        f'if exist "{bat_q}" (start "" "{bat_q}") '
        f'else (start "" "{py_exe}" -m meccha_chameleon_tools)',
        "del \"%~f0\"",
        f'del "{py_q}"',
    ]
    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return bat_path


def apply_staged_update(staging_dir: str, root: str, remote_sha: str) -> None:
    src_root = staging_dir
    if not os.path.isdir(os.path.join(staging_dir, "meccha_chameleon_tools")):
        for name in os.listdir(staging_dir):
            path = os.path.join(staging_dir, name)
            if os.path.isdir(path) and os.path.isdir(os.path.join(path, "meccha_chameleon_tools")):
                src_root = path
                break
    _copy_tree_merge(src_root, root)
    write_version_file(remote_sha)
    _pip_install_requirements(root)
    print(f"[UPDATE] files merged to {remote_sha[:7]}", flush=True)
    shutil.rmtree(os.path.dirname(staging_dir), ignore_errors=True)


def _schedule_zip_update(root: str, remote_sha: str) -> None:
    work = tempfile.mkdtemp(prefix="peterhack-update-")
    zip_path = os.path.join(work, "main.zip")
    print("[UPDATE] downloading latest source from GitHub…", flush=True)
    _download_zip(zip_path)
    staging = _extract_zip_root(zip_path, work)
    bat_path = _write_zip_apply_script(root, staging, remote_sha)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    subprocess.Popen(["cmd", "/c", bat_path], cwd=root, creationflags=flags, close_fds=True)
    print("[UPDATE] applying in background — Peterhack will restart…", flush=True)
    os._exit(0)


def run_startup_check(argv=None) -> str | None:
    """
    Check GitHub main for a newer commit. Returns 'restart' if this process
    should exit (update applied or zip apply scheduled).
    """
    argv = argv if argv is not None else sys.argv[1:]
    if "--no-update" in argv:
        print("[UPDATE] skipped (--no-update)", flush=True)
        return None

    if not _auto_update_enabled():
        print("[UPDATE] disabled in config", flush=True)
        return None

    root = install_root()
    try:
        local = get_local_sha()
        remote = fetch_remote_sha()
    except urllib.error.URLError as exc:
        print(f"[UPDATE] offline or unreachable: {exc.reason}", flush=True)
        return None
    except Exception as exc:
        print(f"[UPDATE] check failed: {exc}", flush=True)
        return None

    if not remote:
        return None

    if _sha_match(local, remote):
        label = local[:7] if local else remote[:7]
        print(f"[UPDATE] up to date ({label})", flush=True)
        return None

    print(
        f"[UPDATE] newer source on GitHub: {remote[:7]}"
        f" (local {local[:7] if local else 'unknown'})",
        flush=True,
    )

    if _is_git_repo(root) and _git_on_path():
        try:
            update_via_git(root, remote)
            _relaunch_peterhack(root)
            return "restart"
        except Exception as exc:
            print(f"[UPDATE] git failed, using zip fallback: {exc}", flush=True)

    _schedule_zip_update(root, remote)
    return "restart"


def check_for_updates_manual() -> tuple[bool, str]:
    """Force a check; returns (updated_or_pending, message)."""
    argv = ["--manual-check"]
    root = install_root()
    try:
        local = get_local_sha()
        remote = fetch_remote_sha()
    except Exception as exc:
        return False, f"Update check failed:\n{exc}"

    if not remote:
        return False, "Could not read latest commit from GitHub."

    if _sha_match(local, remote):
        label = local[:7] if local else remote[:7]
        return False, f"Already on latest main ({label})."

    if _is_git_repo(root) and _git_on_path():
        try:
            update_via_git(root, remote)
            _relaunch_peterhack(root)
            os._exit(0)
        except Exception as exc:
            return False, f"Git update failed:\n{exc}\nTry re-downloading the ZIP."

    try:
        _schedule_zip_update(root, remote)
    except Exception as exc:
        return False, f"Download failed:\n{exc}"
    return True, f"Downloading {remote[:7]}… Peterhack will restart automatically."
