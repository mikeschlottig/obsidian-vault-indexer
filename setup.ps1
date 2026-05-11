# Obsidian Vault Indexer — Windows Setup
# Run once as Administrator: .\setup.ps1
# Subsequent runs are safe (idempotent).

param(
    [string]$ProjectRoot = $PSScriptRoot,
    [int]   $ServerPort  = 37842,
    [int]   $IndexEveryMinutes = 30
)

$ErrorActionPreference = "Stop"

Write-Host "=== Obsidian Vault Indexer Setup ===" -ForegroundColor Cyan
Write-Host "Project: $ProjectRoot"
Write-Host "Server:  http://localhost:$ServerPort"
Write-Host "Index interval: every $IndexEveryMinutes minutes"
Write-Host ""

# ── Verify uv is installed ────────────────────────────────────────────────────
Write-Host "[1/5] Checking uv..." -ForegroundColor Yellow
try {
    $uvVersion = uv --version 2>&1
    Write-Host "  uv found: $uvVersion" -ForegroundColor Green
} catch {
    Write-Host "  uv not found. Installing via PowerShell..." -ForegroundColor Red
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    # Reload PATH
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("PATH", "User")
    Write-Host "  uv installed." -ForegroundColor Green
}

# ── Install Python dependencies ───────────────────────────────────────────────
Write-Host "[2/5] Installing Python dependencies..." -ForegroundColor Yellow
Push-Location $ProjectRoot
uv sync --quiet
Write-Host "  Dependencies installed." -ForegroundColor Green
Pop-Location

# ── Create data directory ─────────────────────────────────────────────────────
Write-Host "[3/5] Creating data directory..." -ForegroundColor Yellow
$dataDir = Join-Path $env:USERPROFILE ".obsidian-indexer"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
Write-Host "  Data directory: $dataDir" -ForegroundColor Green

# ── Run initial index ─────────────────────────────────────────────────────────
Write-Host "[4/5] Running initial index (this may take a moment)..." -ForegroundColor Yellow
Push-Location $ProjectRoot
try {
    uv run python indexer.py
    Write-Host "  Initial index complete." -ForegroundColor Green
} catch {
    Write-Host "  Warning: Initial index failed. Will retry via scheduler." -ForegroundColor Yellow
    Write-Host "  Error: $_" -ForegroundColor Yellow
}
Pop-Location

# ── Create Task Scheduler tasks ───────────────────────────────────────────────
Write-Host "[5/5] Creating Windows Task Scheduler tasks..." -ForegroundColor Yellow

$uvExe      = (Get-Command uv).Source
$indexerCmd = "python indexer.py --root `"$ProjectRoot`""
$serverCmd  = "uvicorn server:app --port $ServerPort --host 127.0.0.1 --no-access-log"

# Helper to create/replace a task
function Register-ObsidianTask {
    param($Name, $Description, $Action, $Trigger)

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "  Replaced existing task: $Name" -ForegroundColor Gray
    }

    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType S4U `
        -RunLevel Highest

    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask `
        -TaskName   $Name `
        -Description $Description `
        -Action     $Action `
        -Trigger    $Trigger `
        -Principal  $principal `
        -Settings   $settings | Out-Null

    Write-Host "  Registered: $Name" -ForegroundColor Green
}

# --- Task 1: Indexer (every N minutes) ---
$indexAction = New-ScheduledTaskAction `
    -Execute    $uvExe `
    -Argument   "run $indexerCmd" `
    -WorkingDirectory $ProjectRoot

$indexTrigger = @(
    # Run at logon (so first run happens on login)
    $(New-ScheduledTaskTrigger -AtLogOn),
    # Then repeat every N minutes indefinitely
    $(New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes $IndexEveryMinutes) -Once -At (Get-Date))
)

Register-ObsidianTask `
    -Name        "ObsidianVaultIndexer" `
    -Description "Incremental Obsidian vault indexer — runs every $IndexEveryMinutes min" `
    -Action      $indexAction `
    -Trigger     $indexTrigger

# --- Task 2: Server (at logon, stays running) ---
$serverAction = New-ScheduledTaskAction `
    -Execute    $uvExe `
    -Argument   "run $serverCmd" `
    -WorkingDirectory $ProjectRoot

$serverTrigger = New-ScheduledTaskTrigger -AtLogOn

Register-ObsidianTask `
    -Name        "ObsidianVaultServer" `
    -Description "Obsidian Vault Index API server on port $ServerPort" `
    -Action      $serverAction `
    -Trigger     $serverTrigger

# ── Launch the server now (don't wait for reboot) ────────────────────────────
Write-Host ""
Write-Host "Starting server in background..." -ForegroundColor Yellow
Push-Location $ProjectRoot
Start-Process -FilePath $uvExe `
    -ArgumentList @("run", "uvicorn", "server:app", "--port", "$ServerPort", "--host", "127.0.0.1", "--no-access-log") `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden
Pop-Location

Start-Sleep -Seconds 2

# ── Verify server is up ───────────────────────────────────────────────────────
try {
    $health = Invoke-RestMethod "http://localhost:$ServerPort/api/health" -TimeoutSec 5
    Write-Host "  Server is up! Docs indexed: $($health.total_documents)" -ForegroundColor Green
} catch {
    Write-Host "  Server starting (may take a few seconds)..." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Dashboard URL: http://localhost:$ServerPort" -ForegroundColor White
Write-Host "Data directory: $dataDir" -ForegroundColor White
Write-Host ""
Write-Host "Scheduled tasks registered:" -ForegroundColor White
Write-Host "  ObsidianVaultIndexer  — runs every $IndexEveryMinutes minutes" -ForegroundColor Gray
Write-Host "  ObsidianVaultServer   — starts at login" -ForegroundColor Gray
Write-Host ""
Write-Host "To force a re-index now:" -ForegroundColor White
Write-Host "  uv run python src/indexer.py" -ForegroundColor Gray
Write-Host ""
Write-Host "To open the dashboard:" -ForegroundColor White
Write-Host "  Start-Process 'http://localhost:$ServerPort'" -ForegroundColor Gray
Write-Host ""
