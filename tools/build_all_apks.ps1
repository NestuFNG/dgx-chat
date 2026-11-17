# Build the DGX Chat APK variants from the current source.
#
# Always builds the sanitized variant (no server address baked in).
# Optionally builds pre-configured variants for your own deployments:
#
#   $env:DGX_APK_LAN_ORIGIN    = "http://your-nas.local:8439"
#   $env:DGX_APK_DOMAIN_ORIGIN = "https://chat.example.com"
#   powershell -ExecutionPolicy Bypass -File tools\build_all_apks.ps1
#
# Outputs land in release/ (sanitized) and private/ (pre-configured, git-ignored).
$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$jdk = $env:JAVA_HOME
$sdk = $env:ANDROID_HOME
if (-not $jdk -or -not $sdk) {
    Write-Host "Set JAVA_HOME (JDK 17) and ANDROID_HOME (Android SDK 35) first."
    exit 1
}
$env:ANDROID_SDK_ROOT = $sdk
$env:Path = "$jdk\bin;$sdk\platform-tools;" + $env:Path

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
$pnpm = (Get-Command pnpm -ErrorAction SilentlyContinue).Source
if (-not $node) { Write-Host "node not found on PATH"; exit 1 }
if (-not $pnpm) {
    Write-Host "pnpm not found on PATH. Install it or invoke this repo's build via 'node scripts/build.mjs' in app/."
    exit 1
}

$variants = @(
    @{ name = "sanitized"; origin = ""; dest = Join-Path $repo "release\DGX-Chat-1.0.0.apk" }
)
if ($env:DGX_APK_LAN_ORIGIN) {
    $variants += @{ name = "lan"; origin = $env:DGX_APK_LAN_ORIGIN; dest = Join-Path $repo "private\DGX-Chat-1.0.0-nas.apk" }
}
if ($env:DGX_APK_DOMAIN_ORIGIN) {
    $variants += @{ name = "domain"; origin = $env:DGX_APK_DOMAIN_ORIGIN; dest = Join-Path $repo "private\DGX-Chat-1.0.0-domain.apk" }
}

$results = @()
foreach ($v in $variants) {
    Write-Host "`n========== $($v.name) =========="
    if ($v.origin) { $env:DGX_API_ORIGIN = $v.origin } else { Remove-Item Env:\DGX_API_ORIGIN -ErrorAction SilentlyContinue }

    Push-Location (Join-Path $repo "app")
    $buildOut = & $node $pnpm run build 2>&1 | Out-String
    if ($buildOut -notmatch "compiled successfully") { Write-Host "BUILD OUTPUT:`n$buildOut" }
    Pop-Location

    Push-Location (Join-Path $repo "android")
    & $node ".\node_modules\@capacitor\cli\bin\capacitor" sync android 2>&1 | Out-Null
    Push-Location "android"
    $gradleOut = & .\gradlew.bat assembleRelease --console=plain 2>&1 | Out-String
    $ok = $gradleOut -match "BUILD SUCCESSFUL"
    Write-Host "  gradle: $(if ($ok) { 'SUCCESS' } else { 'FAILED' })"
    if (-not $ok) { Write-Host ($gradleOut -split "`n" | Select-Object -Last 15 | Out-String) }
    Pop-Location
    Pop-Location

    if ($ok) {
        New-Item -ItemType Directory -Force -Path (Split-Path $v.dest) | Out-Null
        Copy-Item (Join-Path $repo "android\android\app\build\outputs\apk\release\app-release.apk") $v.dest -Force
        $hash = (Get-FileHash $v.dest -Algorithm SHA256).Hash
        "$hash  $(Split-Path $v.dest -Leaf)" | Set-Content ($v.dest + ".sha256") -Encoding ASCII
        $results += [PSCustomObject]@{
            Variant = $v.name
            File    = Split-Path $v.dest -Leaf
            Bytes   = (Get-Item $v.dest).Length
            Sha256  = $hash.Substring(0, 16)
        }
    }
}

Remove-Item Env:\DGX_API_ORIGIN -ErrorAction SilentlyContinue
Write-Host "`n========== restoring sanitized dist =========="
Push-Location (Join-Path $repo "app")
& $node $pnpm run build 2>&1 | Out-Null
Pop-Location

Write-Host ""
$results | Format-Table -AutoSize
