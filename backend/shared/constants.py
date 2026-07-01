from __future__ import annotations

CHAT_EVENT_TYPES = {"player_chat", "chat_log"}
SNAPSHOT_EVENT_TYPES = {
    "plugin_started",
    "snapshot",
    "map_changed",
    "active_quest_changed",
    "map_loaded",
    "map_change",
    "quest_added",
    "quest_details_changed",
}
ENVIRONMENT_EVENT_TYPES = {"environment_alert"}
APPROVED_ENVIRONMENT_ALERT_TYPES = {
    "combat_over",
    "combat_started",
    "danger_spike",
    "enemy_patrol_nearby",
    "status_effect",
    "under_attack",
}
GAMEPLAY_EVENT_TYPES = {
    "mission_objective_added",
    "mission_objective_completed",
    "mission_objective_updated",
    "mission_progress_started",
    "mission_progress_updated",
    "party_defeated",
    "party_member_down",
    "party_member_recovered",
    "vanquish_complete",
    "vanquish_progress",
}

CHAT_CHANNELS = {"party", "local", "guild", "alliance", "whisper", "system", "warning", "emote"}
NOISY_CHANNELS = {"trade"}
SUPPRESSED_EVENT_TYPES = {"quest_added", "quest_details_changed"}

GAME_LOGS_TABLE = "game_logs"
ENVIRONMENT_ALERTS_TABLE = "environment_alerts"
COMPANION_REPLIES_TABLE = "companion_replies"
MEMORIES_TABLE = "memories"

DEFAULT_SESSION_ID = "local-playtest"
