#include "PlaymatePlugin.h"

#include <GWCA/Constants/Constants.h>
#include <GWCA/Context/CharContext.h>
#include <GWCA/GameEntities/Agent.h>
#include <GWCA/GameEntities/Item.h>
#include <GWCA/GameContainers/Array.h>
#include <GWCA/GameEntities/Map.h>
#include <GWCA/GameEntities/Quest.h>
#include <GWCA/Managers/AgentMgr.h>
#include <GWCA/Managers/ChatMgr.h>
#include <GWCA/Managers/GameThreadMgr.h>
#include <GWCA/Managers/ItemMgr.h>
#include <GWCA/Managers/MapMgr.h>
#include <GWCA/Managers/PartyMgr.h>
#include <GWCA/Managers/QuestMgr.h>
#include <GWCA/Managers/StoCMgr.h>
#include <GWCA/GameEntities/Party.h>
#include <GWCA/Packets/StoC.h>

#include <HttpClient.h>
#include <glaze/glaze.hpp>
#include <imgui.h>
#include <sapi.h>
#include <windows.h>

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <cwctype>
#include <fstream>
#include <format>
#include <cmath>
#include <iomanip>
#include <limits>
#include <optional>
#include <sstream>
#include <thread>
#include <string_view>
#include <utility>

#pragma comment(lib, "sapi.lib")

namespace {
    PlaymatePlugin* active_plugin = nullptr;

    std::string WideToUtf8(const wchar_t* value)
    {
        if (!value || !*value) {
            return {};
        }
        const int len = WideCharToMultiByte(CP_UTF8, 0, value, -1, nullptr, 0, nullptr, nullptr);
        if (len <= 1) {
            return {};
        }
        std::string out;
        out.resize(static_cast<size_t>(len - 1));
        WideCharToMultiByte(CP_UTF8, 0, value, -1, out.data(), len, nullptr, nullptr);
        return out;
    }

    std::wstring Utf8ToWide(const std::string& value)
    {
        if (value.empty()) {
            return {};
        }
        const int len = MultiByteToWideChar(CP_UTF8, 0, value.c_str(), static_cast<int>(value.size()), nullptr, 0);
        if (len <= 0) {
            return {};
        }
        std::wstring out;
        out.resize(static_cast<size_t>(len));
        MultiByteToWideChar(CP_UTF8, 0, value.c_str(), static_cast<int>(value.size()), out.data(), len);
        return out;
    }

    std::string ChannelName(const GW::Chat::Channel channel)
    {
        switch (channel) {
            case GW::Chat::CHANNEL_ALLIANCE: return "alliance";
            case GW::Chat::CHANNEL_ALLIES: return "allies";
            case GW::Chat::CHANNEL_ALL: return "local";
            case GW::Chat::CHANNEL_EMOTE: return "emote";
            case GW::Chat::CHANNEL_WARNING: return "warning";
            case GW::Chat::CHANNEL_GUILD: return "guild";
            case GW::Chat::CHANNEL_GLOBAL: return "global";
            case GW::Chat::CHANNEL_GROUP: return "party";
            case GW::Chat::CHANNEL_TRADE: return "trade";
            case GW::Chat::CHANNEL_ADVISORY: return "system";
            case GW::Chat::CHANNEL_WHISPER: return "whisper";
            case GW::Chat::CHANNEL_COMMAND: return "command";
            default: return "unknown";
        }
    }

    std::string TrimTrailingSlash(std::string value)
    {
        while (!value.empty() && value.back() == '/') {
            value.pop_back();
        }
        return value;
    }

    std::string PlayerMessageText(const wchar_t* raw)
    {
        if (!raw || !*raw) {
            return {};
        }
        const auto channel = GW::Chat::GetChannel(*raw);
        if (channel != GW::Chat::CHANNEL_UNKNOW) {
            return WideToUtf8(raw + 1);
        }
        return WideToUtf8(raw);
    }

    bool IsAllowedChatLogChannel(const GW::Chat::Channel channel)
    {
        switch (channel) {
            case GW::Chat::CHANNEL_ALL:
            case GW::Chat::CHANNEL_EMOTE:
            case GW::Chat::CHANNEL_GUILD:
            case GW::Chat::CHANNEL_ALLIANCE:
            case GW::Chat::CHANNEL_WHISPER:
            case GW::Chat::CHANNEL_ADVISORY:
                return true;
            default:
                return false;
        }
    }

    bool LooksGwEncoded(const wchar_t* message)
    {
        if (!message || !*message) {
            return true;
        }

        size_t total = 0;
        size_t encoded = 0;
        for (const wchar_t* cursor = message; *cursor; ++cursor) {
            ++total;
            const wchar_t ch = *cursor;
            if ((ch < 0x20 && ch != L'\t' && ch != L'\n' && ch != L'\r') || ch > 0xFF) {
                ++encoded;
            }
        }
        return total == 0 || message[0] < 0x20 || message[0] > 0xFF || encoded * 4 > total;
    }

    std::string StripGwMarkup(std::string text)
    {
        std::string out;
        out.reserve(text.size());
        bool in_tag = false;
        for (const char ch : text) {
            if (ch == '<') {
                in_tag = true;
                continue;
            }
            if (ch == '>' && in_tag) {
                in_tag = false;
                continue;
            }
            if (in_tag || static_cast<unsigned char>(ch) < 0x20) {
                continue;
            }
            out.push_back(ch);
        }

        std::istringstream stream(out);
        std::ostringstream collapsed;
        std::string word;
        while (stream >> word) {
            if (collapsed.tellp() > 0) {
                collapsed << ' ';
            }
            collapsed << word;
        }
        return collapsed.str();
    }

    bool ContainsInsensitive(const std::string& haystack, const std::string& needle)
    {
        return std::search(
                   haystack.begin(),
                   haystack.end(),
                   needle.begin(),
                   needle.end(),
                   [](const char a, const char b) {
                       return std::tolower(static_cast<unsigned char>(a)) == std::tolower(static_cast<unsigned char>(b));
                   })
            != haystack.end();
    }

    std::string UrlEncode(const std::string& value)
    {
        std::ostringstream encoded;
        encoded << std::uppercase << std::hex;
        for (const unsigned char ch : value) {
            if (std::isalnum(ch) || ch == '-' || ch == '_' || ch == '.' || ch == '~') {
                encoded << static_cast<char>(ch);
            }
            else {
                encoded << '%' << std::setw(2) << std::setfill('0') << static_cast<int>(ch);
                encoded << std::setfill(' ');
            }
        }
        return encoded.str();
    }

    bool IsMeaningfulSystemLine(const std::string& message)
    {
        static constexpr std::string_view keywords[] = {
            "quest",
            "mission",
            "objective",
            "completed",
            "accepted",
            "updated",
            "reward",
            "sold",
            "bought",
            "received",
            "acquired",
            "gold",
            "item",
            "inventory",
        };
        return std::ranges::any_of(keywords, [&](const std::string_view keyword) {
            return ContainsInsensitive(message, std::string(keyword));
        });
    }

    bool IsReadableChatText(const std::string& message)
    {
        if (message.size() < 3) {
            return false;
        }
        if (ContainsInsensitive(message, "GWToolbox++") || ContainsInsensitive(message, "Plugins detected")
            || ContainsInsensitive(message, "Plugins are NOT permitted")) {
            return false;
        }

        size_t letters = 0;
        size_t readable = 0;
        for (const unsigned char ch : message) {
            if (std::isalpha(ch)) {
                ++letters;
            }
            if (std::isalnum(ch) || std::ispunct(ch) || std::isspace(ch)) {
                ++readable;
            }
        }
        return letters >= 2 && readable * 10 >= message.size() * 9;
    }

    std::optional<std::string> FilterChatLogMessage(const GW::Chat::Channel channel, const wchar_t* message)
    {
        if (!IsAllowedChatLogChannel(channel) || LooksGwEncoded(message)) {
            return std::nullopt;
        }

        auto cleaned = StripGwMarkup(WideToUtf8(message));
        if (!IsReadableChatText(cleaned)) {
            return std::nullopt;
        }
        if (channel == GW::Chat::CHANNEL_ADVISORY && !IsMeaningfulSystemLine(cleaned)) {
            return std::nullopt;
        }
        return cleaned;
    }

    std::string CurrentUtcTimestamp()
    {
        SYSTEMTIME now;
        GetSystemTime(&now);
        return std::format(
            "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
            now.wYear,
            now.wMonth,
            now.wDay,
            now.wHour,
            now.wMinute,
            now.wSecond,
            now.wMilliseconds);
    }

    std::wstring CurrentLocalLogDate()
    {
        SYSTEMTIME now;
        GetLocalTime(&now);
        return std::format(L"{:04}-{:02}-{:02}", now.wYear, now.wMonth, now.wDay);
    }

    uint64_t MonotonicMs()
    {
        return GetTickCount64();
    }

    uint32_t EstimateTtsPostPlayDelayMs(const std::wstring& message)
    {
        const size_t visible_chars = std::ranges::count_if(message, [](const wchar_t ch) {
            return !std::iswspace(ch);
        });
        const size_t word_count = static_cast<size_t>(std::ranges::count(message, L' ')) + 1;
        const uint32_t by_chars = static_cast<uint32_t>(visible_chars * 55);
        const uint32_t by_words = static_cast<uint32_t>(word_count * 310);
        return std::clamp(std::max(by_chars, by_words) + 650U, 1800U, 7000U);
    }

    uint32_t EstimateMultiMessageDelayMs(const std::wstring& message)
    {
        const uint32_t base_delay = EstimateTtsPostPlayDelayMs(message);
        return std::clamp(base_delay + (base_delay / 2U) + 1200U, 3000U, 10000U);
    }

