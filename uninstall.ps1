param(
    [ValidateSet('infrastructure', 'games', 'all')]
    [string]$Mode
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Roaming = $env:APPDATA
if ([string]::IsNullOrWhiteSpace($Roaming)) {
    $Roaming = Join-Path $env:USERPROFILE 'AppData\Roaming'
}
$AppDataRoot = Join-Path $Roaming 'PersonalDM'
$Library = Join-Path $AppDataRoot 'library'
$Saves = Join-Path $AppDataRoot 'saves'

Write-Host 'PersonalDM uninstall - only the items listed below will be removed.' -ForegroundColor Cyan
Write-Host "Saves: $Saves (this script never removes saves)."
Write-Host ''
Write-Host 'infrastructure - stop local services and remove managed runtimes:'
Write-Host '  tools\comfy, tools\comfy-runtime, src\backend\venv и src\frontend\node_modules.'
Write-Host 'games - remove generated-image cache, while keeping campaign.db and saves.'
Write-Host 'all - perform both modes and remove application files, including this script.'
Write-Host 'Ollama, Node.js and Python installed separately are not removed.'
Write-Host ''

if (-not $Mode) {
    $Mode = Read-Host "Enter mode (infrastructure / games / all); blank cancels"
}
if (@('infrastructure', 'games', 'all') -notcontains $Mode) {
    Write-Host 'Cancelled.'
    exit 0
}
$answer = Read-Host "Введите DELETE для подтверждения режима '$Mode'"
if ($answer -cne 'DELETE') {
    Write-Host 'Cancelled.'
    exit 0
}

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'personalDM.*(uvicorn|main.py.*8188)' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

function Remove-Safe {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
        Write-Host "Removed: $Path"
    }
}

if (@('infrastructure', 'all') -contains $Mode) {
    Remove-Safe (Join-Path $Root 'tools\comfy')
    Remove-Safe (Join-Path $Root 'tools\comfy-runtime')
    Remove-Safe (Join-Path $Root 'src\backend\venv')
    Remove-Safe (Join-Path $Root 'src\frontend\node_modules')
}
if (@('games', 'all') -contains $Mode) {
    Remove-Safe (Join-Path $Library 'generated')
    Remove-Safe (Join-Path $Library 'exports')
    Remove-Safe (Join-Path $Library 'backups')
}
if ($Mode -eq 'all') {
    $keep = @('.git')
    Get-ChildItem -LiteralPath $Root -Force |
        Where-Object { $_.Name -notin $keep -and $_.FullName -ne $MyInvocation.MyCommand.Path } |
        ForEach-Object { Remove-Safe $_.FullName }
    $self = $MyInvocation.MyCommand.Path
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', "Start-Sleep 1; Remove-Item -LiteralPath '$self' -Force"
}
Write-Host 'Done. Saves were not touched.' -ForegroundColor Green
