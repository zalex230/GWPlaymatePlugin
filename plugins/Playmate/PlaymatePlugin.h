#pragma once

#include <functional>
#include <windows.h>

#include <GWCA/Managers/UIMgr.h>
#include <GWCA/Utilities/Hook.h>
#include <ToolboxUIPlugin.h>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

namespace GW::Packet::StoC {
    struct AgentAdd;
    struct AgentState;
    struct CreateMissionProgress;
    struct ObjectiveAdd;
    struct ObjectiveDone;
    struct ObjectiveUpdateName;
    struct PartyDefeated;
    struct SpeechBubble;
    struct UpdateMissionProgress;
    struct VanquishComplete;
    struct VanquishProgress;
}

class PlaymatePlugin : public ToolboxUIPlugin {
public:
    PlaymatePlugin();
    ~PlaymatePlugin() override;

    [[nodiscard]] const char* Name() const override { return "Playmate"; }
    [[nodiscard]] bool HasSettings() const override { return true; }

    void Initialize(ImGuiContext* ctx, ImGuiAllocFns allocator_fns, HMODULE toolbox_dll) override;
    void SignalTerminate() override;
    void Terminate() override;
    bool CanTerminate() override;
    void Update(float delta_ms) override;
    void Draw(IDirect3DDevice9* device) override;
    void DrawSettings() override;
    void LoadSettings(const wchar_t* folder) override;
    void SaveSettings(const wchar_t* folder) override;

private:
    struct Snapshot {
        uint32_t map_id = 0;
        std::string map_name;
        uint32_t instance_type = 0;
        uint32_t district = 0;
        uint32_t instance_time = 0;
        uint32_t active_quest_id = 0;
        uint32_t quest_count = 0;
        std::string active_quest_name;
        std::string active_quest_objectives;
    };

    struct TelemetryEvent {
        std::string source = "gwtoolboxpp-playmate";
        std::string persona;
        std::string client_time;
        std::string event_type;
        std::string sender;
        std::string channel;
        std::string message;
        uint32_t map_id = 0;
        std::string map_name;
        uint32_t instance_type = 0;
        uint32_t district = 0;
        uint32_t instance_time = 0;
        uint32_t active_quest_id = 0;
        uint32_t quest_count = 0;
        std::string active_quest_name;
        std::string active_quest_objectives;
        float player_x = 0.0f;
        float player_y = 0.0f;
        float player_hp = 0.0f;
        uint32_t hostile_count = 0;
        uint32_t close_hostile_count = 0;
        uint32_t dead_hostile_count = 0;
        uint32_t closest_hostile_agent_id = 0;
        float closest_hostile_distance = 0.0f;
        std::string alert_type;
        std::string severity;
        uint32_t agent_id = 0;
        std::string agent_name;
        uint32_t objective_id = 0;
        std::string objective_name;
        float progress_current = 0.0f;
        float progress_total = 0.0f;
        uint32_t foes_killed = 0;
        uint32_t foes_remaining = 0;
    };

    struct RepliesResponse {
        std::vector<std::string> replies;
        struct ReplyItem {
            std::string message;
            std::string audio_url;
            std::string audio_mime_type;
            std::string audio_expires_at;
            bool multi_message = false;
            uint32_t line_index = 0;
            uint32_t line_count = 0;
        };
        std::vector<ReplyItem> reply_items;
    };

    struct QueuedReply {
        std::wstring message;
        std::string audio_url;
        bool multi_message = false;
        uint32_t line_index = 0;
        uint32_t line_count = 0;
        uint64_t not_before_ms = 0;
    };

    struct QueuedTtsRequest {
        std::wstring message;
        std::string audio_url;
        uint32_t post_play_delay_ms = 0;
    };

    struct EnvironmentScan {
        bool valid = false;
        float player_x = 0.0f;
        float player_y = 0.0f;
        float player_hp = 0.0f;
        uint32_t hostile_count = 0;
        uint32_t close_hostile_count = 0;
        uint32_t dead_hostile_count = 0;
        uint32_t closest_hostile_agent_id = 0;
        float closest_hostile_distance = 0.0f;
        bool in_combat = false;
        uint32_t selected_target_agent_id = 0;
        std::string selected_target_name;
        float selected_target_distance = 0.0f;
    };

    struct RecentHostileDeath {
        uint32_t agent_id = 0;
        std::string agent_name;
        float x = 0.0f;
        float y = 0.0f;
        uint64_t observed_ms = 0;
    };

    struct DecodedMapName {
        wchar_t encoded[8]{};
        wchar_t decoded[128]{};
        bool requested = false;
    };

    void RegisterHooks();
    void RemoveHooks();
    void StartWorker();
    void StopWorker();
    void WorkerLoop();
    bool WriteTelemetryLocal(const TelemetryEvent& event);
    bool PostTelemetry(const TelemetryEvent& event);
    void PollReplies();
    void QueueTelemetry(std::string event_type, std::string sender, std::string channel, std::string message);
    void QueueEnvironmentAlert(std::string alert_type, std::string severity, std::string message, const EnvironmentScan& scan);
    void QueueGameplayEvent(TelemetryEvent event);
    void QueueItemDropEvent(const GW::Packet::StoC::AgentAdd& packet);
    void QueueSnapshotEvent(const char* event_type);
    void MaybeQueueEnvironmentAlert();
    void QueueReply(QueuedReply reply);
    void FlushRepliesToChat();
    void ShowCompanionSpeechBubble(const std::wstring& reply) const;
    void QueueCompanionTts(QueuedTtsRequest request);
    void GenerateAndPlayCompanionTts(const QueuedTtsRequest& request);
    void WaitForTtsPlaybackSlot();
    void MarkTtsPlaybackStarted(const std::wstring& reply, uint32_t extra_delay_ms);
    void ApplyConfig();
    void SetStatus(std::string status);

