param(
    [string]$LogPath,
    [int]$SampleSize = 3,
    [int]$TopMessages = 20
)

$ErrorActionPreference = "Stop"

function Resolve-PlaymateLogPath {
    param([string]$RequestedPath)

    if ([string]::IsNullOrWhiteSpace($RequestedPath)) {
        $folder = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "GWToolboxpp\$env:COMPUTERNAME\Playmate"
        $today = Get-Date -Format "yyyy-MM-dd"
        $candidate = Join-Path $folder "telemetry-$today.jsonl"
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }

        $latest = Get-ChildItem -LiteralPath $folder -Filter "telemetry-*.jsonl" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($latest) {
            return $latest.FullName
        }
        throw "No Playmate telemetry log found in $folder"
    }

    if (Test-Path -LiteralPath $RequestedPath -PathType Container) {
        $latest = Get-ChildItem -LiteralPath $RequestedPath -Filter "telemetry-*.jsonl" |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($latest) {
            return $latest.FullName
        }
        throw "No telemetry-*.jsonl file found in $RequestedPath"
    }

    if (!(Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
        throw "Log file not found: $RequestedPath"
    }
    return (Resolve-Path -LiteralPath $RequestedPath).Path
}

function Get-FieldValue {
    param($Event, [string]$Name)

    $property = $Event.PSObject.Properties[$Name]
    if (!$property -or $null -eq $property.Value -or $property.Value -eq "") {
        return "<empty>"
    }
    return [string]$property.Value
}

function Limit-Text {
    param([string]$Value, [int]$MaxLength = 140)

    if ([string]::IsNullOrEmpty($Value)) {
        return ""
    }
    $clean = ($Value -replace "\s+", " ").Trim()
    if ($clean.Length -le $MaxLength) {
        return $clean
    }
    return $clean.Substring(0, $MaxLength - 3) + "..."
}

function Normalize-Message {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return "<empty>"
    }
    return (($Value.ToLowerInvariant() -replace "\d+", "#" -replace "\s+", " ").Trim())
}

function Write-GroupTable {
    param([string]$Title, [object[]]$Rows, [string]$Label)

    Write-Output ""
    Write-Output "## $Title"
    Write-Output ""
    Write-Output "| $Label | Count |"
    Write-Output "| --- | ---: |"
    foreach ($row in $Rows) {
        Write-Output "| $($row.Name) | $($row.Count) |"
    }
}

$resolvedLogPath = Resolve-PlaymateLogPath $LogPath
$events = New-Object System.Collections.ArrayList
$invalidLines = New-Object System.Collections.ArrayList
$lineNumber = 0

foreach ($line in Get-Content -LiteralPath $resolvedLogPath) {
    $lineNumber++
    if ([string]::IsNullOrWhiteSpace($line)) {
        continue
    }

    try {
        $null = $events.Add(($line | ConvertFrom-Json))
    }
    catch {
        $null = $invalidLines.Add($lineNumber)
    }
}

$eventArray = @($events | ForEach-Object { $_ })
$eventCount = $eventArray.Count
$logItem = Get-Item -LiteralPath $resolvedLogPath

Write-Output "# Playmate Telemetry Review"
Write-Output ""
Write-Output "- Log: ``$resolvedLogPath``"
Write-Output "- Last modified: $($logItem.LastWriteTime)"
Write-Output "- Events parsed: $eventCount"
Write-Output "- Invalid JSONL lines: $($invalidLines.Count)"

if ($eventCount -eq 0) {
    Write-Output ""
    Write-Output "No telemetry events were found."
    exit 0
}

Write-GroupTable "Event Types" ($eventArray | Group-Object event_type | Sort-Object Count -Descending) "event_type"
Write-GroupTable "Channels" ($eventArray | Group-Object channel | Sort-Object Count -Descending) "channel"
Write-GroupTable "Maps" ($eventArray | Group-Object map_id | Sort-Object Count -Descending | Select-Object -First 20) "map_id"
Write-GroupTable "Personas" ($eventArray | Group-Object persona | Sort-Object Count -Descending) "persona"

Write-Output ""
Write-Output "## Message Samples"
foreach ($group in ($eventArray | Group-Object event_type | Sort-Object Name)) {
    Write-Output ""
    Write-Output "### $($group.Name)"
    foreach ($event in @($group.Group | Select-Object -First $SampleSize)) {
        $time = Get-FieldValue $event "client_time"
        $channel = Get-FieldValue $event "channel"
        $persona = Get-FieldValue $event "persona"
        $message = Limit-Text (Get-FieldValue $event "message")
        Write-Output "- ``$time`` [$channel] [$persona] $message"
    }
}

$messagePatterns = $eventArray |
    Where-Object { $_.message } |
    Group-Object { Normalize-Message ([string]$_.message) } |
    Sort-Object Count -Descending |
    Select-Object -First $TopMessages

Write-Output ""
Write-Output "## Repeated Message Patterns"
Write-Output ""
Write-Output "| Pattern | Count |"
Write-Output "| --- | ---: |"
foreach ($pattern in $messagePatterns) {
    Write-Output "| $(Limit-Text $pattern.Name 120) | $($pattern.Count) |"
}

$snapshotCount = @($eventArray | Where-Object { $_.event_type -eq "snapshot" }).Count
$snapshotRatio = if ($eventCount -gt 0) { $snapshotCount / $eventCount } else { 0 }

Write-Output ""
Write-Output "## Proposed Filter Matrix"
Write-Output ""
Write-Output "| Area | Observed signal | Suggested handling |"
Write-Output "| --- | --- | --- |"
Write-Output "| party chat | Player-authored party messages | retain; these are high-value companion context |"
Write-Output "| map/quest events | map and active quest changes | retain; these anchor session chronology |"
if ($snapshotRatio -gt 0.5) {
    Write-Output "| snapshots | snapshots are more than half of captured events | increase snapshot interval or emit only on meaningful state changes |"
}
else {
    Write-Output "| snapshots | periodic context heartbeat | retain temporarily; revisit volume after longer sessions |"
}
foreach ($pattern in @($messagePatterns | Where-Object { $_.Count -gt 3 } | Select-Object -First 5)) {
    Write-Output "| repeated message | $(Limit-Text $pattern.Name 80) | review for suppression, throttling, or event-specific parsing |"
}
Write-Output "| sensitive fields | persona, chat, quest text | review before backend upload; keep Supabase disabled until approved |"
