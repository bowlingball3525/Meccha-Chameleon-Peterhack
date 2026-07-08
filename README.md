<p align="center">
  <img src="peter.png" alt="Peterhack Logo" width="200"/>
</p>

# Meccha Chameleon — Peterhack

External ESP, aimbot, exploits, player tracking, and character-paint tools for **MECCHA CHAMELEON** (UE5.6 / `PenguinHotel-Win64-Shipping.exe`).

**Discord:** https://discord.gg/7T3damu79F

> **Built with AI:** This project is actively developed with **[Cursor AI Composer 2.5](https://cursor.com)** — an AI coding assistant used for feature implementation, bridge hook work, debugging, and documentation.

---

## Build Version

| | |
|---|---|
| **Version (Git)** | `e477680` |
| **Full SHA** | `e477680d25699834dc1547499225f5f0eff9cb67` |
| **Branch** | `main` |
| **Bridge DLL** | `bridge/meccha-xenos-bridge.dll` — 1,540,608 bytes |
| **Paint pipeline** | Official `mesh_first_paint` (SilentJMA v1.6+ route) |
| **Feature commit** | ESP/dead-filter/bridge fixes in `fff8094` |

The in-app **CHANGELOG** tab shows the same short SHA from the `VERSION` file. Auto-update pulls from [GitHub main](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack).

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

### Startup flow

1. **Wait for game** — `Peterhack.bat` polls for `PenguinHotel-Win64-Shipping.exe`, then attaches with `pymem` (Administrator required).
2. **Auto-inject bridge** — On connect, injects `bridge/meccha-xenos-bridge.dll` and waits for TCP on `127.0.0.1:47654`.
3. **Background threads** — ESP snapshot loop, exploits tick (~20 Hz), and optional auto-update run off the UI thread so the overlay stays smooth.
4. **Overlay** — Transparent PyQt5 window on top of the game draws ESP, radar, and aimbot FOV.

### Memory reading (ESP + aimbot)

| Layer | What it does |
|---|---|
| **Offset resolver** | Dumper-7 offsets for GWorld, GameState, PlayerArray, bones, health, `IsHunter`, Steam IDs, etc. |
| **Player discovery** | Primary: `AGameStateBase::PlayerArray` → `PawnPrivate` per `PlayerState`. Supplemental actor scan catches lobby mannequins and null/stale `PawnPrivate` (infection / replication lag). |
| **Sticky pawn cache** | Brief misses do not drop players; cache clears on disconnect, map change, match end, or large local teleport (lobby→match). |
| **Team filter** | Uses replicated **`IsHunter`** role byte (`pawn+0x0C3A`) — not pawn class names. |
| **Dead player filter** | Hides on ragdoll physics latch, elimination latch, dead class names (`Ragdoll`/`Corpse`/`Dead`), and local-only `bDead`. Does **not** guess from unreliable remote health reads. |
| **ESP snapshot thread** | Background thread builds paint snapshots (camera, positions, bones, clones) at 4–45 Hz depending on menu/heavy mode — decoupled from overlay FPS cap. |
| **Overlay paint** | UI thread only reads the latest snapshot and draws; no blocking game memory reads during paint. |

### Session modes

Peterhack classifies your local client (`in_match`, `freecam`, `spectating`, `unpossessed`, `lobby`, `dead`, etc.) from owned pawn, acknowledged pawn, view target, and net connection.

- **Freecam / spectate** — ESP auto-disables while active and restores your prior ESP toggle when you return to normal play.
- **Unpossessed** — Gun/decoy memory exploits pause until you spawn into a playable pawn; bridge camo/anti-kick can still run once in session.

### Bridge (camo + native exploits)

The injected DLL exposes a **TCP JSON API** on port **47654**. Python sends commands; the DLL runs game-thread work (paint, teleport, rename, anti-kick hooks, skeleton batch reads).

- **Environment camo** — `mesh_first_paint` pipeline with quality tuning 1–20 and `bridge/mesh-profiles/`.
- **Custom image paint** — Same `mesh_first_paint` route with a user RGBA buffer (wrap modes: projector / centered).
- **Anti-kick** — Vtable `ProcessEvent` hooks on local PlayerController / PlayerState / NetConnection; blocks kick and return-to-menu RPCs.
- **Skeleton ESP** — `get_skeleton` batch reads bone world positions when skeleton overlay is enabled.

### Config & logs

| Path | Purpose |
|---|---|
| `esp_config.json` | ESP, exploits, colors, hotkeys (next to `Peterhack.bat`) |
| `C:\peterhack\logs\latest.log` | Main session log |
| `C:\peterhack\logs\anti_kick.log` | Anti-kick hook status + kick/session RPC capture |
| `C:\peterhack\blocked_players.json` | Blocklist Steam64 IDs |

**Requirements:** Run as **Administrator**. Be **in a match** (not main menu only) before camo paint or most bridge exploits.

---

## Menu Tabs

| Tab | What it does |
|---|---|
| **VISUALS** | Dot/box/corner/skeleton ESP, **clone ESP**, snap lines, **OOF arrows**, names, Steam ID, distance, health/shield bars, **team filter** (IsHunter), enemy-only, visible colors |
| **COLORS** | Per-team, skeleton, clone, and visibility colors |
| **PLAYERS** | Session player table, blocklist, copy/save Steam64, kill selected survivor, autokick |
| **RADAR** | Top-down mini-map |
| **AIMBOT** | Hold-key aim (default MB5), bone target, FOV circle, smoothing, visible-only |
| **EXPLOITS** | Memory toggles, hunter/survivor exploits, magnet, god mode, anti-kick, rename, teleport/kill |
| **CAMOUFLAGE** | `mesh_first_paint` environment camo, quality 1–20, pass options, custom image paint |
| **MISC** | Menu language (9 languages), overlay FPS slider (1–165) |
| **CHANGELOG** | Version SHA + auto-update toggle |
| **DEBUGGING** | Per-category log filters for `latest.log` |

Bottom bar: **Save Config**, **ESP on/off**, **Close**, **Discord**.

---

## ESP (detailed)

### On-screen

| Element | Behavior |
|---|---|
| **Dot ESP** | Chest-height world point (bone map or root+offset), distance scaling |
| **2D / corner box** | Projected from root + height |
| **Skeleton** | Chameleon bone indices; bridge `get_skeleton` batch when available, memory fallback throttled |
| **Labels** | Name, Steam64, distance, `[H]`/`[S]` role tags |
| **Health / shield bars** | Resolved per-class health offsets when replicated |
| **Snap lines** | Screen bottom-center → player |
| **Team colors** | Hunter red, survivor green, blocklist orange, enemy-only visible/occluded colors |

### Off-screen

When a player is behind you or outside the view frustum:

- **Only the dot** is drawn — at the screen edge (OOF indicator position).
- Boxes, skeleton, labels, snap lines, and arrows are **skipped** for that player.
- OOF arrow sub-settings apply to the edge dot placement radius.

### Clone ESP

Paint decoys (`BP_*Decoy*` actors) show as **`Username (clone)`** with owner team colors. Scanned from player cache decoy fields + periodic level actor scan.

### Performance modes

| State | Snapshot rate | Notes |
|---|---|---|
| Menu open | ~20 Hz loop, 4 Hz full snap | Overlay capped ~30 FPS |
| Normal match | ~45 Hz loop, ~22 Hz full snap | Overlay up to 165 FPS |
| Heavy (skeleton + clones) | ~35 Hz loop, throttled bones | Extra bridge skeleton batch |
| Freecam / spectate | ~4 Hz relaxed | ESP auto-off; minimal reads |

---

## Aimbot

Hold **MB5** (rebindable) to aim at the closest enemy inside the FOV circle. Writes `ControlRotation` on the local PlayerController. Optional visible-only filter uses cached line-of-sight checks (~200 ms per actor).

---

## Exploits (EXPLOITS tab)

Memory writes on the exploits tick when toggled (~20 Hz, paused when `unpossessed`):

| Toggle | Effect |
|---|---|
| **No Gun Cooldown** | Hunter gun cooldown → 0 |
| **No Recoil** | Camera shake modifier → 0 |
| **No Decoy Cooldown** | Survivor decoy slots forced ready |
| **Anti Detection** | Clears `OverlapCheckCapsules` (Too Buried) |
| **God Mode (Survivor)** | Bridge blocks damage/death RPCs + scrubs local dead flags |
| **Infinite Bullets** | `InfinityBullet` flag (hunter) |
| **Magnet** | **G** toggle — pulls survivors along view (hunter) |
| **Anti-Clipping** | Disables body mesh + capsule collision |
| **Set Decoy Num** | Max decoy spawn count |
| **Anti-Kick** | Bridge vtable hooks (see below) |
| **Auto-Rename / Rename** | Bridge `set_player_name` on game thread |
| **Return to Main Lobby** | Temporarily disables anti-kick, then calls return-to-menu RPC |
| **Teleport / Kill Self** | Bridge `teleport` / `kill` |

### Anti-Kick

Runs **inside the bridge DLL**, not from Python patches.

| Item | Detail |
|---|---|
| **Hook method** | Vtable `ProcessEvent` on local PlayerController, PlayerState, NetConnection |
| **Blocked RPCs** | `ClientWasKicked`, `ClientReturnToMainMenuWithTextReason`, `Kick`, disconnect closes |
| **Log file** | `anti_kick.log` — `[capture-session]` / `[capture-kick]` for diagnostics; not every `ReceiveTick` |
| **Limitation** | EOS/Redpoint platform kicks may still drop the socket below UE RPC layer |

Enable after joining a match. Fully quit the game before replacing `meccha-xenos-bridge.dll`.

---

## Bridge TCP commands (`:47654`)

| Command | Description |
|---|---|
| `ping` / `capabilities` | Health check and command list |
| `paint_full_route` | Environment camo (`mesh_first_paint`) |
| `paint_replication_probe` / pressure probes | Camo replication diagnostics |
| `cancel_paint` | Stop active paint queue |
| `rotate` | Camera or pawn rotation |
| `teleport` / `kill` | Move or destroy local pawn |
| `set_fov` | Camera FOV override |
| `set_anti_kick` | Enable/disable vtable anti-kick |
| `get_anti_kick_log` | Fetch kick log lines |
| `set_player_name` | Replicated rename |
| `get_player_steam_id` | Steam64 lookup via bridge |
| `get_skeleton` | Batch bone world positions for ESP |
| `set_god_mode` / `magnet_tick` | Survivor god mode / hunter magnet helpers |
| `kill_survivor` / `kill_all_survivors` | Host kill RPCs |
| `shutdown` | Stop bridge TCP server |

Shipped binaries live in **`bridge/`** (DLL, injector, **`mesh-profiles/`**).

---

## Camouflage

### Environment camo (`mesh_first_paint`)

**Paint Now** or **F10** starts the official mesh-first paint pipeline over TCP.

| Control | Description |
|---|---|
| **Quality 1–20** | Stroke density / sharpness (20 = extreme, slower) |
| **Disable front pass** | Flat maps only |
| **Back pass only** | Spine/rear orbit |
| **Stop (F9)** | `cancel_paint` |

Default orbit: front → left → right → back → head/shoulders → inner legs.

### Custom character image

PNG/JPG applied via the **same `mesh_first_paint` route** as environment camo:

| Wrap mode | Description |
|---|---|
| **Projector** | Front → back across atlas |
| **Centered** | Chest center; top→head, bottom→feet |

Image quality 1–5, opacity slider, UV test presets, save/load.

---

## Hotkeys

| Key | Action |
|---|---|
| **Insert** / **F1** | Toggle menu + ESP overlay |
| **F10** | Apply environment camouflage |
| **F9** | Stop / cancel camouflage |
| **MB5** (default) | Aimbot hold |
| **G** (default) | Toggle survivor magnet (hunter) |
| **Enter** (rename field) | Queue manual rename |

Drag the menu title bar to reposition.

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

1. Launch **MECCHA CHAMELEON** and join a match (or lobby with characters visible).
2. Run **`Peterhack.bat`** (self-elevates to Administrator).
3. Configure ESP/exploits in the menu; toggle **ESP** in the bottom bar.
4. **CAMOUFLAGE** — set quality, **Paint Now** or **F10**.
5. **EXPLOITS** — enable anti-kick after spawn; check `anti_kick.log` if disconnected.

**Pre-built EXE:** download the **Peterhack** artifact from [GitHub Actions](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack/actions) after a push to `main` (build `e477680` or newer).

---

## Building the Bridge DLL

Requires **Visual Studio** with C++ tools.

```powershell
runtime/scripts/build.ps1
# Output: bridge/meccha-xenos-bridge.dll (+ injector, mesh-profiles)
```

| Path | Role |
|---|---|
| `runtime/src/bridge.cpp` | TCP server, camo, anti-kick, teleport/kill/rename |
| `runtime/src/bridge_peterhack.inc` | Peterhack extensions (anti-kick capture, image paint) |
| `bridge/` | Shipped DLL + **mesh-profiles/** (committed to Git) |

Peterhack extends [SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools) with mesh-first camo, vtable anti-kick, and external ESP/exploits.

---

## Auto-update

On launch, Peterhack can check GitHub `main` and apply updates, then restart.

- **ZIP users** — files merged in place; `esp_config.json` and paint presets preserved.
- **Git clone users** — `git fetch` + `git reset --hard` when Git is installed.
- Disable in **CHANGELOG** tab, or: `python -m meccha_chameleon_tools --no-update`

---

## Troubleshooting

| Symptom | What to try |
|---|---|
| `failed to communicate with bridge DLL` | Run as Admin; be in a match; check `latest.log` |
| `mesh_profile_missing` | Ensure `bridge/mesh-profiles/` exists next to the DLL |
| ESP shows only clones | Update to `e477680`+ (features from `fff8094`) |
| ESP missing players | Turn off team filter to test; check `[ESP] discovery` in log with debug ESP logging |
| ESP shows dead bodies | Ragdoll latch should hide corpses; report if they linger after elimination |
| `RecursionError` in log | Fixed in `fff8094` — pull `e477680`+ and restart |
| Anti-kick log is huge | `[capture-session]` lines are diagnostics, not errors; bridge filters noise in latest build |
| Anti-kick enabled but kicked | Check `anti_kick.log` for `[capture-kick]`; EOS may bypass UE RPCs |
| Magnet does nothing | Must be hunter; press **G** to toggle ON |
| Bridge inject OK but no TCP | Use committed `bridge/meccha-xenos-bridge.dll`; quit game before DLL swap |

---

## Disclaimer

For educational purposes only. Use at your own risk.

---

## Credits

- **[Cursor AI Composer 2.5](https://cursor.com)** — AI-assisted development
- **[SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools)** — Original bridge / mesh-first camo foundation
- **MECCHA CHAMELEON** community — testing and feedback
