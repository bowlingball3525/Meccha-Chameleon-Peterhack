<p align="center">
  <img src="peter.png" alt="Peterhack Logo" width="200"/>
</p>

# Meccha Chameleon — Peterhack

External ESP, aimbot, exploits, and character-paint tools for **MECCHA CHAMELEON** (UE5.6 / `PenguinHotel-Win64-Shipping.exe`).

**Discord:** https://discord.gg/7T3damu79F

> **Built with AI:** This project is actively developed with **[Cursor AI Composer 2.5](https://cursor.com)** — an AI coding assistant used for feature implementation, bridge hook work, debugging, and documentation.

---

## Build Version

| | |
|---|---|
| **Version (Git)** | See `VERSION` file / in-app **CHANGELOG** tab |
| **Bridge DLL** | `bridge/meccha-xenos-bridge.dll` (shipped in repo) |
| **Paint pipeline** | Official `mesh_first_paint` (MecchaCamouflage v1.6+ route) |
| **Peer camo sync** | Stroke replication only (official beta.5 behavior — no post-paint texture sync on the normal path) |

The in-app **CHANGELOG** tab shows the short SHA from the `VERSION` file. Auto-update pulls from [GitHub main](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack).

**Bridge binaries must live in `bridge/`:**

| File | Purpose |
|---|---|
| `bridge/meccha-xenos-bridge.dll` | Injected game-thread bridge (TCP `:47654`) |
| `bridge/meccha-xenos-injector.exe` | DLL injector used by Peterhack |
| `bridge/mesh-profiles/` | Required JSON profiles for `mesh_first_paint` |

Rebuild with `runtime/scripts/build.ps1` — it copies `runtime-bridge.dll` → `bridge/meccha-xenos-bridge.dll`. **Quit the game** before rebuilding if the DLL is locked.

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
3. **Background threads** — ESP snapshot loop, exploits tick (~20 Hz), and optional auto-update run off the UI thread.
4. **Overlay** — Transparent PyQt5 window draws ESP, radar, and aimbot FOV.

### Memory reading (ESP + aimbot)

| Layer | What it does |
|---|---|
| **Offset resolver** | Dumper-7 offsets for GWorld, GameState, PlayerArray, bones, health, `IsHunter`, Steam IDs, etc. |
| **Player discovery** | Primary: `AGameStateBase::PlayerArray` → `PawnPrivate` per `PlayerState`. Supplemental actor scan for lobby mannequins and stale `PawnPrivate`. |
| **Sticky pawn cache** | Brief misses do not drop players; cache clears on disconnect, map change, match end, or large local teleport. |
| **Dead player filter** | Hides corpses via ragdoll latch + replicated `LiveSurvivors_PlayerState` roster. Knockdowns recover when physics clears. Local pawn is never hidden by remote ESP heuristics (fixes exploits pausing after knockdown). Cached ragdoll reads keep ESP smooth (~45 Hz). |
| **Team filter** | Replicated **`IsHunter`** byte (`pawn+0x0C3A`) — not pawn class names. |
| **ESP snapshot thread** | Background snapshots (camera, positions, bones, clones) at 4–45 Hz depending on menu/heavy mode. |

### Session modes

Peterhack classifies your client (`in_match`, `freecam`, `spectating`, `unpossessed`, `lobby`, `dead`, etc.) from owned pawn, acknowledged pawn, view target, and net connection.

- **Freecam / spectate** — ESP stays on with relaxed read rates; gun/decoy memory exploits pause in freecam/spectate only.
- **Unpossessed** — Gun/decoy exploits pause until you spawn into a playable pawn; bridge camo/anti-kick can still run.

### Bridge (camo + native exploits)

The injected DLL exposes a **TCP JSON API** on port **47654**. Python sends commands; the DLL runs game-thread work (paint, teleport, rename, decoy count, anti-kick hooks, skeleton batch reads).

- **Environment camo** — `mesh_first_paint` pipeline with quality **1–20** and `bridge/mesh-profiles/`.
- **Camo replication** — **Decoupled server vs local paint:** your screen uses fixed official pacing (20 strokes/batch, 75 ms). Peer replication uses the **Server sync speed** slider (20–50 strokes/batch, 25–100 ms). Normal paint ends after stroke batches (**no** `RequestFullTextureSync` — matches official beta.5). Other players see camo build via stroke replication.
- **Custom image paint** — Same `mesh_first_paint` route with user RGBA buffer (projector / centered wrap).
- **Anti-kick** — Vtable `ProcessEvent` hooks on local PlayerController / PlayerState / NetConnection.
- **Skeleton ESP** — `get_skeleton` batch reads when skeleton overlay is enabled.
- **Exploit responsiveness** — Bridge drains exploit commands (`set_decoy_num`, `magnet_tick`, anti-kick, etc.) on every paint batch tick so long camo jobs do not block them.

#### Why RPCs go through the bridge

Unreal `ProcessEvent` **must run on the game thread**. External-thread RPCs during map transitions can crash with *"Pure virtual function being called."* RPC exploits (rename, kill, god mode, decoy count, teleport) queue to the DLL. Pure memory pokes (gun cooldown, recoil, decoy CD, anti-detection, infinite bullets, anti-clipping) stay in Python.

#### Object-liveness validation

Before `ProcessEvent`, the bridge validates pointers against **`GUObjectArray`** (not garbage/unreachable). Python mirrors this plus a short post-teleport RPC cooldown.

### Config & logs

| Path | Purpose |
|---|---|
| `esp_config.json` | ESP, exploits, colors, hotkeys (next to `Peterhack.bat`) |
| `C:\peterhack\logs\latest.log` | Main session log |
| `C:\peterhack\logs\anti_kick.log` | Anti-kick hook status + kick/session RPC capture |
| `C:\peterhack\blocked_players.json` | Blocklist Steam64 IDs (still used by autokick if enabled in config — no menu UI) |

**Requirements:** Run as **Administrator**. Be **in a match** (not main menu only) before camo paint or most bridge exploits.

---

## Menu Tabs

| Tab | What it does |
|---|---|
| **VISUALS** | ESP toggles (dot/box/corner/skeleton/clone), snap lines, OOF arrows, names, Steam ID, distance, health/shield, team filter, enemy-only, **radar** (enabled/size/range) |
| **COLORS** | Per-team, skeleton, clone, and visibility colors |
| **AIMBOT** | Hold-key aim, bone target, FOV circle, smoothing, visible-only |
| **EXPLOITS** | Memory toggles, magnet, god mode, anti-kick, rename, teleport/kill self, **kill all survivors** |
| **CAMOUFLAGE** | Environment camo quality 1–20, **server sync speed** slider, Paint Now / Stop |
| **MISC** | Menu language (9 languages), overlay FPS / VSync |
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
| **Skeleton** | Chameleon bone indices; bridge `get_skeleton` batch when available |
| **Labels** | Name, Steam64, distance, `[H]`/`[S]` role tags |
| **Health / shield bars** | Per-class health offsets when replicated |
| **Snap lines** | Screen bottom-center → player |
| **Radar** | Top-down mini-map (size/range on **VISUALS** tab) |

### Off-screen (OOF)

Edge dot only — boxes/skeleton/labels skipped. OOF sub-settings control radius and optional name/distance/health on the indicator.

### Clone ESP

Decoys (`BP_*Decoy*`) show as **`Username (clone)`** with owner team colors.

### Performance modes

| State | Snapshot rate | Notes |
|---|---|---|
| Menu open | ~20 Hz loop, 4 Hz full snap | Overlay capped ~30 FPS |
| Normal match | ~45 Hz loop, ~22 Hz full snap | Overlay up to 165 FPS |
| Heavy (skeleton + clones) | ~35 Hz loop, throttled bones | Extra bridge skeleton batch |
| Freecam / spectate | Relaxed reads | ESP stays on at lower rate |

---

## Aimbot

Hold **MB5** (rebindable) to aim at the closest enemy inside the FOV circle. Writes `ControlRotation` on the local PlayerController. Optional visible-only filter (~200 ms LOS cache per actor).

---

## Exploits (EXPLOITS tab)

Applied on the exploits tick when toggled (~20 Hz). Paused when `unpossessed` or no playable local pawn.

| Toggle | Type | Effect |
|---|---|---|
| **No Gun Cooldown** | memory | Hunter gun cooldown → 0 |
| **No Recoil** | memory | Camera shake modifier → 0 |
| **No Decoy Cooldown** | memory | Survivor decoy slots forced ready |
| **Anti Detection** | memory | Clears `OverlapCheckCapsules` (Too Buried) |
| **Infinite Bullets** | memory | `InfinityBullet` flag (hunter) |
| **Anti-Clipping** | memory | Disables body mesh + capsule collision |
| **God Mode (Survivor)** | bridge | Blocks damage/death RPCs + scrubs local dead flags |
| **Magnet** | bridge | **Enable Magnet** master switch + **G** toggle — `magnet_tick` (hunter) |
| **Set Decoy Num** | bridge | Max decoy count via `set_decoy_num` |
| **Anti-Kick** | bridge | Vtable hooks (see below) |
| **Rename** | bridge | `set_player_name` on game thread |
| **Return to Main Lobby** | bridge | Disables anti-kick briefly, return-to-menu RPC |
| **Teleport / Kill Self** | bridge | `teleport` / `kill` |
| **Kill All Survivors** | bridge | Hunter batch kill via `kill_all_survivors` |

### Anti-Kick

| Item | Detail |
|---|---|
| **Hook method** | Vtable `ProcessEvent` on local PlayerController, PlayerState, NetConnection |
| **Blocked RPCs** | `ClientWasKicked`, `ClientReturnToMainMenuWithTextReason`, `Kick`, disconnect closes |
| **Log file** | `C:\peterhack\logs\anti_kick.log` |
| **Limitation** | EOS/Redpoint platform kicks may still drop the socket |

Enable after joining a match. **Quit the game** before replacing `meccha-xenos-bridge.dll`.

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
| `set_god_mode` / `magnet_tick` | Survivor god mode / hunter magnet |
| `set_decoy_num` | Max decoy spawn count (game-thread RPC) |
| `kill_survivor` / `kill_all_survivors` | Host kill RPCs |
| `shutdown` | Stop bridge TCP server |

---

## Camouflage

### Environment camo (`mesh_first_paint`)

**Paint Now** or **F10** starts the mesh-first paint pipeline over TCP. **F9** / **Stop** sends `cancel_paint`.

| Control | Description |
|---|---|
| **Camo quality 1–20** | Stroke density / sharpness (20 = slowest, sharpest) |
| **Server sync speed 1–10** | How fast **other players** receive stroke batches only — does **not** change local paint speed on your screen |
| | 1 = slow (20/batch, 100 ms) · 6 = balanced · 10 = fast (50/batch, 25 ms) |

Default orbit: front → left → right → back → head/shoulders → inner legs (automatic from pose).

**Peer visibility:** Other clients see camo appear gradually via replicated strokes (official beta.5 style). There is no automatic full-texture sync after paint on the normal path.

### Custom character image

PNG/JPG via the same `mesh_first_paint` route — **Projector** or **Centered** wrap, quality 1–5, opacity slider, UV test presets.

---

## Hotkeys

| Key | Action |
|---|---|
| **Insert** / **F1** | Toggle menu + ESP overlay |
| **F10** | Apply environment camouflage |
| **F9** | Stop / cancel camouflage |
| **MB5** (default) | Aimbot hold |
| **G** (default) | Toggle survivor magnet (hunter) — requires **Enable Magnet** |
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

1. Launch **MECCHA CHAMELEON** and join a match.
2. Run **`Peterhack.bat`** (self-elevates to Administrator).
3. Configure ESP on **VISUALS** (including radar); toggle **ESP** in the bottom bar.
4. **CAMOUFLAGE** — set quality + server sync speed, **Paint Now** or **F10**.
5. **EXPLOITS** — enable **Enable Magnet** before **G**; enable anti-kick after spawn.

**Pre-built EXE:** download the **Peterhack** artifact from [GitHub Actions](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack/actions) after a push to `main`.

---

## Building the Bridge DLL

Requires **Visual Studio** with C++ tools.

```powershell
runtime/scripts/build.ps1
# Deploys to bridge/meccha-xenos-bridge.dll + meccha-xenos-injector.exe + mesh-profiles/
```

| Path | Role |
|---|---|
| `runtime/src/bridge.cpp` | TCP server, camo, anti-kick, teleport/kill/rename, object-liveness guard |
| `runtime/src/bridge_peterhack.inc` | Peterhack extensions (anti-kick, skeleton, `set_decoy_num`, magnet) |
| `bridge/` | **Shipped** DLL + injector + **mesh-profiles/** (committed to Git) |

Peterhack extends [SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools) with mesh-first camo, vtable anti-kick, and external ESP/exploits.

---

## Auto-update

On launch, Peterhack can check GitHub `main` and apply updates, then restart.

- **ZIP users** — files merged in place; `esp_config.json` preserved.
- **Git clone users** — `git fetch` + `git reset --hard` when Git is installed.
- Disable in **CHANGELOG** tab, or: `python -m meccha_chameleon_tools --no-update`

---

## Troubleshooting

| Symptom | What to try |
|---|---|
| `failed to communicate with bridge DLL` | Run as Admin; be in a match; check `latest.log` |
| `mesh_profile_missing` | Ensure `bridge/mesh-profiles/` exists next to the DLL |
| Bridge build: DLL copy failed | Quit the game (DLL loaded in process), then re-run `build.ps1` |
| ESP laggy / low FPS | Pull latest — dead-filter ragdoll reads are cached; position refresh uses fast path |
| ESP shows dead bodies briefly | Corpses latch after ~1.25 s continuous ragdoll; roster removal hides eliminated survivors |
| Exploits do nothing in match | Ensure you have a playable pawn (not lobby/unpossessed); pull latest local-pawn fix |
| Exploits dead during camo paint | Pull latest bridge — exploit commands drain during paint batches |
| Game crashes — *Pure virtual function* | Rebuild bridge; RPCs must go through DLL on game thread |
| Peers see partial/slow camo | Expected with stroke-only sync — raise **Server sync speed**; wait for batches to finish |
| Magnet does nothing | **Enable Magnet** + hunter role + press **G** or UI toggle |
| Anti-kick enabled but kicked | Check `anti_kick.log`; EOS may bypass UE RPCs |

---

## Disclaimer

For educational purposes only. Use at your own risk.

---

## Credits

- **[Cursor AI Composer 2.5](https://cursor.com)** — AI-assisted development
- **[SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools)** — Original bridge / mesh-first camo foundation
- **MECCHA CHAMELEON** community — testing and feedback
