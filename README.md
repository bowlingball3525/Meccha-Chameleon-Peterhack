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
| **Version (Git)** | `3355c5c` |
| **Full SHA** | `3355c5c4a32b682cb1f2e6b7844f2a0143f164fa` |
| **Branch** | `main` |
| **Bridge DLL** | `bridge/meccha-xenos-bridge.dll` — 1,545,216 bytes |
| **Paint pipeline** | Official `mesh_first_paint` (SilentJMA v1.6+ route) |
| **Feature commit** | Camo texture sync for other players + magnet master switch |

The in-app **CHANGELOG** tab shows the same short SHA from the `VERSION` file. Auto-update pulls from [GitHub main](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack). After `git pull`, `VERSION` should match the table below.

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
| **Dead player filter** | Permanent elimination only on strong signals — dead class names (`Ragdoll`/`Corpse`/`Dead`) and local-only `bDead`. Ragdoll **physics** is a *recoverable* hide: a corpse ragdolls continuously (stays hidden), a knocked-down survivor stops and reappears. A knockdown/stun no longer erases a live player for the match. Never guesses from unreliable remote health reads. |
| **ESP snapshot thread** | Background thread builds paint snapshots (camera, positions, bones, clones) at 4–45 Hz depending on menu/heavy mode — decoupled from overlay FPS cap. |
| **Overlay paint** | UI thread only reads the latest snapshot and draws; no blocking game memory reads during paint. |

### Session modes

Peterhack classifies your local client (`in_match`, `freecam`, `spectating`, `unpossessed`, `lobby`, `dead`, etc.) from owned pawn, acknowledged pawn, view target, and net connection.

- **Freecam / spectate** — ESP auto-disables while active and restores your prior ESP toggle when you return to normal play.
- **Unpossessed** — Gun/decoy memory exploits pause until you spawn into a playable pawn; bridge camo/anti-kick can still run once in session.

### Bridge (camo + native exploits)

The injected DLL exposes a **TCP JSON API** on port **47654**. Python sends commands; the DLL runs game-thread work (paint, teleport, rename, decoy count, anti-kick hooks, skeleton batch reads).

- **Environment camo** — `mesh_first_paint` pipeline with quality tuning 1–20 and `bridge/mesh-profiles/`.
- **Camo replication** — Local **visual sync** paints you fully on your screen immediately. Stroke batches replicate to the server (50 strokes/batch, ~50 ms pacing). After all batches finish, the bridge calls **`RequestFullTextureSync`** / **`ServerRequestTextureSync`** so other players receive the full texture — not just the slow stroke trickle.
- **Custom image paint** — Same `mesh_first_paint` route with a user RGBA buffer (wrap modes: projector / centered).
- **Anti-kick** — Vtable `ProcessEvent` hooks on local PlayerController / PlayerState / NetConnection; blocks kick and return-to-menu RPCs.
- **Skeleton ESP** — `get_skeleton` batch reads bone world positions when skeleton overlay is enabled.

#### Why RPCs go through the bridge

Any Unreal `ProcessEvent` call (RPCs like rename, kill, god mode, decoy count, teleport) **must run on the game thread**. Calling them from an external thread races the engine — during a map/world transition it can hit an object mid-teardown and crash the game with *"Pure virtual function being called."* So every RPC exploit is queued to the DLL, which drains it on the game thread inside its `ProcessEvent` hook. Pure memory-poke exploits (gun cooldown, recoil, decoy cooldown, anti-detection, infinite bullets, anti-clipping) stay in Python — they never call a virtual function and are safe.

#### Object-liveness validation

Pointers Python caches (players, meshes, components) can go stale across a lobby→match transition. Before the bridge invokes `ProcessEvent` on any object it validates the pointer against **`GUObjectArray`**: it confirms the object still occupies its own array slot and is not flagged `Garbage`/`Unreachable`. Stale pointers are rejected (the call safely no-ops) instead of crashing the game. Python mirrors the same check plus a short post-transition cooldown.

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

Applied on the exploits tick when toggled (~20 Hz, paused when `unpossessed`). Pure memory pokes run in Python; anything that calls a game function (RPC) is dispatched to the bridge and runs on the game thread.

