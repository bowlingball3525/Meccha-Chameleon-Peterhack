"""Persist blocked Steam IDs for autokick under C:\\peterhack\\."""
import json
import os
import datetime

from meccha_chameleon_tools.log_util import PETERHACK_ROOT

BLOCKLIST_FILE = os.path.join(PETERHACK_ROOT, "blocked_players.json")


def _normalize_steam_id(steam_id: str) -> str:
    return (steam_id or "").strip()


def load_blocklist() -> list:
    """Return list of {steam_id, name, added} dicts."""
    if not os.path.isfile(BLOCKLIST_FILE):
        return []
    try:
        with open(BLOCKLIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        out = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            sid = _normalize_steam_id(entry.get("steam_id", ""))
            if not sid:
                continue
            out.append({
                "steam_id": sid,
                "name": (entry.get("name") or "").strip(),
                "added": entry.get("added") or "",
            })
        return out
    except Exception:
        return []


def save_blocklist(entries: list) -> bool:
    try:
        os.makedirs(os.path.dirname(BLOCKLIST_FILE), exist_ok=True)
        clean = []
        seen = set()
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            sid = _normalize_steam_id(entry.get("steam_id", ""))
            if not sid or sid in seen:
                continue
            seen.add(sid)
            clean.append({
                "steam_id": sid,
                "name": (entry.get("name") or "").strip(),
                "added": entry.get("added") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
        with open(BLOCKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2)
        return True
    except Exception:
        return False


def blocklist_ids(entries=None) -> set:
    entries = entries if entries is not None else load_blocklist()
    return {_normalize_steam_id(e.get("steam_id", "")) for e in entries if _normalize_steam_id(e.get("steam_id", ""))}


def add_blocked_player(steam_id: str, name: str = "") -> bool:
    sid = _normalize_steam_id(steam_id)
    if not sid:
        return False
    entries = load_blocklist()
    for entry in entries:
        if entry.get("steam_id") == sid:
            if name and not entry.get("name"):
                entry["name"] = name.strip()
            return save_blocklist(entries)
    entries.append({
        "steam_id": sid,
        "name": (name or "").strip(),
        "added": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    return save_blocklist(entries)


def remove_blocked_player(steam_id: str) -> bool:
    sid = _normalize_steam_id(steam_id)
    if not sid:
        return False
    entries = [e for e in load_blocklist() if e.get("steam_id") != sid]
    return save_blocklist(entries)
