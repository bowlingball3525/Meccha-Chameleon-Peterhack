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
4. **Write memory (exploits)** — Exploits apply small targeted writes (cooldowns, recoil, collision flags) at ~20 Hz when toggled on.
5. **Bridge (camo + exploits)** — Auto-injects **`bridge/meccha-xenos-bridge.dll`** and talks over **localhost TCP port 47654**. Used for environment camo, teleport, kill, rename, anti-kick hooks, and survivor god mode.
6. **Custom image paint** — PNG/JPG skins use Peterhack’s own remote-call path (`ImportChannel` / UV stamping), separate from bridge camouflage.

**Requirements:** Run as **Administrator** (`Peterhack.bat` self-elevates). Be **in a match** (not the main menu) before applying camo or bridge exploits.

**Logs:** `C:\peterhack\logs\latest.log`  
**Anti-kick log:** `C:\peterhack\logs\anti_kick.log`  
**Blocklist:** `C:\peterhack\blocked_players.json`

---

## Menu Tabs

| Tab | What it does |
|---|---|
| **VISUALS** | ESP dots, 2D/corner boxes, skeleton, **clone ESP**, snap lines, **OOF arrows** (toggle + sub-settings), names, Steam ID, distance, health/shield bars, team filter, enemy-only, visible colors |
| **COLORS** | Per-team, skeleton, clone, and visibility colors |
| **PLAYERS (WIP)** | Session player table, blocklist, copy/save Steam64 IDs, autokick — *player list still being improved* |
| **RADAR** | Top-down mini-map of players |
| **AIMBOT** | Hold key aim (default MB5), bone dropdown, FOV circle, smoothing, visible-only option |
| **EXPLOITS** | Memory toggles, hunter/survivor exploits, magnet, god mode, anti-kick, rename, teleport/kill, **Kill All Survivors (WIP)** |
| **CAMOUFLAGE** | Dynamic 360° environment camo, quality slider (1–20), pass options, custom image paint, UV diagnostic |
| **MISC** | **Menu language** (9 languages), **overlay FPS** slider (1–100) |
| **CHANGELOG** | Version info and auto-update toggle |
| **DEBUGGING** | Per-category console / file log filters (`latest.log`) |

Bottom bar: **Save Config**, **ESP on/off**, **Close**, **Discord**.

---

## Features

### ESP Overlay

| Feature | How it works |
|---|---|
| **Dot / Box / Corner Box / Skeleton** | Reads bone positions from the skeletal mesh, projects world → screen. Dot ESP anchors at **chest**; skeleton uses Chameleon bone indices via bridge batch when available. |
| **Clone ESP** | Shows paint decoy copies with a dot and label **`Username (clone)`**; uses owner team colors. |
| **Snap lines** | Line from screen bottom-center to each player’s projected position. |
| **OOF arrows** | Master toggle — when on, off-screen players get an edge arrow (configurable radius) with optional name, distance, and health. Sub-settings collapse when off to save menu space. |
| **Names / Steam64 / Distance / Roles** | Name from replicated player state; Steam ID on demand; distance from local pawn; `[H]`/`[S]` role tags. |
| **Health / Shield bars** | Reads replicated health/shield floats and draws bars above the player. |
| **Team colors** | Hunter (red), Survivor (green), local, fallback enemy color. |
| **Blocklist highlight** | Blocklisted Steam IDs shown in orange. |
| **Enemy Only + Visible colors** | Optional enemies-only draw; green/purple for visible vs occluded when enabled. |
| **Distance scaling** | Dot radius scales with distance. |
| **Dead player filter** | Drops eliminated players when the `Dead` flag or resolved health confirms death. |
| **Infection / replication** | Sticky pawn cache + supplemental actor scan when `PlayerArray` entries have null `PawnPrivate` (common in infection mode). |
| **Debounced cache** | Brief player-list cache reduces ESP flicker without keeping corpses on screen. |

### Players Tab (WIP)

| Feature | Status |
|---|---|
| **Session player table** | **WIP** — list refresh and Steam ID resolution still being improved (especially infection / late join). |
| **Copy Steam ID / Save to Blocklist** | Copy selected row’s Steam64; append to `blocked_players.json`. |
| **Kill Selected Survivor** | Hunter only — `KillPlayer` on selected survivor (host/session rules apply). |
| **Auto-Kick** | **Host:** Redpoint kick for blocklisted Steam IDs. **Non-host:** optional leave lobby when a blocked player joins. |
| **Blocklist UI** | View / remove saved entries. |

