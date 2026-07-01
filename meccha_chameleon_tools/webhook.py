"""Discord webhook notifications — who is running Peterhack."""
import datetime
import json
import os
import socket
import threading
import urllib.error
import urllib.request

# Paste your Discord webhook URL here for builds you distribute to others.
# Users can override via esp_config.json → discord_webhook_url
DEFAULT_WEBHOOK_URL = ""

_WEBHOOK_PREFIX = "https://discord.com/api/webhooks/"


def get_webhook_url(config=None) -> str:
    if config is not None:
        url = (getattr(config, "discord_webhook_url", "") or "").strip()
        if url:
            return url
    env = os.environ.get("PETERHACK_DISCORD_WEBHOOK", "").strip()
    if env:
        return env
    return (DEFAULT_WEBHOOK_URL or "").strip()


def _valid_webhook(url: str) -> bool:
    return bool(url) and url.startswith(_WEBHOOK_PREFIX)


def _post_webhook(url: str, payload: dict) -> None:
    if not _valid_webhook(url):
        return
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Peterhack/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        print(f"[WEBHOOK] HTTP {exc.code}: {exc.reason}", flush=True)
    except Exception as exc:
        print(f"[WEBHOOK] send failed: {exc}", flush=True)


def _send_async(url: str, payload: dict) -> None:
    threading.Thread(
        target=_post_webhook,
        args=(url, payload),
        daemon=True,
        name="discord-webhook",
    ).start()


def _embed(title: str, fields: list, color: int = 0x7EC850) -> dict:
    clean = []
    for f in fields:
        name = str(f.get("name", ""))[:256]
        value = str(f.get("value", ""))[:1024] or "—"
        entry = {"name": name, "value": value, "inline": bool(f.get("inline", False))}
        clean.append(entry)
    return {
        "title": title[:256],
        "color": color,
        "fields": clean,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def _version_label() -> str:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "VERSION")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                sha = fh.read().strip()
            if sha:
                return sha[:7]
    except Exception:
        pass
    return "unknown"


def _host_fields() -> list:
    return [
        {"name": "Windows user", "value": os.environ.get("USERNAME", "?"), "inline": True},
        {"name": "PC name", "value": os.environ.get("COMPUTERNAME", "?"), "inline": True},
        {"name": "Version", "value": _version_label(), "inline": True},
    ]


def notify_peterhack_launch(config) -> None:
    """Fire when Peterhack starts (before in-game name is known)."""
    url = get_webhook_url(config)
    if not _valid_webhook(url):
        return
    fields = _host_fields()
    fields.append({"name": "Status", "value": "Launched — waiting for match", "inline": False})
    _send_async(url, {"embeds": [_embed("Peterhack started", fields)]})


def notify_peterhack_in_match(config, *, display_name, steam_name, steam_id, team, game_pid) -> None:
    """Fire once when the local player is identified in a match."""
    url = get_webhook_url(config)
    if not _valid_webhook(url):
        return
    fields = _host_fields()
    fields.extend([
        {"name": "In-game name", "value": display_name or "?", "inline": True},
        {"name": "Steam name", "value": steam_name or "?", "inline": True},
        {"name": "Steam ID", "value": steam_id or "?", "inline": True},
        {"name": "Team", "value": team or "?", "inline": True},
        {"name": "Game PID", "value": str(game_pid or 0), "inline": True},
    ])
    try:
        fields.append({"name": "Host", "value": socket.gethostname(), "inline": True})
    except Exception:
        pass
    _send_async(url, {"embeds": [_embed("Peterhack in match", fields)]})


def bind_webhook_config(esp, config) -> None:
    """Attach config to ESP for one-shot in-match webhook."""
    esp._webhook_config = config
