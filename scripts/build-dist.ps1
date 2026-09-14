# PersonalDM opaque Windows distribution.
# Builds PersonalDM.exe (PyInstaller onedir) + thin launchers. No .py sources in the zip.
param(
    [string]$Version = "snapshot",
    [string]$OutRoot = "dist",
    [switch]$SkipFrontendBuild,
    [switch]$SkipExeBuild,
    [string]$ZipFileName = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$SafeVersion = ($Version -replace "[^A-Za-z0-9._-]", "-")
$StageName = "PersonalDM"
$StageDir = Join-Path $OutRoot $StageName
$ZipName = if ($ZipFileName) { $ZipFileName } else { "PersonalDM-$SafeVersion-win.zip" }
$ZipPath = Join-Path $OutRoot $ZipName
$ExeOut = Join-Path $OutRoot "pyinstaller"
$BuildVenv = Join-Path $OutRoot "build-venv"

Write-Host "[dist] repo: $RepoRoot"
Write-Host "[dist] version: $SafeVersion"

if (-not $SkipFrontendBuild) {
    Write-Host "[dist] building frontend..."
    Push-Location (Join-Path $RepoRoot "src/frontend")
    try {
        if (-not (Test-Path "node_modules")) {
            npm install --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
        }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
    }
    finally { Pop-Location }
}

$FrontendDist = Join-Path $RepoRoot "src/frontend/dist/index.html"
if (-not (Test-Path $FrontendDist)) { throw "Frontend build missing: $FrontendDist" }

if (-not $SkipExeBuild) {
    Write-Host "[dist] preparing build venv + PyInstaller..."
    if (-not (Test-Path (Join-Path $BuildVenv "Scripts/python.exe"))) {
        python -m venv $BuildVenv
        if ($LASTEXITCODE -ne 0) { throw "venv failed" }
    }
    $py = Join-Path $BuildVenv "Scripts/python.exe"
    & $py -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }
    & $py -m pip install -e (Join-Path $RepoRoot "src/backend") pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install backend/pyinstaller failed" }
    if (Test-Path $ExeOut) { Remove-Item -Recurse -Force $ExeOut }
    New-Item -ItemType Directory -Force -Path $ExeOut | Out-Null
    Write-Host "[dist] running PyInstaller..."
    & $py -m PyInstaller --noconfirm --clean --distpath $ExeOut --workpath (Join-Path $ExeOut "work") (Join-Path $RepoRoot "scripts/personaldm.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
}

$BuiltApp = Join-Path $ExeOut "PersonalDM"
if (-not (Test-Path (Join-Path $BuiltApp "PersonalDM.exe"))) { throw "PersonalDM.exe missing under $BuiltApp" }

if (Test-Path $StageDir) { Remove-Item -Recurse -Force $StageDir }
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
Write-Host "[dist] staging opaque payload..."
Copy-Item -Recurse -Force (Join-Path $BuiltApp "*") $StageDir

$playBat = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Personal DM
"%~dp0PersonalDM.exe" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
"@
Set-Content -Path (Join-Path $StageDir "play.bat") -Value $playBat -Encoding ascii

$unBat = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Personal DM - Uninstall
"%~dp0PersonalDM.exe" --uninstall
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
"@
Set-Content -Path (Join-Path $StageDir "uninstall.bat") -Value $unBat -Encoding ascii

Copy-Item -Force (Join-Path $RepoRoot "uninstall.ps1") (Join-Path $StageDir "uninstall.ps1")
$stamp = Get-Date -Format o
Set-Content -Path (Join-Path $StageDir "DIST_MODE") -Value "version=$SafeVersion`nbuilt=$stamp`nopaque=1`n" -Encoding utf8

$readme = @"
# PersonalDM (opaque Windows build)

1. Unzip anywhere.
2. Run play.bat (or PersonalDM.exe).
3. First launch migrates DB and asks for text/image providers (Ollama/Comfy or cloud).
4. Uninstall: uninstall.bat

No Python/Node required.
Models/Ollama/ComfyUI are NOT in this zip - bootstrap installs into tools/ next to the exe.
Game data lives under %APPDATA%\PersonalDM.
Application logic ships as PersonalDM.exe (not plain .py sources).
"@
Set-Content -Path (Join-Path $StageDir "README.txt") -Value $readme.Trim() -Encoding utf8

if (Test-Path (Join-Path $StageDir "src")) { throw "Refusing to ship src/ tree in opaque dist" }
foreach ($name in @("src","tools",".git",".github","venv","node_modules")) {
    if (Test-Path (Join-Path $StageDir $name)) { throw "Refusing to ship forbidden path: $name" }
}
$loosePy = @(Get-ChildItem -Path $StageDir -Recurse -Filter *.py -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch '\\_internal\\' })
if ($loosePy.Count -gt 0) { throw ("Loose .py outside _internal: " + ($loosePy.FullName -join ", ")) }

if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null
Write-Host "[dist] zipping $ZipPath ..."
Compress-Archive -Path $StageDir -DestinationPath $ZipPath -CompressionLevel Optimal
$sizeMb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 2)
Write-Host ("[dist] done: {0} ({1} MB)" -f $ZipPath, $sizeMb)
Write-Output $ZipPath
