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
- Samples the environment *behind* your character using a directed edge walk
- Copies those colors directly onto your body so you blend into the surroundings
- Independent **Camo Quality** slider (1–5)

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
| F9 | Toggle ESP |
| F10 | Toggle Camouflage |
| MB5 (default) | Aimbot hold |

---

## Disclaimer

For educational purposes only. Use at your own risk.
