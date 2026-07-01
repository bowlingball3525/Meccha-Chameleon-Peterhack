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
TRAINER = ROOT / "meccha_chameleon_tools" / "trainer.py"

GLOBAL_PATCHES = {
    "GWORLD_RVA": "OFFSET_GWORLD",
}

TRAINER_PATCHES = {
    "RVA_PROCESS_EVENT": "OFFSET_PROCESSEVENT",
}

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

    for const, global_key in TRAINER_PATCHES.items():
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
        (TRAINER, "trainer.py"),
    )
    for path, label in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        changed = []
        missing = []
        for name, value in sorted(updates.items()):
            # Trainer only gets ProcessEvent; core gets everything else.
            if path == TRAINER and name != "RVA_PROCESS_EVENT":
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

    for label, path in (("core.py", CORE), ("trainer.py", TRAINER)):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        print(f"\n=== {label} vs dump ===")
        for name, new_val in sorted(updates.items()):
            if label == "trainer.py" and name != "RVA_PROCESS_EVENT":
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
        print("\nDone — restart Peterhack to pick up new offsets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
