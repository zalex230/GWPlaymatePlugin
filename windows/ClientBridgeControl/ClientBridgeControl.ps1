Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = "Stop"

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:PythonExe = Join-Path $script:RepoRoot ".venv-playmate\Scripts\python.exe"
$script:BridgeUrl = "http://127.0.0.1:8787/health"

if (!(Test-Path $script:PythonExe)) {
    $script:PythonExe = "python"
}

function Get-BridgeProcesses {
    Get-CimInstance Win32_Process -Filter "name = 'python.exe' or name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*backend.windows_bridge.app*" }
}

function Test-BridgeHealth {
    try {
        $response = Invoke-RestMethod -Uri $script:BridgeUrl -TimeoutSec 2
        if ($response.ok) {
            return "Healthy"
        }
        return "Responding, not healthy"
    }
    catch {
        return "No health response"
    }
}

function Get-StatusText {
    $processes = @(Get-BridgeProcesses)
    $health = Test-BridgeHealth
    $processText = if ($processes.Count) {
        ($processes | ForEach-Object { "PID $($_.ProcessId)" }) -join ", "
    }
    else {
        "none"
    }

    return "Windows bridge: $health`r`nProcesses: $processText`r`nURL: http://127.0.0.1:8787"
}

function Start-Bridge {
    $processes = @(Get-BridgeProcesses)
    if ($processes.Count -gt 0 -and (Test-BridgeHealth) -eq "Healthy") {
        return
    }

    Start-Process `
        -FilePath $script:PythonExe `
        -ArgumentList "-m backend.windows_bridge.app" `
        -WorkingDirectory $script:RepoRoot `
        -WindowStyle Hidden | Out-Null
}

function Stop-Bridge {
    Get-BridgeProcesses | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Restart-Bridge {
    Stop-Bridge
    Start-Sleep -Milliseconds 400
    Start-Bridge
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "GWPlaymate Client Bridge Control"
$form.Size = New-Object System.Drawing.Size(460, 240)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false

$title = New-Object System.Windows.Forms.Label
$title.Text = "Client Bridge"
$title.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(22, 18)
$form.Controls.Add($title)

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.Text = "Open starts the Windows bridge. Quit stops it."
$subtitle.Font = New-Object System.Drawing.Font("Segoe UI", 9)
$subtitle.ForeColor = [System.Drawing.Color]::DimGray
$subtitle.AutoSize = $true
$subtitle.Location = New-Object System.Drawing.Point(25, 52)
$form.Controls.Add($subtitle)

$status = New-Object System.Windows.Forms.Label
$status.Font = New-Object System.Drawing.Font("Consolas", 10)
$status.AutoSize = $false
$status.Size = New-Object System.Drawing.Size(405, 72)
$status.Location = New-Object System.Drawing.Point(25, 84)
$form.Controls.Add($status)

$startButton = New-Object System.Windows.Forms.Button
$startButton.Text = "Start"
$startButton.Size = New-Object System.Drawing.Size(86, 30)
$startButton.Location = New-Object System.Drawing.Point(25, 164)
$form.Controls.Add($startButton)

$stopButton = New-Object System.Windows.Forms.Button
$stopButton.Text = "Stop"
$stopButton.Size = New-Object System.Drawing.Size(86, 30)
$stopButton.Location = New-Object System.Drawing.Point(119, 164)
$form.Controls.Add($stopButton)

$restartButton = New-Object System.Windows.Forms.Button
$restartButton.Text = "Restart"
$restartButton.Size = New-Object System.Drawing.Size(86, 30)
$restartButton.Location = New-Object System.Drawing.Point(213, 164)
$form.Controls.Add($restartButton)

$refreshButton = New-Object System.Windows.Forms.Button
$refreshButton.Text = "Refresh"
$refreshButton.Size = New-Object System.Drawing.Size(86, 30)
$refreshButton.Location = New-Object System.Drawing.Point(307, 164)
$form.Controls.Add($refreshButton)

function Refresh-StatusLabel {
    $status.Text = Get-StatusText
}

$startButton.Add_Click({
    Start-Bridge
    Start-Sleep -Milliseconds 500
    Refresh-StatusLabel
})

$stopButton.Add_Click({
    Stop-Bridge
    Start-Sleep -Milliseconds 300
    Refresh-StatusLabel
})

$restartButton.Add_Click({
    Restart-Bridge
    Start-Sleep -Milliseconds 500
    Refresh-StatusLabel
})

$refreshButton.Add_Click({
    Refresh-StatusLabel
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick({ Refresh-StatusLabel })
$timer.Start()

$form.Add_Shown({
    Start-Bridge
    Start-Sleep -Milliseconds 500
    Refresh-StatusLabel
})

$form.Add_FormClosing({
    $timer.Stop()
    Stop-Bridge
})

[void]$form.ShowDialog()
