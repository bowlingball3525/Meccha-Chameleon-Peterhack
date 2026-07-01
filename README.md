<p align="center">
  <img src="peter.png" alt="Peterhack Logo" width="200"/>
</p>

# Meccha Chameleon — Peterhack

External ESP, aimbot, exploits, player tracking, and character-paint tools for **MECCHA CHAMELEON** (UE5.6 / `PenguinHotel-Win64-Shipping.exe`).

**Discord:** https://discord.gg/7T3damu79F

---

## How It Works

Peterhack is a **fully external** tool. It does not modify game files on disk.

1. **Attach** — On launch, Peterhack finds the game process and opens it with `pymem` (read/write memory from outside the game).
2. **Read game state** — It resolves UE5 offsets (GWorld, actors, bones, health, team, etc.) and builds a player list each frame.
3. **Draw overlay** — A transparent PyQt5 window sits on top of the game and renders ESP, radar, and the aimbot FOV circle.
4. **Write memory (optional)** — Exploits and aimbot apply small targeted writes (cooldowns, recoil, view angles, collision flags, etc.).
5. **Camouflage (bridge)** — Environment camo injects **`meccha-xenos-bridge.dll`** into the game, then talks over **localhost TCP** port **47654** (`paint_full_route`, `rotate`, `cancel_paint`, etc.). Bridge files live in **`C:\peterhack\camo\`**.
6. **Custom image paint (native)** — PNG/JPG skins use Peterhack’s own remote-call path (`ImportChannel` / UV stamping) and are separate from bridge camouflage.

**Requirements:** Run as **Administrator** (`Peterhack.bat` self-elevates). Be **in a match** (not the main menu) before applying camo.

**Logs:** `C:\peterhack\logs\latest.log`  
**Blocklist:** `C:\peterhack\blocked_players.json`

---

## Menu Tabs

| Tab | What it does |
|---|---|
| **VISUALS** | ESP dots, 2D boxes, skeleton, snap lines, OOF arrows, names, Steam ID, distance, health/shield bars |
| **PLAYERS** | Session player table, copy/save Steam64 IDs, blocklist, optional autokick |
| **RADAR** | Top-down mini-map of players |
| **AIMBOT** | Hold key aim (default MB5), FOV circle, smoothing, bone offset |
| **EXPLOITS** | Memory toggles — no gun CD, no recoil, decoy CD/count, noclip, anti-kick watchdog, auto-rename |
| **COLORS** | Per-team and skeleton colors |
| **CAMOUFLAGE** | Full 360° environment camo + custom image paint + UV diagnostic |
| **CHANGELOG** | Version info and auto-update toggle |

Bottom bar: **Save Config**, **ESP on/off**, **Close**, **Discord** (opens invite link).

---

## Features

### ESP Overlay
- Player dots, 2D boxes, skeleton, snap lines, off-screen (OOF) arrows
- Names, optional Steam64 ID, distance, health bar, shield bar
- Blocklisted players highlighted in orange with `[BLOCKED]` tag
- Team colors (Hunter / Survivor / local / fallback)
- Distance-based dot scaling
- FPS counter (overlay top-left)
- Debounced player cache to reduce flicker

### Players tab
- Live session list (name, team, Steam64 ID)
- **Copy Steam ID** / **Save to Blocklist**
- Blocklist stored at `C:\peterhack\blocked_players.json`
- **Auto-Kick** blocklisted players (host: Redpoint kick; non-host: optional leave lobby)

### Radar
- Configurable size, range, and opacity
- Same player data as ESP, drawn as a mini-map

### Aimbot
- Hold-to-aim (default **MB5**)
- FOV limit, smoothing, vertical bone offset
- Optional FOV circle on overlay

### Exploits (EXPLOITS tab)
Memory writes applied when toggled on (throttled ~20 Hz, not every overlay frame):

| Toggle | Effect |
|---|---|
| No Gun Cooldown | Hunter gun cooldown → 0 |
| No Recoil | Camera shake modifier alpha → 0 |
| No Decoy Cooldown | Decoy cooldown timers → 0 |
| Set Decoy Num | Sets max decoy spawn count |
| Anti-Clipping | Disables collision on local mesh (noclip) |
| Anti-Kick | Logs disconnect / pawn loss (watchdog only) |
| Auto-Rename | Sets **CustomPlayerName** (in-game display name) |

Enable **Debug Logging** to emit `[TRAINER:TAG]` lines to `latest.log`.

### Environment Camouflage (bridge)

Every **Paint Now** / **F10** runs **full 360° wrap** (four scene-capture passes). There is no front-only mode.

| Pass | Label | Camera yaw offset |
|---|---|---|
| 1 | Left side | 90° |
| 2 | Right side | 270° |
| 3 | Front | 180° |
| 4 | Back | 0° (restores starting view) |

Each pass **orbits the camera** (controller `ControlRotation` only — your character does not spin), scene-captures the environment, and paints visible mesh UVs. Expect ~30–40 seconds per pass (~2–3 minutes total).

**Flow:**
1. Peterhack injects **`meccha-xenos-bridge.dll`** via **`meccha-xenos-injector.exe`** (or reuses an existing bridge if TCP on port **47654** responds).
2. For each pass: restore baseline view → `paint_full_route` with `camera_yaw_offset` (camera orbits inside the bridge; no separate `rotate` call).
3. Sends `cancel_paint` and restores view rotation when done.

**Bridge TCP commands:**

| Command | Description |
|---|---|
| `paint_full_route` | Scene-capture basecolor → UV stroke paint (`camera_yaw_offset` for wrap passes) |
| `rotate` | Legacy camera yaw delta (superseded by `camera_yaw_offset` in paint) |
| `cancel_paint` | Cancel active paint and drain the queue |
| `ping` / `capabilities` | Health check and command list |

**Stop:** **F9** or **Stop Camo** → `cancel_paint` over TCP.

**Bridge binaries** (copied to `C:\peterhack\camo\` on first use):

| File | Role |
|---|---|
| `meccha-xenos-bridge.dll` | In-game TCP bridge (scene capture + server paint batch) |
| `meccha-xenos-injector.exe` | Loads the bridge DLL into the game process |

Peterhack ships prebuilt bridge binaries in `meccha_chameleon_tools/`. To rebuild locally, compile `runtime/src/bridge.cpp` with Visual Studio (see `runtime/scripts/build.ps1`) and copy the output to `meccha-xenos-bridge.dll`.

### Custom Character Paint — Apply Image

Separate from environment camo. Paint any PNG/JPG onto your character atlas:

| Wrap mode | Description |
|---|---|
| **Projector (front → back)** | Image spans the full atlas front → back as one continuous wrap. |
| **Centered (chest outward)** | Image center on chest; top → head, bottom → feet. |

- Auto-trims transparent / solid borders
- White base coat clears old paint before apply
- **Image Quality** slider (1 Draft → 5 Ultra)
- **Run UV Test** — diagnostic overlay to calibrate placement

---

## Hotkeys

| Key | Action |
|---|---|
| **Insert** / **F1** | Toggle menu + ESP overlay |
| **F10** | Apply environment camouflage (360° wrap) |
| **F9** | Stop / cancel camouflage paint |
| **MB5** (default) | Aimbot hold |

Drag the menu title bar to reposition. Menu hotkeys use `RegisterHotKey`; F9/F10 are polled each frame.

---

## Requirements

```
Python 3.10+
PyQt5
pymem
pywin32
Pillow
```

```bash
pip install -r requirements.txt
```

---

## Usage

1. Launch **MECCHA CHAMELEON** and join a match.
2. Run **`Peterhack.bat`** (self-elevates to Administrator).
3. Configure ESP, exploits, and colors in the menu tabs.
4. For environment camo: press **F10** or click **Paint Now** on the CAMOUFLAGE tab.
5. Use the **PLAYERS** tab to copy Steam IDs, manage your blocklist, or enable autokick.

Pre-built EXE: download the **Peterhack** artifact from [GitHub Actions](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack/actions) after a push to `main`, or build locally with PyInstaller (see `.github/workflows/build.yml`).

---

## Auto-update

On launch, Peterhack can check [GitHub main](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack) for newer source and apply it automatically, then restart.

- **ZIP users** — files merged in place; `esp_config.json` and paint presets preserved.
- **Git clone users** — `git fetch` + `git reset --hard` when Git is installed.
- Disable in the **CHANGELOG** tab, or run: `python -m meccha_chameleon_tools --no-update`
- Current version shown on the CHANGELOG tab (`VERSION` file = git commit).

---

## Troubleshooting

| Symptom | What to try |
|---|---|
| `failed to communicate with bridge DLL` | Run as Administrator; be in a match; check `C:\peterhack\logs\latest.log` |
| `could not unload` / `runtime-bridge-*.dll` stuck | **Quit the game completely** and relaunch. Don't run `meccha-camouflage.exe` separately. Run Peterhack as Administrator. |
| `missing bridge binaries` | Ensure `meccha-xenos-bridge.dll` + `meccha-xenos-injector.exe` are in `meccha_chameleon_tools/` (use GitHub Actions EXE or full release, not source-only zip). |
| Bridge inject OK but paint fails on retry | Restart game — do not spam F10 after a failed reinject |
| `bridge DLL is outdated (no rotate)` | Restart game so Peterhack can copy the latest bridge from the bundle |
| Camo only paints one side / body spins | Update to latest build — rotate should move **camera only** |
| Camo takes ~2–3 minutes | Normal for full 360° wrap (four passes) |
| Steam ID shows `—` in PLAYERS tab | Wait a few seconds for replication; select row and Copy again |
| ESP feels laggy | Disable **Show Steam ID** if you do not need it; keep PLAYERS tab closed when not in use |

---

## Disclaimer

For educational purposes only. Use at your own risk.
