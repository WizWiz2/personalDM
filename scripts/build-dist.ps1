# PersonalDM distribution packager.
# Builds a clean player zip: no tools/, data/, venv, node_modules, tests, secrets.
param(
    [string]$Version = "snapshot",
    [string]$OutRoot = "dist",
    [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$SafeVersion = ($Version -replace "[^A-Za-z0-9._-]", "-")
$StageName = "PersonalDM"
$StageDir = Join-Path $OutRoot $StageName
$ZipName = "PersonalDM-$SafeVersion-win.zip"
$ZipPath = Join-Path $OutRoot $ZipName

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
    finally {
        Pop-Location
    }
}

$FrontendDist = Join-Path $RepoRoot "src/frontend/dist/index.html"
if (-not (Test-Path $FrontendDist)) {
    throw "Frontend build missing: $FrontendDist"
}

if (Test-Path $StageDir) {
    Remove-Item -Recurse -Force $StageDir
}
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

function Copy-FileToStage {
    param([string]$RelativePath)
    $src = Join-Path $RepoRoot $RelativePath
    if (-not (Test-Path $src)) { throw "Missing required file: $RelativePath" }
    $dest = Join-Path $StageDir $RelativePath
    $destParent = Split-Path $dest -Parent
    New-Item -ItemType Directory -Force -Path $destParent | Out-Null
    Copy-Item -Force $src $dest
}

function Copy-TreeFiltered {
    param(
        [string]$RelativePath,
        [string[]]$ExcludeDirNames,
        [string[]]$ExcludeFileNames
    )
    $srcRoot = Join-Path $RepoRoot $RelativePath
    if (-not (Test-Path $srcRoot)) { throw "Missing required tree: $RelativePath" }
    Get-ChildItem -Path $srcRoot -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($srcRoot.Length).TrimStart("\", "/")
        $parts = $rel -split "[\\/]"
        $dirParts = @()
        if ($parts.Length -gt 1) {
            $dirParts = $parts[0..($parts.Length - 2)]
        }
        foreach ($part in $dirParts) {
            if ($ExcludeDirNames -contains $part) { return }
        }
        if ($ExcludeFileNames -contains $_.Name) { return }
        if ($_.Extension -in @(".pyc", ".pyo")) { return }
        if ($_.Name -like ".env*") { return }
        if ($_.Name -like "*.db") { return }
        if ($_.Name -like "*.db-*") { return }
        $dest = Join-Path (Join-Path $StageDir $RelativePath) $rel
        $destParent = Split-Path $dest -Parent
        New-Item -ItemType Directory -Force -Path $destParent | Out-Null
        Copy-Item -Force $_.FullName $dest
    }
}

Write-Host "[dist] copying allowlisted files..."

Copy-FileToStage "play.bat"
Copy-FileToStage "uninstall.bat"
if (Test-Path (Join-Path $RepoRoot "uninstall.ps1")) {
    Copy-FileToStage "uninstall.ps1"
}

$stamp = Get-Date -Format o
Set-Content -Path (Join-Path $StageDir "DIST_MODE") -Value "version=$SafeVersion`nbuilt=$stamp`n" -Encoding utf8

$backendExcludeDirs = @(
    "venv", ".venv", "data", "scratch", "tests", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "__pycache__", "personal_dm.egg-info", "live_model_contracts",
    "htmlcov", ".tox"
)
$backendExcludeFiles = @(
    "local_launcher.py", ".coverage", "personal-dm-crash.log"
)
Copy-TreeFiltered -RelativePath "src/backend" -ExcludeDirNames $backendExcludeDirs -ExcludeFileNames $backendExcludeFiles

$feStage = Join-Path $StageDir "src/frontend/dist"
New-Item -ItemType Directory -Force -Path $feStage | Out-Null
Copy-Item -Recurse -Force (Join-Path $RepoRoot "src/frontend/dist\*") $feStage

$readme = @"
# PersonalDM - install

1. Need Python 3.11+ in PATH.
2. Unzip anywhere.
3. Run play.bat.
4. First launch installs deps and asks for text/image providers (Ollama/Comfy or cloud).
5. Uninstall: uninstall.bat.

This zip does NOT include models, Ollama, ComfyUI, or your saves - bootstrap downloads/installs them on first run.

Node.js is NOT required: GUI is prebuilt and served by the backend at http://127.0.0.1:8000
"@
Set-Content -Path (Join-Path $StageDir "README.txt") -Value $readme.Trim() -Encoding utf8

$forbidden = @(
    "src/backend/.env",
    "src/backend/venv",
    "src/backend/data",
    "src/backend/tests",
    "src/frontend/node_modules",
    "src/frontend/src",
    "tools",
    ".git",
    ".github"
)
foreach ($rel in $forbidden) {
    if (Test-Path (Join-Path $StageDir $rel)) {
        throw "Refusing to ship forbidden path: $rel"
    }
}
if (-not (Test-Path (Join-Path $StageDir "src/frontend/dist/index.html"))) {
    throw "Packaged frontend dist/index.html missing"
}
if (-not (Test-Path (Join-Path $StageDir "src/backend/app/main.py"))) {
    throw "Packaged backend main.py missing"
}

if (Test-Path $ZipPath) {
    Remove-Item -Force $ZipPath
}
New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null
Write-Host "[dist] zipping $ZipPath ..."
Compress-Archive -Path $StageDir -DestinationPath $ZipPath -CompressionLevel Optimal

$sizeMb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 2)
Write-Host ("[dist] done: {0} ({1} MB)" -f $ZipPath, $sizeMb)
Write-Output $ZipPath
