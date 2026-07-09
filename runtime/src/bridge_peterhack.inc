// Peterhack bridge implementations.
    // ---- legacy bridge.cpp lines 1001-1254 ----
    auto is_kick_protect_excluded_name(const std::string& lower_name) -> bool
    {
        static const char* excluded[] = {
            "clientcapbandwidth",
            "capbandwidth",
            "bandwidth",
        };
        for (const char* name : excluded)
        {
            if (lower_name == name)
            {
                return true;
            }
        }
        return false;
    }

    auto is_kick_like_function_name(const std::string& lower_name) -> bool
    {
        if (is_kick_protect_excluded_name(lower_name))
        {
            return false;
        }
        static const char* needles[] = {
            "kick",
            "kicked",
            "ban",
            "banned",
            "removeplayer",
            "disconnect",
            "returnmainmenu",
            "returntomainmenu",
            "travelerror",
            "connectionlost",
            "hostmigration",
            "gotokick",
            "wasremoved",
            "sessionfull",
            "redpoint",
            "removefrom",
            "leaved",
            "leavegame",
            "expel",
            "eject",
            "forced",
            "eos",
            "closesocket",
        };
        for (const char* needle : needles)
        {
            if (lower_name.find(needle) != std::string::npos)
            {
                return true;
            }
        }
        return false;
    }

    auto is_connection_protect_function_name(const std::string& lower_name) -> bool
    {
        static const char* needles[] = {
            "close",
            "cleanup",
            "disconnect",
            "connectionlost",
            "destroy",
            "shutdown",
            "kick",
            "removed",
            "leave",
            "travelerror",
            "returnmainmenu",
            "returntomainmenu",
            "ban",
        };
        for (const char* needle : needles)
        {
            if (lower_name.find(needle) != std::string::npos)
            {
                return true;
            }
        }
        return false;
    }

    auto append_anti_kick_log_file(const std::string& line) -> void
    {
        static const wchar_t* paths[] = {
            L"C:\\peterhack\\logs\\anti_kick.log",
            L"C:\\peterhack\\logs\\latest_anti_kick.log",
        };
        for (const wchar_t* path : paths)
        {
            HANDLE file = CreateFileW(
                path,
                FILE_APPEND_DATA,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                nullptr,
                OPEN_ALWAYS,
                FILE_ATTRIBUTE_NORMAL,
                nullptr);
            if (file == INVALID_HANDLE_VALUE)
            {
                continue;
            }
            DWORD written = 0;
            WriteFile(file, line.data(), static_cast<DWORD>(line.size()), &written, nullptr);
            CloseHandle(file);
            return;
        }
        write_bridge_sidecar_text(L".anti_kick.log", line);
    }

    auto try_read_fstring_param_preview(void* params, char* out, std::size_t out_len) -> void
    {
        if (!params || !out || out_len == 0)
        {
            return;
        }
        out[0] = '\0';
        __try
        {
            const auto* base = reinterpret_cast<const std::uint8_t*>(params);
            const auto* data = safe_read<const wchar_t*>(reinterpret_cast<std::uintptr_t>(base));
            const auto num = safe_read<int>(reinterpret_cast<std::uintptr_t>(base + 8));
            if (!data || num <= 1 || num > 256)
            {
                return;
            }
            std::size_t written = 0;
            const int limit = std::min(num - 1, 96);
            for (int i = 0; i < limit && written + 1 < out_len; ++i)
            {
                const wchar_t ch = data[i];
                if (ch == 0)
                {
                    break;
                }
                out[written++] = static_cast<char>(ch < 128 ? ch : '?');
            }
            out[written] = '\0';
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
            if (out_len > 0)
            {
                out[0] = '\0';
            }
        }
    }

    auto anti_kick_lookup_meta(std::uintptr_t func, const char** out_name, const char** out_owner) -> bool
    {
        if (!func || !out_name || !out_owner)
        {
            return false;
        }
        std::lock_guard<std::mutex> lock(g_anti_kick_meta_mutex);
        for (std::size_t i = 0; i < g_anti_kick_func_meta_count; ++i)
        {
            if (g_anti_kick_func_meta[i].func == func)
            {
                *out_name = g_anti_kick_func_meta[i].name;
                *out_owner = g_anti_kick_func_meta[i].owner;
                return true;
            }
        }
        return false;
    }

    auto anti_kick_record_block(std::uintptr_t func, void* params,
                                const char* fallback_name = nullptr,
                                const char* fallback_owner = nullptr) -> void
    {
        const char* func_name = fallback_name ? fallback_name : "unknown";
        const char* owner = fallback_owner ? fallback_owner : "local";
        anti_kick_lookup_meta(func, &func_name, &owner);

        char detail[128]{};
        try_read_fstring_param_preview(params, detail, sizeof(detail));

        Reflection ref{};
        std::string ref_failure{};
        const char* kicker = "unknown host";
        if (ref.init(ref_failure))
        {
            kicker = cached_session_host_name(ref);
        }

        const auto now_ms = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch())
                .count());
        const auto seq = g_anti_kick_log_seq.fetch_add(1) + 1;

        {
            std::lock_guard<std::mutex> lock(g_anti_kick_log_mutex);
            auto& entry = g_anti_kick_log[g_anti_kick_log_head % AntiKickLogCapacity];
            entry.seq = seq;
            entry.timestamp_ms = now_ms;
            std::snprintf(entry.function_name, sizeof(entry.function_name), "%s", func_name);
            std::snprintf(entry.object_owner, sizeof(entry.object_owner), "%s", owner);
            std::snprintf(entry.kicker, sizeof(entry.kicker), "%s", kicker ? kicker : "unknown host");
            std::snprintf(entry.detail, sizeof(entry.detail), "%s", detail);
            g_anti_kick_log_head = (g_anti_kick_log_head + 1) % AntiKickLogCapacity;
        }

        std::string line = "[anti-kick] seq=" + std::to_string(seq) +
                           " BLOCKED kick from " + std::string(kicker ? kicker : "unknown host") +
                           " — " + func_name + " on " + owner;
        if (detail[0] != '\0')
        {
            line += " reason=\"";
            line += detail;
            line += "\"";
        }
        line += "\n";
        append_anti_kick_log_file(line);
    }

    auto scan_kick_functions_on_object(
        Reflection& ref,
        std::uintptr_t object,
        const char* owner_label,
        const std::function<void(std::uintptr_t, const char*, const char*)>& remember) -> void
    {
        if (!live_uobject(object) || !owner_label || !remember)
        {
            return;
        }
        const bool net_connection = std::string(owner_label) == "NetConnection";
        auto cls = ref.class_ptr(object);
        for (int depth = 0; cls && depth < 64; ++depth)
        {
            for (auto child = safe_read<std::uintptr_t>(cls + OffChildren); child;
                 child = safe_read<std::uintptr_t>(child + OffUFieldNext))
            {
                const auto name = ref.object_name(child);
                if (name.empty())
                {
                    continue;
                }
                const auto lower = lower_copy(name);
                const bool protect = net_connection ? is_connection_protect_function_name(lower)
                                                    : is_kick_like_function_name(lower);
                if (!protect)
                {
                    continue;
                }
                remember(child, owner_label, name.c_str());
            }
            cls = safe_read<std::uintptr_t>(cls + OffSuperStruct);
        }
    }

    // ---- legacy bridge.cpp lines 2747-3666 ----
    auto resolve_process_event_address(const std::uintptr_t sample_object) -> std::uintptr_t
    {
        if (!live_uobject(sample_object))
        {
            return 0;
        }
        const auto vtable = safe_read<std::uintptr_t>(sample_object);
        if (!vtable)
        {
            return 0;
        }
        const auto target = safe_read<std::uintptr_t>(
            vtable + static_cast<std::uintptr_t>(ProcessEventVtableIndex) * sizeof(std::uintptr_t));
        return address_in_main_module(target) ? target : 0;
    }

    auto is_executable_code(std::uintptr_t address) -> bool
    {
        if (!address)
        {
            return false;
        }
        MEMORY_BASIC_INFORMATION info{};
        if (VirtualQuery(reinterpret_cast<void*>(address), &info, sizeof(info)) != sizeof(info))
        {
            return false;
        }
        if (info.State != MEM_COMMIT)
        {
            return false;
        }
        const DWORD protect = info.Protect & 0xFF;
        return protect == PAGE_EXECUTE || protect == PAGE_EXECUTE_READ || protect == PAGE_EXECUTE_READWRITE ||
               protect == PAGE_EXECUTE_WRITECOPY;
    }

    auto resolve_absolute_jmp_target(const std::uintptr_t address) -> std::uintptr_t
    {
        const auto* bytes = reinterpret_cast<const std::uint8_t*>(address);
        if (bytes[0] == 0xFF && bytes[1] == 0x25)
        {
            const auto* slot = reinterpret_cast<const std::uintptr_t*>(address + 6);
            return safe_read<std::uintptr_t>(reinterpret_cast<std::uintptr_t>(slot));
        }
        if (bytes[0] == 0xE9)
        {
            const auto rel = safe_read<std::int32_t>(address + 1);
            return address + 5 + static_cast<std::uintptr_t>(rel);
        }
        if (bytes[0] == 0x48 && bytes[1] == 0xB8 && bytes[10] == 0xFF && bytes[11] == 0xE0)
        {
            return safe_read<std::uintptr_t>(address + 2);
        }
        return 0;
    }

    auto process_event_entry_is_hooked(const std::uintptr_t address) -> bool
    {
        if (!address)
        {
            return false;
        }
        const auto* bytes = reinterpret_cast<const std::uint8_t*>(address);
        return (bytes[0] == 0xFF && bytes[1] == 0x25) || bytes[0] == 0xE9 ||
               (bytes[0] == 0x48 && bytes[1] == 0xB8 && bytes[10] == 0xFF && bytes[11] == 0xE0);
    }

    auto game_thread_work_pending() -> bool
    {
        {
            std::lock_guard<std::mutex> lock(g_paint_jobs_mutex);
            for (const auto& job : g_paint_jobs)
            {
                if (job && !job->done)
                {
                    return true;
                }
            }
        }
        {
            std::lock_guard<std::mutex> lock(g_game_cmd_mutex);
            for (const auto& job : g_game_cmds)
            {
                if (job && !job->done)
                {
                    return true;
                }
            }
        }
        return false;
    }

    auto write_absolute_jmp64(void* from, void* to) -> bool
    {
        if (!from || !to)
        {
            return false;
        }
        std::uint8_t patch[ProcessEventInlinePatchSize]{};
        patch[0] = 0xFF;
        patch[1] = 0x25;
        patch[2] = 0x00;
        patch[3] = 0x00;
        patch[4] = 0x00;
        patch[5] = 0x00;
        std::memcpy(patch + 6, &to, sizeof(to));
        DWORD old_protect = 0;
        if (!VirtualProtect(from, ProcessEventInlinePatchSize, PAGE_EXECUTE_READWRITE, &old_protect))
        {
            return false;
        }
        std::memcpy(from, patch, ProcessEventInlinePatchSize);
        FlushInstructionCache(GetCurrentProcess(), from, ProcessEventInlinePatchSize);
        DWORD ignored = 0;
        VirtualProtect(from, ProcessEventInlinePatchSize, old_protect, &ignored);
        return true;
    }

    auto install_process_event_inline_hook(const std::uintptr_t sample_object, std::string& failure) -> bool
    {
        std::lock_guard<std::mutex> lock(g_inline_hook_mutex);
        if (g_pe_inline_hook.installed)
        {
            return true;
        }
        const auto target = resolve_process_event_address(sample_object);
        if (!target)
        {
            failure = "process_event_address_unavailable";
            return false;
        }

        const auto chained_handler = process_event_entry_is_hooked(target)
                                         ? resolve_absolute_jmp_target(target)
                                         : 0;
        if (process_event_entry_is_hooked(target))
        {
            if (!chained_handler || !is_executable_code(chained_handler))
            {
                failure = "process_event_already_hooked_unsupported";
                return false;
            }
        }
        if (chained_handler)
        {
            if (!safe_copy(g_pe_inline_hook.original, reinterpret_cast<const void*>(target), ProcessEventInlinePatchSize))
            {
                failure = "process_event_existing_hook_bytes_unreadable";
                return false;
            }
            if (!write_absolute_jmp64(reinterpret_cast<void*>(target), reinterpret_cast<void*>(&hooked_process_event)))
            {
                failure = "process_event_chain_hook_patch_failed";
                return false;
            }
            g_pe_inline_hook.target = reinterpret_cast<void*>(target);
            g_pe_inline_hook.trampoline = nullptr;
            g_pe_inline_hook.chained = true;
            g_pe_inline_hook.installed = true;
            g_original_process_event.store(chained_handler);
            return true;
        }

        auto* trampoline = VirtualAlloc(nullptr, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
        if (!trampoline)
        {
            failure = "process_event_trampoline_alloc_failed";
            return false;
        }
        if (!safe_copy(g_pe_inline_hook.original, reinterpret_cast<const void*>(target), ProcessEventInlinePatchSize))
        {
            VirtualFree(trampoline, 0, MEM_RELEASE);
            failure = "process_event_original_bytes_unreadable";
            return false;
        }
        std::memcpy(trampoline, g_pe_inline_hook.original, ProcessEventInlinePatchSize);
        const auto resume = reinterpret_cast<std::uint8_t*>(target) + ProcessEventInlinePatchSize;
        if (!write_absolute_jmp64(reinterpret_cast<std::uint8_t*>(trampoline) + ProcessEventInlinePatchSize, resume))
        {
            VirtualFree(trampoline, 0, MEM_RELEASE);
            failure = "process_event_trampoline_jmp_failed";
            return false;
        }
        if (!write_absolute_jmp64(reinterpret_cast<void*>(target), reinterpret_cast<void*>(&hooked_process_event)))
        {
            VirtualFree(trampoline, 0, MEM_RELEASE);
            failure = "process_event_inline_hook_patch_failed";
            return false;
        }
        if (!is_executable_code(reinterpret_cast<std::uintptr_t>(trampoline)))
        {
            DWORD old_protect = 0;
            if (VirtualProtect(reinterpret_cast<void*>(target), ProcessEventInlinePatchSize, PAGE_EXECUTE_READWRITE, &old_protect))
            {
                std::memcpy(reinterpret_cast<void*>(target), g_pe_inline_hook.original, ProcessEventInlinePatchSize);
                FlushInstructionCache(GetCurrentProcess(), reinterpret_cast<void*>(target), ProcessEventInlinePatchSize);
                DWORD ignored = 0;
                VirtualProtect(reinterpret_cast<void*>(target), ProcessEventInlinePatchSize, old_protect, &ignored);
            }
            VirtualFree(trampoline, 0, MEM_RELEASE);
            failure = "process_event_trampoline_invalid";
            return false;
        }
        g_pe_inline_hook.target = reinterpret_cast<void*>(target);
        g_pe_inline_hook.trampoline = trampoline;
        g_pe_inline_hook.chained = false;
        g_pe_inline_hook.installed = true;
        g_original_process_event.store(reinterpret_cast<std::uintptr_t>(trampoline));
        return true;
    }

    auto uninstall_process_event_inline_hook() -> void
    {
        std::lock_guard<std::mutex> lock(g_inline_hook_mutex);
        if (!g_pe_inline_hook.installed || !g_pe_inline_hook.target)
        {
            return;
        }
        DWORD old_protect = 0;
        if (VirtualProtect(g_pe_inline_hook.target, ProcessEventInlinePatchSize, PAGE_EXECUTE_READWRITE, &old_protect))
        {
            std::memcpy(g_pe_inline_hook.target, g_pe_inline_hook.original, ProcessEventInlinePatchSize);
            FlushInstructionCache(GetCurrentProcess(), g_pe_inline_hook.target, ProcessEventInlinePatchSize);
            DWORD ignored = 0;
            VirtualProtect(g_pe_inline_hook.target, ProcessEventInlinePatchSize, old_protect, &ignored);
        }
        if (g_pe_inline_hook.trampoline)
        {
            VirtualFree(g_pe_inline_hook.trampoline, 0, MEM_RELEASE);
        }
        g_pe_inline_hook = {};
        g_original_process_event.store(0);
    }

    auto uninstall_anti_kick_vtable_hooks() -> void
    {
        std::lock_guard<std::mutex> lock(g_anti_kick_vtable_mutex);
        for (std::size_t i = 0; i < g_anti_kick_vtable_restore_count; ++i)
        {
            auto& restore = g_anti_kick_vtable_restores[i];
            if (!restore.vtable)
            {
                continue;
            }
            void** entry = &restore.vtable[restore.slot];
            DWORD old_protect = 0;
            if (VirtualProtect(entry, sizeof(void*), PAGE_EXECUTE_READWRITE, &old_protect))
            {
                *entry = restore.original;
                FlushInstructionCache(GetCurrentProcess(), entry, sizeof(void*));
                DWORD ignored = 0;
                VirtualProtect(entry, sizeof(void*), old_protect, &ignored);
            }
        }
        g_anti_kick_vtable_restore_count = 0;
        g_anti_kick_vtable_hook_installed = false;
        if (!g_pe_inline_hook.installed)
        {
            g_original_process_event.store(0);
        }
    }

    auto patch_vtable_process_event_slot(void** vtable, void** out_original) -> bool
    {
        if (!vtable || !out_original)
        {
            return false;
        }
        void** entry = &vtable[ProcessEventVtableIndex];
        DWORD old_protect = 0;
        if (!VirtualProtect(entry, sizeof(void*), PAGE_EXECUTE_READWRITE, &old_protect))
        {
            return false;
        }
        *out_original = *entry;
        *entry = reinterpret_cast<void*>(&hooked_process_event);
        FlushInstructionCache(GetCurrentProcess(), entry, sizeof(void*));
        DWORD ignored = 0;
        VirtualProtect(entry, sizeof(void*), old_protect, &ignored);
        return *out_original != nullptr;
    }

    auto install_anti_kick_vtable_hooks(const std::uintptr_t controller,
                                        const std::uintptr_t player_state,
                                        const std::uintptr_t net_connection,
                                        std::string& failure) -> bool
    {
        std::lock_guard<std::mutex> lock(g_anti_kick_vtable_mutex);

        auto hook_object_vtable = [&](const std::uintptr_t object) -> bool {
            if (!live_uobject(object))
            {
                return false;
            }
            const auto vtable = safe_read<std::uintptr_t>(object);
            if (!vtable)
            {
                return false;
            }
            auto* vtable_slots = reinterpret_cast<void**>(vtable);
            for (std::size_t i = 0; i < g_anti_kick_vtable_restore_count; ++i)
            {
                if (g_anti_kick_vtable_restores[i].vtable == vtable_slots)
                {
                    return true;
                }
            }
            if (g_anti_kick_vtable_restore_count >= AntiKickMaxVtableHooks)
            {
                return false;
            }
            void* original = nullptr;
            if (!patch_vtable_process_event_slot(vtable_slots, &original))
            {
                return false;
            }
            if (!is_executable_code(reinterpret_cast<std::uintptr_t>(original))
                || reinterpret_cast<std::uintptr_t>(original) == static_cast<std::uintptr_t>(-1))
            {
                void** entry = &vtable_slots[ProcessEventVtableIndex];
                DWORD old_protect = 0;
                if (VirtualProtect(entry, sizeof(void*), PAGE_EXECUTE_READWRITE, &old_protect))
                {
                    *entry = original;
                    FlushInstructionCache(GetCurrentProcess(), entry, sizeof(void*));
                    DWORD ignored = 0;
                    VirtualProtect(entry, sizeof(void*), old_protect, &ignored);
                }
                return false;
            }
            auto& restore = g_anti_kick_vtable_restores[g_anti_kick_vtable_restore_count++];
            restore.vtable = vtable_slots;
            restore.slot = ProcessEventVtableIndex;
            restore.original = original;
            if (!g_original_process_event.load())
            {
                g_original_process_event.store(reinterpret_cast<std::uintptr_t>(original));
            }
            return true;
        };

        bool hooked = hook_object_vtable(controller);
        // Do not hook PlayerState/NetConnection vtables — those objects are recreated on
        // lobby/match transitions; a stale class vtable hook caused AV 0xFFFFFFFFFFFFFFFF.
        (void)player_state;
        (void)net_connection;
        if (!hooked || g_anti_kick_vtable_restore_count == 0)
        {
            failure = "anti_kick_vtable_hook_failed";
            if (!g_anti_kick_vtable_hook_installed)
            {
                uninstall_anti_kick_vtable_hooks();
            }
            return false;
        }
        g_anti_kick_vtable_hook_installed = true;
        return true;
    }

    auto anti_kick_resolve_local_player_state() -> std::uintptr_t
    {
        const auto cached = g_anti_kick_local_player_state.load();
        if (live_uobject(cached))
        {
            return cached;
        }
        const auto controller = g_anti_kick_local_controller.load();
        if (live_uobject(controller))
        {
            const auto ps = safe_read<std::uintptr_t>(controller + OffPlayerControllerPlayerState);
            if (live_uobject(ps))
            {
                return ps;
            }
        }
        return 0;
    }

    auto anti_kick_resolve_local_net_connection() -> std::uintptr_t
    {
        const auto cached = g_anti_kick_local_net_connection.load();
        if (live_uobject(cached))
        {
            return cached;
        }
        const auto local_player = g_anti_kick_local_player.load();
        if (live_uobject(local_player))
        {
            const auto conn = safe_read<std::uintptr_t>(local_player + OffUPlayerConnection);
            if (live_uobject(conn))
            {
                return conn;
            }
        }
        return 0;
    }

    auto anti_kick_is_local_player_controller(const std::uintptr_t obj) -> bool
    {
        if (!live_uobject(obj))
        {
            return false;
        }
        if (obj == g_anti_kick_local_controller.load())
        {
            return true;
        }
        if (safe_read<std::uint8_t>(obj + OffPlayerControllerIsLocal) != 0)
        {
            return true;
        }
        const auto local_player = g_anti_kick_local_player.load();
        if (live_uobject(local_player))
        {
            const auto controller = safe_read<std::uintptr_t>(local_player + 0x30);
            return controller == obj;
        }
        return false;
    }

    auto anti_kick_is_local_player_state(const std::uintptr_t obj) -> bool
    {
        if (!live_uobject(obj))
        {
            return false;
        }
        return obj == anti_kick_resolve_local_player_state();
    }

    auto anti_kick_is_local_net_connection(const std::uintptr_t obj) -> bool
    {
        if (!live_uobject(obj))
        {
            return false;
        }
        return obj == anti_kick_resolve_local_net_connection();
    }

    auto anti_kick_is_local_object(const std::uintptr_t obj) -> bool
    {
        return anti_kick_is_local_player_controller(obj)
            || anti_kick_is_local_player_state(obj)
            || anti_kick_is_local_net_connection(obj);
    }

    auto anti_kick_function_targets_local_player(const char* func_name,
                                                 const std::uintptr_t obj) -> bool
    {
        if (!func_name || !live_uobject(obj))
        {
            return false;
        }
        const std::string lower = lower_copy(func_name);
        if (lower.find("netconnection") != std::string::npos
            || lower == "close"
            || lower == "conditionalclose"
            || lower == "cleanup")
        {
            return anti_kick_is_local_net_connection(obj);
        }
        if (lower == "kick" || lower.find("playerstate") != std::string::npos)
        {
            return anti_kick_is_local_player_state(obj);
        }
        if (lower.rfind("client", 0) == 0)
        {
            return anti_kick_is_local_player_controller(obj);
        }
        return anti_kick_is_local_object(obj);
    }

    // Session/join/travel function names — used purely for auto-rejoin discovery.
    // Kept separate from the kick list so the two show up under distinct log tags.
    auto is_session_join_like_name(const std::string& lower_name) -> bool
    {
        static const char* needles[] = {
            "joinsession",
            "join",
            "rejoin",
            "reconnect",
            "session",
            "clienttravel",
            "servertravel",
            "openlevel",
            "browse",
            "invite",
            "enterlobby",
            "joinlobby",
            "findsession",
            "createsession",
        };
        for (const char* needle : needles)
        {
            if (lower_name.find(needle) != std::string::npos)
            {
                return true;
            }
        }
        return false;
    }

    // Disconnect-reason-carrying RPCs whose FString/URL param reveals WHY we were
    // dropped (application kick text vs. transport/EOS failure URL).
    auto is_disconnect_reason_function(const std::string& lower_name) -> bool
    {
        static const char* needles[] = {
            "clienttravel",
            "returntomainmenu",
            "clientwaskicked",
            "waskicked",
            "networkfailure",
            "travelfailure",
            "handlenetworkfailure",
            "notifyconnectionlost",
        };
        for (const char* needle : needles)
        {
            if (lower_name.find(needle) != std::string::npos)
            {
                return true;
            }
        }
        return false;
    }

    auto capture_register_reason_func(const std::uintptr_t func) -> void
    {
        if (!func)
        {
            return;
        }
        const auto count = g_capture_reason_func_count.load(std::memory_order_acquire);
        for (std::size_t i = 0; i < count; ++i)
        {
            if (g_capture_reason_funcs[i].load(std::memory_order_relaxed) == func)
            {
                return;  // already registered
            }
        }
        std::lock_guard<std::mutex> lock(g_capture_reason_mutex);
        const auto cur = g_capture_reason_func_count.load(std::memory_order_relaxed);
        for (std::size_t i = 0; i < cur; ++i)
        {
            if (g_capture_reason_funcs[i].load(std::memory_order_relaxed) == func)
            {
                return;
            }
        }
        if (cur >= CaptureReasonMaxFuncs)
        {
            return;
        }
        g_capture_reason_funcs[cur].store(func, std::memory_order_relaxed);
        g_capture_reason_func_count.store(cur + 1, std::memory_order_release);
    }

    // Classify each distinct UFunction pointer exactly once. Returns true only the
    // first time a given pointer is seen (caller should resolve + keyword-test it);
    // every later call is a lock-free probe that returns false so the hot path stays
    // cheap. When the table fills we stop classifying to bound cost.
    auto capture_mark_func_seen(const std::uintptr_t func) -> bool
    {
        if (g_capture_seen_func_fill.load(std::memory_order_relaxed) >= CaptureSeenTableSize - 1)
        {
            return false;
        }
        std::size_t idx = (func >> 4) & (CaptureSeenTableSize - 1);
        for (std::size_t probe = 0; probe < 64; ++probe)
        {
            const std::size_t slot = (idx + probe) & (CaptureSeenTableSize - 1);
            std::uintptr_t expected = g_capture_seen_funcs[slot].load(std::memory_order_relaxed);
            if (expected == func)
            {
                return false;  // already classified
            }
            if (expected == 0)
            {
                if (g_capture_seen_funcs[slot].compare_exchange_strong(
                        expected, func, std::memory_order_acq_rel))
                {
                    g_capture_seen_func_fill.fetch_add(1, std::memory_order_relaxed);
                    return true;  // newly inserted — classify it
                }
                if (expected == func)
                {
                    return false;  // lost race to same pointer
                }
                // lost race to a different pointer — fall through to the next slot
            }
        }
        return false;  // probe exhausted — treat as seen to bound cost
    }

    auto capture_reset_seen_table() -> void
    {
        for (std::size_t i = 0; i < CaptureSeenTableSize; ++i)
        {
            g_capture_seen_funcs[i].store(0, std::memory_order_relaxed);
        }
        g_capture_seen_func_fill.store(0, std::memory_order_relaxed);
        for (std::size_t i = 0; i < CaptureReasonMaxFuncs; ++i)
        {
            g_capture_reason_funcs[i].store(0, std::memory_order_relaxed);
        }
        g_capture_reason_func_count.store(0, std::memory_order_relaxed);
        {
            std::lock_guard<std::mutex> lock(g_capture_reason_mutex);
            g_capture_reason_seen_text.clear();
        }
    }

    // Widened capture across EVERY object: proves whether any kick RPC fires
    // anywhere (not just the 4 local objects) and discovers session/join functions
    // for auto-rejoin. Each distinct function is resolved once via capture_mark_func_seen.
    auto anti_kick_capture_probe_global(void* object, void* function) -> void
    {
        if (!g_anti_kick_capture_enabled.load() || !object || !function)
        {
            return;
        }
        const auto func = reinterpret_cast<std::uintptr_t>(function);
        if (!capture_mark_func_seen(func))
        {
            return;  // fast path: already classified this function
        }
        const auto pool = g_anti_kick_capture_names_pool.load();
        if (!pool)
        {
            return;
        }
        const auto fname_index = safe_read<std::uint32_t>(func + OffName, 0);
        if (!fname_index)
        {
            return;
        }
        FNameResolver resolver{};
        resolver.pool = pool;
        resolver.table_offset = g_anti_kick_capture_names_table_offset.load();
        resolver.style = g_anti_kick_capture_names_style.load();
        std::string fname = resolver.resolve(fname_index);
        if (fname.empty())
        {
            return;
        }
        const std::string lower = lower_copy(fname);
        const bool kickish = is_kick_like_function_name(lower);
        const bool sessionish = !kickish && is_session_join_like_name(lower);
        if (!kickish && !sessionish)
        {
            return;
        }
        const auto obj = reinterpret_cast<std::uintptr_t>(object);
        const auto cls = safe_read<std::uintptr_t>(obj + OffClass);
        const std::uint32_t cls_name_idx = cls ? safe_read<std::uint32_t>(cls + OffName, 0) : 0;
        std::string cls_name = cls_name_idx ? resolver.resolve(cls_name_idx) : std::string("?");
        if (cls_name.empty())
        {
            cls_name = "?";
        }
        const char* tag = kickish ? "capture-kick" : "capture-session";
        append_anti_kick_log_file(std::string("[anti-kick][") + tag + "] " + cls_name + "::" + fname + "\n");

        // If this RPC carries a disconnect reason (travel URL / kick text),
        // remember its pointer so the reason probe can read the param when it fires.
        if (is_disconnect_reason_function(lower))
        {
            capture_register_reason_func(func);
        }
    }

    auto anti_kick_capture_probe(void* object, void* function) -> void
    {
        if (!g_anti_kick_capture_enabled.load() || !object || !function)
        {
            return;
        }
        anti_kick_capture_probe_global(object, function);
        const auto obj = reinterpret_cast<std::uintptr_t>(object);
        const char* owner = nullptr;
        if (obj == g_anti_kick_local_controller.load())
        {
            owner = "PlayerController";
        }
        else if (obj == g_anti_kick_local_player_state.load())
        {
            owner = "PlayerState";
        }
        else if (obj == g_anti_kick_local_net_connection.load())
        {
            owner = "NetConnection";
        }
        else if (obj == g_anti_kick_local_pawn.load())
        {
            owner = "Pawn";
        }
        else
        {
            return;  // not one of our local objects — cheap pointer gate
        }

        const auto pool = g_anti_kick_capture_names_pool.load();
        if (!pool)
        {
            return;
        }
        const auto func = reinterpret_cast<std::uintptr_t>(function);
        const auto fname_index = safe_read<std::uint32_t>(func + OffName, 0);
        if (!fname_index)
        {
            return;
        }

        FNameResolver resolver{};
        resolver.pool = pool;
        resolver.table_offset = g_anti_kick_capture_names_table_offset.load();
        resolver.style = g_anti_kick_capture_names_style.load();
        std::string fname = resolver.resolve(fname_index);
        if (fname.empty())
        {
            return;
        }

        const std::string lower = lower_copy(fname);
        if (!is_kick_like_function_name(lower) && !is_session_join_like_name(lower))
        {
            return;
        }

        std::string key = std::string(owner) + "::" + fname;
        {
            std::lock_guard<std::mutex> lock(g_anti_kick_capture_mutex);
            if (g_anti_kick_capture_seen.size() > 512 || g_anti_kick_capture_seen.count(key))
            {
                return;
            }
            g_anti_kick_capture_seen.insert(key);
        }
        append_anti_kick_log_file("[anti-kick][capture] ProcessEvent " + key + "\n");
    }

    // Native diagnostic probe: when a registered disconnect-reason RPC fires, read
    // its FString URL/reason param — that string is the engine's actual close
    // reason. Runs on every ProcessEvent but is a cheap pointer scan until such an
    // RPC is discovered (i.e. right around a kick), so it costs nothing normally.
    auto anti_kick_capture_reason(void* function, void* params) -> void
    {
        if (!g_anti_kick_capture_enabled.load() || !function)
        {
            return;
        }
        const auto count = g_capture_reason_func_count.load(std::memory_order_acquire);
        if (count == 0)
        {
            return;
        }
        const auto func = reinterpret_cast<std::uintptr_t>(function);
        bool matched = false;
        for (std::size_t i = 0; i < count; ++i)
        {
            if (g_capture_reason_funcs[i].load(std::memory_order_relaxed) == func)
            {
                matched = true;
                break;
            }
        }
        if (!matched)
        {
            return;
        }

        char detail[192]{};
        try_read_fstring_param_preview(params, detail, sizeof(detail));

        std::string fname = "?";
        const auto pool = g_anti_kick_capture_names_pool.load();
        if (pool)
        {
            const auto fname_index = safe_read<std::uint32_t>(func + OffName, 0);
            if (fname_index)
            {
                FNameResolver resolver{};
                resolver.pool = pool;
                resolver.table_offset = g_anti_kick_capture_names_table_offset.load();
                resolver.style = g_anti_kick_capture_names_style.load();
                std::string resolved = resolver.resolve(fname_index);
                if (!resolved.empty())
                {
                    fname = resolved;
                }
            }
        }

        std::string reason_text = detail[0] != '\0' ? std::string(detail) : std::string("<no-fstring-param>");
        std::string dedupe_key = fname + "|" + reason_text;
        {
            std::lock_guard<std::mutex> lock(g_capture_reason_mutex);
            if (g_capture_reason_seen_text.size() > 64
                || g_capture_reason_seen_text.count(dedupe_key))
            {
                return;
            }
            g_capture_reason_seen_text.insert(dedupe_key);
        }
        append_anti_kick_log_file(
            "[anti-kick][capture-reason] " + fname + " reason='" + reason_text + "'\n");
    }

    auto anti_kick_is_global_kick_name(const char* name) -> bool
    {
        if (!name)
        {
            return false;
        }
        const std::string lower = lower_copy(name);
        // The RPCs chameleonEsp blocks unconditionally. They are kick-specific,
        // so blocking them on any object is safe — and necessary: the
        // BP_FirstPersonCharacter_LINK_C::Kick variant runs on a character
        // pawn, which the local player-state/controller filter would reject.
        return lower == "kick"
            || lower == "serverkick"
            || lower == "clientwaskicked"
            || lower == "clientreturntomainmenuwithtextreason"
            || lower == "clientreturntomainmenu";
    }

    auto anti_kick_should_block(void* object, void* function, void* params) -> bool
    {
        if (!g_anti_kick_enabled.load() || !object || !function)
        {
            return false;
        }
        const auto obj = reinterpret_cast<std::uintptr_t>(object);
        const auto func = reinterpret_cast<std::uintptr_t>(function);
        const auto count = g_anti_kick_blocked_function_count.load();
        for (std::size_t i = 0; i < count && i < AntiKickMaxBlockedFunctions; ++i)
        {
            if (g_anti_kick_blocked_functions[i].load() != func)
            {
                continue;
            }
            const char* func_name = "unknown";
            const char* owner = "local";
            anti_kick_lookup_meta(func, &func_name, &owner);
            // Known kick RPCs block regardless of which object they run on
            // (chameleonEsp behaviour). Only the heuristic scanned functions
            // keep the local-player filter, since blocking those globally
            // could swallow unrelated events aimed at other players.
            if (!anti_kick_is_global_kick_name(func_name)
                && !anti_kick_function_targets_local_player(func_name, obj))
            {
                return false;
            }
            g_anti_kick_block_count.fetch_add(1);
            anti_kick_record_block(func, params);
            return true;
        }
        // Pointer miss: the engine recreates BP UFunction objects between
        // rounds, so a recreated Kick has a new pointer but the same FName.
        // Match the function's FName comparison index against the recorded
        // kick names so blocking survives round transitions.
        const auto name_count = g_anti_kick_blocked_name_count.load();
        if (name_count > 0)
        {
            const auto name_index = safe_read<std::uint32_t>(func + OffName, 0);
            if (name_index != 0)
            {
                for (std::size_t i = 0; i < name_count && i < AntiKickMaxBlockedNames; ++i)
                {
                    if (g_anti_kick_blocked_name_indices[i].load() != name_index)
                    {
                        continue;
                    }
                    g_anti_kick_block_count.fetch_add(1);
                    anti_kick_record_block(func, params, g_anti_kick_blocked_name_texts[i], "by-name");
                    return true;
                }
            }
        }
        return false;
    }

    auto god_mode_scrub_pawn(std::uintptr_t pawn) -> void
    {
        if (!live_uobject(pawn))
        {
            return;
        }
        __try
        {
            const double max_hp = *reinterpret_cast<double*>(pawn + GodModeOffMaxHealth);
            if (max_hp > 0.0 && std::isfinite(max_hp))
            {
                *reinterpret_cast<double*>(pawn + GodModeOffHealth) = max_hp;
            }
            *reinterpret_cast<std::uint8_t*>(pawn + GodModeOffInvincible) = 1;
            *reinterpret_cast<std::uint8_t*>(pawn + GodModeOffDead) = 0;
        }
        __except (EXCEPTION_EXECUTE_HANDLER)
        {
        }
    }

    auto god_mode_should_block(void* object, void* function) -> bool
    {
        if (!g_god_mode_enabled.load() || !object || !function)
        {
            return false;
        }
        const auto pawn = g_god_mode_local_pawn.load();
        if (!pawn || reinterpret_cast<std::uintptr_t>(object) != pawn)
        {
            return false;
        }
        const auto func = reinterpret_cast<std::uintptr_t>(function);
        const auto count = g_god_mode_blocked_function_count.load();
        for (std::size_t i = 0; i < count && i < GodModeMaxBlockedFunctions; ++i)
        {
            if (g_god_mode_blocked_functions[i].load() != func)
            {
                continue;
            }
            g_god_mode_block_count.fetch_add(1);
            return true;
        }
        return false;
    }

    // ---- legacy bridge.cpp lines 5458-6084 ----
    // ---- Skeleton ESP (MechaSRC port: GetBoneName + GetSocketLocation) ---------
    struct FNameLite
    {
        std::int32_t ComparisonIndex{0};
        std::uint32_t Number{0};
    };

    struct SkelBonePairNames
    {
        const char* const* a;
        std::size_t a_count;
        const char* const* b;
        std::size_t b_count;
    };

    static const char* const kSkelAliasHead[] = {"head", "Head", "neck", "neck_01", "Neck"};
    static const char* const kSkelAliasNeck[] = {"neck", "neck_01", "Neck"};
    static const char* const kSkelAliasSpine3[] = {"spine3", "spine_03", "spine_05", "Spine2", "Spine1", "spine"};
    static const char* const kSkelAliasSpine2[] = {"spine2", "spine_02", "Spine1"};
    static const char* const kSkelAliasSpine1[] = {"spine1", "spine_01", "spine", "pelvis", "Hips"};
    static const char* const kSkelAliasPelvis[] = {"pelvis", "Hips", "spine1", "spine_01"};
    static const char* const kSkelAliasClavL[] = {"shoulder_L", "clavicle_l", "LeftShoulder"};
    static const char* const kSkelAliasUpperArmL[] = {"upper_arm_L", "upperarm_l", "LeftArm"};
    static const char* const kSkelAliasLowerArmL[] = {"lower_arm_L", "lowerarm_l", "LeftForeArm"};
    static const char* const kSkelAliasHandL[] = {"hand_L", "hand_l", "LeftHand"};
    static const char* const kSkelAliasClavR[] = {"shoulder_R", "clavicle_r", "RightShoulder"};
    static const char* const kSkelAliasUpperArmR[] = {"upper_arm_R", "upperarm_r", "RightArm"};
    static const char* const kSkelAliasLowerArmR[] = {"lower_arm_R", "lowerarm_r", "RightForeArm"};
    static const char* const kSkelAliasHandR[] = {"hand_R", "hand_r", "RightHand"};
    static const char* const kSkelAliasThighL[] = {"hip_L", "thigh_l", "LeftUpLeg"};
    static const char* const kSkelAliasCalfL[] = {"lower_leg_L", "calf_l", "LeftLeg"};
    static const char* const kSkelAliasFootL[] = {"foot_l", "foot_L", "LeftFoot"};
    static const char* const kSkelAliasThighR[] = {"hip_R", "thigh_r", "RightUpLeg"};
    static const char* const kSkelAliasCalfR[] = {"lower_leg_R", "calf_r", "RightLeg"};
    static const char* const kSkelAliasFootR[] = {"foot_r", "foot_R", "RightFoot"};

    static const SkelBonePairNames kSkelConnections[] = {
        {kSkelAliasHead, 5, kSkelAliasNeck, 3},
        {kSkelAliasNeck, 3, kSkelAliasSpine3, 6},
        {kSkelAliasSpine3, 6, kSkelAliasSpine2, 3},
        {kSkelAliasSpine2, 3, kSkelAliasSpine1, 5},
        {kSkelAliasClavL, 3, kSkelAliasSpine3, 6},
        {kSkelAliasUpperArmL, 3, kSkelAliasClavL, 3},
        {kSkelAliasLowerArmL, 3, kSkelAliasUpperArmL, 3},
        {kSkelAliasHandL, 3, kSkelAliasLowerArmL, 3},
        {kSkelAliasClavR, 3, kSkelAliasSpine3, 6},
        {kSkelAliasUpperArmR, 3, kSkelAliasClavR, 3},
        {kSkelAliasLowerArmR, 3, kSkelAliasUpperArmR, 3},
        {kSkelAliasHandR, 3, kSkelAliasLowerArmR, 3},
        {kSkelAliasThighL, 3, kSkelAliasPelvis, 4},
        {kSkelAliasThighL, 3, kSkelAliasSpine1, 5},
        {kSkelAliasCalfL, 3, kSkelAliasThighL, 3},
        {kSkelAliasFootL, 3, kSkelAliasCalfL, 3},
        {kSkelAliasThighR, 3, kSkelAliasPelvis, 4},
        {kSkelAliasThighR, 3, kSkelAliasSpine1, 5},
        {kSkelAliasCalfR, 3, kSkelAliasThighR, 3},
        {kSkelAliasFootR, 3, kSkelAliasCalfR, 3},
    };

    std::mutex g_skel_cache_mutex;
    std::unordered_map<std::uintptr_t, std::unordered_map<std::string, int>> g_skel_bone_index_cache;
    std::atomic<std::uintptr_t> g_skel_get_bone_name_fn{0};
    std::atomic<std::uintptr_t> g_skel_get_socket_location_fn{0};

    struct SkelActorEntry
    {
        std::uintptr_t actor{0};
        std::uintptr_t mesh{0};
    };

    auto skel_lower_copy(std::string value) -> std::string
    {
        for (auto& ch : value)
        {
            if (ch >= 'A' && ch <= 'Z')
            {
                ch = static_cast<char>(ch + 32);
            }
        }
        return value;
    }

    auto skel_write_int32_prop(std::uintptr_t prop, std::uint8_t* container, std::int32_t value) -> bool
    {
        const auto offset = prop_offset(prop);
        if (offset < 0)
        {
            return false;
        }
        return safe_write<std::int32_t>(reinterpret_cast<std::uintptr_t>(container + offset), value);
    }

    auto skel_read_fname_prop(std::uintptr_t prop, std::uint8_t* container, FNameLite& out) -> bool
    {
        const auto offset = prop_offset(prop);
        if (offset < 0)
        {
            return false;
        }
        out.ComparisonIndex = safe_read<std::int32_t>(reinterpret_cast<std::uintptr_t>(container + offset));
        out.Number = safe_read<std::uint32_t>(reinterpret_cast<std::uintptr_t>(container + offset + 4));
        return out.ComparisonIndex != 0;
    }

    auto skel_fname_valid(Reflection& ref, const FNameLite& name) -> bool
    {
        if (name.ComparisonIndex == 0)
        {
            return false;
        }
        const auto resolved = ref.names.resolve(static_cast<std::uint32_t>(name.ComparisonIndex));
        return !resolved.empty() && resolved != "None";
    }

    auto skel_resolve_functions(Reflection& ref) -> std::pair<std::uintptr_t, std::uintptr_t>
    {
        auto bone_fn = g_skel_get_bone_name_fn.load();
        auto socket_fn = g_skel_get_socket_location_fn.load();
        if (!bone_fn)
        {
            if (const auto cls = ref.find_class("SkinnedMeshComponent"))
            {
                bone_fn = ref.find_function_in_class(cls, "GetBoneName");
            }
            if (!bone_fn)
            {
                if (const auto cls = ref.find_class("SkeletalMeshComponent"))
                {
                    bone_fn = ref.find_function_in_class(cls, "GetBoneName");
                }
            }
            if (bone_fn)
            {
                g_skel_get_bone_name_fn.store(bone_fn);
            }
        }
        if (!socket_fn)
        {
            if (const auto cls = ref.find_class("SceneComponent"))
            {
                socket_fn = ref.find_function_in_class(cls, "GetSocketLocation");
            }
            if (socket_fn)
            {
                g_skel_get_socket_location_fn.store(socket_fn);
            }
        }
        return {bone_fn, socket_fn};
    }

    auto skel_write_fname_prop(std::uintptr_t prop, std::uint8_t* container, const FNameLite& value) -> bool
    {
        const auto offset = prop_offset(prop);
        if (offset < 0)
        {
            return false;
        }
        const auto base = reinterpret_cast<std::uintptr_t>(container + offset);
        return safe_write<std::int32_t>(base, value.ComparisonIndex) &&
               safe_write<std::uint32_t>(base + 4, value.Number);
    }

    auto skel_call_get_bone_name(Reflection& ref,
                                 std::uintptr_t mesh,
                                 std::uintptr_t function,
                                 int bone_index,
                                 FNameLite& out_name) -> bool
    {
        if (!live_uobject(mesh) || !function || bone_index < 0)
        {
            return false;
        }
        const auto params_size = safe_read<int>(function + OffPropertiesSize, 0);
        if (params_size <= 0 || params_size > 1024)
        {
            return false;
        }
        std::vector<std::uint8_t> params(static_cast<std::size_t>(params_size), 0);
        bool wrote = false;
        for (auto prop = safe_read<std::uintptr_t>(function + OffChildProperties); prop;
             prop = safe_read<std::uintptr_t>(prop + OffFFieldNext))
        {
            const auto name = skel_lower_copy(ref.names.resolve(safe_read<std::uint32_t>(prop + OffFFieldName)));
            if (name == "boneindex")
            {
                wrote = skel_write_int32_prop(prop, params.data(), bone_index) || wrote;
            }
        }
        if (!wrote)
        {
            // MechaSRC / UE layout: int32 BoneIndex @0, FName ReturnValue @8
            safe_write<std::int32_t>(reinterpret_cast<std::uintptr_t>(params.data()), bone_index);
            wrote = true;
        }
        std::string failure{};
        if (!process_event(mesh, function, params.data(), failure))
        {
            return false;
        }
        for (auto prop = safe_read<std::uintptr_t>(function + OffChildProperties); prop;
             prop = safe_read<std::uintptr_t>(prop + OffFFieldNext))
        {
            const auto name = skel_lower_copy(ref.names.resolve(safe_read<std::uint32_t>(prop + OffFFieldName)));
            if (name == "returnvalue")
            {
                if (skel_read_fname_prop(prop, params.data(), out_name) && skel_fname_valid(ref, out_name))
                {
                    return true;
                }
                break;
            }
        }
        out_name.ComparisonIndex = safe_read<std::int32_t>(reinterpret_cast<std::uintptr_t>(params.data() + 8));
        out_name.Number = safe_read<std::uint32_t>(reinterpret_cast<std::uintptr_t>(params.data() + 12));
        return skel_fname_valid(ref, out_name);
    }

    auto skel_call_get_socket_location(Reflection& ref,
                                       std::uintptr_t mesh,
                                       std::uintptr_t function,
                                       const FNameLite& socket_name,
                                       sdk::FVector& out_loc) -> bool
    {
        if (!live_uobject(mesh) || !function || !skel_fname_valid(ref, socket_name))
        {
            return false;
        }
        const auto params_size = safe_read<int>(function + OffPropertiesSize, 0);
        if (params_size <= 0 || params_size > 1024)
        {
            return false;
        }
        std::vector<std::uint8_t> params(static_cast<std::size_t>(params_size), 0);
        bool wrote = false;
        for (auto prop = safe_read<std::uintptr_t>(function + OffChildProperties); prop;
             prop = safe_read<std::uintptr_t>(prop + OffFFieldNext))
        {
            const auto name = skel_lower_copy(ref.names.resolve(safe_read<std::uint32_t>(prop + OffFFieldName)));
            if (name == "insocketname" || name == "socketname")
            {
                wrote = skel_write_fname_prop(prop, params.data(), socket_name) || wrote;
            }
        }
        if (!wrote)
        {
            // MechaSRC / UE layout: FName InSocketName @0, FVector ReturnValue @8
            safe_write<std::int32_t>(reinterpret_cast<std::uintptr_t>(params.data()),
                                     socket_name.ComparisonIndex);
            safe_write<std::uint32_t>(reinterpret_cast<std::uintptr_t>(params.data() + 4), socket_name.Number);
            wrote = true;
        }
        std::string failure{};
        if (!process_event(mesh, function, params.data(), failure))
        {
            return false;
        }
        if (sdk_read_return_vector3_param(ref, function, params.data(), out_loc))
        {
            return std::isfinite(out_loc.X) && std::isfinite(out_loc.Y) && std::isfinite(out_loc.Z);
        }
        out_loc.X = safe_read<double>(reinterpret_cast<std::uintptr_t>(params.data() + 8));
        out_loc.Y = safe_read<double>(reinterpret_cast<std::uintptr_t>(params.data() + 16));
        out_loc.Z = safe_read<double>(reinterpret_cast<std::uintptr_t>(params.data() + 24));
        return std::isfinite(out_loc.X) && std::isfinite(out_loc.Y) && std::isfinite(out_loc.Z);
    }

    auto skel_resolve_actor_mesh(std::uintptr_t actor) -> std::uintptr_t
    {
        if (!live_uobject(actor))
        {
            return 0;
        }
        for (const std::uintptr_t off : {static_cast<std::uintptr_t>(0x0418),
                                         static_cast<std::uintptr_t>(0x04C8),
                                         static_cast<std::uintptr_t>(0x0328),
                                         static_cast<std::uintptr_t>(0x02B0)})
        {
            const auto mesh = safe_read<std::uintptr_t>(actor + off);
            if (live_uobject(mesh))
            {
                return mesh;
            }
        }
        return 0;
    }

    auto skel_ensure_bone_map(Reflection& ref,
                              std::uintptr_t mesh,
                              std::uintptr_t get_bone_name_fn,
                              std::unordered_map<std::string, int>& out_map) -> bool
    {
        {
            std::lock_guard<std::mutex> lock(g_skel_cache_mutex);
            const auto it = g_skel_bone_index_cache.find(mesh);
            if (it != g_skel_bone_index_cache.end() && !it->second.empty())
            {
                out_map = it->second;
                return true;
            }
        }
        out_map.clear();
        for (int i = 0; i < 300; ++i)
        {
            FNameLite bone_name{};
            if (!skel_call_get_bone_name(ref, mesh, get_bone_name_fn, i, bone_name))
            {
                if (i > 5 && !out_map.empty())
                {
                    break;
                }
                continue;
            }
            const std::string resolved = ref.names.resolve(static_cast<std::uint32_t>(bone_name.ComparisonIndex));
            if (resolved.empty() || resolved == "None")
            {
                if (i > 5 && !out_map.empty())
                {
                    break;
                }
                continue;
            }
            out_map[skel_lower_copy(resolved)] = i;
        }
        if (out_map.empty())
        {
            return false;
        }
        std::lock_guard<std::mutex> lock(g_skel_cache_mutex);
        g_skel_bone_index_cache[mesh] = out_map;
        return true;
    }

    auto skel_find_bone_index(const std::unordered_map<std::string, int>& bone_map,
                              const char* const* aliases,
                              std::size_t alias_count) -> int
    {
        for (std::size_t i = 0; i < alias_count; ++i)
        {
            const auto it = bone_map.find(skel_lower_copy(aliases[i]));
            if (it != bone_map.end())
            {
                return it->second;
            }
        }
        return -1;
    }

    auto skel_bone_world_pos(Reflection& ref,
                             std::uintptr_t mesh,
                             std::uintptr_t get_bone_name_fn,
                             std::uintptr_t get_socket_location_fn,
                             int bone_index,
                             sdk::FVector& out_loc) -> bool
    {
        FNameLite bone_name{};
        if (!skel_call_get_bone_name(ref, mesh, get_bone_name_fn, bone_index, bone_name))
        {
            return false;
        }
        return skel_call_get_socket_location(ref, mesh, get_socket_location_fn, bone_name, out_loc);
    }

    auto skel_build_bones_json(Reflection& ref,
                               std::uintptr_t mesh,
                               std::uintptr_t get_bone_name_fn,
                               std::uintptr_t get_socket_location_fn,
                               const std::unordered_map<std::string, int>& bone_map,
                               std::string& bones_json) -> bool
    {
        struct AimBoneSpec
        {
            const char* key;
            const char* const* aliases;
            std::size_t alias_count;
        };
        static const AimBoneSpec kAimBones[] = {
            {"head", kSkelAliasHead, 5},
            {"neck_01", kSkelAliasNeck, 3},
            {"spine_03", kSkelAliasSpine3, 6},
            {"spine_02", kSkelAliasSpine2, 3},
            {"spine_01", kSkelAliasSpine1, 5},
            {"pelvis", kSkelAliasPelvis, 4},
            {"clavicle_l", kSkelAliasClavL, 3},
            {"upperarm_l", kSkelAliasUpperArmL, 3},
            {"lowerarm_l", kSkelAliasLowerArmL, 3},
            {"hand_l", kSkelAliasHandL, 3},
            {"clavicle_r", kSkelAliasClavR, 3},
            {"upperarm_r", kSkelAliasUpperArmR, 3},
            {"lowerarm_r", kSkelAliasLowerArmR, 3},
            {"hand_r", kSkelAliasHandR, 3},
            {"thigh_l", kSkelAliasThighL, 3},
            {"calf_l", kSkelAliasCalfL, 3},
            {"foot_l", kSkelAliasFootL, 3},
            {"thigh_r", kSkelAliasThighR, 3},
            {"calf_r", kSkelAliasCalfR, 3},
            {"foot_r", kSkelAliasFootR, 3},
        };
        bool any = false;
        bones_json.clear();
        for (const auto& spec : kAimBones)
        {
            const int idx = skel_find_bone_index(bone_map, spec.aliases, spec.alias_count);
            if (idx < 0)
            {
                continue;
            }
            sdk::FVector pos{};
            if (!skel_bone_world_pos(ref, mesh, get_bone_name_fn, get_socket_location_fn, idx, pos))
            {
                continue;
            }
            if (!std::isfinite(pos.X) || !std::isfinite(pos.Y) || !std::isfinite(pos.Z))
            {
                continue;
            }
            if (any)
            {
                bones_json += ",";
            }
            any = true;
            bones_json += "\"" + std::string(spec.key) + "\":[" + std::to_string(pos.X) + "," +
                          std::to_string(pos.Y) + "," + std::to_string(pos.Z) + "]";
        }
        return any;
    }

    auto skel_build_for_actor(Reflection& ref,
                              std::uintptr_t actor,
                              std::uintptr_t mesh_override,
                              std::string& segments_json,
                              std::string& bones_json,
                              bool bones_only) -> bool
    {
        const auto mesh = mesh_override ? mesh_override : skel_resolve_actor_mesh(actor);
        if (!mesh)
        {
            return false;
        }
        const auto [get_bone_name_fn, get_socket_location_fn] = skel_resolve_functions(ref);
        if (!get_bone_name_fn || !get_socket_location_fn)
        {
            return false;
        }
        std::unordered_map<std::string, int> bone_map{};
        if (!skel_ensure_bone_map(ref, mesh, get_bone_name_fn, bone_map))
        {
            return false;
        }
        bool any = false;
        segments_json.clear();
        if (!bones_only)
        {
            for (const auto& conn : kSkelConnections)
            {
                const int idx_a = skel_find_bone_index(bone_map, conn.a, conn.a_count);
                const int idx_b = skel_find_bone_index(bone_map, conn.b, conn.b_count);
                if (idx_a < 0 || idx_b < 0)
                {
                    continue;
                }
                sdk::FVector a{};
                sdk::FVector b{};
                if (!skel_bone_world_pos(ref, mesh, get_bone_name_fn, get_socket_location_fn, idx_a, a) ||
                    !skel_bone_world_pos(ref, mesh, get_bone_name_fn, get_socket_location_fn, idx_b, b))
                {
                    continue;
                }
                if (!std::isfinite(a.X) || !std::isfinite(a.Y) || !std::isfinite(a.Z) ||
                    !std::isfinite(b.X) || !std::isfinite(b.Y) || !std::isfinite(b.Z))
                {
                    continue;
                }
                if (!any)
                {
                    any = true;
                }
                else
                {
                    segments_json += ",";
                }
                segments_json += "[" + std::to_string(a.X) + "," + std::to_string(a.Y) + "," + std::to_string(a.Z) +
                                 "," + std::to_string(b.X) + "," + std::to_string(b.Y) + "," + std::to_string(b.Z) + "]";
            }
        }
        const bool bones_ok = skel_build_bones_json(ref, mesh, get_bone_name_fn, get_socket_location_fn, bone_map, bones_json);
        return any || bones_ok;
    }

    auto skel_parse_actor_entries(const std::string& payload) -> std::vector<SkelActorEntry>
    {
        std::vector<SkelActorEntry> entries{};
        const std::string actor_list = json_extract_string(payload, "actors");
        const std::string mesh_list = json_extract_string(payload, "meshes");
        if (!actor_list.empty())
        {
            std::vector<std::uintptr_t> meshes{};
            if (!mesh_list.empty())
            {
                std::size_t start = 0;
                while (start < mesh_list.size())
                {
                    const auto comma = mesh_list.find(',', start);
                    const auto token = mesh_list.substr(start, comma == std::string::npos ? std::string::npos : comma - start);
                    meshes.push_back(parse_hex_address(token));
                    if (comma == std::string::npos)
                    {
                        break;
                    }
                    start = comma + 1;
                }
            }
            std::size_t start = 0;
            std::size_t mesh_idx = 0;
            while (start < actor_list.size())
            {
                const auto comma = actor_list.find(',', start);
                const auto token = actor_list.substr(start, comma == std::string::npos ? std::string::npos : comma - start);
                const auto addr = parse_hex_address(token);
                if (addr)
                {
                    SkelActorEntry entry{};
                    entry.actor = addr;
                    if (mesh_idx < meshes.size())
                    {
                        entry.mesh = meshes[mesh_idx];
                    }
                    entries.push_back(entry);
                }
                ++mesh_idx;
                if (comma == std::string::npos)
                {
                    break;
                }
                start = comma + 1;
            }
            return entries;
        }
        const auto single = json_extract_string(payload, "actor");
        if (!single.empty())
        {
            SkelActorEntry entry{};
            entry.actor = parse_hex_address(single);
            const auto single_mesh = json_extract_string(payload, "mesh");
            if (!single_mesh.empty())
            {
                entry.mesh = parse_hex_address(single_mesh);
            }
            if (entry.actor)
            {
                entries.push_back(entry);
            }
            return entries;
        }
        const double one = json_extract_double(payload, "actor");
        if (one > 0)
        {
            SkelActorEntry entry{};
            entry.actor = static_cast<std::uintptr_t>(one);
            const double mesh_val = json_extract_double(payload, "mesh");
            if (mesh_val > 0)
            {
                entry.mesh = static_cast<std::uintptr_t>(mesh_val);
            }
            entries.push_back(entry);
        }
        return entries;
    }

    auto skel_parse_actor_list(const std::string& payload) -> std::vector<std::uintptr_t>
    {
        std::vector<std::uintptr_t> actors{};
        for (const auto& entry : skel_parse_actor_entries(payload))
        {
            if (entry.actor)
            {
                actors.push_back(entry.actor);
            }
        }
        return actors;
    }

    // RuntimePaintableComponent layout (matches Python OFF_RUNTIME_PAINTABLE / OFF_MAX_DECOY_SPAWN).
    constexpr std::uintptr_t OffPawnRuntimePaintable = 0x0B68;
    constexpr std::uintptr_t OffCompMaxDecoySpawnCount = 0x01A8;

    auto handle_game_set_decoy_num(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        int count = static_cast<int>(json_extract_double(payload, "count"));
        if (count < 0) count = 0;
        if (count > 99) count = 99;

        if (!live_uobject(ctx.pawn))
        {
            return response_json(false, "pawn_unavailable", 0, 1, "local pawn not available");
        }

        const auto comp = safe_read<std::uintptr_t>(ctx.pawn + OffPawnRuntimePaintable);
        if (!live_uobject(comp))
        {
            return response_json(false, "component_unavailable", 0, 1, "RuntimePaintableComponent not available");
        }

        // Write the cap directly, then fire the replicated RPC on the game thread
        // so the server accepts the new value. Both run under live_uobject guards.
        const bool wrote_mem = safe_write<std::int32_t>(comp + OffCompMaxDecoySpawnCount, count);

        std::string rpc_used{};
        bool rpc_ok = false;
        for (const char* fn_name : {"ServerSetMaxDecoySpawnCount", "SetMaxDecoySpawnCount"})
        {
            const auto fn = ref.find_function(comp, fn_name);
            if (!fn)
            {
                continue;
            }
            const auto params_size = safe_read<int>(fn + OffPropertiesSize, 0);
            if (params_size < 0 || params_size > 1024)
            {
                continue;
            }
            std::vector<std::uint8_t> params(static_cast<std::size_t>(std::max(4, params_size)), 0);
            bool wrote_param = false;
            for (auto prop = safe_read<std::uintptr_t>(fn + OffChildProperties); prop;
                 prop = safe_read<std::uintptr_t>(prop + OffFFieldNext))
            {
                const auto pname = ref.names.resolve(safe_read<std::uint32_t>(prop + OffFFieldName));
                if (pname == "ReturnValue")
                {
                    continue;
                }
                const auto off = prop_offset(prop);
                if (off >= 0 && static_cast<std::size_t>(off) + 4 <= params.size())
                {
                    wrote_param = safe_write<std::int32_t>(
                        reinterpret_cast<std::uintptr_t>(params.data() + off), count);
                }
                break;  // single int32 param
            }
            if (!wrote_param)
            {
                safe_write<std::int32_t>(reinterpret_cast<std::uintptr_t>(params.data()), count);
            }
            std::string failure{};
            if (process_event(comp, fn, params.data(), failure))
            {
                rpc_ok = true;
                rpc_used = fn_name;
                break;
            }
        }

        const std::string meta = "\"count\":" + std::to_string(count) +
                                 ",\"mem_write\":" + (wrote_mem ? "true" : "false") +
                                 ",\"rpc\":\"" + json_escape(rpc_used) + "\"";
        const bool ok = wrote_mem || rpc_ok;
        return response_json(ok, "set_decoy_num", ok ? 1 : 0, ok ? 0 : 1,
                             ok ? "decoy count applied" : "failed to apply decoy count", meta);
    }

    auto handle_game_get_skeleton(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        (void)ctx;
        std::string hook_failure{};
        if (!install_process_event_hook(hook_failure))
        {
            return response_json(false, "skeleton_hook_failed", 0, 1, hook_failure);
        }
        const auto entries = skel_parse_actor_entries(payload);
        if (entries.empty())
        {
            return response_json(false, "actors_unavailable", 0, 1, "no actor pointers supplied");
        }
        const std::string bones_only_text = json_extract_string(payload, "bones_only");
        const bool bones_only = bones_only_text == "true" || bones_only_text == "1";
        std::string results = "\"results\":[";
        bool first_actor = true;
        int ok_count = 0;
        for (const auto& entry : entries)
        {
            if (!first_actor)
            {
                results += ",";
            }
            first_actor = false;
            std::string segments = "[]";
            std::string bones = "{}";
            std::string bones_body{};
            const bool ok = skel_build_for_actor(ref, entry.actor, entry.mesh, segments, bones_body, bones_only);
            if (ok)
            {
                ++ok_count;
            }
            if (!bones_body.empty())
            {
                bones = "{" + bones_body + "}";
            }
            results += "{\"actor\":\"" + hex_address(entry.actor) + "\",\"mesh\":\"" + hex_address(entry.mesh) +
                       "\",\"segments\":[" + segments + "],\"bones\":" + bones + "}";
        }
        results += "]";
        const std::string meta = results + ",\"ok_count\":" + std::to_string(ok_count) +
                                 ",\"method\":\"GetBoneName+GetSocketLocation\"";
        return response_json(true, "get_skeleton", ok_count, static_cast<int>(entries.size()) - ok_count,
                             ok_count > 0 ? "skeleton data collected" : "skeleton data empty", meta);
    }

    // ---- legacy bridge.cpp lines 9338-9364 ----
    auto execute_game_command_on_game_thread(const std::string& request) -> std::string
    {
        std::string failure{};
        if (!install_process_event_hook(failure))
        {
            return response_json(false, failure.c_str(), 0, 1, failure);
        }
        auto job = std::make_shared<QueuedGameCommand>();
        job->request = request;
        {
            std::lock_guard<std::mutex> lock(g_game_cmd_mutex);
            g_game_cmds.push_back(job);
        }
        if (const auto thread_id = g_game_thread_id.load())
        {
            PostThreadMessageW(thread_id, PaintDispatchMessage, 0, 0);
        }
        std::unique_lock<std::mutex> lock(g_game_cmd_mutex);
        const bool completed = g_game_cmd_cv.wait_for(lock, std::chrono::seconds(30), [&]() {
            return job->done;
        });
        if (!completed)
        {
            return response_json(false, "game_thread_dispatch_timeout", 0, 1, "game thread did not process command");
        }
        return job->response;
    }

    // ---- legacy bridge.cpp lines 9366-9384 ----
    auto drain_game_commands_on_game_thread() -> void
    {
        std::vector<std::shared_ptr<QueuedGameCommand>> jobs{};
        {
            std::lock_guard<std::mutex> lock(g_game_cmd_mutex);
            jobs.swap(g_game_cmds);
        }
        for (const auto& job : jobs)
        {
            if (!job) continue;
            const auto response = handle_game_command_direct(job->request);
            {
                std::lock_guard<std::mutex> lock(g_game_cmd_mutex);
                job->response = response;
                job->done = true;
            }
            g_game_cmd_cv.notify_all();
        }
    }

    // ---- legacy bridge.cpp lines 9386-10722 ----
    auto handle_game_teleport(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        const double x = json_extract_double(payload, "x");
        const double y = json_extract_double(payload, "y");
        const double z = json_extract_double(payload, "z");

        if (!live_uobject(ctx.pawn))
        {
            return response_json(false, "pawn_unavailable", 0, 1, "local pawn not available");
        }

        const auto set_location_fn = ref.find_function(ctx.pawn, "K2_SetActorLocation");
        if (!set_location_fn)
        {
            return response_json(false, "function_unavailable", 0, 1, "K2_SetActorLocation not found");
        }

        sdk::Actor_K2_SetActorLocation params{};
        params.NewLocation = {x, y, z};
        params.bTeleport = true;

        std::string pe_failure{};
        if (!process_event(ctx.pawn, set_location_fn, reinterpret_cast<std::uint8_t*>(&params), pe_failure))
        {
            return response_json(false, "process_event_failed", 0, 1, pe_failure);
        }

        std::string meta = "\"target_x\":" + std::to_string(x) +
                           ",\"target_y\":" + std::to_string(y) +
                           ",\"target_z\":" + std::to_string(z) +
                           ",\"return_value\":" + (params.ReturnValue ? "true" : "false");
        return response_json(true, "teleport", 0, 0, "teleported", meta);
    }

    auto handle_game_set_fov(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        const double fov = json_extract_double(payload, "fov");
        if (fov < 1.0 || fov > 179.0)
        {
            return response_json(false, "invalid_fov", 0, 1, "FOV must be between 1 and 179");
        }

        if (!live_uobject(ctx.controller))
        {
            return response_json(false, "controller_unavailable", 0, 1, "player controller not available");
        }

        const auto camera_manager = safe_read<std::uintptr_t>(ctx.controller + sdk::FieldOffsets::PlayerController_PlayerCameraManager);
        if (!live_uobject(camera_manager))
        {
            return response_json(false, "camera_manager_unavailable", 0, 1, "PlayerCameraManager not available");
        }

        const auto set_fov_fn = ref.find_function(camera_manager, "SetFOV");
        if (set_fov_fn)
        {
            sdk_call_single_number(ref, camera_manager, "SetFOV", fov);
            return response_json(true, "set_fov", 0, 0, "FOV set via SetFOV function",
                                "\"fov\":" + std::to_string(fov) + ",\"method\":\"SetFOV\"");
        }

        const auto fov_offset = ref.resolve_property_offset("PlayerCameraManager", "FOVAngle");
        if (fov_offset >= 0)
        {
            *reinterpret_cast<float*>(camera_manager + static_cast<std::uintptr_t>(fov_offset)) = static_cast<float>(fov);
            return response_json(true, "set_fov", 0, 0, "FOV set via FOVAngle property",
                                "\"fov\":" + std::to_string(fov) + ",\"method\":\"FOVAngle\"");
        }

        const auto default_fov_offset = ref.resolve_property_offset("PlayerCameraManager", "DefaultFOV");
        if (default_fov_offset >= 0)
        {
            *reinterpret_cast<float*>(camera_manager + static_cast<std::uintptr_t>(default_fov_offset)) = static_cast<float>(fov);
            return response_json(true, "set_fov", 0, 0, "FOV set via DefaultFOV property",
                                "\"fov\":" + std::to_string(fov) + ",\"method\":\"DefaultFOV\"");
        }

        return response_json(false, "fov_method_unavailable", 0, 1,
                            "could not find SetFOV function or FOVAngle/DefaultFOV property");
    }

    auto handle_game_kill(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        if (!live_uobject(ctx.pawn))
        {
            return response_json(false, "pawn_unavailable", 0, 1, "local pawn not available");
        }

        const bool kill_enemies = json_extract_bool(payload, "enemies");

        if (kill_enemies)
        {
            return response_json(false, "not_implemented", 0, 1, "enemy kill not yet implemented via bridge");
        }

        {
            const auto destroy_fn = ref.find_function(ctx.pawn, "K2_DestroyActor");
            if (destroy_fn)
            {
                const auto params_size = safe_read<int>(destroy_fn + OffPropertiesSize, 0);
                if (params_size >= 0 && params_size <= 1024)
                {
                    std::vector<std::uint8_t> p(std::max(1, params_size), 0);
                    std::string pe_failure{};
                    if (process_event(ctx.pawn, destroy_fn, p.data(), pe_failure))
                    {
                        return response_json(true, "kill", 0, 0, "K2_DestroyActor called", "\"method\":\"K2_DestroyActor\"");
                    }
                }
            }
        }

        {
            const auto pawn_class_name = ref.class_name(ctx.pawn);
            for (const auto* prop_name : {"Health", "HP", "CurrentHealth"})
            {
                const auto offset = ref.resolve_property_offset(pawn_class_name.c_str(), prop_name);
                if (offset >= 0)
                {
                    *reinterpret_cast<float*>(ctx.pawn + static_cast<std::uintptr_t>(offset)) = 0.0f;
                    return response_json(true, "kill", 0, 0,
                                        std::string("set ") + prop_name + " to 0",
                                        "\"method\":\"property_" + std::string(prop_name) + "\"");
                }
            }
        }

        {
            const auto suicide_fn = ref.find_function(ctx.pawn, "Suicide");
            if (suicide_fn)
            {
                const auto params_size = safe_read<int>(suicide_fn + OffPropertiesSize, 0);
                if (params_size >= 0 && params_size <= 1024)
                {
                    std::vector<std::uint8_t> p(std::max(1, params_size), 0);
                    std::string pe_failure{};
                    if (process_event(ctx.pawn, suicide_fn, p.data(), pe_failure))
                    {
                        return response_json(true, "kill", 0, 0, "Suicide called", "\"method\":\"Suicide\"");
                    }
                }
            }
        }

        {
            const auto damage_fn = ref.find_function(ctx.pawn, "ReceiveDamage");
            if (damage_fn)
            {
                const auto params_size = safe_read<int>(damage_fn + OffPropertiesSize, 0);
                if (params_size > 0 && params_size <= 1024)
                {
                    std::vector<std::uint8_t> p(static_cast<std::size_t>(params_size), 0);
                    for (auto prop = safe_read<std::uintptr_t>(damage_fn + OffChildProperties); prop; prop = safe_read<std::uintptr_t>(prop + OffFFieldNext))
                    {
                        const auto name = ref.names.resolve(safe_read<std::uint32_t>(prop + OffFFieldName));
                        if (name != "ReturnValue")
                        {
                            write_number(ref, prop, p.data(), 99999.0);
                            break;
                        }
                    }
                    std::string pe_failure{};
                    if (process_event(ctx.pawn, damage_fn, p.data(), pe_failure))
                    {
                        return response_json(true, "kill", 0, 0, "ReceiveDamage called", "\"method\":\"ReceiveDamage\"");
                    }
                }
            }
        }

        {
            const auto console_fn = ref.find_function(ctx.controller, "ConsoleCommand");
            if (console_fn)
            {
                const auto params_size = safe_read<int>(console_fn + OffPropertiesSize, 0);
                if (params_size > 0 && params_size <= 4096)
                {
                    std::vector<std::uint8_t> p(static_cast<std::size_t>(params_size), 0);
                    std::vector<std::wstring> backing{};
                    for (auto prop = safe_read<std::uintptr_t>(console_fn + OffChildProperties); prop; prop = safe_read<std::uintptr_t>(prop + OffFFieldNext))
                    {
                        const auto name = ref.names.resolve(safe_read<std::uint32_t>(prop + OffFFieldName));
                        if (name == "Command" || name == "command")
                        {
                            sdk_write_fstring_param(prop, p.data(), "kill", backing);
                        }
                    }
                    std::string pe_failure{};
                    if (process_event(ctx.controller, console_fn, p.data(), pe_failure))
                    {
                        return response_json(true, "kill", 0, 0, "ConsoleCommand 'kill'", "\"method\":\"ConsoleCommand\"");
                    }
                }
            }
        }

        return response_json(false, "kill_failed", 0, 1, "all kill strategies failed");
    }

    constexpr std::uintptr_t CheatOffIsHunter = 0x0C3A;
    constexpr std::uintptr_t CheatOffDead = 0x05AA;
    constexpr std::uintptr_t CheatOffMyPlayerState = 0x808;

    auto cheat_is_leon_character(Reflection& ref, std::uintptr_t object) -> bool
    {
        if (!live_uobject(object))
        {
            return false;
        }
        const auto name = ref.class_name(object);
        return name.find("BP_FirstPersonCharacter_cLeon_Character") != std::string::npos
            && name.rfind("Default__", 0) != 0;
    }

    auto cheat_is_hunter_pawn(Reflection& ref, std::uintptr_t object) -> bool
    {
        if (!cheat_is_leon_character(ref, object))
        {
            return false;
        }
        const auto name = ref.class_name(object);
        if (name.find("Hunter") != std::string::npos)
        {
            return true;
        }
        return safe_read<std::uint8_t>(object + CheatOffIsHunter, 0) != 0;
    }

    auto cheat_is_survivor_pawn(Reflection& ref, std::uintptr_t object) -> bool
    {
        if (!cheat_is_leon_character(ref, object))
        {
            return false;
        }
        if (cheat_is_hunter_pawn(ref, object))
        {
            return false;
        }
        const auto name = ref.class_name(object);
        if (name.find("Survivor") != std::string::npos)
        {
            return true;
        }
        return safe_read<std::uint8_t>(object + CheatOffIsHunter, 1) == 0;
    }

    auto cheat_is_pawn_dead(std::uintptr_t object) -> bool
    {
        return live_uobject(object) && safe_read<std::uint8_t>(object + CheatOffDead, 0) != 0;
    }

    auto cheat_read_actor_location(Reflection& ref, std::uintptr_t actor, sdk::FVector& out, std::string& failure) -> bool
    {
        const auto get_loc_fn = ref.find_function(actor, "K2_GetActorLocation");
        if (!get_loc_fn)
        {
            return false;
        }
        const auto params_size = std::max(0x18, safe_read<int>(get_loc_fn + OffPropertiesSize, 0x18));
        std::vector<std::uint8_t> loc_params(static_cast<std::size_t>(params_size), 0);
        if (!process_event(actor, get_loc_fn, loc_params.data(), failure)
            || params_size < static_cast<int>(sizeof(sdk::FVector)))
        {
            return false;
        }
        out = *reinterpret_cast<const sdk::FVector*>(loc_params.data());
        return true;
    }

    auto cheat_read_actor_forward(Reflection& ref, std::uintptr_t actor, sdk::FVector& out, std::string& failure) -> bool
    {
        const auto forward_fn = ref.find_function(actor, "GetActorForwardVector");
        if (forward_fn)
        {
            struct ForwardParams
            {
                sdk::FVector ReturnValue{};
            } params{};
            if (process_event(actor, forward_fn, reinterpret_cast<std::uint8_t*>(&params), failure))
            {
                out = params.ReturnValue;
                const double len = std::sqrt(out.X * out.X + out.Y * out.Y + out.Z * out.Z);
                if (len > 1e-6)
                {
                    out.X /= len;
                    out.Y /= len;
                    out.Z /= len;
                    return true;
                }
            }
        }

        const auto get_rot_fn = ref.find_function(actor, "K2_GetActorRotation");
        if (!get_rot_fn)
        {
            return false;
        }
        sdk::Actor_K2_GetActorRotation rot_params{};
        if (!process_event(actor, get_rot_fn, reinterpret_cast<std::uint8_t*>(&rot_params), failure))
        {
            return false;
        }
        constexpr double kDegToRad = 3.14159265358979323846 / 180.0;
        const double pitch = rot_params.ReturnValue.Pitch * kDegToRad;
        const double yaw = rot_params.ReturnValue.Yaw * kDegToRad;
        const double cp = std::cos(pitch);
        out.X = std::cos(yaw) * cp;
        out.Y = std::sin(yaw) * cp;
        out.Z = std::sin(pitch);
        return true;
    }

    auto cheat_resolve_hunter_player_state(Reflection& ref, const SdkContext& ctx, std::uintptr_t hunter) -> std::uintptr_t
    {
        std::uintptr_t source_ps = safe_read<std::uintptr_t>(hunter + CheatOffMyPlayerState);
        if (live_uobject(source_ps))
        {
            return source_ps;
        }
        if (live_uobject(ctx.controller))
        {
            source_ps = safe_read<std::uintptr_t>(ctx.controller + OffPlayerControllerPlayerState);
            if (live_uobject(source_ps))
            {
                return source_ps;
            }
        }
        return 0;
    }

    auto cheat_kill_survivor_internal(Reflection& ref,
                                      const SdkContext& ctx,
                                      std::uintptr_t target_pawn,
                                      const sdk::FVector* endpoint,
                                      std::string& method_out,
                                      std::string& failure_out) -> bool
    {
        method_out.clear();
        failure_out.clear();
        if (!live_uobject(ctx.pawn) || !live_uobject(target_pawn))
        {
            failure_out = "hunter or target pawn unavailable";
            return false;
        }
        if (!cheat_is_hunter_pawn(ref, ctx.pawn))
        {
            failure_out = "local pawn is not hunter";
            return false;
        }
        if (!cheat_is_survivor_pawn(ref, target_pawn))
        {
            failure_out = "target is not a survivor";
            return false;
        }
        if (cheat_is_pawn_dead(target_pawn))
        {
            failure_out = "target already dead";
            return false;
        }

        const auto source_ps = cheat_resolve_hunter_player_state(ref, ctx, ctx.pawn);
        sdk::FVector endpoint_local{};
        const sdk::FVector* endpoint_ptr = endpoint;
        if (!endpoint_ptr)
        {
            if (!cheat_read_actor_location(ref, target_pawn, endpoint_local, failure_out))
            {
                failure_out = failure_out.empty() ? "could not read target location" : failure_out;
            }
            else
            {
                endpoint_ptr = &endpoint_local;
            }
        }

        const auto hunter_cls = ref.find_class_any("BP_FirstPersonCharacter_cLeon_Character_Hunter_C");
        std::uintptr_t kill_fn = hunter_cls ? ref.find_function_in_class(hunter_cls, "KillPlayer") : 0;
        if (!kill_fn)
        {
            kill_fn = ref.find_function(ctx.pawn, "KillPlayer");
        }
        if (kill_fn)
        {
            const auto params_size = std::max(0x10, safe_read<int>(kill_fn + OffPropertiesSize, 0x10));
            std::vector<std::uint8_t> params(static_cast<std::size_t>(params_size), 0);
            *reinterpret_cast<std::uintptr_t*>(params.data()) = target_pawn;
            if (params_size >= 16)
            {
                *reinterpret_cast<std::uintptr_t*>(params.data() + 8) = source_ps;
            }
            failure_out.clear();
            if (process_event(ctx.pawn, kill_fn, params.data(), failure_out))
            {
                method_out = "KillPlayer";
                return true;
            }
        }

        if (endpoint_ptr)
        {
            for (const char* shot_name : {"SpawnShotEffect(Server)", "SpawnShotEffect_Server_"})
            {
                const auto shot_fn = ref.find_function(ctx.pawn, shot_name);
                if (!shot_fn)
                {
                    continue;
                }
                const auto params_size = std::max(0x40, safe_read<int>(shot_fn + OffPropertiesSize, 0x40));
                std::vector<std::uint8_t> params(static_cast<std::size_t>(params_size), 0);
                if (params_size >= static_cast<int>(sizeof(sdk::FVector)))
                {
                    *reinterpret_cast<sdk::FVector*>(params.data()) = *endpoint_ptr;
                }
                if (params_size > 0x18)
                {
                    params[0x18] = 1;
                }
                failure_out.clear();
                if (process_event(ctx.pawn, shot_fn, params.data(), failure_out))
                {
                    method_out = shot_name;
                    return true;
                }
            }
        }

        if (live_uobject(ctx.world))
        {
            const auto game_mode = safe_read<std::uintptr_t>(ctx.world + 0x1A8);
            if (live_uobject(game_mode))
            {
                const auto gm_kill_fn = ref.find_function(game_mode, "KillPlayer");
                if (gm_kill_fn)
                {
                    const auto params_size = std::max(0x10, safe_read<int>(gm_kill_fn + OffPropertiesSize, 0x10));
                    std::vector<std::uint8_t> params(static_cast<std::size_t>(params_size), 0);
                    *reinterpret_cast<std::uintptr_t*>(params.data()) = target_pawn;
                    if (params_size >= 16)
                    {
                        *reinterpret_cast<std::uintptr_t*>(params.data() + 8) = source_ps;
                    }
                    failure_out.clear();
                    if (process_event(game_mode, gm_kill_fn, params.data(), failure_out))
                    {
                        method_out = "KillPlayer(gamemode)";
                        return true;
                    }
                }
            }
        }

        if (failure_out.empty())
        {
            failure_out = "KillPlayer/SpawnShotEffect unavailable or rejected";
        }
        return false;
    }

    auto handle_game_kill_survivor(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        const auto target_pawn = static_cast<std::uintptr_t>(json_extract_double(payload, "target_pawn"));
        if (!live_uobject(ctx.pawn))
        {
            return response_json(false, "pawn_unavailable", 0, 1, "local hunter pawn not available");
        }
        if (!live_uobject(target_pawn))
        {
            return response_json(false, "target_unavailable", 0, 1, "target survivor pawn invalid");
        }

        sdk::FVector endpoint{};
        const sdk::FVector* endpoint_ptr = nullptr;
        const bool endpoint_supplied =
            payload.find("\"target_x\"") != std::string::npos ||
            payload.find("\"target_y\"") != std::string::npos ||
            payload.find("\"target_z\"") != std::string::npos;
        if (endpoint_supplied)
        {
            endpoint.X = json_extract_double(payload, "target_x");
            endpoint.Y = json_extract_double(payload, "target_y");
            endpoint.Z = json_extract_double(payload, "target_z");
            endpoint_ptr = &endpoint;
        }

        std::string method{};
        std::string failure{};
        if (!cheat_kill_survivor_internal(ref, ctx, target_pawn, endpoint_ptr, method, failure))
        {
            return response_json(false, "kill_survivor_failed", 0, 1, failure);
        }

        std::string meta = "\"method\":\"" + json_escape(method) + "\"";
        meta += ",\"target_pawn\":\"" + hex_address(target_pawn) + "\"";
        return response_json(true, "kill_survivor", 1, 0, "kill sent", meta);
    }

    auto handle_game_magnet_tick(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        if (!json_extract_bool(payload, "active"))
        {
            return response_json(true, "magnet_tick", 0, 0, "magnet inactive", "\"active\":false");
        }
        if (!live_uobject(ctx.pawn))
        {
            return response_json(false, "pawn_unavailable", 0, 1, "local hunter pawn not available");
        }
        if (!cheat_is_hunter_pawn(ref, ctx.pawn))
        {
            return response_json(false, "not_hunter", 0, 1, "magnet requires hunter pawn");
        }

        std::string failure{};
        sdk::FVector my_location{};
        if (!cheat_read_actor_location(ref, ctx.pawn, my_location, failure))
        {
            return response_json(false, "location_unavailable", 0, 1,
                                failure.empty() ? "K2_GetActorLocation failed" : failure);
        }

        sdk::FVector forward{};
        if (!cheat_read_actor_forward(ref, ctx.pawn, forward, failure))
        {
            return response_json(false, "forward_unavailable", 0, 1,
                                failure.empty() ? "GetActorForwardVector failed" : failure);
        }

        const auto set_location_fn = ref.find_function(ctx.pawn, "K2_SetActorLocation");
        if (!set_location_fn)
        {
            return response_json(false, "function_unavailable", 0, 1, "K2_SetActorLocation not found");
        }

        int depth_index = 0;
        int survivors = 0;
        int moved = 0;
        ref.for_each_object([&](std::uintptr_t object) -> bool {
            if (!live_uobject(object) || object == ctx.pawn)
            {
                return false;
            }
            if (!cheat_is_survivor_pawn(ref, object))
            {
                return false;
            }
            if (cheat_is_pawn_dead(object))
            {
                return false;
            }
            ++survivors;
            const float depth_spread = static_cast<float>(depth_index) * 120.0f;
            const float distance = 150.0f + depth_spread;
            sdk::Actor_K2_SetActorLocation params{};
            params.NewLocation = {
                my_location.X + forward.X * distance,
                my_location.Y + forward.Y * distance,
                my_location.Z + forward.Z * distance,
            };
            params.bTeleport = true;
            std::string pe_failure{};
            if (process_event(object, set_location_fn, reinterpret_cast<std::uint8_t*>(&params), pe_failure))
            {
                ++moved;
            }
            ++depth_index;
            return false;
        });

        std::string meta = "\"survivors\":" + std::to_string(survivors) +
                           ",\"moved\":" + std::to_string(moved) +
                           ",\"depth_used\":" + std::to_string(depth_index);
        return response_json(true, "magnet_tick", moved, 0, "magnet applied", meta);
    }

    auto handle_game_kill_all_survivors(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        if (!live_uobject(ctx.pawn))
        {
            return response_json(false, "pawn_unavailable", 0, 1, "local hunter pawn not available");
        }
        if (!cheat_is_hunter_pawn(ref, ctx.pawn))
        {
            return response_json(false, "not_hunter", 0, 1, "kill-all requires hunter pawn");
        }

        const bool seed = json_extract_bool(payload, "seed");
        std::lock_guard<std::mutex> lock(g_kill_all_mutex);
        if (seed)
        {
            g_kill_all_queue.clear();
            ref.for_each_object([&](std::uintptr_t object) -> bool {
                if (!live_uobject(object) || object == ctx.pawn)
                {
                    return false;
                }
                if (!cheat_is_survivor_pawn(ref, object) || cheat_is_pawn_dead(object))
                {
                    return false;
                }
                g_kill_all_queue.push_back(object);
                return false;
            });
            std::string meta = "\"seeded\":" + std::to_string(g_kill_all_queue.size()) +
                               ",\"remaining\":" + std::to_string(g_kill_all_queue.size());
            return response_json(true, "kill_all_survivors", 0, 0, "kill-all queue seeded", meta);
        }

        while (!g_kill_all_queue.empty())
        {
            const auto target = g_kill_all_queue.front();
            if (!live_uobject(target) || !cheat_is_survivor_pawn(ref, target) || cheat_is_pawn_dead(target))
            {
                g_kill_all_queue.erase(g_kill_all_queue.begin());
                continue;
            }

            std::string method{};
            std::string failure{};
            const bool ok = cheat_kill_survivor_internal(ref, ctx, target, nullptr, method, failure);
            g_kill_all_queue.erase(g_kill_all_queue.begin());
            std::string meta = "\"killed_this_tick\":" + std::string(ok ? "1" : "0") +
                               ",\"remaining\":" + std::to_string(g_kill_all_queue.size()) +
                               ",\"method\":\"" + json_escape(method) + "\"";
            if (!ok)
            {
                meta += ",\"failure\":\"" + json_escape(failure) + "\"";
            }
            return response_json(ok, ok ? "kill_all_survivors" : "kill_all_survivors_failed",
                                 ok ? 1 : 0, ok ? 0 : 1,
                                 ok ? "killed one survivor" : failure, meta);
        }

        return response_json(true, "kill_all_survivors", 0, 0, "kill-all queue empty",
                             "\"killed_this_tick\":0,\"remaining\":0");
    }

    // SEH-safe hex dump of a memory window (byte-by-byte, never faults).
    auto netconn_hex_window(std::uintptr_t addr, std::size_t count) -> std::string
    {
        std::string out;
        out.reserve(count * 2);
        char buf[3];
        for (std::size_t i = 0; i < count; ++i)
        {
            const std::uint8_t b = safe_read<std::uint8_t>(addr + i, 0);
            std::snprintf(buf, sizeof(buf), "%02x", b);
            out += buf;
        }
        return out;
    }

    auto netconn_resolve_from_context(const SdkContext& ctx) -> std::uintptr_t
    {
        std::uintptr_t conn = 0;
        if (live_uobject(ctx.local_player))
        {
            conn = safe_read<std::uintptr_t>(ctx.local_player + OffUPlayerConnection);
        }
        if (!live_uobject(conn))
        {
            conn = anti_kick_resolve_local_net_connection();
        }
        return conn;
    }

    auto netconn_watch_loop() -> void
    {
        g_netconn_watch_thread_alive.store(true);
        const auto conn = g_netconn_watch_conn.load();
        const auto local_player = g_netconn_watch_local_player.load();
        const auto start = GetTickCount64();

        // Baseline: connection pointer, its vtable, and a memory window that
        // should span EConnectionState + close/disconnect bookkeeping fields.
        constexpr std::size_t WindowBytes = 0x80;
        const auto baseline_vtable = safe_read<std::uintptr_t>(conn);
        append_anti_kick_log_file(
            "[netconn-watch] armed conn=" + hex_address(conn) +
            " local_player=" + hex_address(local_player) +
            " vtable=" + hex_address(baseline_vtable) +
            " window[0x00..0x80]=" + netconn_hex_window(conn, WindowBytes) + "\n");

        bool reported = false;
        while (g_netconn_watch_enabled.load())
        {
            Sleep(100);
            const auto elapsed = GetTickCount64() - start;

            // The local player's Connection pointer being nulled/swapped is the
            // cleanest "you were disconnected" signal.
            std::uintptr_t cur_conn = conn;
            if (live_uobject(local_player))
            {
                cur_conn = safe_read<std::uintptr_t>(local_player + OffUPlayerConnection);
            }

            const bool ptr_changed = live_uobject(local_player) && cur_conn != conn;
            const bool obj_invalid = !live_uobject(conn);

            if (ptr_changed || obj_invalid)
            {
                std::string reason = ptr_changed ? "local_player.Connection changed"
                                                 : "connection object became invalid";
                append_anti_kick_log_file(
                    "[netconn-watch] CLOSED after +" + std::to_string(elapsed) + "ms — " + reason +
                    " old_conn=" + hex_address(conn) +
                    " new_conn=" + hex_address(cur_conn) +
                    " vtable_now=" + hex_address(safe_read<std::uintptr_t>(conn)) +
                    " window[0x00..0x80]=" + netconn_hex_window(conn, WindowBytes) + "\n");
                reported = true;
                break;
            }
        }
        if (!reported)
        {
            append_anti_kick_log_file("[netconn-watch] stopped (no close observed)\n");
        }
        g_netconn_watch_thread_alive.store(false);
    }

    auto handle_game_set_netconn_watch(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        (void)ref;
        const bool enabled = json_extract_bool(payload, "enabled");
        if (!enabled)
        {
            g_netconn_watch_enabled.store(false);
            return response_json(true, "set_netconn_watch", 0, 0, "netconn watch disabled",
                                 "\"enabled\":false");
        }
        if (g_netconn_watch_thread_alive.load())
        {
            return response_json(true, "set_netconn_watch", 0, 0, "netconn watch already running",
                                 "\"enabled\":true,\"already_running\":true");
        }
        const auto conn = netconn_resolve_from_context(ctx);
        if (!live_uobject(conn))
        {
            return response_json(false, "net_connection_unavailable", 0, 1,
                                 "local NetConnection not resolvable — join a match first");
        }
        g_netconn_watch_conn.store(conn);
        g_netconn_watch_local_player.store(ctx.local_player);
        g_netconn_watch_enabled.store(true);
        std::thread(netconn_watch_loop).detach();
        return response_json(true, "set_netconn_watch", 0, 0, "netconn watch armed",
                             "\"enabled\":true,\"net_connection\":\"" + hex_address(conn) + "\"");
    }

    auto handle_game_dump_netconn_vtable(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        (void)ref;
        const std::size_t slots = std::min<std::size_t>(
            static_cast<std::size_t>(std::max(0.0, json_extract_double(payload, "slots"))), 256);
        const std::size_t slot_count = slots ? slots : 128;
        const auto conn = netconn_resolve_from_context(ctx);
        if (!live_uobject(conn))
        {
            return response_json(false, "net_connection_unavailable", 0, 1,
                                 "local NetConnection not resolvable — join a match first");
        }
        const auto vtable = safe_read<std::uintptr_t>(conn);
        if (!vtable)
        {
            return response_json(false, "vtable_unavailable", 0, 1, "NetConnection vtable pointer null");
        }
        const auto module = main_module_range();
        const auto cls = safe_read<std::uintptr_t>(conn + OffClass);
        const std::uint32_t cls_name_idx = cls ? safe_read<std::uint32_t>(cls + OffName, 0) : 0;
        const std::string cls_name = cls_name_idx ? ref.names.resolve(cls_name_idx) : std::string("?");
        append_anti_kick_log_file(
            "[netconn-vtable] class=" + cls_name +
            " conn=" + hex_address(conn) +
            " vtable=" + hex_address(vtable) +
            " module_base=" + hex_address(module.base) +
            " slots=" + std::to_string(slot_count) + "\n");
        std::size_t in_module = 0;
        for (std::size_t i = 0; i < slot_count; ++i)
        {
            const auto fn = safe_read<std::uintptr_t>(vtable + i * sizeof(std::uintptr_t));
            if (!fn)
            {
                continue;
            }
            const bool in_mod = address_in_main_module(fn);
            std::string line = "[netconn-vtable] slot " + std::to_string(i) +
                               " (0x" + early_hex_address(i * sizeof(std::uintptr_t)).substr(2) + ")" +
                               " = " + hex_address(fn);
            if (in_mod)
            {
                ++in_module;
                line += " rva=" + hex_address(fn - module.base);
            }
            else
            {
                line += " (off-module)";
            }
            append_anti_kick_log_file(line + "\n");
        }
        const std::string meta =
            "\"class\":\"" + json_escape(cls_name) + "\"" +
            ",\"net_connection\":\"" + hex_address(conn) + "\"" +
            ",\"vtable\":\"" + hex_address(vtable) + "\"" +
            ",\"module_base\":\"" + hex_address(module.base) + "\"" +
            ",\"slots\":" + std::to_string(slot_count) +
            ",\"in_module\":" + std::to_string(in_module);
        return response_json(true, "dump_netconn_vtable", 0, 0,
                             "vtable dumped to anti_kick.log", meta);
    }

    auto handle_game_set_anti_kick(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        const bool enabled = json_extract_bool(payload, "enabled");
        if (!enabled)
        {
            g_anti_kick_enabled.store(false);
            g_anti_kick_capture_enabled.store(false);
            g_anti_kick_local_controller.store(0);
            g_anti_kick_local_player.store(0);
            g_anti_kick_local_player_state.store(0);
            g_anti_kick_local_net_connection.store(0);
            g_anti_kick_local_pawn.store(0);
            {
                std::lock_guard<std::mutex> lock(g_anti_kick_capture_mutex);
                g_anti_kick_capture_seen.clear();
            }
            capture_reset_seen_table();
            g_anti_kick_blocked_function_count.store(0);
            g_anti_kick_last_logged_blocked_count = static_cast<std::size_t>(-1);
            g_anti_kick_last_logged_net_connection = 0;
            g_anti_kick_last_logged_no_player_state = false;
            g_anti_kick_last_logged_no_net_connection = false;
            for (std::size_t i = 0; i < AntiKickMaxBlockedFunctions; ++i)
            {
                g_anti_kick_blocked_functions[i].store(0);
            }
            g_anti_kick_blocked_name_count.store(0);
            for (std::size_t i = 0; i < AntiKickMaxBlockedNames; ++i)
            {
                g_anti_kick_blocked_name_indices[i].store(0);
            }
            g_anti_kick_session = {};
            {
                std::lock_guard<std::mutex> lock(g_anti_kick_host_name_mutex);
                g_anti_kick_host_name[0] = '\0';
                g_anti_kick_host_name_ms = 0;
            }
            {
                std::lock_guard<std::mutex> lock(g_anti_kick_meta_mutex);
                g_anti_kick_func_meta_count = 0;
            }
            uninstall_anti_kick_vtable_hooks();
            append_anti_kick_log_file("[anti-kick] disabled\n");
            return response_json(true, "set_anti_kick", 0, 0, "anti-kick disabled",
                                "\"enabled\":false,\"blocked_functions\":0");
        }

        if (!live_uobject(ctx.controller))
        {
            return response_json(false, "controller_unavailable", 0, 1, "local PlayerController not available");
        }

        g_anti_kick_session.world = ctx.world;
        g_anti_kick_session.gamestate_off = ref.resolve_property_offset("World", "GameState");
        g_anti_kick_session.playerarray_off = ref.resolve_property_offset("GameStateBase", "PlayerArray");
        for (const char* prop_name :
             {"HostPlayerState", "SessionOwnerPlayerState", "HostingPlayerState", "OwnerPlayerState"})
        {
            const int off = ref.resolve_property_offset("GameState", prop_name);
            if (off > 0)
            {
                g_anti_kick_session.host_ps_direct_off = off;
                break;
            }
        }
        for (const char* prop_name :
             {"bIsSessionHost", "bIsHost", "bIsHosting", "bIsSessionOwner", "bIsLobbyHost", "bIsHostPlayer"})
        {
            const int off = ref.resolve_property_offset("PlayerState", prop_name);
            if (off > 0)
            {
                g_anti_kick_session.host_flag_off = off;
                break;
            }
        }
        {
            std::lock_guard<std::mutex> lock(g_anti_kick_host_name_mutex);
            g_anti_kick_host_name[0] = '\0';
            g_anti_kick_host_name_ms = 0;
        }

        std::uintptr_t blocked[AntiKickMaxBlockedFunctions]{};
        std::size_t blocked_count = 0;
        {
            std::lock_guard<std::mutex> lock(g_anti_kick_meta_mutex);
            g_anti_kick_func_meta_count = 0;
        }

        auto remember_blocked = [&](std::uintptr_t function, const char* owner, const char* name) {
            if (!function || blocked_count >= AntiKickMaxBlockedFunctions)
            {
                return;
            }
            for (std::size_t i = 0; i < blocked_count; ++i)
            {
                if (blocked[i] == function)
                {
                    return;
                }
            }
            blocked[blocked_count++] = function;
            std::lock_guard<std::mutex> lock(g_anti_kick_meta_mutex);
            if (g_anti_kick_func_meta_count < AntiKickMaxBlockedFunctions)
            {
                auto& meta = g_anti_kick_func_meta[g_anti_kick_func_meta_count++];
                meta.func = function;
                std::snprintf(meta.name, sizeof(meta.name), "%s", name ? name : "?");
                std::snprintf(meta.owner, sizeof(meta.owner), "%s", owner ? owner : "local");
            }
        };

        auto add_named = [&](std::uintptr_t object, const char* owner, const char* function_name) {
            if (!live_uobject(object))
            {
                return;
            }
            const auto function = ref.find_function(object, function_name);
            if (function)
            {
                remember_blocked(function, owner, function_name);
            }
        };

        auto add_class_named = [&](const char* class_name_text, const char* owner, const char* function_name) {
            const auto cls = ref.find_class_any(class_name_text);
            if (!cls)
            {
                append_anti_kick_log_file(std::string("[anti-kick] class not found: ") + class_name_text + "\n");
                return;
            }
            const auto function = ref.find_function_in_class(cls, function_name);
            if (function)
            {
                remember_blocked(function, owner, function_name);
            }
            else
            {
                append_anti_kick_log_file(std::string("[anti-kick] function not found: ") + class_name_text + "." + function_name + "\n");
            }
        };

        add_named(ctx.controller, "PlayerController", "ClientWasKicked");
        add_named(ctx.controller, "PlayerController", "ClientReturnToMainMenuWithTextReason");
        add_named(ctx.controller, "PlayerController", "ClientReturnToMainMenu");

        // The kick RPCs this game actually uses (ported from chameleonEsp):
        // Kick on the online PlayerState and on the LINK character. Resolve
        // them from the classes themselves — instance-based lookup misses the
        // LINK variant entirely, and the local PlayerState pointer can be
        // stale/null at enable time.
        add_class_named("BP_FirstPersonPlayerState_Online_C", "PlayerStateOnline", "Kick");
        add_class_named("BP_FirstPersonCharacter_LINK_C", "CharacterLINK", "Kick");

        std::uintptr_t player_state = safe_read<std::uintptr_t>(ctx.controller + OffPlayerControllerPlayerState);
        if (!live_uobject(player_state) && live_uobject(ctx.pawn))
        {
            player_state = safe_read<std::uintptr_t>(ctx.pawn + OffPawnPlayerState);
        }
        if (!live_uobject(player_state))
        {
            player_state = g_anti_kick_local_player_state.load();
        }

        std::uintptr_t net_connection = 0;
        if (live_uobject(ctx.local_player))
        {
            net_connection = safe_read<std::uintptr_t>(ctx.local_player + OffUPlayerConnection);
        }
        if (!live_uobject(net_connection))
        {
            net_connection = g_anti_kick_local_net_connection.load();
        }

        const std::function<void(std::uintptr_t, const char*, const char*)> remember_fn = remember_blocked;
        scan_kick_functions_on_object(ref, ctx.controller, "PlayerController", remember_fn);
        // Also register PlayerState / NetConnection kick+close RPCs. These are
        // caught by the global inline ProcessEvent hook (installed below), which
        // sees calls on every object, so they no longer depend on the controller
        // vtable. add_named resolves the well-known ones explicitly; the scan
        // picks up any class-specific variants.
        if (live_uobject(player_state))
        {
            add_named(player_state, "PlayerState", "Kick");
            add_named(player_state, "PlayerState", "ServerKick");
            scan_kick_functions_on_object(ref, player_state, "PlayerState", remember_fn);
        }
        if (live_uobject(net_connection))
        {
            add_named(net_connection, "NetConnection", "Close");
            add_named(net_connection, "NetConnection", "ConditionalCleanUp");
            add_named(net_connection, "NetConnection", "CleanUp");
            scan_kick_functions_on_object(ref, net_connection, "NetConnection", remember_fn);
        }

        if (blocked_count == 0)
        {
            return response_json(false, "kick_functions_unavailable", 0, 1,
                                "could not resolve kick RPC functions on local controller/player state");
        }

        if (!live_uobject(ctx.controller))
        {
            return response_json(false, "controller_unavailable", 0, 1, "local PlayerController not available");
        }

        std::string hook_failure{};
        if (!g_anti_kick_vtable_hook_installed)
        {
            if (!install_anti_kick_vtable_hooks(ctx.controller, player_state, net_connection, hook_failure))
            {
                return response_json(false, "anti_kick_hook_failed", 0, 1, hook_failure);
            }
        }
        if (!g_anti_kick_vtable_hook_installed)
        {
            return response_json(false, "anti_kick_hook_failed", 0, 1, "anti_kick_vtable_hook_missing");
        }
        // Install the global inline ProcessEvent hook too. The controller vtable
        // hook only sees RPCs dispatched on the local controller; kick paths that
        // run on PlayerState or NetConnection (e.g. host-initiated Kick / Close)
        // only pass through the engine-wide ProcessEvent. This is the same hook
        // paint uses, so it is already crash-tested on this build.
        std::string pe_hook_failure{};
        if (!install_process_event_hook(pe_hook_failure))
        {
            // Non-fatal: fall back to controller vtable coverage only.
            append_anti_kick_log_file(
                "[anti-kick] warning: global ProcessEvent hook unavailable (" +
                pe_hook_failure + ") — coverage limited to controller RPCs\n");
        }
        if (!g_original_process_event.load() || !is_executable_code(g_original_process_event.load()))
        {
            return response_json(false, "anti_kick_hook_failed", 0, 1, "process_event_original_unavailable");
        }

        g_anti_kick_local_controller.store(ctx.controller);
        g_anti_kick_local_player.store(ctx.local_player);
        g_anti_kick_local_player_state.store(player_state);
        g_anti_kick_local_net_connection.store(net_connection);
        g_anti_kick_local_pawn.store(ctx.pawn);
        // Arm capture mode: log distinct UFunctions dispatched on our local
        // objects so a host kick's real call path becomes visible in the log.
        g_anti_kick_capture_names_pool.store(ref.names.pool);
        g_anti_kick_capture_names_table_offset.store(ref.names.table_offset);
        g_anti_kick_capture_names_style.store(ref.names.style);
        {
            std::lock_guard<std::mutex> lock(g_anti_kick_capture_mutex);
            g_anti_kick_capture_seen.clear();
        }
        capture_reset_seen_table();
        g_anti_kick_capture_enabled.store(true);
        g_anti_kick_blocked_function_count.store(blocked_count);
        for (std::size_t i = 0; i < AntiKickMaxBlockedFunctions; ++i)
        {
            g_anti_kick_blocked_functions[i].store(i < blocked_count ? blocked[i] : 0);
        }
        // Record the FName comparison indices of the well-known kick RPC names
        // so the by-name fallback keeps blocking after the engine recreates the
        // Blueprint UFunction objects on a round transition.
        {
            std::size_t name_count = 0;
            std::lock_guard<std::mutex> lock(g_anti_kick_meta_mutex);
            for (std::size_t i = 0; i < g_anti_kick_func_meta_count && name_count < AntiKickMaxBlockedNames; ++i)
            {
                if (!anti_kick_is_global_kick_name(g_anti_kick_func_meta[i].name))
                {
                    continue;
                }
                const auto name_index = safe_read<std::uint32_t>(g_anti_kick_func_meta[i].func + OffName, 0);
                if (!name_index)
                {
                    continue;
                }
                bool duplicate = false;
                for (std::size_t j = 0; j < name_count; ++j)
                {
                    if (g_anti_kick_blocked_name_indices[j].load() == name_index)
                    {
                        duplicate = true;
                        break;
                    }
                }
                if (duplicate)
                {
                    continue;
                }
                std::snprintf(g_anti_kick_blocked_name_texts[name_count],
                              sizeof(g_anti_kick_blocked_name_texts[name_count]),
                              "%s", g_anti_kick_func_meta[i].name);
                g_anti_kick_blocked_name_indices[name_count].store(name_index);
                ++name_count;
            }
            g_anti_kick_blocked_name_count.store(name_count);
        }
        g_anti_kick_enabled.store(true);

        std::string monitored = "\"monitored_functions\":[";
        {
            std::lock_guard<std::mutex> lock(g_anti_kick_meta_mutex);
            for (std::size_t i = 0; i < g_anti_kick_func_meta_count; ++i)
            {
                if (i > 0)
                {
                    monitored += ",";
                }
                monitored += "\"" + json_escape(g_anti_kick_func_meta[i].name) + "\"";
            }
        }
        monitored += "]";

        std::string summary = "[anti-kick] enabled — monitoring " + std::to_string(blocked_count) + " RPC(s): ";
        {
            std::lock_guard<std::mutex> lock(g_anti_kick_meta_mutex);
            for (std::size_t i = 0; i < g_anti_kick_func_meta_count; ++i)
            {
                if (i > 0)
                {
                    summary += ", ";
                }
                summary += g_anti_kick_func_meta[i].owner;
                summary += ".";
                summary += g_anti_kick_func_meta[i].name;
            }
        }
        summary += "\n";
        if (blocked_count != g_anti_kick_last_logged_blocked_count
            || net_connection != g_anti_kick_last_logged_net_connection)
        {
            g_anti_kick_last_logged_blocked_count = blocked_count;
            g_anti_kick_last_logged_net_connection = net_connection;
            append_anti_kick_log_file(summary);
        }

        std::string meta = "\"enabled\":true,\"hook_mode\":\"vtable\",\"hooks_locked\":"
                           + std::string(g_anti_kick_vtable_hook_installed ? "true" : "false") +
                           ",\"blocked_functions\":" + std::to_string(blocked_count) +
                           ",\"controller\":\"" + hex_address(ctx.controller) + "\"" +
                           ",\"player_state\":\"" + hex_address(player_state) + "\"" +
                           ",\"net_connection\":\"" + hex_address(net_connection) + "\"" +
                           ",\"blocks_so_far\":" + std::to_string(g_anti_kick_block_count.load()) + "," + monitored;
        return response_json(true, "set_anti_kick", 0, 0, "anti-kick enabled", meta);
    }

    auto handle_game_set_god_mode(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        const bool enabled = json_extract_bool(payload, "enabled");
        if (!enabled)
        {
            g_god_mode_enabled.store(false);
            g_god_mode_local_pawn.store(0);
            g_god_mode_blocked_function_count.store(0);
            for (std::size_t i = 0; i < GodModeMaxBlockedFunctions; ++i)
            {
                g_god_mode_blocked_functions[i].store(0);
            }
            append_anti_kick_log_file("[god-mode] disabled\n");
            return response_json(true, "set_god_mode", 0, 0, "god mode disabled",
                                "\"enabled\":false,\"blocked_functions\":0");
        }

        if (!live_uobject(ctx.pawn))
        {
            return response_json(false, "pawn_unavailable", 0, 1,
                                "wait until spawned in as survivor before enabling god mode");
        }

        std::uintptr_t blocked[GodModeMaxBlockedFunctions]{};
        std::size_t blocked_count = 0;
        auto remember_blocked = [&](std::uintptr_t function) {
            if (!function || blocked_count >= GodModeMaxBlockedFunctions)
            {
                return;
            }
            for (std::size_t i = 0; i < blocked_count; ++i)
            {
                if (blocked[i] == function)
                {
                    return;
                }
            }
            blocked[blocked_count++] = function;
        };
        auto add_class_named = [&](const char* class_name_text, const char* function_name) {
            const auto cls = ref.find_class_any(class_name_text);
            if (!cls)
            {
                append_anti_kick_log_file(std::string("[god-mode] class not found: ") + class_name_text + "\n");
                return;
            }
            const auto function = ref.find_function_in_class(cls, function_name);
            if (function)
            {
                remember_blocked(function);
            }
            else
            {
                append_anti_kick_log_file(
                    std::string("[god-mode] function not found: ") + class_name_text + "." + function_name + "\n");
            }
        };

        add_class_named("BP_FirstPersonCharacter_Main_C", "Damage");
        add_class_named("BP_FirstPersonCharacter_LINK_C", "Damage");
        add_class_named("BP_FirstPersonCharacter_cLeon_Character_C", "DeathPlayer");

        if (blocked_count == 0)
        {
            return response_json(false, "god_mode_functions_unavailable", 0, 1,
                                "could not resolve Damage/DeathPlayer UFunctions");
        }

        std::string pe_hook_failure{};
        if (!install_process_event_hook(pe_hook_failure))
        {
            return response_json(false, "god_mode_hook_failed", 0, 1, pe_hook_failure);
        }
        if (!g_original_process_event.load() || !is_executable_code(g_original_process_event.load()))
        {
            return response_json(false, "god_mode_hook_failed", 0, 1, "process_event_original_unavailable");
        }

        g_god_mode_local_pawn.store(ctx.pawn);
        g_god_mode_blocked_function_count.store(blocked_count);
        for (std::size_t i = 0; i < GodModeMaxBlockedFunctions; ++i)
        {
            g_god_mode_blocked_functions[i].store(i < blocked_count ? blocked[i] : 0);
        }
        god_mode_scrub_pawn(ctx.pawn);
        g_god_mode_enabled.store(true);

        append_anti_kick_log_file(
            "[god-mode] enabled — blocking " + std::to_string(blocked_count) +
            " UFunction(s) on pawn " + hex_address(ctx.pawn) + "\n");

        std::string meta = "\"enabled\":true,\"blocked_functions\":" + std::to_string(blocked_count) +
                           ",\"pawn\":\"" + hex_address(ctx.pawn) + "\"" +
                           ",\"blocks_so_far\":" + std::to_string(g_god_mode_block_count.load());
        return response_json(true, "set_god_mode", 0, 0, "god mode enabled", meta);
    }

    auto handle_get_anti_kick_log(const std::string& payload) -> std::string
    {
        const std::uint64_t since_seq = static_cast<std::uint64_t>(json_extract_double(payload, "since_seq"));
        std::string entries = "\"entries\":[";
        bool first = true;
        std::size_t returned = 0;
        {
            std::lock_guard<std::mutex> lock(g_anti_kick_log_mutex);
            const std::size_t total = std::min(g_anti_kick_log_head, AntiKickLogCapacity);
            for (std::size_t i = 0; i < total; ++i)
            {
                const auto& entry = g_anti_kick_log[(g_anti_kick_log_head + AntiKickLogCapacity - total + i) % AntiKickLogCapacity];
                if (entry.seq <= since_seq)
                {
                    continue;
                }
                if (!first)
                {
                    entries += ",";
                }
                first = false;
                ++returned;
                entries += "{\"seq\":" + std::to_string(entry.seq) +
                           ",\"timestamp_ms\":" + std::to_string(entry.timestamp_ms) +
                           ",\"function\":\"" + json_escape(entry.function_name) + "\"" +
                           ",\"owner\":\"" + json_escape(entry.object_owner) + "\"" +
                           ",\"kicker\":\"" + json_escape(entry.kicker) + "\"" +
                           ",\"detail\":\"" + json_escape(entry.detail) + "\"}";
            }
        }
        entries += "]";
        std::string monitored = "\"monitored_functions\":[";
        {
            std::lock_guard<std::mutex> lock(g_anti_kick_meta_mutex);
            for (std::size_t i = 0; i < g_anti_kick_func_meta_count; ++i)
            {
                if (i > 0)
                {
                    monitored += ",";
                }
                monitored += "\"" + json_escape(g_anti_kick_func_meta[i].name) + "\"";
            }
        }
        monitored += "]";
        const std::string meta = entries + "," + monitored +
                                 ",\"latest_seq\":" + std::to_string(g_anti_kick_log_seq.load()) +
                                 ",\"blocks_so_far\":" + std::to_string(g_anti_kick_block_count.load()) +
                                 ",\"enabled\":" + json_bool(g_anti_kick_enabled.load()) +
                                 ",\"returned\":" + std::to_string(returned);
        return response_json(true, "get_anti_kick_log", 0, 0, "ok", meta);
    }

    auto trim_player_name(std::string name) -> std::string
    {
        while (!name.empty() && (name.front() == ' ' || name.front() == '\t'))
        {
            name.erase(name.begin());
        }
        while (!name.empty() && (name.back() == ' ' || name.back() == '\t'))
        {
            name.pop_back();
        }
        if (name.size() > 32)
        {
            name.resize(32);
        }
        return name;
    }

    auto parse_hex_u64(const std::string& text) -> std::uintptr_t
    {
        if (text.empty())
        {
            return 0;
        }
        const char* start = text.c_str();
        if (text.rfind("0x", 0) == 0 || text.rfind("0X", 0) == 0)
        {
            start += 2;
        }
        char* end = nullptr;
        const auto value = std::strtoull(start, &end, 16);
        return end && end > start ? static_cast<std::uintptr_t>(value) : 0;
    }

    auto extract_steam64_text(const std::string& text) -> std::string
    {
        for (std::size_t i = 0; i + 17 <= text.size(); ++i)
        {
            if (text.compare(i, 7, "7656119") != 0)
            {
                continue;
            }
            bool ok = true;
            for (int j = 7; j < 17; ++j)
            {
                const char ch = text[i + static_cast<std::size_t>(j)];
                if (ch < '0' || ch > '9')
                {
                    ok = false;
                    break;
                }
            }
            if (ok)
            {
                return text.substr(i, 17);
            }
        }
        return {};
    }

    auto read_fstring_param(const std::uint8_t* base) -> std::string
    {
        if (!base)
        {
            return {};
        }
        const auto data = safe_read<const wchar_t*>(reinterpret_cast<std::uintptr_t>(base));
        const auto num = safe_read<int>(reinterpret_cast<std::uintptr_t>(base + 8));
        if (!data || num <= 1 || num > 256)
        {
            return {};
        }
        std::string out{};
        out.reserve(static_cast<std::size_t>(num));
        for (int i = 0; i < num - 1; ++i)
        {
            const wchar_t ch = safe_read<wchar_t>(
                reinterpret_cast<std::uintptr_t>(data + static_cast<std::size_t>(i) * sizeof(wchar_t)));
            if (!ch)
            {
                break;
            }
            out.push_back(ch >= 0 && ch < 128 ? static_cast<char>(ch) : '?');
        }
        return out;
    }

    auto read_player_state_display_name(std::uintptr_t player_state) -> std::string
    {
        if (!live_uobject(player_state))
        {
            return {};
        }
        static const int name_offsets[] = {0x388, 0x3F0, 0x340};
        for (const int off : name_offsets)
        {
            const auto name = read_fstring_param(reinterpret_cast<const std::uint8_t*>(player_state + off));
            if (!name.empty())
            {
                return trim_player_name(name);
            }
        }
        return {};
    }

    auto resolve_session_host_player_state(Reflection& ref) -> std::uintptr_t
    {
        (void)ref;
        const auto& sess = g_anti_kick_session;
        if (!sess.world || sess.gamestate_off <= 0)
        {
            return 0;
        }
        const auto gamestate = safe_read<std::uintptr_t>(sess.world + static_cast<std::uintptr_t>(sess.gamestate_off));
        if (!live_uobject(gamestate))
        {
            return 0;
        }
        if (sess.host_ps_direct_off > 0)
        {
            const auto host_ps = safe_read<std::uintptr_t>(
                gamestate + static_cast<std::uintptr_t>(sess.host_ps_direct_off));
            if (live_uobject(host_ps))
            {
                return host_ps;
            }
        }
        if (sess.playerarray_off <= 0 || sess.host_flag_off <= 0)
        {
            return 0;
        }
        const auto player_array = safe_read<sdk::TArray<std::uintptr_t>>(
            gamestate + static_cast<std::uintptr_t>(sess.playerarray_off));
        if (!player_array.Data || player_array.Num <= 0)
        {
            return 0;
        }
        const int limit = std::min(player_array.Num, 32);
        for (int i = 0; i < limit; ++i)
        {
            const auto ps = safe_read<std::uintptr_t>(
                reinterpret_cast<std::uintptr_t>(player_array.Data) + static_cast<std::uintptr_t>(i) * 8);
            if (!live_uobject(ps))
            {
                continue;
            }
            if (safe_read<std::uint8_t>(ps + static_cast<std::uintptr_t>(sess.host_flag_off)) != 0)
            {
                return ps;
            }
        }
        return 0;
    }

    auto cached_session_host_name(Reflection& ref) -> const char*
    {
        const auto now_ms = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch())
                .count());
        if (g_anti_kick_host_name[0] != '\0' && (now_ms - g_anti_kick_host_name_ms) < 2000)
        {
            return g_anti_kick_host_name;
        }

        std::lock_guard<std::mutex> lock(g_anti_kick_host_name_mutex);
        if (g_anti_kick_host_name[0] != '\0' && (now_ms - g_anti_kick_host_name_ms) < 2000)
        {
            return g_anti_kick_host_name;
        }

        std::string host_name{};
        const auto host_ps = resolve_session_host_player_state(ref);
        if (host_ps)
        {
            host_name = read_player_state_display_name(host_ps);
        }
        if (host_name.empty())
        {
            host_name = "unknown host";
        }
        std::snprintf(g_anti_kick_host_name, sizeof(g_anti_kick_host_name), "%s", host_name.c_str());
        g_anti_kick_host_name_ms = now_ms;
        return g_anti_kick_host_name;
    }

    auto conv_net_id_repl_to_steam64(Reflection& ref, const std::uint8_t* net_id, std::string& failure) -> std::string
    {
        if (!net_id)
        {
            return {};
        }
        bool any = false;
        for (int i = 0; i < 0x30; ++i)
        {
            if (net_id[i] != 0)
            {
                any = true;
                break;
            }
        }
        if (!any)
        {
            return {};
        }

        const auto helpers = sdk_find_object_named(ref, "Default__OnlineHelpers");
        const auto fn_conv = helpers ? ref.find_function(helpers, "Conv_FUniqueNetIdReplToString") : 0;
        if (!helpers || !fn_conv)
        {
            failure = "conv_function_unavailable";
            return extract_steam64_text(std::string(reinterpret_cast<const char*>(net_id), 0x30));
        }

        alignas(16) std::uint8_t conv_params[0x40]{};
        std::memcpy(conv_params, net_id, 0x30);
        if (!process_event(helpers, fn_conv, conv_params, failure))
        {
            return {};
        }
        return extract_steam64_text(read_fstring_param(conv_params + 0x30));
    }

    auto handle_game_get_player_steam_id(const std::string& payload, Reflection& ref) -> std::string
    {
        const auto ps_text = json_extract_string(payload, "player_state");
        const auto player_state = parse_hex_u64(ps_text);
        if (!live_uobject(player_state))
        {
            return response_json(false, "player_state_unavailable", 0, 1, "invalid PlayerState pointer");
        }

        std::string failure{};
        std::string steam_id{};

        const auto helpers = sdk_find_object_named(ref, "Default__OnlineHelpers");
        const auto fn_get_ps = helpers ? ref.find_function(helpers, "GetPlayerStateUniqueNetId") : 0;
        if (helpers && fn_get_ps)
        {
            alignas(16) std::uint8_t get_params[0x38]{};
            *reinterpret_cast<std::uintptr_t*>(get_params) = player_state;
            if (process_event(helpers, fn_get_ps, get_params, failure))
            {
                steam_id = conv_net_id_repl_to_steam64(ref, get_params + 8, failure);
            }
        }

        if (steam_id.empty())
        {
            const auto fn_bp = ref.find_function(player_state, "BP_GetUniqueId");
            if (fn_bp)
            {
                alignas(16) std::uint8_t bp_params[0x30]{};
                if (process_event(player_state, fn_bp, bp_params, failure))
                {
                    steam_id = conv_net_id_repl_to_steam64(ref, bp_params, failure);
                }
            }
        }

        if (steam_id.empty())
        {
            const auto unique_off = ref.resolve_property_offset("PlayerState", "UniqueID");
            if (unique_off >= 0)
            {
                steam_id = conv_net_id_repl_to_steam64(
                    ref,
                    reinterpret_cast<const std::uint8_t*>(player_state + static_cast<std::uintptr_t>(unique_off)),
                    failure);
            }
        }

        if (steam_id.empty())
        {
            const auto saved_off = ref.resolve_property_offset("PlayerState", "SavedNetworkAddress");
            if (saved_off >= 0)
            {
                steam_id = extract_steam64_text(read_fstring_param(
                    reinterpret_cast<const std::uint8_t*>(
                        player_state + static_cast<std::uintptr_t>(saved_off))));
            }
        }

        if (steam_id.empty())
        {
            const auto msg = failure.empty() ? "Steam ID not replicated yet" : failure;
            return response_json(false, "steam_id_unavailable", 0, 1, msg);
        }

        const std::string meta = "\"steam_id\":\"" + json_escape(steam_id) + "\"";
        return response_json(true, "get_player_steam_id", 0, 0, steam_id, meta);
    }

    auto handle_game_set_player_name(const std::string& payload, Reflection& ref, const SdkContext& ctx) -> std::string
    {
        std::string name = trim_player_name(json_extract_string(payload, "name"));
        if (name.empty())
        {
            return response_json(false, "invalid_name", 0, 1, "name must not be empty");
        }

        if (!live_uobject(ctx.controller))
        {
            return response_json(false, "controller_unavailable", 0, 1, "local PlayerController not available");
        }

        std::uintptr_t player_state = safe_read<std::uintptr_t>(ctx.controller + OffPlayerControllerPlayerState);
        if (!live_uobject(player_state) && live_uobject(ctx.pawn))
        {
            player_state = safe_read<std::uintptr_t>(ctx.pawn + OffPawnPlayerState);
        }
        if (!live_uobject(player_state))
        {
            return response_json(false, "player_state_unavailable", 0, 1, "local PlayerState not available");
        }

        std::uintptr_t set_name_fn = 0;
        std::string func_used{};
        for (const char* candidate : {"SetName(Server)", "SetName"})
        {
            set_name_fn = ref.find_function(player_state, candidate);
            if (set_name_fn)
            {
                func_used = candidate;
                break;
            }
        }
        if (!set_name_fn)
        {
            return response_json(false, "function_unavailable", 0, 1, "SetName UFunction not found on PlayerState");
        }

        const auto params_size = safe_read<int>(set_name_fn + OffPropertiesSize, 0);
        if (params_size <= 0 || params_size > 4096)
        {
            return response_json(false, "params_unavailable", 0, 1, "SetName parameter block unavailable");
        }

        std::vector<std::uint8_t> params(static_cast<std::size_t>(params_size), 0);
        std::vector<std::wstring> backing{};
        bool wrote_string = false;
        for (auto prop = safe_read<std::uintptr_t>(set_name_fn + OffChildProperties); prop;
             prop = safe_read<std::uintptr_t>(prop + OffFFieldNext))
        {
            const auto prop_name = lower_copy(ref.names.resolve(safe_read<std::uint32_t>(prop + OffFFieldName)));
            if (contains_text(prop_name, "customplayername") || prop_name == "name")
            {
                wrote_string = sdk_write_fstring_param(prop, params.data(), name, backing) || wrote_string;
            }
        }
        if (!wrote_string && params_size >= static_cast<int>(sizeof(ScriptStringParam)))
        {
            backing.push_back(widen_ascii(name));
            auto& wide = backing.back();
            auto* dest = reinterpret_cast<ScriptStringParam*>(params.data());
            dest->data = wide.empty() ? nullptr : wide.data();
            dest->num = static_cast<int>(wide.size()) + 1;
            dest->max = dest->num;
            wrote_string = true;
        }
        if (!wrote_string)
        {
            return response_json(false, "params_unavailable", 0, 1, "could not populate SetName FString parameter");
        }

        std::string pe_failure{};
        if (!process_event(player_state, set_name_fn, params.data(), pe_failure))
        {
            return response_json(false, "process_event_failed", 0, 1, pe_failure);
        }

        std::string meta = "\"name\":\"" + json_escape(name) + "\",\"function\":\"" + json_escape(func_used) + "\"";
        return response_json(true, "set_player_name", 0, 0, "SetName RPC sent", meta);
    }

    auto handle_game_command_direct(const std::string& request) -> std::string
    {
        std::string failure{};
        Reflection ref{};
        if (!ref.init(failure))
        {
            return response_json(false, failure.c_str(), 0, 1, failure.empty() ? "SDK reflection init failed" : failure);
        }
        SdkContext ctx{};
        try
        {
            ctx = sdk_resolve_context(ref);
        }
        catch (const SdkResolutionException& ex)
        {
            return response_json(false, ex.stage.c_str(), 0, 1, ex.what());
        }

        const std::string payload = json_extract_payload(request);

        if (request.find("\"type\":\"teleport\"") != std::string::npos)
        {
            return handle_game_teleport(payload, ref, ctx);
        }
        if (request.find("\"type\":\"set_fov\"") != std::string::npos)
        {
            return handle_game_set_fov(payload, ref, ctx);
        }
        if (request.find("\"type\":\"kill\"") != std::string::npos)
        {
            return handle_game_kill(payload, ref, ctx);
        }
        if (request.find("\"type\":\"kill_survivor\"") != std::string::npos)
        {
            return handle_game_kill_survivor(payload, ref, ctx);
        }
        if (request.find("\"type\":\"kill_all_survivors\"") != std::string::npos)
        {
            return handle_game_kill_all_survivors(payload, ref, ctx);
        }
        if (request.find("\"type\":\"magnet_tick\"") != std::string::npos)
        {
            return handle_game_magnet_tick(payload, ref, ctx);
        }
        if (request.find("\"type\":\"set_anti_kick\"") != std::string::npos)
        {
            return handle_game_set_anti_kick(payload, ref, ctx);
        }
        if (request.find("\"type\":\"set_god_mode\"") != std::string::npos)
        {
            return handle_game_set_god_mode(payload, ref, ctx);
        }
        if (request.find("\"type\":\"set_decoy_num\"") != std::string::npos)
        {
            return handle_game_set_decoy_num(payload, ref, ctx);
        }
        if (request.find("\"type\":\"get_skeleton\"") != std::string::npos)
        {
            return handle_game_get_skeleton(payload, ref, ctx);
        }
        if (request.find("\"type\":\"set_netconn_watch\"") != std::string::npos)
        {
            return handle_game_set_netconn_watch(payload, ref, ctx);
        }
        if (request.find("\"type\":\"dump_netconn_vtable\"") != std::string::npos)
        {
            return handle_game_dump_netconn_vtable(payload, ref, ctx);
        }
        if (request.find("\"type\":\"set_player_name\"") != std::string::npos)
        {
            return handle_game_set_player_name(payload, ref, ctx);
        }
        if (request.find("\"type\":\"get_player_steam_id\"") != std::string::npos)
        {
            return handle_game_get_player_steam_id(payload, ref);
        }
        if (request.find("\"type\":\"rotate\"") != std::string::npos)
        {
            const std::string payload = json_extract_payload(request);
            const double yaw_delta = json_extract_double(payload, "yaw");
            const std::string rotate_target = json_extract_string(payload, "target");
            const bool rotate_camera = rotate_target == "camera" || rotate_target == "control";
            bool rotated = false;
            if (!rotate_camera && live_uobject(ctx.pawn))
            {
                const auto get_rot_fn = ref.find_function(ctx.pawn, "K2_GetActorRotation");
                sdk::Actor_K2_GetActorRotation get_params{};
                if (get_rot_fn &&
                    process_event(ctx.pawn, get_rot_fn, reinterpret_cast<std::uint8_t*>(&get_params), failure))
                {
                    const auto set_rot_fn = ref.find_function(ctx.pawn, "K2_SetActorRotation");
                    if (set_rot_fn)
                    {
                        sdk::Actor_K2_SetActorRotation set_params{};
                        set_params.NewRotation = get_params.ReturnValue;
                        set_params.NewRotation.Yaw += yaw_delta;
                        set_params.bTeleportPhysics = true;
                        rotated = process_event(ctx.pawn, set_rot_fn, reinterpret_cast<std::uint8_t*>(&set_params), failure);
                    }
                }
            }
            if (!rotated && live_uobject(ctx.controller))
            {
                const auto set_cr_fn = ref.find_function(ctx.controller, "SetControlRotation");
                if (set_cr_fn)
                {
                    sdk::Controller_SetControlRotation cr_params{};
                    cr_params.NewRotation = safe_read<sdk::FRotator>(
                        ctx.controller + sdk::FieldOffsets::Controller_ControlRotation);
                    cr_params.NewRotation.Yaw += yaw_delta;
                    rotated = process_event(ctx.controller, set_cr_fn, reinterpret_cast<std::uint8_t*>(&cr_params), failure);
                }
            }
            return response_json(rotated, "rotate", 0, rotated ? 0 : 1, rotated ? "rotated" : "rotate failed",
                                std::string("\"yaw_delta\":") + std::to_string(yaw_delta) +
                                    ",\"rotate_target\":\"" + json_escape(rotate_camera ? "camera" : "pawn") + "\"");
        }
        return response_json(false, "unknown_game_command", 0, 1, "unknown game command type");
    }