    float Distance2D(const GW::GamePos& a, const GW::GamePos& b)
    {
        const float dx = a.x - b.x;
        const float dy = a.y - b.y;
        return std::sqrt(dx * dx + dy * dy);
    }

    bool IsControlledCharacterAgent(const uint32_t agent_id)
    {
        if (!agent_id) {
            return false;
        }
        const GW::AgentLiving* player = GW::Agents::GetControlledCharacter();
        return player && player->agent_id == agent_id;
    }

    bool IsAgentInCurrentParty(const uint32_t agent_id)
    {
        if (!agent_id) {
            return false;
        }
        if (IsControlledCharacterAgent(agent_id)) {
            return true;
        }
        const GW::PartyInfo* party = GW::PartyMgr::GetPartyInfo();
        if (!party) {
            return false;
        }
        for (const GW::HeroPartyMember& hero : party->heroes) {
            if (hero.agent_id == agent_id) {
                return true;
            }
        }
        for (const GW::HenchmanPartyMember& henchman : party->henchmen) {
            if (henchman.agent_id == agent_id) {
                return true;
            }
        }
        for (const GW::AgentID other : party->others) {
            if (other == agent_id) {
                return true;
            }
        }
        for (const GW::PlayerPartyMember& player : party->players) {
            if (GW::Agents::GetAgentIdByLoginNumber(player.login_number) == agent_id) {
                return true;
            }
        }
        return false;
    }

    std::string PartyAgentName(const uint32_t agent_id)
    {
        const GW::Agent* agent = GW::Agents::GetAgentByID(agent_id);
        const GW::AgentLiving* living = agent ? agent->GetAsAgentLiving() : nullptr;
        if (living && living->login_number) {
            return WideToUtf8(GW::Agents::GetPlayerNameByLoginNumber(living->login_number));
        }
        return {};
    }

    std::string LivingAgentName(const GW::AgentLiving* living)
    {
        if (!living) {
            return {};
        }
        if (living->login_number) {
            return WideToUtf8(GW::Agents::GetPlayerNameByLoginNumber(living->login_number));
        }

        struct DecodedAgentName {
            std::wstring encoded;
            wchar_t decoded[128]{};
            bool requested = false;
        };
        static std::mutex cache_mutex;
        static std::unordered_map<uint32_t, DecodedAgentName> cache;

        const wchar_t* encoded_name = GW::Agents::GetAgentEncName(living);
        if (!encoded_name || !*encoded_name) {
            return {};
        }

        std::lock_guard lock(cache_mutex);
        auto& entry = cache[living->agent_id];
        if (entry.encoded != encoded_name) {
            entry = DecodedAgentName{};
            entry.encoded = encoded_name;
        }
        if (entry.decoded[0]) {
            return WideToUtf8(entry.decoded);
        }
        if (!entry.requested) {
            entry.requested = true;
            GW::UI::AsyncDecodeStr(entry.encoded.c_str(), entry.decoded, _countof(entry.decoded));
        }
        return {};
    }

    std::string DyeItemName(const GW::Item* item)
    {
        if (!item || item->model_id != GW::Constants::ItemID::Dye) {
            return {};
        }
        switch (item->dye.dye1) {
            case GW::DyeColor::Black: return "Black Dye";
            case GW::DyeColor::White: return "White Dye";
            case GW::DyeColor::Silver: return "Silver Dye";
            case GW::DyeColor::Red: return "Red Dye";
            case GW::DyeColor::Blue: return "Blue Dye";
            case GW::DyeColor::Green: return "Green Dye";
            case GW::DyeColor::Purple: return "Purple Dye";
            case GW::DyeColor::Yellow: return "Yellow Dye";
            case GW::DyeColor::Orange: return "Orange Dye";
            case GW::DyeColor::Brown: return "Brown Dye";
            case GW::DyeColor::Pink: return "Pink Dye";
            case GW::DyeColor::Gray: return "Gray Dye";
            default: return "Dye";
        }
    }

    std::string NotableRarityName(const GW::Item* item)
    {
        if (!item) {
            return {};
        }
        if ((item->interaction & 0x10) != 0) {
            return "Green";
        }
        if ((item->interaction & 0x20000) != 0) {
            return "Gold";
        }
        if ((item->interaction & 0x400000) != 0) {
            return "Purple";
        }
        return {};
    }

    bool IsReplyTriggerEvent(const std::string& event_type)
    {
        static constexpr std::string_view triggers[] = {
            "player_chat",
            "environment_alert",
            "party_member_down",
            "party_defeated",
            "mission_objective_completed",
            "vanquish_complete",
        };
        return std::ranges::any_of(triggers, [&](const std::string_view trigger) {
            return event_type == trigger;
        });
    }

    const char* AgeText(const uint64_t timestamp_ms, char* buffer, const size_t buffer_size)
    {
        if (!timestamp_ms) {
            strncpy_s(buffer, buffer_size, "never", _TRUNCATE);
            return buffer;
        }
        const auto age_ms = MonotonicMs() - timestamp_ms;
        if (age_ms < 1000) {
            strncpy_s(buffer, buffer_size, "now", _TRUNCATE);
            return buffer;
        }
        snprintf(buffer, buffer_size, "%.1fs ago", static_cast<double>(age_ms) / 1000.0);
        return buffer;
    }

    std::wstring SpeechBubbleMessage(const std::wstring& reply)
    {
        constexpr size_t max_visible_chars = 110;
        std::wstring cleaned;
        cleaned.reserve(std::min(reply.size(), max_visible_chars));
        for (const wchar_t ch : reply) {
            if (ch == L'\r' || ch == L'\n' || ch == L'\t') {
                cleaned.push_back(L' ');
            }
            else if (ch >= 0x20) {
                cleaned.push_back(ch);
            }
            if (cleaned.size() >= max_visible_chars) {
                break;
            }
        }

        while (!cleaned.empty() && cleaned.back() == L' ') {
            cleaned.pop_back();
        }
        if (cleaned.empty()) {
            return {};
        }

        return std::format(L"\x108\x107{}\x1", cleaned);
    }

    std::wstring TtsMessage(const std::wstring& reply)
    {
        constexpr size_t max_tts_chars = 260;
        std::wstring cleaned;
        cleaned.reserve(std::min(reply.size(), max_tts_chars));
        bool last_was_space = false;
        for (const wchar_t ch : reply) {
            const bool is_space = ch == L'\r' || ch == L'\n' || ch == L'\t' || ch == L' ';
            if (is_space) {
                if (!last_was_space && !cleaned.empty()) {
                    cleaned.push_back(L' ');
                }
                last_was_space = true;
            }
            else if (ch >= 0x20) {
                cleaned.push_back(ch);
                last_was_space = false;
            }
            if (cleaned.size() >= max_tts_chars) {
                break;
            }
        }
        while (!cleaned.empty() && cleaned.back() == L' ') {
            cleaned.pop_back();
        }
        return cleaned;
    }

    std::vector<uint32_t> WStringCodepoints(const std::wstring& value)
    {
        std::vector<uint32_t> out;
        out.reserve(value.size());
        for (const wchar_t ch : value) {
            out.push_back(static_cast<uint32_t>(ch));
        }
        return out;
    }