    [[nodiscard]] Snapshot BuildSnapshot() const;
    [[nodiscard]] EnvironmentScan BuildEnvironmentScan() const;
    [[nodiscard]] std::string MapNameForId(uint32_t map_id) const;
    [[nodiscard]] std::string CurrentPersonaName() const;
    [[nodiscard]] std::wstring CurrentPersonaNameWide() const;
    [[nodiscard]] std::pair<std::string, std::string> GetConfig() const;
    [[nodiscard]] std::filesystem::path LocalLogPath() const;
    [[nodiscard]] std::string EventsUrl() const;
    [[nodiscard]] std::string RepliesUrl() const;

    static void OnSendChat(GW::HookStatus* status, GW::UI::UIMessage message_id, void* wparam, void* lparam);
    static void OnWriteToChatLog(GW::HookStatus* status, GW::UI::UIMessage message_id, void* wparam, void* lparam);
    static void OnMapOrQuestEvent(GW::HookStatus* status, GW::UI::UIMessage message_id, void* wparam, void* lparam);
    static void OnAgentState(GW::HookStatus* status, GW::Packet::StoC::AgentState* packet);
    static void OnAgentAdd(GW::HookStatus* status, GW::Packet::StoC::AgentAdd* packet);
    static void OnPartyDefeated(GW::HookStatus* status, GW::Packet::StoC::PartyDefeated* packet);
    static void OnSpeechBubble(GW::HookStatus* status, GW::Packet::StoC::SpeechBubble* packet);
    static void OnObjectiveAdd(GW::HookStatus* status, GW::Packet::StoC::ObjectiveAdd* packet);
    static void OnObjectiveDone(GW::HookStatus* status, GW::Packet::StoC::ObjectiveDone* packet);
    static void OnObjectiveUpdateName(GW::HookStatus* status, GW::Packet::StoC::ObjectiveUpdateName* packet);
    static void OnCreateMissionProgress(GW::HookStatus* status, GW::Packet::StoC::CreateMissionProgress* packet);
    static void OnUpdateMissionProgress(GW::HookStatus* status, GW::Packet::StoC::UpdateMissionProgress* packet);
    static void OnVanquishProgress(GW::HookStatus* status, GW::Packet::StoC::VanquishProgress* packet);
    static void OnVanquishComplete(GW::HookStatus* status, GW::Packet::StoC::VanquishComplete* packet);

private:
    bool enabled_ = true;
    bool local_capture_ = true;
    bool send_to_backend_ = false;
    bool inject_replies_ = true;
    bool show_speech_bubbles_ = true;
    bool speak_replies_ = true;
    bool environment_radar_ = true;
    std::atomic_bool telemetry_enabled_ = true;
    std::atomic_bool local_capture_enabled_ = true;
    std::atomic_bool backend_enabled_ = false;
    std::atomic_bool reply_injection_enabled_ = true;
    std::atomic_bool speech_bubbles_enabled_ = true;
    std::atomic_bool tts_enabled_ = true;
    std::atomic_bool environment_radar_enabled_ = true;
    std::atomic<int> poll_interval_ms_ = 1000;
    float poll_interval_sec_ = 1.0f;
    float snapshot_interval_sec_ = 8.0f;
    float radar_interval_sec_ = 3.0f;
    char backend_url_input_[256] = "http://127.0.0.1:8787";
    char api_token_input_[160] = "";

    mutable std::mutex config_mutex_;
    std::string backend_url_ = "http://127.0.0.1:8787";
    std::string api_token_;
    std::filesystem::path local_log_folder_;

    std::atomic_bool running_ = false;
    std::thread worker_;
    mutable std::mutex queue_mutex_;
    std::condition_variable queue_cv_;
    std::deque<TelemetryEvent> outbound_;
    std::deque<QueuedReply> inbound_replies_;
    std::deque<QueuedTtsRequest> tts_requests_;
    uint64_t next_tts_play_allowed_ms_ = 0;
    uint64_t next_multi_reply_allowed_ms_ = 0;

    mutable std::mutex status_mutex_;
    std::string status_ = "Idle";
    std::string last_event_status_ = "No events sent yet";
    std::string last_reply_status_ = "No replies yet";
    std::string last_backend_error_;
    size_t local_written_count_ = 0;
    size_t sent_count_ = 0;
    size_t failed_count_ = 0;
    size_t received_count_ = 0;
    bool waiting_for_reply_ = false;
    uint64_t waiting_since_ms_ = 0;
    uint64_t last_sent_ms_ = 0;
    uint64_t last_reply_ms_ = 0;

    float snapshot_elapsed_ms_ = 0.0f;
    float radar_elapsed_ms_ = 0.0f;
    uint32_t last_map_id_ = 0;
    uint32_t last_active_quest_id_ = 0;
    uint32_t last_hostile_count_ = 0;
    uint32_t last_close_hostile_count_ = 0;
    float last_player_hp_ = 0.0f;
    bool last_in_combat_ = false;
    mutable std::mutex gameplay_state_mutex_;
    std::unordered_map<uint32_t, uint32_t> last_agent_states_;
    mutable std::unordered_map<uint32_t, bool> known_hostile_alive_;
    mutable std::deque<RecentHostileDeath> recent_hostile_deaths_;
    std::unordered_map<uint32_t, float> last_mission_progress_;
    mutable std::mutex map_name_cache_mutex_;
    mutable std::unordered_map<uint32_t, std::unique_ptr<DecodedMapName>> map_name_cache_;

    GW::HookEntry send_chat_entry_;
    GW::HookEntry write_chat_entry_;
    GW::HookEntry world_event_entry_;
    GW::HookEntry stoc_event_entry_;
};
