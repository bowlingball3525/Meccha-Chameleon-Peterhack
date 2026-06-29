#!/usr/bin/env python3
"""Extract offsets from Dumper-7 dump for Peterhack."""
import json
import re
from pathlib import Path

DUMP = Path(r"C:\dumper-7\5.6.1-44394996+++UE5+Release-5.6-Chameleon")
CORE = Path(__file__).resolve().parents[1] / "meccha_chameleon_tools" / "core.py"


def parse_function_rva(entry):
    """FunctionsInfo entry: [ret, params, rva, flags]"""
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
    """ClassesInfo field: [type_info, offset, size, flags]."""
    if isinstance(fdef, list) and len(fdef) >= 2 and isinstance(fdef[1], int):
        return fdef[1]
    return None


def main():
    oi = json.loads((DUMP / "Dumpspace" / "OffsetsInfo.json").read_text(encoding="utf-8"))
    print("=== Global offsets (OffsetsInfo.json) ===")
    for k, v in oi["data"]:
        if k.startswith("OFFSET") or k.startswith("INDEX"):
            print(f"  {k} = 0x{v:X}")

    fi = json.loads((DUMP / "Dumpspace" / "FunctionsInfo.json").read_text(encoding="utf-8"))
    rpc_funcs = find_class_functions(fi["data"], "URuntimePaintableComponent")
    names = [
        "PaintAtUV", "ClearChannel", "ExportChannelToBytes", "ImportChannelFromBytes",
        "BeginStroke", "EndStroke", "RequestFullTextureSync", "PaintAtScreenPosition",
        "HitTestAtScreenPosition", "GetInitializedPaintMesh",
    ]
    print("\n=== RuntimePaintableComponent exec thunks ===")
    func_rvas = {}
    for name in names:
        entry = rpc_funcs.get(name)
        rva = parse_function_rva(entry)
        if rva is not None:
            func_rvas[name] = rva
            print(f"  {name} = 0x{rva:X}")
        else:
            print(f"  {name} = NOT FOUND")

    ph = (DUMP / "CppSDK" / "SDK" / "PenguinHotel_classes.hpp").read_text(encoding="utf-8", errors="replace")
    rpc_start = ph.find("class URuntimePaintableComponent final")
    rpc_block = ph[rpc_start:rpc_start + 6000]
    print("\n=== URuntimePaintableComponent members ===")
    for name in (
        "AlbedoRenderTarget", "DynamicMaterialInstance", "CurrentBrushSettings",
        "bAutoRecordStrokes", "bAutoFlushStrokes",
    ):
        m = re.search(rf"{name};\s*//\s*(0x[0-9A-Fa-f]+)", rpc_block)
        print(f"  {name} = {m.group(1) if m else 'NOT FOUND'}")

    char = (DUMP / "CppSDK" / "SDK" / "BP_FirstPersonCharacter_cLeon_Character_classes.hpp").read_text(
        encoding="utf-8", errors="replace"
    )
    m = re.search(r"RuntimePaintable;\s*//\s*(0x[0-9A-Fa-f]+)", char)
    print(f"  pawn RuntimePaintable = {m.group(1) if m else 'NOT FOUND'}")

    ci = json.loads((DUMP / "Dumpspace" / "ClassesInfo.json").read_text(encoding="utf-8"))
    class_offsets = {}
    for block in ci["data"]:
        if not isinstance(block, dict):
            continue
        for cls_name, fields in block.items():
            if cls_name.startswith("_"):
                continue
            if not isinstance(fields, list):
                continue
            for item in fields:
                if not isinstance(item, dict):
                    continue
                for fname, fdef in item.items():
                    off = parse_member_offset(fdef)
                    if off is not None:
                        class_offsets[f"{cls_name}::{fname}"] = off

    for key in (
        "APlayerController::AcknowledgedPawn",
        "APlayerCameraManager::CameraCachePrivate",
        "AActor::RootComponent",
        "USceneComponent::RelativeLocation",
        "APlayerState::PawnPrivate",
    ):
        off = class_offsets.get(key)
        print(f"  {key} = {f'0x{off:X}' if off is not None else 'NOT FOUND'}")

    text = CORE.read_text(encoding="utf-8")
    print("\n=== core.py vs dump (paint RVAs) ===")
    mapping = {
        "RVA_EXEC_PAINT_AT_UV": func_rvas.get("PaintAtUV"),
        "RVA_PAINT_AT_UV_DUMP": func_rvas.get("PaintAtUV"),
        "RVA_EXEC_PAINT_AT_SCREEN": func_rvas.get("PaintAtScreenPosition"),
        "RVA_EXEC_HITTEST_AT_SCREEN": func_rvas.get("HitTestAtScreenPosition"),
        "RVA_CLEAR_CHANNEL_NATIVE": func_rvas.get("ClearChannel"),
        "RVA_EXPORT_CHANNEL_NATIVE": func_rvas.get("ExportChannelToBytes"),
        "RVA_IMPORT_CHANNEL_NATIVE": func_rvas.get("ImportChannelFromBytes"),
        "RVA_EXEC_IMPORT_CHANNEL": func_rvas.get("ImportChannelFromBytes"),
        "RVA_BEGIN_STROKE_NATIVE": func_rvas.get("BeginStroke"),
        "RVA_END_STROKE_NATIVE": func_rvas.get("EndStroke"),
        "RVA_EXEC_REQUEST_TEXTURE_SYNC": func_rvas.get("RequestFullTextureSync"),
        "RVA_PAINT_AT_SCREEN_NATIVE": func_rvas.get("PaintAtScreenPosition"),
        "RVA_HITTEST_AT_SCREEN_NATIVE": func_rvas.get("HitTestAtScreenPosition"),
        "RVA_GET_PAINT_MESH_NATIVE": func_rvas.get("GetInitializedPaintMesh"),
    }
    changes = []
    for const, new_val in mapping.items():
        m = re.search(rf"{const}\s*=\s*(0x[0-9A-Fa-f]+)", text)
        old = int(m.group(1), 16) if m else None
        if new_val is None:
            print(f"  {const}: dump missing")
        elif old == new_val:
            print(f"  {const}: OK (0x{new_val:X})")
        else:
            print(f"  {const}: CHANGE 0x{old:X} -> 0x{new_val:X}")
            changes.append((const, new_val))

    return changes, func_rvas, class_offsets


if __name__ == "__main__":
    main()
