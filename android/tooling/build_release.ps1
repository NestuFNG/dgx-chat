<#
.SYNOPSIS
  Reproducible DGX Chat release build: cap sync -> gradle assembleRelease -> publish -> verify.

.DESCRIPTION
  This is the scripted equivalent of BUILD.md. It reads webDir from
  capacitor.config.json and writes the APK to android\dist-out\<OutputName>.

  Requires JAVA_HOME (JDK 17) and ANDROID_HOME (Android SDK) to be set; node and
  pnpm must be on PATH. Nothing machine-specific is hard-coded.

.EXAMPLE
  .\tooling\build_release.ps1
  .\tooling\build_release.ps1 -OutputName DGX-Chat-preview.apk -SkipVerify
#>
[CmdletBinding()]
param(
    [string]$OutputName = "DGX-Chat.apk",
    [switch]$SkipSync,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"

$Root        = Split-Path -Parent $PSScriptRoot          # .../android
$NativeDir   = Join-Path $Root "android"
$CapCli      = Join-Path $Root "node_modules\@capacitor\cli\bin\capacitor"
$DistOut     = Join-Path $Root "dist-out"
$ApkSource   = Join-Path $NativeDir "app\build\outputs\apk\release\app-release.apk"
$ApkTarget   = Join-Path $DistOut  $OutputName

function Require-Env([string]$Name, [string]$Help) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if (-not $value) { throw "$Name is not set. $Help" }
    return $value
}

$JdkHome     = Require-Env "JAVA_HOME"    "Install JDK 17 and point JAVA_HOME at it."
$AndroidHome = Require-Env "ANDROID_HOME" "Install the Android SDK and point ANDROID_HOME at it."
$env:JAVA_HOME        = $JdkHome
$env:ANDROID_HOME     = $AndroidHome
$env:ANDROID_SDK_ROOT = $AndroidHome
$env:Path             = "$JdkHome\bin;$AndroidHome\platform-tools;" + $env:Path

$NodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $NodeExe) { throw "node is not on PATH." }
if (-not (Test-Path $CapCli)) { throw "Capacitor CLI not found at $CapCli - run 'pnpm install' in android/ first." }

if (-not $SkipSync) {
    Write-Host "== cap sync android ==" -ForegroundColor Cyan
    Push-Location $Root
    try { & $NodeExe $CapCli sync android; if ($LASTEXITCODE -ne 0) { throw "cap sync failed ($LASTEXITCODE)" } }
    finally { Pop-Location }
}

Write-Host "== gradlew assembleRelease ==" -ForegroundColor Cyan
Push-Location $NativeDir
try {
    if ($IsWindows -or $env:OS -eq "Windows_NT") { & .\gradlew.bat assembleRelease --console=plain }
    else { & ./gradlew assembleRelease --console=plain }
    if ($LASTEXITCODE -ne 0) { throw "gradle assembleRelease failed ($LASTEXITCODE)" }
}
finally { Pop-Location }

if (-not (Test-Path $ApkSource)) { throw "expected APK not found: $ApkSource" }
New-Item -ItemType Directory -Force -Path $DistOut | Out-Null
Copy-Item $ApkSource $ApkTarget -Force

Write-Host ""
Write-Host "APK : $ApkTarget"
Write-Host ("size: {0:N0} bytes" -f (Get-Item $ApkTarget).Length)
Write-Host ("sha256: " + (Get-FileHash $ApkTarget -Algorithm SHA256).Hash.ToLower())

if (-not $SkipVerify) {
    & (Join-Path $PSScriptRoot "verify_apk.ps1") -Apk $ApkTarget
}
