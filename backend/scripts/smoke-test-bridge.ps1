$ErrorActionPreference = "Stop"

$event = @{
    source = "gwtoolboxpp-playmate"
    persona = "Smoke Test"
    client_time = (Get-Date).ToUniversalTime().ToString("o")
    event_type = "player_chat"
    sender = "Player"
    channel = "party"
    message = "Bridge smoke test"
    map_id = 0
    instance_type = 0
    district = 0
    instance_time = 0
    active_quest_id = 0
    quest_count = 0
    active_quest_name = ""
    active_quest_objectives = ""
    session_id = "local-playtest"
} | ConvertTo-Json

Write-Host "Posting synthetic player_chat event to Windows bridge..."
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8787/v1/playmate/events" -Body $event -ContentType "application/json"

Write-Host "Polling replies. If Hermes is running, this should return at most one fallback reply."
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8787/v1/playmate/replies"
