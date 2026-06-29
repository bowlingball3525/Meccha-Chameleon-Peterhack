<p align="center">
  <img src="peter.png" alt="Peterhack Logo" width="200"/>
</p>

# Meccha Chameleon — Peterhack

A heavily modified ESP and character-paint tool for **MECCHA CHAMELEON** (UE5.6).

---

## Features

### ESP Overlay
- Player dots, boxes, skeleton, snap lines, OOF arrows
- Name, distance, health/shield bars
- Radar mini-map
- Aimbot with FOV circle and visibility check
- FPS indicator in the overlay top-left

### Custom Character Paint — Apply Image
Paint any PNG/JPG directly onto your character's texture atlas with two wrap modes:

| Mode | Description |
|---|---|
| **Projector (front → back)** | Image starts at the front side seam, wraps continuously around to the back. The full image is visible front and back as one piece. |
| **Centered (chest outward)** | Image center sits on the chest. Top of image → character head, bottom → feet. Seam hidden at the spine. |

- Auto-trims transparent and solid-color borders before painting
- White base coat clears previous paint before applying
- Independent **Image Quality** slider (1 = Draft → 5 = Ultra)
- Game process priority is lowered while painting to free CPU (restores automatically)

### Camouflage (F10)
- **Standard mode:** SilentJMA bridge (`meccha-camouflage.exe`) paints from the camera view with proper material sync — best for the side facing the camera (back in 3rd person).
- **360° wrap:** Samples the floor in a ring around your body, bridge-paints the back once, then UV-stamps the front and sides on top.
- Start / Stop buttons in the CAMOUFLAGE tab; F10 to apply, F9 to stop.
- Requires bridge TCP — if setup fails, click into the game and press F10 once, then retry.
- Independent **Camo Quality** slider (1–20)
- Logs: `C:\peterhack\logs\latest.log`

### Trainer
- TRAINER tab: No Gun CD, No Recoil, and related toggles with `[TRAINER:TAG]` debug lines in `latest.log`.

### Launcher
- `Peterhack.bat` — auto-elevates to Administrator, no manual "Run as Admin" needed

---

## Requirements

```
Python 3.10+
PyQt5
pymem
pywin32
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Usage

1. Launch MECCHA CHAMELEON and get into a match.
2. Run `Peterhack.bat` as Administrator (it self-elevates automatically).
3. The overlay and control panel will appear.

---

## Hotkeys

| Key | Action |
|---|---|
| F9 | Stop camouflage / toggle ESP (overlay poll) |
| F10 | Apply camouflage (overlay poll; bridge EXE also listens globally) |
| MB5 (default) | Aimbot hold |

---

## Disclaimer

For educational purposes only. Use at your own risk.