### Radar

Configurable size, range, and opacity. Same player data as ESP, drawn as a top-down mini-map relative to your pawn.

### Aimbot

| Setting | How it works |
|---|---|
| **Hold key (default MB5)** | While held, finds the closest enemy within FOV to screen center. |
| **Lock bone** | Head, neck, chest, spine, or pelvis. |
| **FOV / Smoothing / Offset** | Limits target cone; lerps view angles; vertical offset fallback. |
| **Visible check** | Optional line-of-sight filter. |
| **FOV circle** | Draws aim cone on overlay when enabled. |

Writes `ControlRotation` on the local PlayerController via pymem.

### Exploits (EXPLOITS Tab)

Memory writes when toggled on (~20 Hz exploits tick):

| Toggle | How it works |
|---|---|
| **No Gun Cooldown** | Hunter gun cooldown → 0 each tick. |
| **No Recoil** | Camera shake modifier alpha → 0. |
| **No Decoy Cooldown** | Survivor decoy cooldown slots forced ready. |
| **Anti Detection (Survivor)** | Clears `OverlapCheckCapsules` (“Too Buried” reveal). |
| **God Mode (Survivor)** | Bridge blocks damage/death RPCs and scrubs health/dead flags on your pawn (survivor/hider only). |
| **Infinite Bullets (Hunter)** | Sets `InfinityBullet` flag each tick. |
| **Magnet (Hunter)** | Toggle with **G** (rebind via **Record Key**). Global hotkey + polling; pulls survivors along your view. |
| **Kill All Survivors (WIP)** | One-click staggered `KillPlayer` on all survivors — **work in progress**, unreliable in some modes. |
| **Return to Main Lobby** | `ClientReturnToMainMenuWithTextReason`; temporarily disables anti-kick so the RPC is not blocked. |
| **Set Decoy Num** | Writes max decoy spawn count. |
| **Anti-Clipping (noclip)** | Disables collision on local body mesh + capsule. |
| **Anti-Kick** | Bridge vtable hooks block kick/disconnect RPCs (see below). |
| **Auto-Rename** | Queued background rename via bridge; debounced after typing stops. |
| **Rename button** | Manual rename (Enter or **Rename**). |
| **Teleport / Kill Self** | Bridge TCP: `K2_SetActorLocation` / destroy local pawn. |

#### Anti-Kick (detailed)

Anti-kick runs **inside the injected bridge DLL**, not from Python memory writes.

| Layer | Behavior |
|---|---|
| **Hook method** | **Vtable ProcessEvent hooks** on local `PlayerController`, `PlayerState`, and `NetConnection` — does not patch global ProcessEvent. |
| **Blocked RPCs** | `ClientWasKicked`, `ClientReturnToMainMenu`, `PlayerState.Kick`, `NetConnection.Close`, etc. |
| **Auto-scan** | Scans class hierarchies for kick/ban/disconnect function names. |
| **Kick logger** | Blocks logged to `anti_kick.log` with function name and host username when available. |
| **Limitation** | EOS/Redpoint platform kicks may still drop the socket at the network layer. |

Enable Anti-Kick **after** joining a match for best results. Fully quit the game before updating `meccha-xenos-bridge.dll`.

### Bridge Commands (TCP `:47654`)

| Command | Description |
|---|---|
| `ping` / `capabilities` | Health check and command list |
| `paint_full_route` / mesh-first paint | Environment camo passes |
| `rotate` | Camera or pawn rotation |
| `cancel_paint` | Cancel active paint queue |
| `teleport` / `kill` | Move or destroy local pawn |
| `set_fov` | Camera FOV override |
| `set_anti_kick` | Enable/disable vtable anti-kick hooks |
| `get_anti_kick_log` | Fetch kick block log entries |
| `set_player_name` | Replicated rename via `SetName(Server)` |
| `shutdown` | Stop bridge TCP server |

Bridge auto-injects from **`bridge/`** next to `Peterhack.bat` (includes **`mesh-profiles/`** for camo).

### Environment Camouflage (Bridge)

360° environment camo via **`Paint Now`** / **F10**. Camera angles are computed from pawn root rotation and current view.

**Default pass order:** front → left → right → back → head/shoulders → inner legs