    std::string LowerAscii(std::string value)
    {
        std::ranges::transform(value, value.begin(), [](const unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        return value;
    }

    bool IsWavAudio(const std::filesystem::path& path, const std::string& mime_type)
    {
        const std::string lowered_mime = LowerAscii(mime_type);
        if (lowered_mime == "audio/wav" || lowered_mime == "audio/wave" || lowered_mime == "audio/x-wav") {
            return true;
        }
        return LowerAscii(path.extension().string()) == ".wav";
    }

    std::string AudioCacheExtension(const std::string& mime_type)
    {
        const std::string lowered_mime = LowerAscii(mime_type);
        if (lowered_mime == "audio/wav" || lowered_mime == "audio/wave" || lowered_mime == "audio/x-wav") {
            return ".wav";
        }
        if (lowered_mime == "audio/mpeg" || lowered_mime == "audio/mp3") {
            return ".mp3";
        }
        return ".mp3";
    }

    void PlayAudioAsync(const std::filesystem::path& path, const std::string& mime_type = {})
    {
        using MciSendStringWFn = DWORD(WINAPI*)(LPCWSTR, LPWSTR, UINT, HWND);
        static HMODULE winmm = LoadLibraryW(L"winmm.dll");
        static auto mci_send_string = winmm ? reinterpret_cast<MciSendStringWFn>(GetProcAddress(winmm, "mciSendStringW")) : nullptr;
        static std::mutex mci_mutex;
        static std::wstring current_alias;
        static uint32_t alias_id = 0;

        if (!mci_send_string) {
            return;
        }

        std::lock_guard lock(mci_mutex);
        if (!current_alias.empty()) {
            const std::wstring close = L"close " + current_alias;
            mci_send_string(close.c_str(), nullptr, 0, nullptr);
            current_alias.clear();
        }

        current_alias = L"PlaymateTts" + std::to_wstring(++alias_id);
        const std::wstring device_type = IsWavAudio(path, mime_type) ? L"waveaudio" : L"mpegvideo";
        const std::wstring open = L"open \"" + path.wstring() + L"\" type " + device_type + L" alias " + current_alias;
        if (mci_send_string(open.c_str(), nullptr, 0, nullptr) != 0) {
            current_alias.clear();
            return;
        }
        const std::wstring play = L"play " + current_alias;
        mci_send_string(play.c_str(), nullptr, 0, nullptr);
    }

    bool DownloadAudioUrl(const std::string& url, const std::filesystem::path& audio_path, std::string* error)
    {
        if (url.empty()) {
            if (error) {
                *error = "empty audio URL";
            }
            return false;
        }

        HttpRequest request;
        request.SetUrl(url.c_str());
        request.SetMethod(HttpMethod::Get);
        request.SetUserAgent("GWToolbox++ Playmate");
        request.SetTimeoutMs(15000);
        request.SetConnectTimeoutMs(2500);
        request.SetFollowLocation(true);
        request.SetVerifyHost(false);
        request.SetVerifyPeer(false);
        request.SetHeader("Accept", "audio/mpeg,audio/wav,audio/*");

        if (!request.Perform() || request.GetStatusCode() < 200 || request.GetStatusCode() >= 300 || request.GetContent().empty()) {
            if (error) {
                *error = std::format("audio download failed: HTTP {}", request.GetStatusCode());
            }
            return false;
        }

        std::ofstream out(audio_path, std::ios::binary);
        out.write(request.GetContent().data(), static_cast<std::streamsize>(request.GetContent().size()));
        if (!out.good()) {
            if (error) {
                *error = "audio cache write failed";
            }
            return false;
        }
        return true;
    }

    bool SpeakWithWindowsFemaleVoice(const std::wstring& text)
    {
        if (text.empty()) {
            return false;
        }

        const HRESULT coinit = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        const bool uninitialize = SUCCEEDED(coinit);
        if (FAILED(coinit) && coinit != RPC_E_CHANGED_MODE) {
            return false;
        }

        ISpVoice* voice = nullptr;
        HRESULT hr = CoCreateInstance(CLSID_SpVoice, nullptr, CLSCTX_ALL, IID_ISpVoice, reinterpret_cast<void**>(&voice));
        if (SUCCEEDED(hr) && voice) {
            ISpObjectTokenCategory* category = nullptr;
            ISpObjectToken* female_voice = nullptr;
            IEnumSpObjectTokens* voices = nullptr;
            if (SUCCEEDED(CoCreateInstance(CLSID_SpObjectTokenCategory, nullptr, CLSCTX_ALL, IID_ISpObjectTokenCategory, reinterpret_cast<void**>(&category))) && category) {
                if (SUCCEEDED(category->SetId(SPCAT_VOICES, FALSE))
                    && SUCCEEDED(category->EnumTokens(L"Gender=Female", nullptr, &voices))
                    && voices) {
                    ULONG fetched = 0;
                    if (SUCCEEDED(voices->Next(1, &female_voice, &fetched)) && fetched == 1 && female_voice) {
                        voice->SetVoice(female_voice);
                        female_voice->Release();
                    }
                    voices->Release();
                }
                category->Release();
            }
            hr = voice->Speak(text.c_str(), SPF_DEFAULT, nullptr);
            voice->Release();
        }

        if (uninitialize) {
            CoUninitialize();
        }
        return SUCCEEDED(hr);
    }
}

DLLAPI ToolboxPlugin* ToolboxPluginInstance()
{
    static PlaymatePlugin instance;
    return &instance;
}

PlaymatePlugin::PlaymatePlugin()
{
    ApplyConfig();
}

PlaymatePlugin::~PlaymatePlugin()
{
    StopWorker();
}

void PlaymatePlugin::Initialize(ImGuiContext* ctx, const ImGuiAllocFns allocator_fns, const HMODULE toolbox_dll)
{
    ToolboxUIPlugin::Initialize(ctx, allocator_fns, toolbox_dll);
    active_plugin = this;
    InitHttpClient();
    RegisterHooks();
    StartWorker();
    QueueSnapshotEvent("plugin_started");
}

void PlaymatePlugin::SignalTerminate()
{
    RemoveHooks();
    StopWorker();
    ToolboxUIPlugin::SignalTerminate();
}

void PlaymatePlugin::Terminate()
{
    RemoveHooks();
    StopWorker();
    ShutdownHttpClient();
    if (active_plugin == this) {
        active_plugin = nullptr;
    }
    ToolboxUIPlugin::Terminate();
}

bool PlaymatePlugin::CanTerminate()
{
    return !worker_.joinable();
}

void PlaymatePlugin::LoadSettings(const wchar_t* folder)
{
    ToolboxUIPlugin::LoadSettings(folder);
    std::string backend_url = backend_url_input_;
    std::string api_token = api_token_input_;
    LoadSetting("enabled", enabled_);
    LoadSetting("local_capture", local_capture_);
    LoadSetting("send_to_backend", send_to_backend_);
    LoadSetting("inject_replies", inject_replies_);
    LoadSetting("show_speech_bubbles", show_speech_bubbles_);
    LoadSetting("speak_replies", speak_replies_);
    LoadSetting("environment_radar", environment_radar_);
    LoadSetting("backend_url", backend_url);
    LoadSetting("api_token", api_token);
    LoadSetting("poll_interval_sec", poll_interval_sec_);
    LoadSetting("snapshot_interval_sec", snapshot_interval_sec_);
    LoadSetting("radar_interval_sec", radar_interval_sec_);

    strncpy_s(backend_url_input_, backend_url.c_str(), _TRUNCATE);
    strncpy_s(api_token_input_, api_token.c_str(), _TRUNCATE);
    {
        std::lock_guard lock(config_mutex_);
        const std::filesystem::path plugin_folder = folder;
        const auto computer_folder = plugin_folder.parent_path();
        local_log_folder_ = (computer_folder.empty() ? plugin_folder : computer_folder) / L"Playmate";
    }
    poll_interval_sec_ = std::clamp(poll_interval_sec_, 0.25f, 30.0f);
    snapshot_interval_sec_ = std::clamp(snapshot_interval_sec_, 30.0f, 120.0f);
    radar_interval_sec_ = std::clamp(radar_interval_sec_, 2.0f, 30.0f);
    ApplyConfig();
}

void PlaymatePlugin::SaveSettings(const wchar_t* folder)
{
    SaveSetting("enabled", enabled_);
    SaveSetting("local_capture", local_capture_);
    SaveSetting("send_to_backend", send_to_backend_);
    SaveSetting("inject_replies", inject_replies_);
    SaveSetting("show_speech_bubbles", show_speech_bubbles_);
    SaveSetting("speak_replies", speak_replies_);
    SaveSetting("environment_radar", environment_radar_);
    SaveSetting("backend_url", std::string(backend_url_input_));
    SaveSetting("api_token", std::string(api_token_input_));
    SaveSetting("poll_interval_sec", poll_interval_sec_);
    SaveSetting("snapshot_interval_sec", snapshot_interval_sec_);
    SaveSetting("radar_interval_sec", radar_interval_sec_);
    ToolboxUIPlugin::SaveSettings(folder);
}

void PlaymatePlugin::DrawSettings()
{
    bool config_changed = false;
    config_changed |= ImGui::Checkbox("Enable telemetry", &enabled_);
    config_changed |= ImGui::Checkbox("Write local JSONL capture", &local_capture_);
    config_changed |= ImGui::Checkbox("Send telemetry to backend", &send_to_backend_);
    config_changed |= ImGui::Checkbox("Inject companion replies into party chat", &inject_replies_);
    config_changed |= ImGui::Checkbox("Show companion speech bubbles", &show_speech_bubbles_);
    config_changed |= ImGui::Checkbox("Speak companion replies", &speak_replies_);
    config_changed |= ImGui::Checkbox("Enable environment radar", &environment_radar_);
    config_changed |= ImGui::InputText("Local backend URL", backend_url_input_, sizeof(backend_url_input_));
    config_changed |= ImGui::InputText("Local API token", api_token_input_, sizeof(api_token_input_), ImGuiInputTextFlags_Password);
    config_changed |= ImGui::SliderFloat("Reply poll interval", &poll_interval_sec_, 0.25f, 10.0f, "%.2fs");
    config_changed |= ImGui::SliderFloat("Snapshot interval", &snapshot_interval_sec_, 30.0f, 120.0f, "%.0fs");
    config_changed |= ImGui::SliderFloat("Radar interval", &radar_interval_sec_, 2.0f, 15.0f, "%.1fs");
    if (config_changed) {
        ApplyConfig();
    }

    std::lock_guard lock(status_mutex_);
    ImGui::Separator();
    ImGui::Text("Status: %s", status_.c_str());
    ImGui::TextWrapped("Last event: %s", last_event_status_.c_str());
    if (waiting_for_reply_) {
        char age[32] = {};
        ImGui::Text("Companion: waiting for reply (%s)", AgeText(waiting_since_ms_, age, sizeof(age)));
    }
    else {
        ImGui::Text("Companion: idle");
    }
    ImGui::TextWrapped("Last reply: %s", last_reply_status_.c_str());
    if (!last_backend_error_.empty()) {
        ImGui::TextWrapped("Last backend error: %s", last_backend_error_.c_str());
    }
    const auto persona = CurrentPersonaName();
    ImGui::Text("Persona: %s", persona.c_str());
    const auto log_path = WideToUtf8(LocalLogPath().wstring().c_str());
    ImGui::TextWrapped("Local log: %s", log_path.c_str());
    ImGui::Text("Local: %zu", local_written_count_);
    ImGui::SameLine();
    ImGui::Text("Sent: %zu", sent_count_);
    ImGui::SameLine();
    ImGui::Text("Failed: %zu", failed_count_);
    ImGui::SameLine();
    ImGui::Text("Replies: %zu", received_count_);
}

void PlaymatePlugin::Draw(IDirect3DDevice9*)
{
    if (!GetVisiblePtr() || !*GetVisiblePtr()) {
        return;
    }
    if (ImGui::Begin(Name(), GetVisiblePtr(), GetWinFlags())) {
        DrawSettings();
    }
    ImGui::End();
}

void PlaymatePlugin::Update(const float delta_ms)
{
    FlushRepliesToChat();

    if (!enabled_) {
        return;
    }

    snapshot_elapsed_ms_ += delta_ms;
    radar_elapsed_ms_ += delta_ms;
    const Snapshot snapshot = BuildSnapshot();
    const bool map_changed = snapshot.map_id != 0 && snapshot.map_id != last_map_id_;
    const bool quest_changed = snapshot.active_quest_id != last_active_quest_id_;
    const bool interval_elapsed = snapshot_elapsed_ms_ >= snapshot_interval_sec_ * 1000.0f;
    if (map_changed || quest_changed || interval_elapsed) {
        QueueSnapshotEvent(map_changed ? "map_changed" : quest_changed ? "active_quest_changed" : "snapshot");
        snapshot_elapsed_ms_ = 0.0f;
        last_map_id_ = snapshot.map_id;
        last_active_quest_id_ = snapshot.active_quest_id;
    }
    if (radar_elapsed_ms_ >= radar_interval_sec_ * 1000.0f) {
        radar_elapsed_ms_ = 0.0f;
        MaybeQueueEnvironmentAlert();
    }
}

void PlaymatePlugin::RegisterHooks()
{
    GW::UI::RegisterUIMessageCallback(&send_chat_entry_, GW::UI::UIMessage::kSendChatMessage, OnSendChat, 0x8000);
    GW::UI::RegisterUIMessageCallback(&write_chat_entry_, GW::UI::UIMessage::kWriteToChatLog, OnWriteToChatLog, 0x8000);
    GW::UI::RegisterUIMessageCallback(&world_event_entry_, GW::UI::UIMessage::kMapLoaded, OnMapOrQuestEvent, 0x8000);
    GW::UI::RegisterUIMessageCallback(&world_event_entry_, GW::UI::UIMessage::kMapChange, OnMapOrQuestEvent, 0x8000);
    GW::UI::RegisterUIMessageCallback(&world_event_entry_, GW::UI::UIMessage::kQuestAdded, OnMapOrQuestEvent, 0x8000);
    GW::UI::RegisterUIMessageCallback(&world_event_entry_, GW::UI::UIMessage::kQuestDetailsChanged, OnMapOrQuestEvent, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::AgentAdd>(&stoc_event_entry_, OnAgentAdd, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::AgentState>(&stoc_event_entry_, OnAgentState, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::PartyDefeated>(&stoc_event_entry_, OnPartyDefeated, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::SpeechBubble>(&stoc_event_entry_, OnSpeechBubble, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::ObjectiveAdd>(&stoc_event_entry_, OnObjectiveAdd, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::ObjectiveDone>(&stoc_event_entry_, OnObjectiveDone, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::ObjectiveUpdateName>(&stoc_event_entry_, OnObjectiveUpdateName, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::CreateMissionProgress>(&stoc_event_entry_, OnCreateMissionProgress, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::UpdateMissionProgress>(&stoc_event_entry_, OnUpdateMissionProgress, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::VanquishProgress>(&stoc_event_entry_, OnVanquishProgress, 0x8000);
    GW::StoC::RegisterPacketCallback<GW::Packet::StoC::VanquishComplete>(&stoc_event_entry_, OnVanquishComplete, 0x8000);
}

void PlaymatePlugin::RemoveHooks()
{
    GW::UI::RemoveUIMessageCallback(&send_chat_entry_);
    GW::UI::RemoveUIMessageCallback(&write_chat_entry_);
    GW::UI::RemoveUIMessageCallback(&world_event_entry_);
    GW::StoC::RemoveCallbacks(&stoc_event_entry_);
}

void PlaymatePlugin::StartWorker()
{
    if (running_.exchange(true)) {
        return;
    }
    worker_ = std::thread(&PlaymatePlugin::WorkerLoop, this);
}

void PlaymatePlugin::StopWorker()
{
    if (!running_.exchange(false)) {
        return;
    }
    queue_cv_.notify_all();
    if (worker_.joinable()) {
        worker_.join();
    }
}

void PlaymatePlugin::WorkerLoop()
{
    auto next_poll = std::chrono::steady_clock::now();
    while (running_) {
        TelemetryEvent event;
        bool has_event = false;
        {
            std::unique_lock lock(queue_mutex_);
            queue_cv_.wait_until(lock, next_poll, [&] { return !running_ || !outbound_.empty() || !tts_requests_.empty(); });
            if (!running_) {
                break;
            }
            if (!tts_requests_.empty()) {
                auto reply = std::move(tts_requests_.front());
                tts_requests_.pop_front();
                lock.unlock();
                GenerateAndPlayCompanionTts(reply);
                continue;
            }
            if (!outbound_.empty()) {
                event = std::move(outbound_.front());
                outbound_.pop_front();
                has_event = true;
            }
        }

        if (has_event) {
            bool wrote_local = false;
            bool posted_remote = false;
            bool failed = false;

            if (local_capture_enabled_.load()) {
                wrote_local = WriteTelemetryLocal(event);
                failed = failed || !wrote_local;
            }

            if (backend_enabled_.load()) {
                posted_remote = PostTelemetry(event);
                failed = failed || !posted_remote;
            }

            std::lock_guard lock(status_mutex_);
            if (wrote_local) {
                ++local_written_count_;
            }
            if (posted_remote) {
                ++sent_count_;
                last_sent_ms_ = MonotonicMs();
                last_backend_error_.clear();
                last_event_status_ = std::format("{} accepted by bridge", event.event_type);
                if (IsReplyTriggerEvent(event.event_type)) {
                    waiting_for_reply_ = true;
                    waiting_since_ms_ = last_sent_ms_;
                    last_reply_status_ = "Waiting for Hermes";
                }
            }
            if (failed) {
                ++failed_count_;
                last_event_status_ = std::format("{} failed", event.event_type);
                if (wrote_local) {
                    status_ = "Captured locally; backend failed";
                }
            }
            else {
                status_ = posted_remote ? "Captured and sent" : "Captured locally";
            }

            if (posted_remote && IsReplyTriggerEvent(event.event_type)) {
                next_poll = std::chrono::steady_clock::now();
            }
        }

        const auto now = std::chrono::steady_clock::now();
        if (now >= next_poll) {
            if (ShouldPollReplies()) {
                PollReplies();
            }
            next_poll = now + std::chrono::milliseconds(ReplyPollDelayMs());
        }
    }
}

bool PlaymatePlugin::WriteTelemetryLocal(const TelemetryEvent& event)
{
    const auto path = LocalLogPath();
    if (path.empty()) {
        SetStatus("Local log path is empty");
        return false;
    }

    std::error_code ec;
    std::filesystem::create_directories(path.parent_path(), ec);
    if (ec) {
        SetStatus("Failed to create local log folder");
        return false;
    }

    std::ofstream out(path, std::ios::app | std::ios::binary);
    if (!out) {
        SetStatus("Failed to open local telemetry log");
        return false;
    }

    out << glz::write_json(event).value_or(std::string{}) << '\n';
    return out.good();
}

bool PlaymatePlugin::PostTelemetry(const TelemetryEvent& event)
{
    const auto [backend_url, token] = GetConfig();
    if (backend_url.empty()) {
        SetStatus("Backend URL is empty");
        return false;
    }

    HttpRequest request;
    request.SetUrl(EventsUrl().c_str());
    request.SetMethod(HttpMethod::Post);
    request.SetUserAgent("GWToolbox++ Playmate");
    request.SetTimeoutMs(1500);
    request.SetConnectTimeoutMs(750);
    request.SetHeader("Content-Type", "application/json");
    if (!token.empty()) {
        request.SetHeader("Authorization", ("Bearer " + token).c_str());
    }

    const auto json = glz::write_json(event).value_or(std::string{});
    request.SetPostContent(json, ContentFlag::Copy);
    const bool ok = request.Perform() && request.GetStatusCode() >= 200 && request.GetStatusCode() < 300;
    if (!ok) {
        const auto error = std::format("POST failed: {} HTTP {}", request.GetStatusStr(), request.GetStatusCode());
        {
            std::lock_guard lock(status_mutex_);
            last_backend_error_ = error;
        }
        SetStatus(error);
    }
    return ok;
}

void PlaymatePlugin::PollReplies()
{
    if (!telemetry_enabled_.load() || !backend_enabled_.load() || !reply_injection_enabled_.load()) {
        return;
    }

    const auto [backend_url, token] = GetConfig();
    if (backend_url.empty()) {
        return;
    }

    HttpRequest request;
    request.SetUrl(RepliesUrl().c_str());
    request.SetMethod(HttpMethod::Get);
    request.SetUserAgent("GWToolbox++ Playmate");
    request.SetTimeoutMs(1200);
    request.SetConnectTimeoutMs(500);
    if (!token.empty()) {
        request.SetHeader("Authorization", ("Bearer " + token).c_str());
    }

    if (!(request.Perform() && request.GetStatusCode() >= 200 && request.GetStatusCode() < 300)) {
        return;
    }

    auto& content = request.GetContent();
    if (content.empty()) {
        return;
    }

    RepliesResponse parsed;
    if (auto ec = glz::read_json(parsed, content); !ec) {
        if (!parsed.reply_items.empty()) {
            for (const auto& reply : parsed.reply_items) {
                if (!reply.message.empty()) {
                    QueueReply({
                        Utf8ToWide(reply.message),
                        reply.audio_url,
                        reply.audio_mime_type,
                        reply.suppress_tts,
                        reply.multi_message,
                        reply.line_index,
                        reply.line_count,
                        reply.reply_delay_ms,
                        reply.post_play_delay_ms,
                    });
                }
            }
        }
        else {
            for (const auto& reply : parsed.replies) {
                if (!reply.empty()) {
                    QueueReply({Utf8ToWide(reply)});
                }
            }
        }
        return;
    }

    const auto first_non_space = std::ranges::find_if(content, [](const char ch) {
        return !std::isspace(static_cast<unsigned char>(ch));
    });
    if (first_non_space == content.end() || *first_non_space == '{' || *first_non_space == '[') {
        std::lock_guard lock(status_mutex_);
        last_reply_status_ = "Reply JSON parse failed";
        return;
    }

    QueueReply({Utf8ToWide(content)});
}

bool PlaymatePlugin::ShouldPollReplies() const
{
    if (!telemetry_enabled_.load() || !backend_enabled_.load() || !reply_injection_enabled_.load()) {
        return false;
    }

    std::lock_guard lock(status_mutex_);
    const uint64_t now = MonotonicMs();
    if (waiting_for_reply_) {
        return true;
    }
    return last_reply_ms_ > 0 && now - last_reply_ms_ < 15000;
}

int PlaymatePlugin::ReplyPollDelayMs() const
{
    if (!telemetry_enabled_.load() || !backend_enabled_.load() || !reply_injection_enabled_.load()) {
        return 60000;
    }

    std::lock_guard lock(status_mutex_);
    const uint64_t now = MonotonicMs();
    if (waiting_for_reply_) {
        return std::min(poll_interval_ms_.load(), 350);
    }
    if (last_reply_ms_ > 0 && now - last_reply_ms_ < 15000) {
        return poll_interval_ms_.load();
    }
    return 60000;
}

void PlaymatePlugin::QueueTelemetry(std::string event_type, std::string sender, std::string channel, std::string message)
{
    if (!telemetry_enabled_.load() || (!local_capture_enabled_.load() && !backend_enabled_.load()) || message.empty()) {
        return;
    }
    if (channel == "trade") {
        return;
    }

    const Snapshot snapshot = BuildSnapshot();
    TelemetryEvent event;
    event.persona = CurrentPersonaName();
    event.client_time = CurrentUtcTimestamp();
    event.event_type = std::move(event_type);
    event.sender = std::move(sender);
    event.channel = std::move(channel);
    event.message = std::move(message);
    event.map_id = snapshot.map_id;
    event.map_name = snapshot.map_name;
    event.instance_type = snapshot.instance_type;
    event.district = snapshot.district;
    event.instance_time = snapshot.instance_time;
    event.active_quest_id = snapshot.active_quest_id;
    event.quest_count = snapshot.quest_count;
    event.active_quest_name = snapshot.active_quest_name;
    event.active_quest_objectives = snapshot.active_quest_objectives;

    {
        std::lock_guard lock(queue_mutex_);
        if (outbound_.size() >= 256) {
            outbound_.pop_front();
        }
        outbound_.push_back(std::move(event));
    }
    queue_cv_.notify_one();
}

void PlaymatePlugin::QueueEnvironmentAlert(std::string alert_type, std::string severity, std::string message, const EnvironmentScan& scan)
{
    if (!telemetry_enabled_.load() || (!local_capture_enabled_.load() && !backend_enabled_.load()) || message.empty()) {
        return;
    }

    const Snapshot snapshot = BuildSnapshot();
    TelemetryEvent event;
    event.persona = CurrentPersonaName();
    event.client_time = CurrentUtcTimestamp();
    event.event_type = "environment_alert";
    event.sender = "System";
    event.channel = "system";
    event.message = std::move(message);
    event.map_id = snapshot.map_id;
    event.map_name = snapshot.map_name;
    event.instance_type = snapshot.instance_type;
    event.district = snapshot.district;
    event.instance_time = snapshot.instance_time;
    event.active_quest_id = snapshot.active_quest_id;
    event.quest_count = snapshot.quest_count;
    event.active_quest_name = snapshot.active_quest_name;
    event.active_quest_objectives = snapshot.active_quest_objectives;
    event.player_x = scan.player_x;
    event.player_y = scan.player_y;
    event.player_hp = scan.player_hp;
    event.player_hp_previous = scan.player_hp_previous;
    event.player_hp_drop = scan.player_hp_drop;
    event.hp_threshold_crossed = scan.hp_threshold_crossed;
    event.damage_severity = scan.damage_severity;
    event.hostile_count = scan.hostile_count;
    event.close_hostile_count = scan.close_hostile_count;
    event.dead_hostile_count = scan.dead_hostile_count;
    event.closest_hostile_agent_id = scan.closest_hostile_agent_id;
    event.closest_hostile_distance = scan.closest_hostile_distance;
    event.agent_id = scan.selected_target_agent_id;
    event.agent_name = scan.selected_target_name;
    event.alert_type = std::move(alert_type);
    event.severity = std::move(severity);

    {
        std::lock_guard lock(queue_mutex_);
        if (outbound_.size() >= 256) {
            outbound_.pop_front();
        }
        outbound_.push_back(std::move(event));
    }
    queue_cv_.notify_one();
}

void PlaymatePlugin::QueueGameplayEvent(TelemetryEvent event)
{
    if (!telemetry_enabled_.load() || (!local_capture_enabled_.load() && !backend_enabled_.load()) || event.message.empty()) {
        return;
    }

    const Snapshot snapshot = BuildSnapshot();
    event.persona = CurrentPersonaName();
    event.client_time = CurrentUtcTimestamp();
    if (event.sender.empty()) {
        event.sender = "System";
    }
    if (event.channel.empty()) {
        event.channel = "system";
    }
    event.map_id = snapshot.map_id;
    event.map_name = snapshot.map_name;
    event.instance_type = snapshot.instance_type;
    event.district = snapshot.district;
    event.instance_time = snapshot.instance_time;
    event.active_quest_id = snapshot.active_quest_id;
    event.quest_count = snapshot.quest_count;
    event.active_quest_name = snapshot.active_quest_name;
    event.active_quest_objectives = snapshot.active_quest_objectives;

    {
        std::lock_guard lock(queue_mutex_);
        if (outbound_.size() >= 256) {
            outbound_.pop_front();
        }
        outbound_.push_back(std::move(event));
    }
    queue_cv_.notify_one();
}

void PlaymatePlugin::QueueItemDropEvent(const GW::Packet::StoC::AgentAdd& packet)
{
    if (!telemetry_enabled_.load() || (!local_capture_enabled_.load() && !backend_enabled_.load())) {
        return;
    }
    if (packet.type != 4 || packet.unk3 != 0) {
        return;
    }

    const GW::Item* item = GW::Items::GetItemById(packet.agent_type);
    const std::string dye_name = DyeItemName(item);
    const std::string rarity_name = NotableRarityName(item);
    const bool is_black_dye = dye_name == "Black Dye";
    if (!is_black_dye && rarity_name.empty()) {
        return;
    }
    const std::string item_name = is_black_dye ? dye_name : std::format("{} rarity item", rarity_name);

    RecentHostileDeath likely_source;
    float best_distance = std::numeric_limits<float>::max();
    const uint64_t now_ms = MonotonicMs();
    {
        std::lock_guard lock(gameplay_state_mutex_);
        for (const RecentHostileDeath& death : recent_hostile_deaths_) {
            if (now_ms - death.observed_ms > 20000) {
                continue;
            }
            const float dx = packet.position.x - death.x;
            const float dy = packet.position.y - death.y;
            const float distance = std::sqrt(dx * dx + dy * dy);
            if (distance < best_distance) {
                best_distance = distance;
                likely_source = death;
            }
        }
    }

    TelemetryEvent event;
    event.event_type = "item_drop";
    event.sender = "Loot";
    event.channel = "system";
    event.severity = "NORMAL";
    event.alert_type = "item_drop";
    event.message = item_name;
    if (likely_source.agent_id && best_distance <= 1800.0f && !likely_source.agent_name.empty()) {
        event.agent_id = likely_source.agent_id;
        event.agent_name = likely_source.agent_name;
        event.message = std::format("Item dropped: {}, likely from {}.", item_name, likely_source.agent_name);
    }
    else {
        event.message = std::format("Item dropped: {}.", item_name);
    }
    QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::QueueSnapshotEvent(const char* event_type)
{
    if (!event_type || !*event_type) {
        return;
    }
    const std::string_view type(event_type);
    if (type == "map_loaded" && !GW::Map::GetIsMapLoaded()) {
        return;
    }
    QueueTelemetry(event_type, "System", "system", event_type);
}

void PlaymatePlugin::MaybeQueueEnvironmentAlert()
{
    if (!environment_radar_enabled_.load()) {
        return;
    }
    EnvironmentScan scan = BuildEnvironmentScan();
    if (!scan.valid) {
        last_hostile_count_ = 0;
        last_close_hostile_count_ = 0;
        last_player_hp_ = 0.0f;
        last_in_combat_ = false;
        return;
    }

    const bool entered_close_range = scan.close_hostile_count > 0 && last_close_hostile_count_ == 0;
    const bool danger_spike = scan.close_hostile_count >= 3 && scan.close_hostile_count > last_close_hostile_count_;
    const bool combat_started = scan.in_combat && !last_in_combat_;
    const bool combat_ended = !scan.in_combat && last_in_combat_ && scan.hostile_count == 0;
    const bool has_hp_baseline = last_player_hp_ > 0.0f;
    const float hp_drop = has_hp_baseline ? last_player_hp_ - scan.player_hp : 0.0f;
    const auto crossed_threshold = [&](const float threshold) {
        return has_hp_baseline && last_player_hp_ >= threshold && scan.player_hp < threshold;
    };
    const bool crossed_below_75_hp = crossed_threshold(0.75f);
    const bool crossed_below_half_hp = crossed_threshold(0.50f);
    const bool crossed_below_35_hp = crossed_threshold(0.35f);
    const bool crossed_below_20_hp = crossed_threshold(0.20f);
    const bool crossed_damage_threshold =
        crossed_below_75_hp || crossed_below_half_hp || crossed_below_35_hp || crossed_below_20_hp;
    const bool significant_hp_drop = hp_drop >= 0.02f;

    if (significant_hp_drop || crossed_damage_threshold) {
        scan.player_hp_previous = last_player_hp_;
        scan.player_hp_drop = std::max(0.0f, hp_drop);
        if (crossed_below_20_hp) {
            scan.hp_threshold_crossed = "20%";
        }
        else if (crossed_below_35_hp) {
            scan.hp_threshold_crossed = "35%";
        }
        else if (crossed_below_half_hp) {
            scan.hp_threshold_crossed = "50%";
        }
        else if (crossed_below_75_hp) {
            scan.hp_threshold_crossed = "75%";
        }
        if (scan.player_hp < 0.20f || crossed_below_20_hp) {
            scan.damage_severity = "near_death";
        }
        else if (scan.player_hp < 0.35f || crossed_below_35_hp) {
            scan.damage_severity = "critical";
        }
        else if (hp_drop >= 0.12f || crossed_below_half_hp) {
            scan.damage_severity = "heavy";
        }
        else {
            scan.damage_severity = "normal";
        }
        QueueEnvironmentAlert(
            "under_attack",
            (crossed_below_half_hp || scan.player_hp < 0.35f || hp_drop >= 0.12f) ? "HIGH" : "NORMAL",
            std::format(
                "Azele is taking hits. Health is at {:.0f} percent after a {:.0f} percent drop.",
                scan.player_hp * 100.0f,
                scan.player_hp_drop * 100.0f),
            scan);
    }
    else if (danger_spike) {
        QueueEnvironmentAlert(
            "danger_spike",
            "HIGH",
            std::format("{} hostile enemies are close.", scan.close_hostile_count),
            scan);
    }
    else if (combat_started) {
        const std::string target_label = scan.selected_target_name.empty() ? "selected target" : scan.selected_target_name;
        QueueEnvironmentAlert(
            "combat_started",
            "HIGH",
            scan.selected_target_agent_id
                ? std::format("Combat started with {} selected.", target_label)
                : "Combat started.",
            scan);
    }
    else if (entered_close_range) {
        QueueEnvironmentAlert(
            "enemy_patrol_nearby",
            "NORMAL",
            std::format("Enemy nearby at {:.0f} range.", scan.closest_hostile_distance),
            scan);
    }
    else if (combat_ended) {
        QueueEnvironmentAlert("combat_over", "LOW", "Combat ended.", scan);
    }

    last_hostile_count_ = scan.hostile_count;
    last_close_hostile_count_ = scan.close_hostile_count;
    last_player_hp_ = scan.player_hp;
    last_in_combat_ = scan.in_combat;
}

void PlaymatePlugin::QueueReply(QueuedReply reply)
{
    if (reply.message.empty()) {
        return;
    }
    {
        std::lock_guard lock(queue_mutex_);
        const uint64_t now = MonotonicMs();
        if (reply.multi_message && reply.line_index > 1) {
            reply.not_before_ms = std::max(now + reply.reply_delay_ms, next_multi_reply_allowed_ms_);
        }
        else {
            reply.not_before_ms = now;
        }
        if (reply.multi_message && reply.line_count > reply.line_index) {
            const uint32_t delay_ms = reply.post_play_delay_ms > 0
                ? reply.post_play_delay_ms
                : EstimateMultiMessageDelayMs(reply.message);
            next_multi_reply_allowed_ms_ = std::max(next_multi_reply_allowed_ms_, now + delay_ms);
        }
        inbound_replies_.push_back(std::move(reply));
    }
}

void PlaymatePlugin::FlushRepliesToChat()
{
    if (!reply_injection_enabled_.load()) {
        return;
    }

    while (true) {
        QueuedReply reply;
        {
            std::lock_guard lock(queue_mutex_);
            if (inbound_replies_.empty()) {
                return;
            }
            const uint64_t now = MonotonicMs();
            if (inbound_replies_.front().not_before_ms > now) {
                return;
            }
            reply = std::move(inbound_replies_.front());
            inbound_replies_.pop_front();
        }
        const auto persona = CurrentPersonaNameWide();
        ShowCompanionSpeechBubble(reply.message);
        if (!reply.suppress_tts) {
            QueuedTtsRequest tts_request{reply.message, reply.audio_url, reply.audio_mime_type, reply.suppress_tts};
            if (reply.multi_message && reply.line_count > reply.line_index) {
                tts_request.post_play_delay_ms = reply.post_play_delay_ms > 0
                    ? reply.post_play_delay_ms
                    : EstimateMultiMessageDelayMs(reply.message);
            }
            QueueCompanionTts(std::move(tts_request));
        }
        GW::Chat::WriteChat(GW::Chat::CHANNEL_GROUP, reply.message.c_str(), persona.c_str(), true);
        std::lock_guard lock(status_mutex_);
        ++received_count_;
        waiting_for_reply_ = false;
        last_reply_ms_ = MonotonicMs();
        last_reply_status_ = reply.suppress_tts ? "Reply received; TTS suppressed" : "Reply received";
    }
}

void PlaymatePlugin::QueueCompanionTts(QueuedTtsRequest request)
{
    if (request.suppress_tts || !tts_enabled_.load()) {
        return;
    }
    request.message = TtsMessage(request.message);
    if (request.message.empty() && request.audio_url.empty()) {
        return;
    }
    {
        std::lock_guard lock(queue_mutex_);
        if (tts_requests_.size() >= 4) {
            tts_requests_.pop_front();
        }
        tts_requests_.push_back(std::move(request));
    }
    queue_cv_.notify_one();
}

void PlaymatePlugin::WaitForTtsPlaybackSlot()
{
    const uint64_t now = MonotonicMs();
    if (next_tts_play_allowed_ms_ > now) {
        std::this_thread::sleep_for(std::chrono::milliseconds(next_tts_play_allowed_ms_ - now));
    }
}

void PlaymatePlugin::MarkTtsPlaybackStarted(const std::wstring& reply, const uint32_t extra_delay_ms)
{
    next_tts_play_allowed_ms_ = MonotonicMs() + std::max(EstimateTtsPostPlayDelayMs(reply), extra_delay_ms);
}

void PlaymatePlugin::GenerateAndPlayCompanionTts(const QueuedTtsRequest& request)
{
    const std::wstring& reply = request.message;
    if (reply.empty()) {
        return;
    }

    const auto cache_dir = LocalLogPath().parent_path() / "tts-cache";
    std::error_code ec;
    std::filesystem::create_directories(cache_dir, ec);
    if (ec) {
        std::lock_guard lock(status_mutex_);
        last_reply_status_ = "TTS cache unavailable";
        return;
    }

    if (!request.audio_url.empty()) {
        const auto cache_key = std::format("{:x}{}", std::hash<std::string>{}(request.audio_url), AudioCacheExtension(request.audio_mime_type));
        const auto audio_path = cache_dir / cache_key;
        if (!std::filesystem::exists(audio_path)) {
            std::string error;
            if (!DownloadAudioUrl(request.audio_url, audio_path, &error)) {
                std::lock_guard lock(status_mutex_);
                last_reply_status_ = error.empty() ? "Companion audio download failed" : error;
            }
            else {
                std::lock_guard lock(status_mutex_);
                last_reply_status_ = "Companion audio downloaded";
            }
        }
        if (std::filesystem::exists(audio_path)) {
            WaitForTtsPlaybackSlot();
            const std::string audio_mime_type = request.audio_mime_type;
            GW::GameThread::Enqueue([audio_path, audio_mime_type] {
                PlayAudioAsync(audio_path, audio_mime_type);
            });
            MarkTtsPlaybackStarted(reply, request.post_play_delay_ms);
            return;
        }
    }

    const auto cache_key = std::format("{:x}_{}.mp3", std::hash<std::wstring>{}(reply), reply.size());
    const auto audio_path = cache_dir / cache_key;
    if (!std::filesystem::exists(audio_path)) {
        glz::generic request_body = glz::generic::object_t{};
        const auto codepoints = WStringCodepoints(reply);
        request_body["encoded"] = codepoints;
        request_body["decoded"] = codepoints;
        request_body["language"] = static_cast<uint32_t>(GW::Constants::Language::English);
        request_body["speaker_gender"] = "f";
        request_body["speaker_race"] = "Human";
        request_body["player_gender"] = "f";

        HttpRequest tts_request;
        tts_request.SetUrl("https://tts.gwtoolbox.com/decode.mp3");
        tts_request.SetMethod(HttpMethod::Post);
        tts_request.SetUserAgent("GWToolbox++ Playmate");
        tts_request.SetTimeoutMs(10000);
        tts_request.SetConnectTimeoutMs(2500);
        tts_request.SetFollowLocation(true);
        tts_request.SetVerifyHost(false);
        tts_request.SetVerifyPeer(false);
        tts_request.SetHeader("Content-Type", "application/json");
        tts_request.SetHeader("Accept", "audio/mpeg");
        const auto json = glz::write_json(request_body).value_or(std::string{});
        tts_request.SetPostContent(json, ContentFlag::Copy);

        if (!tts_request.Perform() || tts_request.GetStatusCode() < 200 || tts_request.GetStatusCode() >= 300 || tts_request.GetContent().empty()) {
            const bool fallback_spoke = SpeakWithWindowsFemaleVoice(reply);
            std::lock_guard lock(status_mutex_);
            last_reply_status_ = fallback_spoke
                ? std::format("GWDevHub TTS rejected plain text; used Windows female voice fallback (HTTP {})", tts_request.GetStatusCode())
                : std::format("TTS failed: HTTP {}", tts_request.GetStatusCode());
            return;
        }

        std::ofstream out(audio_path, std::ios::binary);
        out.write(tts_request.GetContent().data(), static_cast<std::streamsize>(tts_request.GetContent().size()));
        if (!out.good()) {
            std::lock_guard lock(status_mutex_);
            last_reply_status_ = "TTS cache write failed";
            return;
        }
    }

    WaitForTtsPlaybackSlot();
    GW::GameThread::Enqueue([audio_path] {
        PlayAudioAsync(audio_path);
    });
    MarkTtsPlaybackStarted(reply, request.post_play_delay_ms);
}

void PlaymatePlugin::ShowCompanionSpeechBubble(const std::wstring& reply) const
{
    if (!speech_bubbles_enabled_.load()) {
        return;
    }

    const std::wstring bubble = SpeechBubbleMessage(reply);
    if (bubble.empty()) {
        return;
    }

    GW::GameThread::Enqueue([bubble] {
        const GW::AgentLiving* player = GW::Agents::GetControlledCharacter();
        if (!player || !player->agent_id) {
            return;
        }

        GW::Packet::StoC::SpeechBubble packet;
        packet.agent_id = player->agent_id;
        wcsncpy_s(packet.message, bubble.c_str(), _TRUNCATE);
        GW::StoC::EmulatePacket(&packet);
    });
}

void PlaymatePlugin::ApplyConfig()
{
    poll_interval_sec_ = std::clamp(poll_interval_sec_, 0.25f, 30.0f);
    snapshot_interval_sec_ = std::clamp(snapshot_interval_sec_, 30.0f, 120.0f);
    telemetry_enabled_.store(enabled_);
    local_capture_enabled_.store(local_capture_);
    backend_enabled_.store(send_to_backend_);
    reply_injection_enabled_.store(inject_replies_);
    speech_bubbles_enabled_.store(show_speech_bubbles_);
    tts_enabled_.store(speak_replies_);
    environment_radar_enabled_.store(environment_radar_);
    poll_interval_ms_.store(static_cast<int>(poll_interval_sec_ * 1000.0f));
    radar_interval_sec_ = std::clamp(radar_interval_sec_, 2.0f, 30.0f);
    std::lock_guard lock(config_mutex_);
    backend_url_ = TrimTrailingSlash(backend_url_input_);
    api_token_ = api_token_input_;
}

void PlaymatePlugin::SetStatus(std::string status)
{
    std::lock_guard lock(status_mutex_);
    status_ = std::move(status);
}

PlaymatePlugin::Snapshot PlaymatePlugin::BuildSnapshot() const
{
    Snapshot snapshot;
    if (!GW::Map::GetIsMapLoaded()) {
        return snapshot;
    }

    snapshot.map_id = static_cast<uint32_t>(GW::Map::GetMapID());
    snapshot.map_name = MapNameForId(snapshot.map_id);
    snapshot.instance_type = static_cast<uint32_t>(GW::Map::GetInstanceType());
    snapshot.district = static_cast<uint32_t>(std::max(0, GW::Map::GetDistrict()));
    snapshot.instance_time = GW::Map::GetInstanceTime();
    snapshot.active_quest_id = static_cast<uint32_t>(GW::QuestMgr::GetActiveQuestId());

    const GW::QuestLog* quest_log = GW::QuestMgr::GetQuestLog();
    if (quest_log && quest_log->valid()) {
        snapshot.quest_count = quest_log->size();
    }

    if (const GW::Quest* quest = GW::QuestMgr::GetActiveQuest()) {
        snapshot.active_quest_name = WideToUtf8(quest->name);
        snapshot.active_quest_objectives = WideToUtf8(quest->objectives);
    }
    return snapshot;
}

std::string PlaymatePlugin::MapNameForId(const uint32_t map_id) const
{
    if (!map_id) {
        return {};
    }

    std::lock_guard lock(map_name_cache_mutex_);
    std::unique_ptr<DecodedMapName>& entry = map_name_cache_[map_id];
    if (!entry) {
        entry = std::make_unique<DecodedMapName>();
    }
    if (entry->decoded[0]) {
        return WideToUtf8(entry->decoded);
    }
    if (entry->requested) {
        return {};
    }

    const GW::AreaInfo* area = GW::Map::GetMapInfo(static_cast<GW::Constants::MapID>(map_id));
    if (!(area && area->name_id && GW::UI::UInt32ToEncStr(area->name_id, entry->encoded, _countof(entry->encoded)))) {
        return {};
    }

    entry->requested = true;
    GW::UI::AsyncDecodeStr(entry->encoded, entry->decoded, _countof(entry->decoded));
    return {};
}

PlaymatePlugin::EnvironmentScan PlaymatePlugin::BuildEnvironmentScan() const
{
    EnvironmentScan scan;
    if (!GW::Map::GetIsMapLoaded() || GW::Map::GetInstanceType() != GW::Constants::InstanceType::Explorable) {
        return scan;
    }

    GW::AgentArray* agents = GW::Agents::GetAgentArray();
    const GW::AgentLiving* me = agents ? GW::Agents::GetControlledCharacter() : nullptr;
    if (!agents || !me) {
        return scan;
    }

    scan.valid = true;
    scan.player_x = me->pos.x;
    scan.player_y = me->pos.y;
    scan.player_hp = me->hp;
    scan.closest_hostile_distance = std::numeric_limits<float>::max();
    const uint64_t observed_ms = MonotonicMs();

    const GW::AgentLiving* selected_target = GW::Agents::GetTargetAsAgentLiving();
    if (selected_target
        && selected_target != me
        && selected_target->allegiance == GW::Constants::Allegiance::Enemy
        && selected_target->GetIsAlive()) {
        const float target_distance = Distance2D(me->pos, selected_target->pos);
        if (target_distance <= GW::Constants::Range::Compass) {
            scan.selected_target_agent_id = selected_target->agent_id;
            scan.selected_target_name = LivingAgentName(selected_target);
            scan.selected_target_distance = target_distance;
        }
    }

    for (const GW::Agent* agent : *agents) {
        if (!agent || agent == me || !GW::Agents::GetAgentMatchesFlags(agent, GW::TargetFilter::AnyLiving)) {
            continue;
        }
        const GW::AgentLiving* living = agent->GetAsAgentLiving();
        if (!living || living->allegiance != GW::Constants::Allegiance::Enemy) {
            continue;
        }

        const bool is_alive = living->GetIsAlive();
        {
            std::lock_guard lock(gameplay_state_mutex_);
            const auto previous = known_hostile_alive_.find(living->agent_id);
            if (previous != known_hostile_alive_.end() && previous->second && !is_alive) {
                recent_hostile_deaths_.push_back(
                    {
                        living->agent_id,
                        LivingAgentName(living),
                        living->pos.x,
                        living->pos.y,
                        observed_ms,
                    });
                while (recent_hostile_deaths_.size() > 12) {
                    recent_hostile_deaths_.pop_front();
                }
            }
            known_hostile_alive_[living->agent_id] = is_alive;
            for (auto it = recent_hostile_deaths_.begin(); it != recent_hostile_deaths_.end();) {
                if (observed_ms - it->observed_ms > 20000) {
                    it = recent_hostile_deaths_.erase(it);
                }
                else {
                    ++it;
                }
            }
        }

        if (!is_alive) {
            ++scan.dead_hostile_count;
            continue;
        }

        const float distance = Distance2D(me->pos, living->pos);
        if (distance > GW::Constants::Range::Compass) {
            continue;
        }

        ++scan.hostile_count;
        if (distance <= 1500.0f) {
            ++scan.close_hostile_count;
        }
        if (distance < scan.closest_hostile_distance) {
            scan.closest_hostile_distance = distance;
            scan.closest_hostile_agent_id = living->agent_id;
        }
        scan.in_combat = scan.in_combat || living->GetInCombatStance() || living->GetIsAttacking() || living->GetIsCasting();
    }

    if (scan.closest_hostile_distance == std::numeric_limits<float>::max()) {
        scan.closest_hostile_distance = 0.0f;
    }
    scan.in_combat = scan.in_combat || me->GetInCombatStance() || scan.close_hostile_count > 0;
    return scan;
}

std::string PlaymatePlugin::CurrentPersonaName() const
{
    return WideToUtf8(CurrentPersonaNameWide().c_str());
}

std::wstring PlaymatePlugin::CurrentPersonaNameWide() const
{
    const GW::CharContext* context = GW::GetCharContext();
    if (!context) {
        return L"Unknown Character";
    }

    const size_t name_length = wcsnlen_s(context->player_name, _countof(context->player_name));
    if (name_length == 0) {
        return L"Unknown Character";
    }
    return {context->player_name, name_length};
}

std::pair<std::string, std::string> PlaymatePlugin::GetConfig() const
{
    std::lock_guard lock(config_mutex_);
    return {backend_url_, api_token_};
}

std::filesystem::path PlaymatePlugin::LocalLogPath() const
{
    std::lock_guard lock(config_mutex_);
    if (local_log_folder_.empty()) {
        return {};
    }
    return local_log_folder_ / (L"telemetry-" + CurrentLocalLogDate() + L".jsonl");
}

std::string PlaymatePlugin::EventsUrl() const
{
    const auto [backend_url, _] = GetConfig();
    return backend_url + "/v1/playmate/events";
}

std::string PlaymatePlugin::RepliesUrl() const
{
    const auto [backend_url, _] = GetConfig();
    const std::string persona = CurrentPersonaName();
    if (persona.empty()) {
        return backend_url + "/v1/playmate/replies";
    }
    return backend_url + "/v1/playmate/replies?persona=" + UrlEncode(persona);
}

void PlaymatePlugin::OnSendChat(GW::HookStatus*, const GW::UI::UIMessage message_id, void* wparam, void*)
{
    if (!active_plugin || message_id != GW::UI::UIMessage::kSendChatMessage || !wparam) {
        return;
    }
    const auto* packet = static_cast<GW::UI::UIPacket::kSendChatMessage*>(wparam);
    if (!packet->message || !*packet->message) {
        return;
    }

    const auto channel = GW::Chat::GetChannel(*packet->message);
    if (channel != GW::Chat::CHANNEL_GROUP) {
        return;
    }

    active_plugin->QueueTelemetry("player_chat", "Player", ChannelName(channel), PlayerMessageText(packet->message));
}

void PlaymatePlugin::OnWriteToChatLog(GW::HookStatus*, const GW::UI::UIMessage message_id, void* wparam, void*)
{
    if (!active_plugin || message_id != GW::UI::UIMessage::kWriteToChatLog || !wparam) {
        return;
    }

    const auto* packet = static_cast<GW::UI::UIPacket::kWriteToChatLog*>(wparam);
    if (!packet->message || !*packet->message) {
        return;
    }

    const auto channel = packet->channel;
    if (channel == GW::Chat::CHANNEL_GROUP) {
        return;
    }

    const auto filtered_message = FilterChatLogMessage(channel, packet->message);
    if (!filtered_message) {
        return;
    }
    active_plugin->QueueTelemetry("chat_log", "Game", ChannelName(channel), *filtered_message);
}

void PlaymatePlugin::OnMapOrQuestEvent(GW::HookStatus*, const GW::UI::UIMessage message_id, void*, void*)
{
    if (!active_plugin) {
        return;
    }

    switch (message_id) {
        case GW::UI::UIMessage::kMapLoaded:
            {
                std::lock_guard lock(active_plugin->gameplay_state_mutex_);
                active_plugin->last_agent_states_.clear();
                active_plugin->last_mission_progress_.clear();
            }
            active_plugin->QueueSnapshotEvent("map_loaded");
            break;
        case GW::UI::UIMessage::kMapChange:
            {
                std::lock_guard lock(active_plugin->gameplay_state_mutex_);
                active_plugin->last_agent_states_.clear();
                active_plugin->last_mission_progress_.clear();
            }
            active_plugin->QueueSnapshotEvent("map_change");
            break;
        case GW::UI::UIMessage::kQuestAdded:
            active_plugin->QueueSnapshotEvent("quest_added");
            break;
        case GW::UI::UIMessage::kQuestDetailsChanged:
            active_plugin->QueueSnapshotEvent("quest_details_changed");
            break;
        default:
            break;
    }
}

void PlaymatePlugin::OnAgentState(GW::HookStatus*, GW::Packet::StoC::AgentState* packet)
{
    if (!active_plugin || !packet || !IsAgentInCurrentParty(packet->agent_id)) {
        return;
    }

    constexpr uint32_t dead_state_bit = 0x10;
    const bool is_dead = (packet->state & dead_state_bit) != 0;
    bool was_dead = false;
    {
        std::lock_guard lock(active_plugin->gameplay_state_mutex_);
        const auto previous = active_plugin->last_agent_states_.find(packet->agent_id);
        was_dead = previous != active_plugin->last_agent_states_.end() && (previous->second & dead_state_bit) != 0;
        active_plugin->last_agent_states_[packet->agent_id] = packet->state;
    }

    if (is_dead == was_dead) {
        return;
    }

    TelemetryEvent event;
    event.event_type = is_dead ? "party_member_down" : "party_member_recovered";
    event.message = is_dead ? "Party member down." : "Party member recovered.";
    event.agent_id = packet->agent_id;
    event.agent_name = PartyAgentName(packet->agent_id);
    event.alert_type = event.event_type;
    event.severity = is_dead ? "HIGH" : "LOW";
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnAgentAdd(GW::HookStatus*, GW::Packet::StoC::AgentAdd* packet)
{
    if (!active_plugin || !packet) {
        return;
    }
    active_plugin->QueueItemDropEvent(*packet);
}

void PlaymatePlugin::OnPartyDefeated(GW::HookStatus*, GW::Packet::StoC::PartyDefeated*)
{
    if (!active_plugin) {
        return;
    }

    TelemetryEvent event;
    event.event_type = "party_defeated";
    event.message = "The party was defeated.";
    event.alert_type = event.event_type;
    event.severity = "HIGH";
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnSpeechBubble(GW::HookStatus*, GW::Packet::StoC::SpeechBubble* packet)
{
    if (!active_plugin || !packet || !packet->agent_id || IsControlledCharacterAgent(packet->agent_id)) {
        return;
    }
    if (!*packet->message || LooksGwEncoded(packet->message)) {
        return;
    }

    const auto cleaned = FilterChatLogMessage(GW::Chat::CHANNEL_ALL, packet->message);
    if (!cleaned) {
        return;
    }

    TelemetryEvent event;
    event.event_type = "npc_speech_bubble";
    event.sender = std::format("Agent {}", packet->agent_id);
    event.channel = "local";
    event.message = *cleaned;
    event.agent_id = packet->agent_id;
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnObjectiveAdd(GW::HookStatus*, GW::Packet::StoC::ObjectiveAdd* packet)
{
    if (!active_plugin || !packet) {
        return;
    }

    TelemetryEvent event;
    event.event_type = "mission_objective_added";
    event.objective_id = packet->objective_id;
    event.objective_name = WideToUtf8(packet->name);
    event.message = event.objective_name.empty() ? "Mission objective added." : std::format("Mission objective added: {}", event.objective_name);
    event.severity = "NORMAL";
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnObjectiveDone(GW::HookStatus*, GW::Packet::StoC::ObjectiveDone* packet)
{
    if (!active_plugin || !packet) {
        return;
    }

    TelemetryEvent event;
    event.event_type = "mission_objective_completed";
    event.objective_id = packet->objective_id;
    event.message = std::format("Mission objective completed: {}.", packet->objective_id);
    event.alert_type = event.event_type;
    event.severity = "HIGH";
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnObjectiveUpdateName(GW::HookStatus*, GW::Packet::StoC::ObjectiveUpdateName* packet)
{
    if (!active_plugin || !packet) {
        return;
    }

    TelemetryEvent event;
    event.event_type = "mission_objective_updated";
    event.objective_id = packet->objective_id;
    event.objective_name = WideToUtf8(packet->objective_name);
    event.message = event.objective_name.empty() ? "Mission objective updated." : std::format("Mission objective updated: {}", event.objective_name);
    event.severity = "NORMAL";
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnCreateMissionProgress(GW::HookStatus*, GW::Packet::StoC::CreateMissionProgress* packet)
{
    if (!active_plugin || !packet) {
        return;
    }

    {
        std::lock_guard lock(active_plugin->gameplay_state_mutex_);
        active_plugin->last_mission_progress_[packet->id] = packet->filled;
    }

    TelemetryEvent event;
    event.event_type = "mission_progress_started";
    event.objective_id = packet->id;
    event.progress_current = packet->filled;
    event.progress_total = 1.0f;
    event.message = std::format("Mission progress started at {:.0f} percent.", packet->filled * 100.0f);
    event.severity = "NORMAL";
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnUpdateMissionProgress(GW::HookStatus*, GW::Packet::StoC::UpdateMissionProgress* packet)
{
    if (!active_plugin || !packet) {
        return;
    }

    bool changed_enough = false;
    {
        std::lock_guard lock(active_plugin->gameplay_state_mutex_);
        const auto previous = active_plugin->last_mission_progress_.find(packet->id);
        changed_enough = previous == active_plugin->last_mission_progress_.end()
            || std::abs(previous->second - packet->filled) >= 0.01f;
        active_plugin->last_mission_progress_[packet->id] = packet->filled;
    }
    if (!changed_enough) {
        return;
    }

    TelemetryEvent event;
    event.event_type = "mission_progress_updated";
    event.objective_id = packet->id;
    event.progress_current = packet->filled;
    event.progress_total = 1.0f;
    event.message = std::format("Mission progress updated to {:.0f} percent.", packet->filled * 100.0f);
    event.severity = packet->filled >= 1.0f ? "HIGH" : "NORMAL";
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnVanquishProgress(GW::HookStatus*, GW::Packet::StoC::VanquishProgress* packet)
{
    if (!active_plugin || !packet) {
        return;
    }

    TelemetryEvent event;
    event.event_type = "vanquish_progress";
    event.foes_killed = packet->foes_killed;
    event.foes_remaining = packet->foes_remaining;
    event.progress_current = static_cast<float>(packet->foes_killed);
    event.progress_total = static_cast<float>(packet->foes_killed + packet->foes_remaining);
    event.message = std::format("Vanquish progress: {} foes killed, {} remaining.", packet->foes_killed, packet->foes_remaining);
    event.severity = packet->foes_remaining <= 5 ? "HIGH" : "NORMAL";
    active_plugin->QueueGameplayEvent(std::move(event));
}

void PlaymatePlugin::OnVanquishComplete(GW::HookStatus*, GW::Packet::StoC::VanquishComplete* packet)
{
    if (!active_plugin || !packet) {
        return;
    }

    TelemetryEvent event;
    event.event_type = "vanquish_complete";
    event.map_id = packet->map_id;
    event.message = std::format("Vanquish complete. Reward: {} XP, {} gold.", packet->experience, packet->gold);
    event.alert_type = event.event_type;
    event.severity = "HIGH";
    active_plugin->QueueGameplayEvent(std::move(event));
}