| Toggle | Type | Effect |
|---|---|---|
| **No Gun Cooldown** | memory | Hunter gun cooldown → 0 |
| **No Recoil** | memory | Camera shake modifier → 0 |
| **No Decoy Cooldown** | memory | Survivor decoy slots forced ready |
| **Anti Detection** | memory | Clears `OverlapCheckCapsules` (Too Buried) |
| **Infinite Bullets** | memory | `InfinityBullet` flag (hunter) |
| **Anti-Clipping** | memory | Disables body mesh + capsule collision |
| **God Mode (Survivor)** | bridge | Blocks damage/death RPCs + scrubs local dead flags |
| **Magnet** | bridge | **Enable Magnet (Hunter)** master switch (default OFF) + **G** toggle / UI button — pulls survivors along view (hunter), `magnet_tick` |
| **Set Decoy Num** | bridge | Max decoy spawn count via `set_decoy_num` (game-thread RPC) |
| **Anti-Kick** | bridge | Vtable hooks (see below) |
| **Auto-Rename / Rename** | bridge | `set_player_name` on game thread |
| **Return to Main Lobby** | bridge | Temporarily disables anti-kick, then calls return-to-menu RPC |
| **Teleport / Kill Self** | bridge | `teleport` / `kill` |

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
| `set_decoy_num` | Set max decoy spawn count (game-thread RPC) |
| `kill_survivor` / `kill_all_survivors` | Host kill RPCs |
| `shutdown` | Stop bridge TCP server |

Shipped binaries live in **`bridge/`** (DLL, injector, **`mesh-profiles/`**).

---

## Camouflage

### Environment camo (`mesh_first_paint`)

**Paint Now** or **F10** starts the official mesh-first paint pipeline over TCP.

After paint completes, check `latest.log` for `[CAMO] server replication X.Xs texture_sync=ok` — `texture_sync=ok` means other players should see your full camo (not just partial stroke replication).

| Control | Description |
|---|---|
| **Quality 1–20** | Stroke density / sharpness (20 = extreme, slower) |
| **Disable front pass** | Flat maps only |
| **Back pass only** | Spine/rear orbit |
| **Stop (F9)** | `cancel_paint` |

**Replication timing:** At quality 12, expect ~15–25 s for server stroke batches plus texture sync. You look fully painted locally right away; others catch up once batches + sync finish. Painting after the match starts (not lobby-only) gives the most reliable visibility to other players.

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
| **G** (default) | Toggle survivor magnet (hunter) — requires **Enable Magnet** in EXPLOITS |
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
5. **EXPLOITS** — check **Enable Magnet (Hunter)** before using **G**; enable anti-kick after spawn; check `anti_kick.log` if disconnected.

**Pre-built EXE:** download the **Peterhack** artifact from [GitHub Actions](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack/actions) after a push to `main` (build `3355c5c` or newer).

---

## Building the Bridge DLL

Requires **Visual Studio** with C++ tools.

```powershell
runtime/scripts/build.ps1
# Output: bridge/meccha-xenos-bridge.dll (+ injector, mesh-profiles)
```

| Path | Role |
|---|---|
| `runtime/src/bridge.cpp` | TCP server, camo, anti-kick, teleport/kill/rename, `GUObjectArray` object-liveness guard |
| `runtime/src/bridge_peterhack.inc` | Peterhack extensions (anti-kick capture, image paint, skeleton, `set_decoy_num`) |
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
| ESP shows only clones | Pull latest `main` (ESP discovery fixes from `fff8094`+) |
| ESP missing players | Turn off team filter to test; check `[ESP] discovery` in log with debug ESP logging |
| ESP forgets / loses survivors | Fixed — ragdoll physics is now a recoverable hide, not a match-long latch; knocked-down survivors reappear when they get up |
| ESP shows dead bodies | Corpses ragdoll continuously and stay hidden; a genuine corpse may briefly show for ~1.25 s before hiding |
| Game crashes — *"Pure virtual function being called"* | Fixed — exploit RPCs run on the game thread via the bridge and validate objects against `GUObjectArray`; rebuild/replace `bridge/meccha-xenos-bridge.dll` |
| Camo looks full for me but not others | Fixed — bridge now runs server texture sync after stroke batches; pull latest + replace DLL; wait for `texture_sync=ok` in log |
| Camo others see partial paint until hunter spawns | Same fix — old builds skipped `RequestFullTextureSync` after batches; update bridge DLL |
| `RecursionError` in log | Fixed in `fff8094` — pull latest and restart |
| Anti-kick log is huge | `[capture-session]` lines are diagnostics, not errors; bridge filters noise in latest build |
| Anti-kick enabled but kicked | Check `anti_kick.log` for `[capture-kick]`; EOS may bypass UE RPCs |
| Magnet does nothing | Check **Enable Magnet (Hunter)** in EXPLOITS; must be hunter; press **G** or use Magnet button to toggle ON |
| Bridge inject OK but no TCP | Use committed `bridge/meccha-xenos-bridge.dll`; quit game before DLL swap |

---

## Disclaimer

For educational purposes only. Use at your own risk.

---

## Credits

- **[Cursor AI Composer 2.5](https://cursor.com)** — AI-assisted development
- **[SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools)** — Original bridge / mesh-first camo foundation
- **MECCHA CHAMELEON** community — testing and feedback
