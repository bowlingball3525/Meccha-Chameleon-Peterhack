#!/usr/bin/env python3
"""Extract offsets from Dumper-7 dump and optionally patch core.py."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "meccha_chameleon_tools" / "core.py"
EXPLOITS = ROOT / "meccha_chameleon_tools" / "exploits.py"
SDK_HPP = ROOT / "runtime" / "include" / "sdk.hpp"

GLOBAL_PATCHES = {
    "GWORLD_RVA": "OFFSET_GWORLD",
}

EXPLOITS_PATCHES = {
    "RVA_PROCESS_EVENT": "OFFSET_PROCESSEVENT",
}

# .get("Class::Field", 0xFALLBACK) literals in core.py when resolver cache misses.
FALLBACK_OFFSETS = {
    "AActor::RootComponent": ("AActor", "RootComponent"),
    "APawn::PlayerState": ("APawn", "PlayerState"),
    "APawn::Controller": ("APawn", "Controller"),
    "AController::PlayerState": ("AController", "PlayerState"),
    "AController::ControlRotation": ("AController", "ControlRotation"),
    "APlayerState::PawnPrivate": ("APlayerState", "PawnPrivate"),
    "APlayerController::PlayerCameraManager": ("APlayerController", "PlayerCameraManager"),
    "APlayerController::Player": ("APlayerController", "Player"),
    "APlayerController::AcknowledgedPawn": ("APlayerController", "AcknowledgedPawn"),
    "UWorld::OwningGameInstance": ("UWorld", "OwningGameInstance"),
    "UWorld::GameState": ("UWorld", "GameState"),
    "UGameInstance::LocalPlayers": ("UGameInstance", "LocalPlayers"),
}

SDK_FIELD_PATCHES = {
    "UWorld_OwningGameInstance": ("UWorld", "OwningGameInstance"),
    "UGameInstance_LocalPlayers": ("UGameInstance", "LocalPlayers"),
    "UPlayer_PlayerController": ("UPlayer", "PlayerController"),
    "Controller_ControlRotation": ("AController", "ControlRotation"),
    "PlayerController_PlayerCameraManager": ("APlayerController", "PlayerCameraManager"),
    "BP_FirstPersonCharacter_RuntimePaintable": ("ABP_FirstPersonCharacter_cLeon_Character_C", "RuntimePaintable"),
    "RuntimePaintable_CurrentBrushSettings": ("URuntimePaintableComponent", "CurrentBrushSettings"),
    "SceneCapture2D_CaptureComponent2D": ("ASceneCapture2D", "CaptureComponent2D"),
    "SceneCaptureComponent_CaptureSource": ("USceneCaptureComponent", "CaptureSource"),
    "SceneCaptureComponent_bAlwaysPersistRenderingState": ("USceneCaptureComponent", "bAlwaysPersistRenderingState"),
    "SceneCaptureComponent2D_ProjectionType": ("USceneCaptureComponent2D", "ProjectionType"),
    "SceneCaptureComponent2D_FOVAngle": ("USceneCaptureComponent2D", "FOVAngle"),
    "SceneCaptureComponent2D_TextureTarget": ("USceneCaptureComponent2D", "TextureTarget"),
}

VERIFY_GLOBALS = (
    ("GObjects", "OFFSET_GOBJECTS"),
    ("GNames", "OFFSET_GNAMES"),
    ("GWorld", "OFFSET_GWORLD"),
    ("ProcessEvent", "OFFSET_PROCESSEVENT"),
)

# Prefer C:\dumper-7 (user may typo as duper-7)
DUMP_ROOTS = [Path(r"C:\dumper-7"), Path(r"C:\duper-7")]

PAINT_FUNCS = (
    "PaintAtUV",
    "ClearChannel",
    "ExportChannelToBytes",
    "ImportChannelFromBytes",
    "BeginStroke",
    "EndStroke",
    "RequestFullTextureSync",
    "PaintAtScreenPosition",
    "HitTestAtScreenPosition",
    "GetInitializedPaintMesh",
)

EXEC_TO_CORE = {
    "PaintAtUV": ("RVA_PAINT_AT_UV_DUMP", "RVA_EXEC_PAINT_AT_UV"),
    "ClearChannel": ("RVA_CLEAR_CHANNEL_NATIVE",),
    "ExportChannelToBytes": ("RVA_EXPORT_CHANNEL_NATIVE",),
    "ImportChannelFromBytes": ("RVA_IMPORT_CHANNEL_NATIVE", "RVA_EXEC_IMPORT_CHANNEL"),
    "BeginStroke": ("RVA_BEGIN_STROKE_NATIVE",),
    "EndStroke": ("RVA_END_STROKE_NATIVE",),
    "RequestFullTextureSync": ("RVA_EXEC_REQUEST_TEXTURE_SYNC",),
    "PaintAtScreenPosition": ("RVA_PAINT_AT_SCREEN_NATIVE", "RVA_EXEC_PAINT_AT_SCREEN"),
    "HitTestAtScreenPosition": ("RVA_HITTEST_AT_SCREEN_NATIVE", "RVA_EXEC_HITTEST_AT_SCREEN"),
    "GetInitializedPaintMesh": ("RVA_GET_PAINT_MESH_NATIVE",),
}

LEGACY_SCAN = {
    "RVA_PAINT_AT_UV_LEGACY": "PaintAtUV",
    "RVA_CLEAR_CHANNEL_LEGACY": "ClearChannel",
    "RVA_EXPORT_CHANNEL_LEGACY": "ExportChannelToBytes",
    "RVA_IMPORT_CHANNEL_LEGACY": "ImportChannelFromBytes",
    "RVA_BEGIN_STROKE_LEGACY": "BeginStroke",
    "RVA_END_STROKE_LEGACY": "EndStroke",
}

# Shift by PaintAtUV exec delta when live deep-worker scan is unavailable.
LEGACY_ESTIMATE_CONSTS = LEGACY_SCAN.keys() | {
    "RVA_IMPORT_RT_NATIVE",
    "RVA_REQUEST_TEXTURE_SYNC",
    "RVA_APPLY_PAINT_TO_MATERIAL",
}


def find_newest_dump() -> Path:
    for root in DUMP_ROOTS:
        if not root.is_dir():
            continue
        subs = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for sub in subs:
            if (sub / "Dumpspace" / "OffsetsInfo.json").is_file():
                return sub
    raise FileNotFoundError(
        "No Dumper-7 dump found under C:\\dumper-7 or C:\\duper-7",
    )


def parse_function_rva(entry):
    if not isinstance(entry, list) or len(entry) < 3:
        return None
    rva = entry[2]
    return int(rva) if isinstance(rva, int) else None


def find_class_functions(data, class_name):
    out = {}
    for block in data:
        if not isinstance(block, dict) or class_name not in block:
            continue
        for item in block[class_name]:
            if isinstance(item, dict):
                out.update(item)
    return out


def parse_member_offset(fdef):
    if isinstance(fdef, list) and len(fdef) >= 2 and isinstance(fdef[1], int):
        return fdef[1]
    return None


def load_class_offsets(dump: Path) -> dict[str, int]:
    ci = json.loads((dump / "Dumpspace" / "ClassesInfo.json").read_text(encoding="utf-8"))
    out = {}
    for block in ci["data"]:
        if not isinstance(block, dict):
            continue
        for cls_name, fields in block.items():
            if cls_name.startswith("_") or not isinstance(fields, list):
                continue
            for item in fields:
                if not isinstance(item, dict):
                    continue
                for fname, fdef in item.items():
                    off = parse_member_offset(fdef)
                    if off is not None:
                        out[f"{cls_name}::{fname}"] = off
    return out


def scan_legacy_workers(func_rvas: dict[str, int]) -> dict[str, int]:
    """Scan deep workers from live game when available."""
    try:
        import pymem
        from meccha_chameleon_tools.core import MecchaESP
    except ImportError:
        return {}

    try:
        pm = pymem.Pymem("PenguinHotel-Win64-Shipping.exe")
    except Exception:
        print("[scan] game not running — skipping deep worker scan")
        return {}

    esp = MecchaESP.__new__(MecchaESP)
    esp.pm = pm
    esp._cached_module_base = 0
    workers = {}
    for const, func in LEGACY_SCAN.items():
        anchor = func_rvas.get(func)
        if not anchor:
            continue
        worker = esp._scan_deep_worker(anchor)
        if worker and esp._native_rva_ok(worker):
            workers[const] = worker
            print(f"[scan] {const} = 0x{worker:X} (from {func})")
    return workers


def patch_const(text: str, name: str, value: int) -> tuple[str, bool]:
    pat = re.compile(rf"({re.escape(name)}\s*=\s*)0x[0-9A-Fa-f]+")
    new = rf"\g<1>0x{value:X}"
    new_text, n = pat.subn(new, text, count=1)
    return new_text, n == 1


def patch_fallback(text: str, key: str, value: int) -> tuple[str, bool]:
    pat = re.compile(rf'(\.get\("{re.escape(key)}",\s*)0x[0-9A-Fa-f]+(\))')
    new = rf"\g<1>0x{value:X}\2"
    new_text, n = pat.subn(new, text)
    return new_text, n > 0


def collect_fallback_updates(dump: Path) -> dict[str, int]:
    classes = load_class_offsets(dump)
    updates: dict[str, int] = {}
    for key, (cls, field) in FALLBACK_OFFSETS.items():
        full = f"{cls}::{field}"
        if full in classes:
            updates[key] = classes[full]
    return updates


def collect_sdk_updates(dump: Path) -> dict[str, int]:
    classes = load_class_offsets(dump)
    updates: dict[str, int] = {}
    for const, (cls, field) in SDK_FIELD_PATCHES.items():
        full = f"{cls}::{field}"
        if full in classes:
            updates[const] = classes[full]
    return updates


def apply_sdk_updates(updates: dict[str, int]) -> None:
    if not SDK_HPP.is_file() or not updates:
        return
    text = SDK_HPP.read_text(encoding="utf-8")
    changed = []
    missing = []
    for name, value in sorted(updates.items()):
        pat = re.compile(rf"(constexpr std::uintptr_t {re.escape(name)} = )0x[0-9A-Fa-f]+")
        new_text, n = pat.subn(rf"\g<1>0x{value:X}", text, count=1)
        if n:
            text = new_text
            changed.append(f"  {name} = 0x{value:X}")
        else:
            missing.append(name)
    if changed:
        SDK_HPP.write_text(text, encoding="utf-8")
        print("Patched runtime/include/sdk.hpp:")
        print("\n".join(changed))
    if missing:
        print(f"SDK field not found in sdk.hpp: {', '.join(missing)}")


def apply_verify_globals(dump: Path) -> None:
    oi = json.loads((dump / "Dumpspace" / "OffsetsInfo.json").read_text(encoding="utf-8"))
    globals_map = {k: v for k, v in oi["data"] if k.startswith("OFFSET")}
    if not CORE.is_file():
        return
    text = CORE.read_text(encoding="utf-8")
    changed = []
    for label, key in VERIFY_GLOBALS:
        value = globals_map.get(key)
        if value is None:
            continue
        pat = re.compile(rf'(\("{label}",\s*)0x[0-9A-Fa-f]+(\))')
        new_text, n = pat.subn(rf"\g<1>0x{value:X}\2", text, count=1)
        if n:
            text = new_text
            changed.append(f"  _verify_paint_build_offsets {label} -> 0x{value:X}")
    if changed:
        CORE.write_text(text, encoding="utf-8")
        print("Patched core.py verify globals:")
        print("\n".join(changed))


def apply_fallback_updates(updates: dict[str, int]) -> None:
    if not CORE.is_file() or not updates:
        return
    text = CORE.read_text(encoding="utf-8")
    changed = []
    missing = []
    for key, value in sorted(updates.items()):
        text, ok = patch_fallback(text, key, value)
        if ok:
            changed.append(f"  {key} fallback -> 0x{value:X}")
        else:
            missing.append(key)
    if changed:
        CORE.write_text(text, encoding="utf-8")
        print("Patched core.py fallbacks:")
        print("\n".join(changed))
    if missing:
        print(f"Fallback not found in core.py: {', '.join(missing)}")


def collect_updates(dump: Path) -> dict[str, int]:
    oi = json.loads((dump / "Dumpspace" / "OffsetsInfo.json").read_text(encoding="utf-8"))
    globals_map = {k: v for k, v in oi["data"] if k.startswith("OFFSET")}
    gnames = globals_map["OFFSET_GNAMES"]
    gobjects = globals_map["OFFSET_GOBJECTS"]

    fi = json.loads((dump / "Dumpspace" / "FunctionsInfo.json").read_text(encoding="utf-8"))
    rpc_funcs = find_class_functions(fi["data"], "URuntimePaintableComponent")

    updates: dict[str, int] = {"FNAMEPOOL_DELTA": gobjects - gnames}

    for const, global_key in GLOBAL_PATCHES.items():
        if global_key in globals_map:
            updates[const] = globals_map[global_key]

    for const, global_key in EXPLOITS_PATCHES.items():
        if global_key in globals_map:
            updates[const] = globals_map[global_key]

    func_rvas = {}
    old_paint_at_uv = None
    for name in PAINT_FUNCS:
        entry = rpc_funcs.get(name)
        rva = parse_function_rva(entry)
        if rva is None:
            print(f"  {name} = NOT FOUND")
            continue
        func_rvas[name] = rva
        if name == "PaintAtUV":
            old_paint_at_uv = rva
        for const in EXEC_TO_CORE.get(name, ()):
            updates[const] = rva

    scanned = scan_legacy_workers(func_rvas)
    updates.update(scanned)

    if not scanned and old_paint_at_uv is not None:
        # Estimate legacy worker shift from PaintAtUV exec movement vs core.py anchor.
        text = CORE.read_text(encoding="utf-8")
        m = re.search(r"RVA_EXEC_PAINT_AT_UV\s*=\s*(0x[0-9A-Fa-f]+)", text)
        if m:
            prev_exec = int(m.group(1), 16)
            delta = old_paint_at_uv - prev_exec
            if delta:
                print(f"[estimate] PaintAtUV exec delta=0x{delta:X} — shifting legacy workers")
                for const in LEGACY_ESTIMATE_CONSTS:
                    m2 = re.search(rf"{re.escape(const)}\s*=\s*(0x[0-9A-Fa-f]+)", text)
                    if m2:
                        updates[const] = int(m2.group(1), 16) + delta

    return updates


def apply_updates(updates: dict[str, int]) -> None:
    targets = (
        (CORE, "core.py"),
        (EXPLOITS, "exploits.py"),
    )
    for path, label in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        changed = []
        missing = []
        for name, value in sorted(updates.items()):
            # Exploits module only gets ProcessEvent; core gets everything else.
            if path == EXPLOITS and name != "RVA_PROCESS_EVENT":
                continue
            if path == CORE and name == "RVA_PROCESS_EVENT":
                continue
            text, ok = patch_const(text, name, value)
            if ok:
                changed.append(f"  {name} = 0x{value:X}")
            else:
                missing.append(name)
        if changed:
            path.write_text(text, encoding="utf-8")
            print(f"Patched {label}:")
            print("\n".join(changed))
        if missing:
            print(f"Not found in {label}: {', '.join(missing)}")


def report(dump: Path, updates: dict[str, int]) -> None:
    print(f"Dump: {dump}")
    oi = json.loads((dump / "Dumpspace" / "OffsetsInfo.json").read_text(encoding="utf-8"))
    print("\n=== Global offsets ===")
    for k, v in oi["data"]:
        if k.startswith("OFFSET") or k.startswith("INDEX"):
            print(f"  {k} = 0x{v:X}")

    for label, path in (("core.py", CORE), ("exploits.py", EXPLOITS)):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        print(f"\n=== {label} vs dump ===")
        for name, new_val in sorted(updates.items()):
            if label == "exploits.py" and name != "RVA_PROCESS_EVENT":
                continue
            if label == "core.py" and name == "RVA_PROCESS_EVENT":
                continue
            m = re.search(rf"{re.escape(name)}\s*=\s*(0x[0-9A-Fa-f]+)", text)
            old = int(m.group(1), 16) if m else None
            if old == new_val:
                print(f"  {name}: OK (0x{new_val:X})")
            elif old is None:
                print(f"  {name}: NEW 0x{new_val:X}")
            else:
                print(f"  {name}: CHANGE 0x{old:X} -> 0x{new_val:X}")


def main():
    parser = argparse.ArgumentParser(description="Extract / apply Dumper-7 offsets")
    parser.add_argument("--dump", type=Path, help="Path to dump folder")
    parser.add_argument("--apply", action="store_true", help="Patch core.py")
    args = parser.parse_args()

    dump = args.dump or find_newest_dump()
    updates = collect_updates(dump)
    report(dump, updates)
    if args.apply:
        apply_updates(updates)
        apply_fallback_updates(collect_fallback_updates(dump))
        apply_sdk_updates(collect_sdk_updates(dump))
        apply_verify_globals(dump)
        print("\nDone — restart Peterhack to pick up new offsets.")
        print("Rebuild bridge DLL if runtime/include/sdk.hpp changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
