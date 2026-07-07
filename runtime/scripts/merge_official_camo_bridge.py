#!/usr/bin/env python3
"""Merge MecchaCamouflage v1.6 mesh_first bridge with Peterhack bridge extras."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
INCLUDE = ROOT / "include"
OFFICIAL = Path(r"C:\Users\lance\AppData\Local\Temp\MecchaCamouflage-1.6.0-beta.4\src\native")
BACKUP = SRC / "bridge_peterhack_legacy.cpp"

PETERHACK_GLOBAL_RANGES = [
    (71, 223),
    (241, 250),
]

PETERHACK_IMPL_RANGES = [
    (1001, 1254),
    (2747, 3666),
    (5458, 6084),
    (9338, 9364),
    (9366, 9384),
    (9386, 10722),
]

PETERHACK_JSON_COMPAT = """
    auto json_extract_string(const std::string& json, const std::string& key) -> std::string
    {
        return json_string_field(json, key);
    }

    auto json_extract_double(const std::string& json, const std::string& key) -> double
    {
        return json_number_field(json, key, 0.0);
    }

    auto json_extract_bool(const std::string& json, const std::string& key) -> bool
    {
        return json_bool_field(json, key, false);
    }

    auto json_extract_payload(const std::string& full_json) -> std::string
    {
        const auto pay_key = full_json.find("\\"payload\\"");
        if (pay_key == std::string::npos)
        {
            return {};
        }
        const auto colon = full_json.find(':', pay_key);
        if (colon == std::string::npos)
        {
            return {};
        }
        const auto brace = full_json.find('{', colon);
        if (brace == std::string::npos)
        {
            return {};
        }
        int depth = 1;
        for (std::size_t i = brace + 1; i < full_json.size(); ++i)
        {
            if (full_json[i] == '{')
            {
                ++depth;
            }
            else if (full_json[i] == '}')
            {
                --depth;
                if (depth == 0)
                {
                    return full_json.substr(brace, i - brace + 1);
                }
            }
        }
        return {};
    }
"""

PETERHACK_SAFE_WRITE = """
    template <typename T>
    auto safe_write(std::uintptr_t address, T value) -> bool
    {
        __try
        {
            *reinterpret_cast<T*>(address) = value;
            return true;
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            return false;
        }
    }

    auto parse_hex_address(const std::string& text) -> std::uintptr_t
    {
        if (text.empty())
        {
            return 0;
        }
        try
        {
            std::string trimmed = text;
            while (!trimmed.empty() && (trimmed.front() == ' ' || trimmed.front() == '"'))
            {
                trimmed.erase(trimmed.begin());
            }
            while (!trimmed.empty() && (trimmed.back() == ' ' || trimmed.back() == '"'))
            {
                trimmed.pop_back();
            }
            if (trimmed.empty())
            {
                return 0;
            }
            std::size_t idx = 0;
            const int base = (trimmed.rfind("0x", 0) == 0 || trimmed.rfind("0X", 0) == 0) ? 16 : 10;
            return static_cast<std::uintptr_t>(std::stoull(trimmed, &idx, base));
        }
        catch (...)
        {
            return 0;
        }
    }
"""

REFLECTION_EXTRA_METHODS = """
        auto find_class_any(const char* name) -> std::uintptr_t
        {
            std::uintptr_t found = 0;
            for_each_object([&](std::uintptr_t obj) {
                if (object_name(obj) != name)
                {
                    return false;
                }
                const auto meta_name = class_name(obj);
                if (meta_name == "Class" || meta_name == "BlueprintGeneratedClass"
                    || meta_name == "WidgetBlueprintGeneratedClass" || meta_name == "AnimBlueprintGeneratedClass")
                {
                    found = obj;
                    return true;
                }
                return false;
            });
            return found;
        }

        auto find_function_in_class(std::uintptr_t cls, const char* function_name) -> std::uintptr_t
        {
            for (int depth = 0; cls && depth < 64; ++depth)
            {
                for (auto child = safe_read<std::uintptr_t>(cls + OffChildren); child;
                     child = safe_read<std::uintptr_t>(child + OffUFieldNext))
                {
                    if (object_name(child) == function_name)
                    {
                        return child;
                    }
                }
                cls = safe_read<std::uintptr_t>(cls + OffSuperStruct);
            }
            return 0;
        }