| Control | Description |
|---|---|
| **Camo quality** | Slider **1–20** — higher = finer strokes / sharper result (20 = extreme). Lower = faster draft passes. |
| **Disable front pass** | For flat maps only — skips front pass. |
| **Back pass only** | Paints spine/rear orbit only. |
| **Paint Now / F10** | Start full camo apply |
| **Stop Camo (F9)** | `cancel_paint` over TCP |

### Custom Character Paint

PNG/JPG onto your character atlas (separate from environment camo):

| Wrap mode | Description |
|---|---|
| **Projector (front → back)** | Image wraps front → back across the atlas. |
| **Centered (chest outward)** | Image center on chest; top → head, bottom → feet. |

Also: image quality slider (1–5), opacity, **UV test mode** dropdown, preset save/load.

### MISC Tab

| Control | Description |
|---|---|
| **Menu language** | English, Español, Français, Deutsch, Português, Русский, 中文, 日本語, 한국어 — labels update live. |
| **Overlay FPS** | 1–100 Hz overlay refresh when menu is hidden; capped at 30 FPS while menu is open. |

### DEBUGGING Tab

Toggle what gets written to `C:\peterhack\logs\latest.log` by tag (`[CAMO]`, `[EXPLOITS:*]`, `[ESP]`, `[AIMBOT]`, etc.). Master switch plus per-category filters.

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
3. Configure ESP, exploits, and colors in the menu tabs.
4. **CAMOUFLAGE** — set quality and pass options, then **Paint Now** or **F10**.
5. **EXPLOITS** — enable anti-kick after spawning; check `anti_kick.log` if kicked.
6. **PLAYERS (WIP)** — blocklist / copy Steam ID; full session list still improving.

Pre-built EXE: download the **Peterhack** artifact from [GitHub Actions](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack/actions) after a push to `main`.

---

## Building the Bridge DLL

Requires **Visual Studio** with C++ tools.

```powershell
runtime/scripts/build.ps1
# Output: bridge/meccha-xenos-bridge.dll (+ injector, mesh-profiles)
# Commit bridge/ when updating the in-game DLL so GitHub users get the latest build.
```

| Path | Role |
|---|---|
| `runtime/src/bridge.cpp` | TCP server, camo, anti-kick, teleport/kill/rename |
| `runtime/src/bridge_peterhack.inc` | Peterhack-specific bridge extensions |
| `bridge/` | Shipped DLL, injector, and **mesh-profiles/** (committed to Git) |

Peterhack extends [SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools) with dynamic multi-pass camo, vtable anti-kick, mesh-first paint, and external ESP/exploits.

---

## Auto-update

On launch, Peterhack can check [GitHub main](https://github.com/bowlingball3525/Meccha-Chameleon-Peterhack) for newer source and apply it automatically, then restart.

- **ZIP users** — files merged in place; `esp_config.json` and paint presets preserved.
- **Git clone users** — `git fetch` + `git reset --hard` when Git is installed.
- Disable in the **CHANGELOG** tab, or run: `python -m meccha_chameleon_tools --no-update`

---

## Troubleshooting

| Symptom | What to try |
|---|---|
| `failed to communicate with bridge DLL` | Run as Administrator; be in a match; check `latest.log` |
| `mesh_profile_missing` / camo fails | Ensure `bridge/mesh-profiles/` exists next to the DLL |
| ESP missing players in infection | Update to latest — sticky pawn + gap scan; turn off **Team Filter** / **Enemy Only** to test |
| ESP shows dead bodies | Update to latest dead-player filter; report if corpses still linger |
| Anti-kick enabled but still kicked | Check `anti_kick.log`; EOS platform kicks may bypass UE RPCs |
| Magnet does nothing | Must be **Hunter**; press **G** to toggle ON; run as Admin |
| Kill All Survivors (WIP) fails | Feature is WIP — use Kill Selected on PLAYERS tab or retry after updates |
| Player list empty / wrong (WIP) | Tab is WIP — blocklist copy/save may still work; check log for `[ESP]` |
| Steam ID shows `—` | Wait for replication; select row and Copy again |
| Bridge inject OK but no TCP | Latest DLL auto-starts TCP on inject; ensure `bridge/meccha-xenos-bridge.dll` is used |

---

## Disclaimer

For educational purposes only. Use at your own risk.

---

## Credits

- **[Cursor AI Composer 2.5](https://cursor.com)** — AI-assisted development
- **[SilentJMA/Meccha-Chameleon-Tools](https://github.com/SilentJMA/Meccha-Chameleon-Tools)** — Original bridge / camo foundation
- **MECCHA CHAMELEON** community — testing and feedback
