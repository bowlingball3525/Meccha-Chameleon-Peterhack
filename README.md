<p align="center">
  <img src="peter.png" alt="Peterhack Logo" width="200"/>
</p>

# Meccha Chameleon — Peterhack

External ESP, aimbot, exploits, and character-paint tools for **MECCHA CHAMELEON** (UE5.6 / `PenguinHotel-Win64-Shipping.exe`).

**Discord:** https://discord.gg/7T3damu79F

---

## How It Works

Peterhack is a **fully external** tool. It does not modify game files on disk.

1. **Attach** — On launch, Peterhack finds the game process and opens it with `pymem` (read/write memory from outside the game).
2. **Read game state** — It resolves UE5 offsets (GWorld, actors, bones, health, team, etc.) and builds a player list each frame.
3. **Draw overlay** — A transparent PyQt5 window sits on top of the game and renders ESP, radar, and the aimbot FOV circle.
4. **Write memory (optional)** — Exploits and aimbot apply small targeted writes (cooldowns, recoil, view angles, collision flags, etc.).
5. **Camouflage (bridge)** — Environment camo uses bundled **SilentJMA Meccha-Chameleon-Tools v1.8.0.1** binaries (`meccha-camouflage.exe`, `meccha-xenos-bridge.dll`, `meccha-xenos-injector.exe`). Peterhack extracts them to **`C:\peterhack\camo\`**, injects the full xenos bridge DLL, then launches the controller. Peterhack talks to the DLL over **localhost TCP** on port **47654** (`paint_full_route`, `rotate`, `cancel_paint`, etc.).
6. **Custom image paint (native)** — PNG/JPG skins use Peterhack’s own remote-call path (`ImportChannel` / UV stamping) and are separate from bridge camouflage.

**Requirements:** Run as **Administrator** (`Peterhack.bat` self-elevates). The game must be running and you should be **in a match** (not the main menu) before applying camo.

**Logs:** `C:\peterhack\logs\latest.log`  
**Camo runtime:** `C:\peterhack\camo\runtime\` (bridge status, port sidecars)

---

## Menu Tabs

| Tab | What it does |
|---|---|
| **VISUALS** | ESP dots, 2D boxes, skeleton, snap lines, OOF arrows, names, distance, health/shield bars |
| **RADAR** | Top-down mini-map of players |
| **AIMBOT** | Hold key aim (default MB5), FOV circle, smoothing, bone offset |
| **EXPLOITS** | Memory toggles — no gun CD, no recoil, decoy CD/count, noclip, anti-kick watchdog, auto-rename |
| **COLORS** | Per-team and skeleton colors |
| **CAMOUFLAGE** | Environment camo + custom image paint + UV diagnostic |
| **CHANGELOG** | Version info and auto-update toggle |

Bottom bar: **Save Config**, **ESP on/off**, **Close**, **Discord** (opens invite link).

---

## Features

### ESP Overlay
- Player dots, 2D boxes, skeleton, snap lines, off-screen (OOF) arrows
- Names, distance, health bar, shield bar
- Team colors (Hunter / Survivor / local / fallback)
- Distance-based dot scaling
- FPS counter (overlay top-left)
- Debounced player cache to reduce flicker

### Radar
- Configurable size, range, and opacity
- Same player data as ESP, drawn as a mini-map

### Aimbot
- Hold-to-aim (default **MB5**)
- FOV limit, smoothing, vertical bone offset
- Optional FOV circle on overlay

### Exploits (EXPLOITS tab)
Memory writes applied each overlay frame when toggled on:

| Toggle | Effect |
|---|---|
| No Gun Cooldown | Hunter gun cooldown → 0 |
| No Recoil | Camera shake modifier alpha → 0 |
| No Decoy Cooldown | Decoy cooldown timers → 0 |
| Set Decoy Num | Sets max decoy spawn count |
| Anti-Clipping | Disables collision on local mesh (noclip) |
| Anti-Kick | Logs disconnect / pawn loss (watchdog only) |
| Auto-Rename | Writes custom name to PlayerState FString fields |

Enable **Debug Logging** to emit `[TRAINER:TAG]` lines to `latest.log`.

### Environment Camouflage (bridge)

Both wrap modes use the **same bridge engine**. The wrap checkbox controls how many scene-capture passes run.

| Setting | Behavior |
|---|---|
| **Wrap OFF** (default) | One `paint_full_route` pass — samples the scene in front of your character and paints your texture (front-facing camo). |
| **Wrap ON (full 360°)** | Four passes so lateral flanks get painted — ends facing forward. |

**Wrap pass order (Wrap ON):**

| Pass | Label | Yaw |
|---|---|---|
| 1 | Left side | 90° |
| 2 | Right side | 270° |
| 3 | Front | 180° |
| 4 | Back | 0° (restores forward view) |

Each pass rotates the pawn/camera, scene-captures the environment, and paints visible mesh UVs via server paint batch. Expect ~30–40 seconds per pass (~2–3 minutes total with wrap).

**Flow when you click Paint Now (or press F10):**
1. Peterhack checks if the bridge TCP server is already up (ping on port **47654** or discovered sidecar).
2. Scans the game process for an already-loaded bridge DLL — **no double inject** if `meccha-xenos-bridge.dll` is present.
3. If not loaded: extracts binaries to `C:\peterhack\camo\`, writes a `.port` sidecar, **injects `meccha-xenos-bridge.dll` first** via `meccha-xenos-injector.exe`, then launches `meccha-camouflage.exe` (controller skips re-inject if TCP is already up).
4. For each pass: optional `rotate` → `paint_full_route` over TCP (scene-capture + server paint batch replication).
5. Sends `cancel_paint` and quiesces paint flags when done; view rotation is restored after wrap.

**Bridge TCP commands:**

| Command | Description |
|---|---|
| `paint_full_route` | Native template brush paint (scene-capture basecolor → UV strokes). |
| `rotate` | Rotate local pawn by yaw delta (`K2_SetActorRotation`). Native camera fallback if unavailable. |
| `cancel_paint` | Cancel active paint and drain the queue. |
| `ping` / `capabilities` | Health check and supported command list. |
| `teleport`, `set_fov`, `kill` | Exposed by bridge; not used by Peterhack UI. |

**Stop:** **F9** or **Stop Camo** → `cancel_paint` over TCP.

Bridge binaries (bundled in `meccha_chameleon_tools/`, extracted to `C:\peterhack\camo\`):
- `meccha-camouflage.exe` — controller / TCP client service
- `meccha-xenos-bridge.dll` — in-game TCP bridge + full paint pipeline (inject this one)
- `meccha-xenos-injector.exe` — loads the xenos bridge DLL into the game process

### Custom Character Paint — Apply Image

Separate from environment camo. Paint any PNG/JPG onto your character atlas:

| Wrap mode | Description |
|---|---|
| **Projector (front → back)** | Image spans the full atlas front → back as one continuous wrap. |
| **Centered (chest outward)** | Image center on chest; top → head, bottom → feet. |

- Auto-trims transparent / solid borders
- White base coat clears old paint before apply
- **Image Quality** slider (1 Draft → 5 Ultra)
- **Run UV Test** — diagnostic overlay (quadrants, islands, grid) to calibrate placement
- Game process priority lowered while painting (restored after)

---

## Hotkeys

| Key | Action |
|---|---|
| **Insert** / **F1** | Toggle menu + ESP overlay |
| **F10** | Apply environment camouflage (same as Paint Now) |
| **F9** | Stop / cancel camouflage paint |
| **MB5** (default) | Aimbot hold |

Drag the menu title bar to reposition. Menu hotkeys use `RegisterHotKey`; F9/F10 are polled each frame so they do not conflict with the bridge controller.

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
3. Use the menu tabs to configure ESP, exploits, and camo.
4. For environment camo: enable **Enable Camouflage**, optionally **Wrap around character (full 360°)**, then **Paint Now**.

Pre-built EXE: download the **Peterhack** artifact from [GitHub Actions](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack/actions) after a push to `main`, or build locally with PyInstaller (see `.github/workflows/build.yml`).

---

## Auto-update

On launch, Peterhack can check [GitHub main](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack) for newer source and apply it automatically, then restart.

- **ZIP users** — files merged in place; `esp_config.json` and paint presets preserved.
- **Git clone users** — `git fetch` + `git reset --hard` when Git is installed.
- Disable in the **CHANGELOG** tab, or run: `python -m meccha_chameleon_tools --no-update`
- Current version shown on the CHANGELOG tab (`VERSION` file = git commit).

---

## Troubleshooting (camo)

| Symptom | What to try |
|---|---|
| `failed to communicate with bridge DLL` | Run as Administrator; be in a match; check `C:\peterhack\logs\latest.log` and `C:\peterhack\camo\runtime\` |
| `wrong bridge in game` / `runtime-bridge` loaded | Restart the game — controller’s embedded DLL lacks `rotate`; Peterhack needs a fresh inject of `meccha-xenos-bridge.dll` |
| Bridge DLL loaded but TCP dead | Restart the game (Peterhack will not inject twice into the same session) |
| Only front painted / white sides | Enable **Wrap around character (full 360°)** — single pass only paints camera-facing surfaces |
| `unknown bridge command` on rotate | Same as runtime-bridge issue — restart game and retry |
| Paint takes a long time with wrap | Normal — four full scene-capture passes (~2–3 min total) |

---

## Disclaimer

For educational purposes only. Use at your own risk.
