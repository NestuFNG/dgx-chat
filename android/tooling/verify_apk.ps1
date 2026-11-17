<#
.SYNOPSIS
  Verify a DGX Chat APK: identity/version/permissions, cleartext config, signature, size+hash.

.DESCRIPTION
  Needs JAVA_HOME and ANDROID_HOME (build-tools with aapt2 + apksigner).
  The highest installed build-tools version is used automatically.

.EXAMPLE
  .\tooling\verify_apk.ps1 -Apk .\dist-out\DGX-Chat.apk
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Apk
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Apk)) { throw "APK not found: $Apk" }
$Apk = (Resolve-Path $Apk).Path

$Sdk = [Environment]::GetEnvironmentVariable("ANDROID_HOME")
if (-not $Sdk) { throw "ANDROID_HOME is not set. Install the Android SDK and point ANDROID_HOME at it." }

$BuildToolsRoot = Join-Path $Sdk "build-tools"
if (-not (Test-Path $BuildToolsRoot)) { throw "No build-tools installed under $BuildToolsRoot" }
$BuildTools = Get-ChildItem -Path $BuildToolsRoot -Directory |
    Sort-Object { [version]($_.Name -replace '[^0-9.]', '') } -Descending |
    Select-Object -First 1
$BuildTools = $BuildTools.FullName

$javaBin = if ($env:JAVA_HOME) { "$env:JAVA_HOME\bin" } else { "" }
if ($javaBin) { $env:Path = "$javaBin;$env:Path" }

Write-Host "== file ==" -ForegroundColor Cyan
$fi = Get-Item $Apk
Write-Host ("path   : " + $fi.FullName)
Write-Host ("size   : {0:N0} bytes" -f $fi.Length)
Write-Host ("sha256 : " + (Get-FileHash $Apk -Algorithm SHA256).Hash.ToLower())

Write-Host "`n== aapt2 badging ==" -ForegroundColor Cyan
$aapt2 = Join-Path $BuildTools "aapt2.exe"
$badging = & $aapt2 dump badging $Apk 2>&1
$badging | Select-String -SimpleMatch `
    "package:", "minSdkVersion", "targetSdkVersion", "uses-permission", `
    "application-label:", "launchable-activity" | ForEach-Object { $_.Line }

Write-Host "`n== manifest: cleartext / network security ==" -ForegroundColor Cyan
& $aapt2 dump xmltree $Apk --file AndroidManifest.xml 2>&1 |
    Select-String "usesCleartextTraffic", "networkSecurityConfig" | ForEach-Object { $_.Line.Trim() }

Write-Host "`n== signature ==" -ForegroundColor Cyan
$apksigner = Join-Path $BuildTools "apksigner.bat"
& $apksigner verify --print-certs -v $Apk 2>&1 |
    Select-String "Verifies", "v2 scheme", "certificate DN", "certificate SHA-256", "key size" |
    ForEach-Object { $_.Line.Trim() }
