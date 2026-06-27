param(
    [Parameter(Mandatory = $true)]
    [string]$GWToolboxRoot,

    [string]$BuildDir = "build-playmate",

    [ValidateSet("Debug", "Release", "RelWithDebInfo", "MinSizeRel")]
    [string]$Configuration = "RelWithDebInfo",

    [string]$InstallTo
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pluginSource = Join-Path $repoRoot "plugins\Playmate"
$gwRoot = (Resolve-Path $GWToolboxRoot).Path
$gwPlugins = Join-Path $gwRoot "plugins"
$gwPluginTarget = Join-Path $gwPlugins "Playmate"
$buildPath = Join-Path $gwRoot $BuildDir

if (!(Test-Path (Join-Path $gwRoot "CMakeLists.txt"))) {
    throw "GWToolboxRoot does not look like a GWToolbox++ checkout: $gwRoot"
}

if (!(Test-Path $gwPlugins)) {
    throw "GWToolboxRoot is missing a plugins folder: $gwPlugins"
}

if (!(Test-Path $pluginSource)) {
    throw "Playmate plugin source not found: $pluginSource"
}

New-Item -ItemType Directory -Force -Path $gwPluginTarget | Out-Null
Copy-Item -Path (Join-Path $pluginSource "*") -Destination $gwPluginTarget -Recurse -Force

if (!(Test-Path $buildPath)) {
    cmake -S $gwRoot -B $buildPath -G "Visual Studio 17 2022" -A Win32
}

cmake --build $buildPath --config $Configuration --target Playmate

$dll = Join-Path $buildPath "bin\$Configuration\Playmate.dll"
if (!(Test-Path $dll)) {
    $dll = Join-Path $gwRoot "bin\$Configuration\Playmate.dll"
}

if (!(Test-Path $dll)) {
    throw "Build completed, but Playmate.dll was not found in the expected output folders."
}

Write-Host "Built $dll"

if ($InstallTo) {
    New-Item -ItemType Directory -Force -Path $InstallTo | Out-Null
    Copy-Item $dll (Join-Path $InstallTo "Playmate.dll") -Force
    Write-Host "Installed Playmate.dll to $InstallTo"
}
