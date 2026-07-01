<p align="center">
  <img src="peter.png" alt="Peterhack Logo" width="200"/>
</p>

# Meccha Chameleon — Peterhack

External ESP, aimbot, exploits, player tracking, and character-paint tools for **MECCHA CHAMELEON** (UE5.6 / `PenguinHotel-Win64-Shipping.exe`).

**Discord:** https://discord.gg/7T3damu79F

> **Built with AI:** This project is actively developed with **[Cursor AI Composer 2.5](https://cursor.com)** — an AI coding assistant used for feature implementation, bridge hook work, debugging, and documentation.

---

## How It Works

Peterhack is a **fully external** cheat. It does not modify game files on disk.

```
┌─────────────────┐     pymem read/write      ┌──────────────────────────┐
│  Peterhack.exe  │ ◄──────────────────────► │  PenguinHotel (UE5.6)    │
│  (Python/PyQt5) │                          │  PenguinHotel-Win64-...  │
└────────┬────────┘                          └────────────┬─────────────┘
         │                                                │
         │  localhost TCP :47654                          │  injected DLL
         └──────────────────────────────────────────────►│  meccha-xenos-bridge.dll
                                                           └──────────────────────────┘
```

1. **Attach** — On launch, Peterhack finds the game process and opens it with `pymem` (external read/write memory).
2. **Read game state** — Resolves UE5 offsets (GWorld, actors, bones, health, team, Steam IDs, etc.) and builds a debounced player list each frame.
3. **Draw overlay** — A transparent PyQt5 window sits on top of the game and renders ESP, radar, and the aimbot FOV circle.
4. **Write memory (trainer)** — Exploits apply small targeted writes (cooldowns, recoil, collision flags) at ~20 Hz when toggled on.
5. **Bridge (camo + exploits)** — Auto-injects **`meccha-xenos-bridge.dll`** and talks over **localhost TCP port 47654**. Used for environment camo, teleport, kill, rename, and anti-kick hooks.
6. **Custom image paint** — PNG/JPG skins use Peterhack’s own remote-call path (`ImportChannel` / UV stamping), separate from bridge camouflage.

**Requirements:** Run as **Administrator** (`Peterhack.bat` self-elevates). Be **in a match** (not the main menu) before applying camo or bridge exploits.

**Logs:** `C:\peterhack\logs\latest.log`  
**Anti-kick log:** `C:\peterhack\logs\anti_kick.log`  
**Blocklist:** `C:\peterhack\blocked_players.json`

---

## Menu Tabs

| Tab | What it does |
|---|---|
| **VISUALS** | ESP dots, 2D/corner boxes, skeleton, snap lines, OOF arrows, names, Steam ID, distance, health/shield bars, team filter, enemy-only, visible colors |
| **COLORS** | Per-team and skeleton colors |
| **PLAYERS** | Session player table, kill selected survivor, copy/save Steam64 IDs, blocklist, optional autokick |
| **RADAR** | Top-down mini-map of players |
| **AIMBOT** | Hold key aim (default MB5), FOV circle, smoothing, bone offset, visible-only option |
| **EXPLOITS** | Memory toggles, hunter/survivor exploits, magnet, anti-kick, rename, teleport/kill |
| **CAMOUFLAGE** | Dynamic 360° environment camo, quality slider, pass options, custom image paint, UV diagnostic |
| **CHANGELOG** | Version info and auto-update toggle |

Bottom bar: **Save Config**, **ESP on/off**, **Close**, **Discord**.

---

## Features

### ESP Overlay

| Feature | How it works |
|---|---|
| **Dot / Box / Corner Box / Skeleton** | Reads bone positions from the skeletal mesh component, projects world → screen, draws on the overlay each frame. |
| **Snap lines** | Line from screen bottom-center to each player’s projected position. |
| **OOF arrows** | Off-screen players get an arrow at the screen edge (or configurable radius) pointing toward them; optional name, distance, health. |
| **Names / Steam64 / Distance** | Name from replicated player state; Steam ID from `FUniqueNetIdRepl` when replicated; distance from local pawn. |
| **Health / Shield bars** | Reads replicated health/shield floats and draws bars above the player. |
| **Team colors** | Hunter (red), Survivor (green), local (green), fallback enemy color. |
| **Blocklist highlight** | Blocklisted Steam IDs shown in orange with `[BLOCKED]` tag. |
| **Enemy Only + Visible colors** | When enabled, only enemies render; optional green/purple for visible vs occluded targets. |
| **Distance scaling** | Dot radius scales with distance for readability. |
| **Debounced cache** | Player list cached briefly to reduce ESP flicker when actors churn. |

### Players Tab

| Feature | How it works |
|---|---|
| **Session list** | Reads `GameState.PlayerArray`; cached ~2.5 s to avoid lag. Steam IDs resolved on demand (copy/save), not every refresh. |
| **Copy Steam ID / Save to Blocklist** | Copies selected row’s Steam64; appends to `blocked_players.json`. |
| **Kill Selected Survivor** | Hunter only — calls `KillPlayer` on the selected survivor’s pawn via ProcessEvent. |
| **Auto-Kick** | When **host**: calls Redpoint `KickPlayerController` for blocklisted Steam IDs. When **non-host** with **Leave on block**: exits lobby if a blocklisted player joins. |

Player list UI uses explicit row colors (no broken alternating white rows) and a always-visible scrollbar.

### Radar

Configurable size, range, and opacity. Same player data as ESP, drawn as a top-down mini-map relative to your pawn.

### Aimbot

| Setting | How it works |
|---|---|
| **Hold key (default MB5)** | While held, finds the closest enemy within FOV to screen center. |
| **FOV / Smoothing / Offset** | Limits target cone; lerps view angles toward target bone; vertical offset for head vs chest. |
| **Visible check** | Optional — only aims at targets passing line-of-sight check. |
| **FOV circle** | Draws aim cone on overlay when enabled. |

Writes `ControlRotation` on the local PlayerController via pymem.

### Exploits (EXPLOITS Tab)

Memory writes applied when toggled on (~20 Hz trainer tick):

| Toggle | How it works |
|---|---|
| **No Gun Cooldown** | Hunter gun cooldown timer → 0 each tick. |
| **No Recoil** | Camera shake modifier alpha → 0 on the local camera manager. |
| **No Decoy Cooldown** | Sets decoy cooldown slots to ready (30.0) each tick — survivor. |
| **Anti Detection (Survivor)** | Clears `OverlapCheckCapsules` so buried survivors are not revealed as “Too Buried”. |
| **Infinite Bullets (Hunter)** | Sets `InfinityBullet` flag each tick. |
| **Magnet (Hunter)** | Toggle with **G** (custom key via **Record Key**). Pulls all survivors into a line along your view direction. Shows **MAGNET ACTIVE** on overlay. |
| **Kill All Survivors** | One-click button — `KillPlayer` on every survivor in session (staggered). |
| **Set Decoy Num** | Writes max decoy spawn count. |
| **Anti-Clipping (noclip)** | Sets collision disabled on local body mesh + capsule. |
| **Anti-Kick** | Bridge vtable hooks block kick/disconnect RPCs (see below). |
| **Auto-Rename** | Queued background rename via bridge; debounced 1.5 s after typing stops. |
| **Rename button** | Manual rename (Enter or **Rename**); queued on background thread — safe to spam without freezing UI. |
| **Teleport / Kill Self** | Bridge TCP: `K2_SetActorLocation` / destroy local pawn. |
| **Debug Logging** | Emits `[TRAINER:TAG]` lines to `latest.log`. |

Hunter/survivor exploits ported from [phxgg/chameleonEsp](https://github.com/phxgg/chameleonEsp) (external memory + ProcessEvent, same offsets).

#### Anti-Kick (detailed)

Anti-kick runs **inside the injected bridge DLL**, not from Python memory writes.

| Layer | Behavior |
|---|---|
| **Hook method** | **Vtable ProcessEvent hooks** on your local `PlayerController`, `PlayerState`, and `NetConnection` — does **not** patch the global ProcessEvent function (UE4SS-safe). |
| **Blocked RPCs** | Explicit: `ClientWasKicked`, `ClientReturnToMainMenu`, `ClientReturnToMainMenuWithTextReason`, `PlayerState.Kick`, `NetConnection.Close`, etc. |
| **Auto-scan** | Scans class hierarchies for kick/ban/disconnect/leave/redpoint/eos-like function names and adds them to the block list. |
| **Kick logger** | Each block logged to `anti_kick.log` with seq, function name, owner class. Trainer polls `get_anti_kick_log` over TCP. |
| **Auto-refresh** | Re-syncs hooks when your controller or player state pointer changes (e.g. lobby → match spawn). |
| **Limitation** | Pure **EOS/Redpoint platform kicks** may still drop the socket at the network layer even when UE RPCs are blocked. Check logs for `NetConnection lost while world active`. |

Enable Anti-Kick **after** joining a match for best results. Fully quit the game before updating `meccha-xenos-bridge.dll`.

#### Rename (detailed)

| Method | Path |
|---|---|
| **Auto-Rename toggle** | After 1.5 s debounce, queues `set_player_name` on a **background worker** (never blocks the UI). |
| **Rename button / Enter** | Queues the same worker; latest name wins if you spam click or type. |
| **Bridge handler** | Runs `SetName(Server)` on the **game thread** via bridge message hook. |
| **Rate limit** | ~0.45 s minimum between bridge rename calls; 10 s TCP timeout. |

Works in lobby as non-host (nameplate above character). Spamming rename no longer freezes the menu.

### Bridge Commands (TCP `:47654`)

| Command | Description |
|---|---|
| `ping` / `capabilities` | Health check and command list |
| `paint_full_route` | Camera settle → UV sample → scene-capture → `ServerPaintBatch` |
| `rotate` | `"target":"camera"` (ControlRotation) or `"target":"pawn"` |
| `cancel_paint` | Cancel active paint queue |
| `teleport` | `{x,y,z}` → `K2_SetActorLocation` |
| `kill` | Destroy local pawn |
| `set_fov` | Camera FOV override (1–179) |
| `set_anti_kick` | Enable/disable vtable anti-kick hooks |
| `get_anti_kick_log` | Fetch kick block log entries since seq |
| `set_player_name` | Replicated rename via `SetName(Server)` |
| `shutdown` | Stop bridge TCP server |

Bridge auto-injects on game connect. Binaries copied to `C:\peterhack\camo\` on first use.

### Environment Camouflage (Bridge)

Every **Paint Now** / **F10** runs a **360° environment camo wrap** via the in-game bridge. Camera angles are computed **dynamically** from your pawn root rotation and current view — standing, crouched, or lay-down emotes are handled automatically.

**Default pass order:** **front → left → right → back → head/shoulders → inner legs**

| Pass | Standing (`dynamic-upright`) | Lay-down / tilted (`dynamic-flat`) |
|---|---|---|
| **Front** | Horizontal, pawn forward | Look **up** from below the body |
| **Left / Right** | Horizontal sides | Horizontal sides |
| **Back** | Horizontal, opposite forward | Look **down** from above |
| **Head / shoulders** | Downward front tilt (~−55°) | Body-relative downward tilt |
| **Inner legs** | Upward front tilt (~+53°) | Side-up tilt (inner thigh UV) |

Each pass sets **camera rotation only** (pawn does not spin), waits **2.0 s** (`camera_settle_ms`), UV-samples the mesh, scene-captures environment colors, and paints via `ServerPaintBatch`. The bridge maps each UV stamp to the correct environment color by projecting the mesh **hit world position** into the scene-capture camera.

**CAMOUFLAGE tab options:**

| Control | Description |
|---|---|
| **Camo quality** | Slider 1–20 — stroke density per pass (higher = smoother, slower). ~30–60 s per pass; six passes ≈ **3–6 minutes** at quality 12+. |
| **Disable front pass (only if flat map)** | Skips front; runs left → right → back + detail passes. Mutually exclusive with back-only. |
| **Back pass only** | Paints **only** the back orbit pass (spine/rear). Mutually exclusive with skip-front. Front/back yaw corrected for camera pullback math. |
| **Paint Now / F10** | Start one full `camo_apply()` |
| **Stop Camo (F9)** | `cancel_paint` over TCP |

**Noclip during camo:** On non-upright orbits, noclip is enabled for the entire front pass so geometry does not block the below-looking-up view. Restored before side/back passes. Separate from the EXPLOITS noclip toggle.

**Flow:**
1. Peterhack injects **`meccha-xenos-bridge.dll`** via **`meccha-xenos-injector.exe`** (or reuses TCP on **47654** if already loaded).
2. **Paint Now** / **F10** saves your view, plans look directions from body/view axes, runs **`paint_full_route`** once per pass.
3. Front pass (and steep inner-legs pass) use **body-anchored scene capture** when needed.
4. Restores your original camera when done.

### Custom Character Paint — Apply Image

Separate from environment camo. Paint any PNG/JPG onto your character atlas:

| Wrap mode | Description |
|---|---|
| **Projector (front → back)** | Image spans the full atlas front → back as one continuous wrap. |
| **Centered (chest outward)** | Image center on chest; top → head, bottom → feet. Island-calibrated UV map for head/torso/legs regions. |

- Auto-trims transparent / solid borders
- White base coat clears old paint before apply
- **Image Quality** slider (1 Draft → 5 Ultra)
- **Run UV Test** — diagnostic overlay (quadrants, islands, grid, slices, full) to calibrate placement

---

## Hotkeys

| Key | Action |
|---|---|
| **Insert** / **F1** | Toggle menu + ESP overlay |
| **F10** | Apply environment camouflage |
| **F9** | Stop / cancel camouflage paint |
| **MB5** (default) | Aimbot hold |
| **Enter** (in Rename field) | Queue manual rename |
| **G** (default) | Toggle survivor magnet (hunter) — rebind in EXPLOITS tab |

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
4. For environment camo: open **CAMOUFLAGE**, set quality and pass options, then **Paint Now** or **F10**.
5. For anti-kick: enable in **EXPLOITS** after spawning in; check `anti_kick.log` if kicked.
6. Use the **PLAYERS** tab to copy Steam IDs, manage your blocklist, or enable autokick.

Pre-built EXE: download the **Peterhack** artifact from [GitHub Actions](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack/actions) after a push to `main`, or build locally with PyInstaller (see `.github/workflows/build.yml`).

---

## Building the Bridge DLL

Requires **Visual Studio** with C++ tools.

```powershell
runtime/scripts/build.ps1
# Output: runtime/.build/bin/runtime-bridge.dll
# Copy to meccha_chameleon_tools/meccha-xenos-bridge.dll and C:\peterhack\camo\
```

**Bridge source (in repo):**

| Path | Role |
|---|---|
| `runtime/src/bridge.cpp` | TCP server, camo paint route, anti-kick vtable hooks, teleport/kill/rename/FOV |
| `runtime/src/injector.cpp` | DLL injector |
| `runtime/include/sdk.hpp` | UE5 struct layouts and offsets |
| `runtime/scripts/build.ps1` | MSVC build script |

Peterhack extends [SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools) with 6-pass dynamic camo, body-anchored scene capture, world-position UV color projection, vtable anti-kick, and game-thread rename.

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
| `failed to communicate with bridge DLL` | Run as Administrator; be in a match; check `latest.log` |
| Anti-kick enabled but still kicked | Check `anti_kick.log` for `BLOCKED` lines. EOS/platform kicks may bypass UE RPCs. Enable after spawn; quit game before DLL update. |
| Anti-kick crashes game on enable | Update to latest build (vtable hooks, not inline ProcessEvent). Fully quit game before replacing DLL. |
| Rename UI freezes when spamming | Update — renames use a background queue; UI stays responsive. |
| Rename stuck / reverts in lobby | Use **Rename** button; enable Debug Logging; ensure bridge connected. |
| Magnet does nothing | You must be **Hunter**; press **G** (or your bound key) to toggle ON. |
| Kill Selected / Kill All fails | Hunter only; host/session rules apply — check `[TRAINER:KILL]` in log. |
| `could not unload` / DLL stuck | **Quit the game completely** and relaunch. Run Peterhack as Administrator. |
| `missing bridge binaries` | Ensure `meccha-xenos-bridge.dll` + `meccha-xenos-injector.exe` are in `meccha_chameleon_tools/` |
| Bridge inject OK but paint fails on retry | Restart game — do not spam F10 after a failed reinject |
| Front pass blocked by floor/geometry | Latest build uses front noclip + body anchor; on flat maps try **Disable front pass** |
| Back pass paints wrong side | Update — front/back yaw swap fixed in latest build |
| Camo takes ~3–6 minutes | Normal for six passes at quality 12+ |
| Steam ID shows `—` in PLAYERS tab | Wait for replication; select row and Copy again |
| ESP feels laggy | Disable **Show Steam ID**; keep PLAYERS tab closed when not in use |

---

## Disclaimer

For educational purposes only. Use at your own risk.

---

## Credits

- **[Cursor AI Composer 2.5](https://cursor.com)** — AI-assisted development (features, bridge hooks, debugging, docs)
- **[SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools)** — Original bridge / camo foundation
- **MECCHA CHAMELEON** community — testing and feedback