"""
PETERHACK_FORWARD_DECLS = """
    struct Reflection;
    struct SdkContext;
    auto drain_game_commands_on_game_thread() -> void;
    auto execute_game_command_on_game_thread(const std::string& request) -> std::string;
    auto handle_game_command_direct(const std::string& request) -> std::string;
    auto handle_game_teleport(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string;
    auto handle_game_set_fov(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string;
    auto handle_game_kill(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string;
    auto handle_game_set_anti_kick(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string;
    auto handle_game_set_god_mode(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string;
    auto handle_game_get_skeleton(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string;
    auto god_mode_should_block(void* object, void* function) -> bool;
    auto god_mode_scrub_pawn(std::uintptr_t pawn) -> void;
    auto handle_get_anti_kick_log(const std::string& payload) -> std::string;
    auto handle_game_set_player_name(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string;
    auto handle_game_get_player_steam_id(const std::string& payload, Reflection& ref) -> std::string;
    auto resolve_process_event_address(std::uintptr_t sample_object) -> std::uintptr_t;
    auto install_process_event_inline_hook(std::uintptr_t sample_object, std::string& failure) -> bool;
    auto uninstall_process_event_inline_hook() -> void;
    auto install_anti_kick_vtable_hooks(std::uintptr_t controller,
                                        std::uintptr_t player_state,
                                        std::uintptr_t net_connection,
                                        std::string& failure) -> bool;
    auto anti_kick_is_local_object(std::uintptr_t object) -> bool;
    auto uninstall_anti_kick_vtable_hooks() -> void;
    auto anti_kick_should_block(void* object, void* function, void* params) -> bool;
    auto anti_kick_capture_probe(void* object, void* function) -> void;
    auto anti_kick_capture_reason(void* function, void* params) -> void;
    auto read_fstring_param(const std::uint8_t* base) -> std::string;
    auto trim_player_name(std::string name) -> std::string;
    auto cached_session_host_name(Reflection& ref) -> const char*;
    auto is_executable_code(std::uintptr_t address) -> bool;
    auto prop_offset(std::uintptr_t prop) -> int;
"""


def extract_ranges(path: Path, ranges: list[tuple[int, int]], header: str) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    chunks: list[str] = []
    for start, end in ranges:
        chunk = "\n".join(lines[start - 1 : end])
        chunks.append(f"    // ---- legacy bridge.cpp lines {start}-{end} ----\n{chunk}")
    return header + "\n\n".join(chunks) + "\n"


def patch_bridge_cpp(text: str) -> str:
    text = text.replace("constexpr int DefaultBridgePort = 47800;", "constexpr int DefaultBridgePort = 47654;")

    if "#include <functional>" not in text:
        text = text.replace("#include <cstring>", "#include <cstring>\n#include <functional>")
    if "#include <unordered_map>" not in text:
        text = text.replace("#include <unordered_set>", "#include <unordered_set>\n#include <unordered_map>")

    text = text.replace('#include "bridge_peterhack.inc"\n\n', "")
    text = text.replace('#include "bridge_peterhack_globals.inc"\n', "")

    globals_anchor = "    std::atomic<bool> g_dump_cancel_requested{false};"
    if '#include "bridge_peterhack_globals.inc"' not in text:
        text = text.replace(
            globals_anchor,
            globals_anchor + '\n\n#include "bridge_peterhack_globals.inc"',
        )

    json_anchor = '#include "bridge_json.inc"'
    if "auto json_extract_string(" not in text:
        text = text.replace(json_anchor, json_anchor + "\n" + PETERHACK_JSON_COMPAT)

    safe_read_anchor = """    template <typename T>
    auto safe_read(std::uintptr_t address, T fallback = T{}) -> T
    {
        __try
        {
            return *reinterpret_cast<T*>(address);
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            return fallback;
        }
    }"""
    if "auto safe_write(" not in text:
        text = text.replace(safe_read_anchor, safe_read_anchor + "\n" + PETERHACK_SAFE_WRITE)

    reflection_anchor = """        auto find_function(std::uintptr_t object, const char* function_name) -> std::uintptr_t
        {
            auto cls = class_ptr(object);
            for (int depth = 0; cls && depth < 64; ++depth)
            {
                for (auto child = safe_read<std::uintptr_t>(cls + OffChildren); child; child = safe_read<std::uintptr_t>(child + OffUFieldNext))
                {
                    if (object_name(child) == function_name)
                    {
                        return child;
                    }
                }
                cls = safe_read<std::uintptr_t>(cls + OffSuperStruct);
            }
            return 0;
        }"""
    reflection_replacement = REFLECTION_EXTRA_METHODS + """
        auto find_function(std::uintptr_t object, const char* function_name) -> std::uintptr_t
        {
            return find_function_in_class(class_ptr(object), function_name);
        }"""
    if "find_class_any(" not in text:
        text = text.replace(reflection_anchor, reflection_replacement)

    impl_anchor = "    auto drain_paint_jobs_on_game_thread() -> void\n    {\n        tick_mesh_first_batch_async_job();"
    if '#include "bridge_peterhack.inc"' not in text:
        text = text.replace(
            impl_anchor,
            '#include "bridge_peterhack.inc"\n\n' + impl_anchor,
        )

    forward_anchor = "    void __fastcall hooked_process_event(void* object, void* function, void* params);"
    if "struct Reflection;" not in text.split(forward_anchor)[0]:
        text = text.replace(
            forward_anchor,
            PETERHACK_FORWARD_DECLS + "\n" + forward_anchor,
        )

    old_hook = """    void __fastcall hooked_process_event(void* object, void* function, void* params)
    {
        g_active_hook_callbacks.fetch_add(1);
        const auto original = g_original_process_event.load();
        (void)object;"""

    new_hook = """    void __fastcall hooked_process_event(void* object, void* function, void* params)
    {
        g_active_hook_callbacks.fetch_add(1);
        anti_kick_capture_probe(object, function);
        anti_kick_capture_reason(function, params);
        if (anti_kick_should_block(object, function, params))
        {
            g_active_hook_callbacks.fetch_sub(1);
            return;
        }
        if (god_mode_should_block(object, function))
        {
            g_active_hook_callbacks.fetch_sub(1);
            return;
        }
        const auto original = g_original_process_event.load();
        (void)object;"""

    if old_hook not in text:
        raise RuntimeError("hooked_process_event patch anchor not found")
    text = text.replace(old_hook, new_hook)

    old_drain_tail = """        tick_mesh_first_batch_async_job();
    }

    void __fastcall hooked_process_event"""
    new_drain_tail = """        tick_mesh_first_batch_async_job();
        drain_game_commands_on_game_thread();
    }

    void __fastcall hooked_process_event"""
    if old_drain_tail not in text:
        raise RuntimeError("drain_paint_jobs patch anchor not found")
    text = text.replace(old_drain_tail, new_drain_tail)

    old_caps = (
        '            std::string commands = "[\\"ping\\",\\"capabilities\\",\\"paint_full_route\\",'
        '\\"paint_replication_probe\\",\\"paint_replication_pressure_probe\\",\\"paint_packed_replay_probe\\",'
        '\\"cancel_paint\\",\\"shutdown\\"]";'
    )
    new_caps = (
        '            std::string commands = "[\\"ping\\",\\"capabilities\\",\\"paint_full_route\\",'
        '\\"paint_replication_probe\\",\\"paint_replication_pressure_probe\\",\\"paint_packed_replay_probe\\",'
        '\\"cancel_paint\\",\\"shutdown\\",\\"teleport\\",\\"set_fov\\",\\"kill\\",\\"rotate\\",'
        '\\"set_anti_kick\\",\\"set_god_mode\\",\\"get_skeleton\\",\\"get_anti_kick_log\\",'
        '\\"set_player_name\\",\\"get_player_steam_id\\",\\"set_netconn_watch\\",\\"dump_netconn_vtable\\"]";'
    )
    text = text.replace(old_caps, new_caps)

    old_shutdown = """        if (line.find("\\"type\\":\\"shutdown\\"") != std::string::npos)
        {
            const int cancelled_active = force_cancel_active_mesh_first_batch_job("shutdown");
            const int cancelled_queued = cancel_queued_paint_jobs("shutdown");
            uninstall_process_event_hook();
            g_running.store(false);"""

    new_shutdown = """        if (line.find("\\"type\\":\\"get_anti_kick_log\\"") != std::string::npos)
        {
            return handle_get_anti_kick_log(json_extract_payload(line));
        }
        if (line.find("\\"type\\":\\"shutdown\\"") != std::string::npos)
        {
            const int cancelled_active = force_cancel_active_mesh_first_batch_job("shutdown");
            const int cancelled_queued = cancel_queued_paint_jobs("shutdown");
            g_anti_kick_enabled.store(false);
            g_god_mode_enabled.store(false);
            g_god_mode_local_pawn.store(0);
            g_god_mode_blocked_function_count.store(0);
            g_netconn_watch_enabled.store(false);
            uninstall_anti_kick_vtable_hooks();
            uninstall_process_event_inline_hook();
            uninstall_process_event_hook();
            g_running.store(false);"""

    if old_shutdown not in text:
        raise RuntimeError("shutdown patch anchor not found")
    text = text.replace(old_shutdown, new_shutdown)

    game_cmds = """
        if (line.find("\\"type\\":\\"teleport\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"set_fov\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"kill\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"set_anti_kick\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"set_god_mode\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"get_skeleton\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"set_netconn_watch\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"dump_netconn_vtable\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"set_player_name\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"get_player_steam_id\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
        if (line.find("\\"type\\":\\"rotate\\"") != std::string::npos)
        {
            return execute_game_command_on_game_thread(line);
        }
"""

    anchor = """        if (line.find("\\"type\\":\\"paint_packed_replay_probe\\"") != std::string::npos)
        {
            return paint_full_route_native(line);
        }
        return response_json(false, "unknown_command", 0, 1, "unknown bridge command");"""

    if anchor not in text:
        raise RuntimeError("handle_request game command anchor not found")
    text = text.replace(anchor, game_cmds + "\n" + anchor.split("\n", 1)[1])

    old_thread_exit = """        closesocket(listener);
        WSACleanup();"""
    new_thread_exit = """        closesocket(listener);
        WSACleanup();
        g_anti_kick_enabled.store(false);
        uninstall_anti_kick_vtable_hooks();
        uninstall_process_event_inline_hook();"""
    if old_thread_exit in text and text.count("uninstall_anti_kick_vtable_hooks();") < 2:
        text = text.replace(old_thread_exit, new_thread_exit, 1)

    old_dllmain = """    if (reason == DLL_PROCESS_ATTACH)
    {
        DisableThreadLibraryCalls(module);
        g_module = module;
    }"""
    new_dllmain = """    if (reason == DLL_PROCESS_ATTACH)
    {
        DisableThreadLibraryCalls(module);
        g_module = module;
        // Peterhack injects via LoadLibrary only (no loader Start call).
        g_running.store(true);
        g_bridge_thread_done.store(false);
        std::thread(bridge_thread).detach();
    }"""
    if old_dllmain not in text:
        if new_dllmain not in text:
            raise RuntimeError("DllMain patch anchor not found")
    else:
        text = text.replace(old_dllmain, new_dllmain)

    return text


def main() -> None:
    if not BACKUP.is_file():
        raise SystemExit(f"Legacy backup missing: {BACKUP} — restore bridge.cpp first")
    if not OFFICIAL.is_dir():
        raise SystemExit(f"Official source missing: {OFFICIAL}")

    globals_path = SRC / "bridge_peterhack_globals.inc"
    impl_path = SRC / "bridge_peterhack.inc"
    globals_path.write_text(
        extract_ranges(BACKUP, PETERHACK_GLOBAL_RANGES, "// Peterhack bridge globals.\n"),
        encoding="utf-8",
        newline="\n",
    )
    impl_path.write_text(
        extract_ranges(BACKUP, PETERHACK_IMPL_RANGES, "// Peterhack bridge implementations.\n"),
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {globals_path} ({globals_path.stat().st_size} bytes)")
    print(f"Wrote {impl_path} ({impl_path.stat().st_size} bytes)")

    for name in ("bridge.cpp", "bridge_json.inc", "bridge_sidecar.inc"):
        shutil.copy2(OFFICIAL / "bridge" / name, SRC / name)
        print(f"Copied official {name}")

    shutil.copy2(OFFICIAL / "include" / "bridge_loader_abi.hpp", INCLUDE / "bridge_loader_abi.hpp")
    shutil.copy2(OFFICIAL / "include" / "sdk.hpp", INCLUDE / "sdk.hpp")
    sdk_path = INCLUDE / "sdk.hpp"
    sdk_text = sdk_path.read_text(encoding="utf-8", errors="replace")
    if "Controller_SetControlRotation" not in sdk_text:
        sdk_text = sdk_text.replace(
            "    struct Actor_K2_GetActorLocation\n    {\n        FVector ReturnValue{};\n    };\n\n    struct KismetRenderingLibrary_CreateRenderTarget2D",
            "    struct Actor_K2_GetActorLocation\n    {\n        FVector ReturnValue{};\n    };\n\n    struct Controller_SetControlRotation\n    {\n        FRotator NewRotation{};\n    };\n\n    struct Actor_K2_GetActorRotation\n    {\n        FRotator ReturnValue{};\n    };\n\n    struct KismetRenderingLibrary_CreateRenderTarget2D",
        )
        sdk_text = sdk_text.replace(
            '    static_assert(sizeof(Actor_K2_GetActorLocation) == 0x18, "K2_GetActorLocation params layout mismatch");\n',
            '    static_assert(sizeof(Actor_K2_GetActorLocation) == 0x18, "K2_GetActorLocation params layout mismatch");\n'
            '    static_assert(sizeof(Actor_K2_GetActorRotation) == 0x18, "K2_GetActorRotation params layout mismatch");\n'
            '    static_assert(sizeof(Controller_SetControlRotation) == 0x18, "SetControlRotation params layout mismatch");\n',
        )
        sdk_path.write_text(sdk_text, encoding="utf-8", newline="\n")
    print("Merged Peterhack structs into sdk.hpp")

    bridge = patch_bridge_cpp((SRC / "bridge.cpp").read_text(encoding="utf-8", errors="replace"))
    (SRC / "bridge.cpp").write_text(bridge, encoding="utf-8", newline="\n")
    print("Patched bridge.cpp")


if __name__ == "__main__":
    main()
