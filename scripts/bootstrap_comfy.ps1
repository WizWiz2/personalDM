param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$RepoRoot = (Resolve-Path $RepoRoot).Path
$ToolsDir = Join-Path $RepoRoot 'tools'
$ComfyWorkspace = Join-Path $ToolsDir 'comfy'
$ComfyDir = Join-Path $ComfyWorkspace 'ComfyUI'
$RuntimeDir = Join-Path $ToolsDir 'comfy-runtime'
$RuntimePython = Join-Path $RuntimeDir 'Scripts\python.exe'
$RequirementsMarker = Join-Path $RuntimeDir '.personaldm-comfy-requirements'

New-Item -ItemType Directory -Force -Path $ToolsDir, $ComfyWorkspace | Out-Null

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE: $FilePath $($Arguments -join ' ')"
    }
}

function Find-BootstrapPython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -c "import sys; print(sys.executable)" *> $null
        if ($LASTEXITCODE -eq 0) {
            return @('py', '-3.12')
        }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @('python')
    }

    throw 'Python is not available for the ComfyUI bootstrap.'
}

if (-not (Test-Path $RuntimePython)) {
    Write-Host '[Setup] Creating isolated ComfyUI Python environment...'
    $bootstrapPython = Find-BootstrapPython
    if ($bootstrapPython.Count -eq 2) {
        Invoke-Checked $bootstrapPython[0] $bootstrapPython[1] '-m' 'venv' $RuntimeDir
    }
    else {
        Invoke-Checked $bootstrapPython[0] '-m' 'venv' $RuntimeDir
    }
}

if (-not (Test-Path (Join-Path $ComfyDir 'main.py'))) {
    Write-Host '[Setup] Downloading latest stable ComfyUI source archive...'
    $archive = Join-Path $ToolsDir 'comfyui-source.zip'
    $archivePart = "$archive.part"
    $extractDir = Join-Path $ToolsDir 'comfyui-extract'

    Remove-Item $archivePart -Force -ErrorAction SilentlyContinue
    Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue

    try {
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/Comfy-Org/ComfyUI/releases/latest' -Headers @{ 'User-Agent' = 'PersonalDM-bootstrap' }
        $downloadUrl = [string]$release.zipball_url
        if ([string]::IsNullOrWhiteSpace($downloadUrl)) {
            throw 'GitHub latest release did not contain zipball_url.'
        }
        Write-Host "[Setup] ComfyUI release: $($release.tag_name)"
    }
    catch {
        Write-Warning "Could not resolve latest release tag: $($_.Exception.Message)"
        Write-Host '[Setup] Falling back to the official ComfyUI master archive.'
        $downloadUrl = 'https://github.com/Comfy-Org/ComfyUI/archive/refs/heads/master.zip'
    }

    Invoke-WebRequest -Uri $downloadUrl -OutFile $archivePart -Headers @{ 'User-Agent' = 'PersonalDM-bootstrap' }
    Move-Item $archivePart $archive -Force

    Expand-Archive -Path $archive -DestinationPath $extractDir -Force
    $sourceDir = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1
    if (-not $sourceDir) {
        throw 'ComfyUI archive did not contain a source directory.'
    }

    Remove-Item $ComfyDir -Recurse -Force -ErrorAction SilentlyContinue
    Move-Item $sourceDir.FullName $ComfyDir
    Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $archive -Force -ErrorAction SilentlyContinue
}

$requirements = Join-Path $ComfyDir 'requirements.txt'
if (-not (Test-Path $requirements)) {
    throw "ComfyUI requirements.txt is missing: $requirements"
}

$requirementsHash = (Get-FileHash -Path $requirements -Algorithm SHA256).Hash
$installedHash = if (Test-Path $RequirementsMarker) {
    (Get-Content $RequirementsMarker -Raw).Trim()
} else {
    ''
}

if ($installedHash -ne $requirementsHash) {
    Write-Host '[Setup] Installing/updating ComfyUI Python dependencies...'
    Invoke-Checked $RuntimePython '-m' 'pip' 'install' '--upgrade' 'pip' 'wheel' 'setuptools'

    # Current ComfyUI guidance recommends current CUDA PyTorch on RTX 20-series and newer.
    # Prefer cu130, but keep a cu128 fallback for machines whose Python/driver combination
    # does not yet have a matching cu130 wheel.
    & $RuntimePython -m pip install --upgrade torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'PyTorch cu130 install failed; retrying with cu128.'
        Invoke-Checked $RuntimePython '-m' 'pip' 'install' '--upgrade' 'torch' 'torchvision' 'torchaudio' '--index-url' 'https://download.pytorch.org/whl/cu128'
    }

    Invoke-Checked $RuntimePython '-m' 'pip' 'install' '-r' $requirements
    Set-Content -Path $RequirementsMarker -Value $requirementsHash -Encoding ascii
}

Write-Host '[Setup] Verifying CUDA visibility for ComfyUI...'
Invoke-Checked $RuntimePython '-c' "import torch; assert torch.cuda.is_available(), 'PyTorch cannot see the NVIDIA GPU'; print('[Setup] CUDA device:', torch.cuda.get_device_name(0))"

Write-Host '[Setup] ComfyUI runtime is ready.'
