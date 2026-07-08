// Peterhack bridge globals.
    // ---- legacy bridge.cpp lines 71-223 ----
    constexpr std::uintptr_t OffPlayerControllerPlayerState = 0x2B0;
    constexpr std::uintptr_t OffPlayerControllerIsLocal = 0x6C4;
    constexpr std::uintptr_t OffPawnPlayerState = 0x2C8;
    constexpr std::size_t ProcessEventInlinePatchSize = 14;
    constexpr std::size_t AntiKickMaxBlockedFunctions = 32;
    constexpr std::size_t AntiKickLogCapacity = 64;

    struct ProcessEventInlineHook
    {
        void* target = nullptr;
        void* trampoline = nullptr;
        std::uint8_t original[ProcessEventInlinePatchSize]{};
        bool installed = false;
        bool chained = false;
    };

    std::mutex g_inline_hook_mutex;
    ProcessEventInlineHook g_pe_inline_hook{};
    constexpr std::uintptr_t OffUPlayerConnection = 0x28;
    constexpr std::size_t AntiKickMaxVtableHooks = 8;
    struct AntiKickVtableRestore
    {
        void** vtable{nullptr};
        std::size_t slot{0};
        void* original{nullptr};
    };
    std::mutex g_anti_kick_vtable_mutex;
    AntiKickVtableRestore g_anti_kick_vtable_restores[AntiKickMaxVtableHooks]{};
    std::size_t g_anti_kick_vtable_restore_count{0};
    bool g_anti_kick_vtable_hook_installed{false};
    std::atomic<bool> g_anti_kick_enabled{false};

    // God mode (MechaSRC port) — block Damage/DeathPlayer on local pawn.
    constexpr std::size_t GodModeMaxBlockedFunctions = 8;
    std::atomic<bool> g_god_mode_enabled{false};
    std::atomic<std::uintptr_t> g_god_mode_local_pawn{0};
    std::atomic<std::uintptr_t> g_god_mode_blocked_functions[GodModeMaxBlockedFunctions]{};
    std::atomic<std::size_t> g_god_mode_blocked_function_count{0};
    std::atomic<int> g_god_mode_block_count{0};
    constexpr std::uintptr_t GodModeOffHealth = 0x0640;
    constexpr std::uintptr_t GodModeOffMaxHealth = 0x0648;
    constexpr std::uintptr_t GodModeOffInvincible = 0x05AB;
    constexpr std::uintptr_t GodModeOffDead = 0x05AA;
    std::atomic<std::uintptr_t> g_anti_kick_local_controller{0};
    std::atomic<std::uintptr_t> g_anti_kick_local_player{0};
    std::atomic<std::uintptr_t> g_anti_kick_local_player_state{0};
    std::atomic<std::uintptr_t> g_anti_kick_local_net_connection{0};
    std::atomic<std::uintptr_t> g_anti_kick_local_pawn{0};

    // Capture mode: while anti-kick is on, log every distinct UFunction that is
    // dispatched on one of our local objects (controller / player state / net
    // connection / pawn). The kick that keeps getting through fires no function
    // in our block list, so we need to SEE what (if anything) the engine calls
    // on us during a host kick. Pointer-gated to the four local objects so it
    // costs almost nothing on the hot ProcessEvent path, and deduped so each
    // distinct call logs once per session. Read-only — never blocks.
    std::atomic<bool> g_anti_kick_capture_enabled{false};
    std::atomic<std::uintptr_t> g_anti_kick_capture_names_pool{0};
    std::atomic<int> g_anti_kick_capture_names_table_offset{0x10};
    std::atomic<int> g_anti_kick_capture_names_style{1};
    std::mutex g_anti_kick_capture_mutex;
    std::unordered_set<std::string> g_anti_kick_capture_seen;

    // Widened capture (every object, not just the 4 local ones). To keep the hot
    // ProcessEvent path cheap we classify each distinct UFunction pointer exactly
    // once: the first time a function is ever dispatched we resolve + keyword-test
    // its name, then record the pointer here so every later call is just a
    // lock-free probe + skip. This lets us prove whether ANY kick RPC fires on a
    // GameSession/OnlineSession/etc., and simultaneously discover the game's
    // session/join functions (needed to wire auto-rejoin).
    constexpr std::size_t CaptureSeenTableSize = 16384;  // power of two
    std::atomic<std::uintptr_t> g_capture_seen_funcs[CaptureSeenTableSize]{};
    std::atomic<std::size_t> g_capture_seen_func_fill{0};

    // Diagnostic native probe: registry of disconnect-reason-carrying RPCs
    // (ClientTravelInternal, ClientReturnToMainMenu*, ClientWasKicked, network/
    // travel-failure). When one of these fires during a kick we read its FString
    // URL/reason param — that string carries the engine's actual close reason,
    // which is what we need to distinguish an application kick from a transport /
    // EOS session drop. Pointers are discovered lazily (once) and compared on the
    // hot path with cheap atomic loads, so cost is negligible until a kick occurs.
    constexpr std::size_t CaptureReasonMaxFuncs = 16;
    std::atomic<std::uintptr_t> g_capture_reason_funcs[CaptureReasonMaxFuncs]{};
    std::atomic<std::size_t> g_capture_reason_func_count{0};
    std::mutex g_capture_reason_mutex;
    std::unordered_set<std::string> g_capture_reason_seen_text;

    // ---- NetConnection close watcher (diagnostic) --------------------------
    // Background thread that polls the local UNetConnection and logs the exact
    // moment it goes away (pointer nulled on the local player, or the object
    // becomes invalid), so we can time the transport close relative to the
    // ProcessEvent capture sequence (ReceiveUnPossess/EndPlay/Destroyed). Pure
    // SEH-guarded reads — no patching — so it cannot crash the game.
    std::atomic<bool> g_netconn_watch_enabled{false};
    std::atomic<bool> g_netconn_watch_thread_alive{false};
    std::atomic<std::uintptr_t> g_netconn_watch_conn{0};
    std::atomic<std::uintptr_t> g_netconn_watch_local_player{0};
    std::atomic<std::uintptr_t> g_anti_kick_blocked_functions[AntiKickMaxBlockedFunctions]{};
    std::atomic<std::size_t> g_anti_kick_blocked_function_count{0};
    // FName comparison indices of the well-known kick RPC names (Kick,
    // ClientWasKicked, ...). The engine recreates BP UFunction objects between
    // rounds, which invalidates the pointer list above; the FName index for a
    // given name is stable for the whole session, so matching on it keeps the
    // block working after a round transition (chameleonEsp re-matches by name
    // on every call for the same reason).
    constexpr std::size_t AntiKickMaxBlockedNames = 8;
    std::atomic<std::uint32_t> g_anti_kick_blocked_name_indices[AntiKickMaxBlockedNames]{};
    char g_anti_kick_blocked_name_texts[AntiKickMaxBlockedNames][64]{};
    std::atomic<std::size_t> g_anti_kick_blocked_name_count{0};
    std::atomic<std::uint64_t> g_anti_kick_block_count{0};
    std::size_t g_anti_kick_last_logged_blocked_count{static_cast<std::size_t>(-1)};
    std::uintptr_t g_anti_kick_last_logged_net_connection{0};
    bool g_anti_kick_last_logged_no_player_state{false};
    bool g_anti_kick_last_logged_no_net_connection{false};

    struct AntiKickFuncMeta
    {
        std::uintptr_t func{0};
        char name[64]{};
        char owner[24]{};
    };

    struct AntiKickLogEntry
    {
        std::uint64_t seq{0};
        std::uint64_t timestamp_ms{0};
        char function_name[64]{};
        char object_owner[24]{};
        char kicker[64]{};
        char detail[128]{};
    };

    struct AntiKickSessionCache
    {
        std::uintptr_t world{0};
        int gamestate_off{0};
        int playerarray_off{0};
        int host_ps_direct_off{0};
        int host_flag_off{0};
    };

    AntiKickSessionCache g_anti_kick_session{};
    std::mutex g_anti_kick_host_name_mutex;
    char g_anti_kick_host_name[64]{};
    std::uint64_t g_anti_kick_host_name_ms{0};

    std::mutex g_anti_kick_meta_mutex;
    AntiKickFuncMeta g_anti_kick_func_meta[AntiKickMaxBlockedFunctions]{};
    std::size_t g_anti_kick_func_meta_count{0};
    std::mutex g_anti_kick_log_mutex;
    AntiKickLogEntry g_anti_kick_log[AntiKickLogCapacity]{};
    std::size_t g_anti_kick_log_head{0};
    std::atomic<std::uint64_t> g_anti_kick_log_seq{0};

    // ---- legacy bridge.cpp lines 241-250 ----
    struct QueuedGameCommand
    {
        std::string request{};
        std::string response{};
        bool done{false};
    };

    std::mutex g_game_cmd_mutex;
    std::condition_variable g_game_cmd_cv;
    std::vector<std::shared_ptr<QueuedGameCommand>> g_game_cmds;

    // Kill-all queue (chameleonEsp: one survivor per game-thread tick).
    std::mutex g_kill_all_mutex;
    std::vector<std::uintptr_t> g_kill_all_queue;
