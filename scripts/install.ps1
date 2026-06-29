#Requires -Version 5.1
<#
.SYNOPSIS
    Agent Memory Engine — Windows PowerShell installer.

.DESCRIPTION
    Local-first persistent memory runtime for coding agents.
    Validates Git, Python 3.11+, and uv; installs dependencies via uv sync;
    runs a lightweight health check; prints ready-to-copy MCP configuration.

    Does NOT: modify Cursor or Claude Code config files by default,
              require Docker, WSL, cloud infrastructure, or vector services.

.PARAMETER ProjectRoot
    Absolute path to the target project root (default: auto-detected from Git).

.PARAMETER SkipUvInstall
    Skip automatic uv installation if uv is missing.

.PARAMETER SkipSync
    Skip the uv sync (dependency installation) step.

.PARAMETER SkipHealthCheck
    Skip the post-install health check.

.PARAMETER ConfigureCursor
    Install the memory engine policy rule into .cursor/rules/ (opt-in only).

.PARAMETER ConfigureClaudeCode
    Install the memory engine policy block into CLAUDE.md (opt-in only).

.PARAMETER Help
    Show this help message and exit.

.EXAMPLE
    .\scripts\install.ps1
    .\scripts\install.ps1 -ProjectRoot "C:\Users\you\my-project"
    .\scripts\install.ps1 -SkipUvInstall
    .\scripts\install.ps1 -SkipSync
    .\scripts\install.ps1 -SkipHealthCheck
    .\scripts\install.ps1 -ConfigureCursor
    .\scripts\install.ps1 -Help
#>

[CmdletBinding()]
param(
    [string]  $ProjectRoot      = "",
    [switch]  $SkipUvInstall,
    [switch]  $SkipSync,
    [switch]  $SkipHealthCheck,
    [switch]  $ConfigureCursor,
    [switch]  $ConfigureClaudeCode,
    [switch]  $Help
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Write-Header {
    Write-Host ""
    Write-Host "+-----------------------------------------------------+" -ForegroundColor Cyan
    Write-Host "|          Agent Memory Engine  .  Installer          |" -ForegroundColor Cyan
    Write-Host "|   Local-first persistent memory for coding agents   |" -ForegroundColor Cyan
    Write-Host "|             Windows PowerShell setup                 |" -ForegroundColor Cyan
    Write-Host "+-----------------------------------------------------+" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Success { param([string]$Msg) Write-Host "  [OK]  $Msg" -ForegroundColor Green }
function Write-Warn    { param([string]$Msg) Write-Host "  [!!]  $Msg" -ForegroundColor Yellow }
function Write-Fail    { param([string]$Msg) Write-Host "  [XX]  $Msg" -ForegroundColor Red }
function Write-Info    { param([string]$Msg) Write-Host "  [->]  $Msg" -ForegroundColor Cyan }
function Write-Step    { param([string]$Msg) Write-Host ""; Write-Host $Msg -ForegroundColor White }

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

if ($Help) {
    Get-Help $PSCommandPath -Full
    exit 0
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

Write-Header

# ---------------------------------------------------------------------------
# Platform check
# ---------------------------------------------------------------------------

Write-Step "Checking platform..."
if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -lt 6) {
    # On Windows PowerShell 5.x, $IsWindows is not defined
    if ($PSVersionTable.PSEdition -eq "Core" -and -not $IsWindows) {
        Write-Fail "This installer is for Windows. Use scripts/install.sh on macOS/Linux."
        exit 1
    }
}
Write-Success "Windows PowerShell detected (v$($PSVersionTable.PSVersion))"

# ---------------------------------------------------------------------------
# Git check
# ---------------------------------------------------------------------------

Write-Step "Checking Git..."
if (-not (Test-CommandExists "git")) {
    Write-Fail "Git is not installed or not in PATH."
    Write-Host ""
    Write-Host "  Install Git for Windows from: https://git-scm.com/download/win" -ForegroundColor Yellow
    Write-Host "  Or via winget: winget install --id Git.Git -e --source winget" -ForegroundColor Yellow
    exit 1
}
$gitVersion = & git --version 2>&1
Write-Success "Git found: $gitVersion"

# ---------------------------------------------------------------------------
# Repository root resolution
# ---------------------------------------------------------------------------

Write-Step "Resolving repository root..."
$RepoRoot = $ProjectRoot

if ([string]::IsNullOrEmpty($RepoRoot)) {
    try {
        $gitRoot = & git rev-parse --show-toplevel 2>&1
        if ($LASTEXITCODE -eq 0) {
            # git returns forward-slash paths; normalize to Windows
            $RepoRoot = $gitRoot.Trim() -replace '/', '\'
        }
    } catch {
        # fall through to script-relative fallback
    }
}

if ([string]::IsNullOrEmpty($RepoRoot)) {
    # Fallback: resolve relative to this script's location
    $scriptDir = Split-Path -Parent $PSCommandPath
    $RepoRoot = Split-Path -Parent $scriptDir
}

# Verify pyproject.toml exists at resolved root
$pyprojectPath = Join-Path $RepoRoot "pyproject.toml"
if (-not (Test-Path $pyprojectPath)) {
    Write-Fail "Cannot locate repository root. Expected pyproject.toml at: $pyprojectPath"
    Write-Info "Pass -ProjectRoot explicitly: .\scripts\install.ps1 -ProjectRoot C:\path\to\agent-memory-engine"
    exit 1
}

Write-Success "Repository root: $RepoRoot"

# ---------------------------------------------------------------------------
# Python detection
# ---------------------------------------------------------------------------

Write-Step "Checking Python 3.11+..."

$pythonExe = $null
$pythonVersion = $null

foreach ($candidate in @("py", "python", "python3")) {
    try {
        # Try py launcher first (Windows-specific, supports version flags)
        if ($candidate -eq "py") {
            $out = & py -3.11 --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $out -match "Python (\d+\.\d+)") {
                $pythonExe = "py -3.11"
                $pythonVersion = $Matches[1]
                break
            }
            # Fall back to default py
            $out = & py --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $out -match "Python (\d+\.\d+)") {
                $ver = $Matches[1]
                $major, $minor = $ver -split '\.' | Select-Object -First 2
                if ([int]$major -ge 3 -and [int]$minor -ge 11) {
                    $pythonExe = "py"
                    $pythonVersion = $ver
                    break
                }
            }
        } else {
            $out = & $candidate --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $out -match "Python (\d+\.\d+)") {
                $ver = $Matches[1]
                $major, $minor = $ver -split '\.' | Select-Object -First 2
                if ([int]$major -ge 3 -and [int]$minor -ge 11) {
                    $pythonExe = $candidate
                    $pythonVersion = $ver
                    break
                }
            }
        }
    } catch {
        continue
    }
}

if ($null -eq $pythonExe) {
    Write-Fail "Python 3.11 or newer is required but was not found."
    Write-Host ""
    Write-Host "  Install Python from: https://www.python.org/downloads/windows/" -ForegroundColor Yellow
    Write-Host "  Or via winget: winget install --id Python.Python.3.11 -e --source winget" -ForegroundColor Yellow
    Write-Host "  Ensure 'Add Python to PATH' is checked during installation." -ForegroundColor Yellow
    exit 1
}

Write-Success "Python $pythonVersion found ($pythonExe)"

# ---------------------------------------------------------------------------
# uv detection / installation
# ---------------------------------------------------------------------------

Write-Step "Checking uv (package manager)..."

if (-not (Test-CommandExists "uv")) {
    if ($SkipUvInstall) {
        Write-Fail "uv not found and -SkipUvInstall was specified."
        Write-Host ""
        Write-Host "  Install uv manually (run in PowerShell):" -ForegroundColor Yellow
        Write-Host "  powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" -ForegroundColor Yellow
        exit 1
    }

    Write-Info "uv not found — installing via Astral installer..."
    try {
        $installScript = Invoke-RestMethod "https://astral.sh/uv/install.ps1"
        Invoke-Expression $installScript
    } catch {
        Write-Fail "Automatic uv installation failed: $_"
        Write-Host ""
        Write-Host "  Install uv manually (run in PowerShell):" -ForegroundColor Yellow
        Write-Host "  powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" -ForegroundColor Yellow
        exit 1
    }

    # Refresh PATH — uv installs to %USERPROFILE%\.cargo\bin or %LOCALAPPDATA%\uv\bin
    $uvBinPaths = @(
        "$env:USERPROFILE\.cargo\bin",
        "$env:LOCALAPPDATA\uv\bin",
        "$env:USERPROFILE\.local\bin"
    )
    foreach ($p in $uvBinPaths) {
        if (Test-Path $p) {
            $env:PATH = "$p;$env:PATH"
        }
    }

    if (-not (Test-CommandExists "uv")) {
        Write-Fail "uv installed but not found in PATH. Restart your terminal and rerun this script."
        exit 1
    }
}

$uvVersion = & uv --version 2>&1
Write-Success "uv found: $uvVersion"

# ---------------------------------------------------------------------------
# Install project dependencies
# ---------------------------------------------------------------------------

if ($SkipSync) {
    Write-Info "Skipping dependency installation (-SkipSync)"
} else {
    Write-Step "Installing project dependencies..."
    Push-Location $RepoRoot
    try {
        & uv sync
        if ($LASTEXITCODE -ne 0) {
            Write-Fail "uv sync failed. See output above for details."
            exit 1
        }
    } finally {
        Pop-Location
    }
    Write-Success "Dependencies installed"
}

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

if (-not $SkipHealthCheck) {
    Write-Step "Running health check..."

    # Python version
    $pyCheck = & uv run --directory $RepoRoot python --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Success "Python: $pyCheck"
    } else {
        Write-Warn "Python version check failed"
    }

    # Git available
    $gitCheck = & git --version 2>&1
    Write-Success "Git: $gitCheck"

    # uv available
    $uvCheck = & uv --version 2>&1
    Write-Success "uv: $uvCheck"

    # SQLite check (Python built-in)
    $sqliteCheck = & uv run --directory $RepoRoot python -c "import sqlite3; print('SQLite', sqlite3.sqlite_version)" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Success "SQLite: $sqliteCheck"
    } else {
        Write-Warn "SQLite check failed"
    }

    # MCP entry point (version/help — non-blocking)
    $mcpCheck = & uv run --directory $RepoRoot memory-engine-mcp --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Success "MCP entry point: $mcpCheck"
    } else {
        Write-Warn "MCP entry point check returned: $mcpCheck"
    }

    Write-Success "Health check complete"
}

# ---------------------------------------------------------------------------
# Optional: policy adapter installation
# ---------------------------------------------------------------------------

if ($ConfigureClaudeCode -or $ConfigureCursor) {
    Write-Step "Installing agent memory policy adapters..."

    if ($ConfigureClaudeCode) {
        try {
            & uv run --directory $RepoRoot memory policy install --project-root $ProjectRoot --client claude-code
            Write-Success "Claude Code policy adapter installed in $ProjectRoot\CLAUDE.md"
        } catch {
            Write-Warn "Claude Code adapter installation failed: $_"
        }
    }

    if ($ConfigureCursor) {
        try {
            & uv run --directory $RepoRoot memory policy install --project-root $ProjectRoot --client cursor
            Write-Success "Cursor policy adapter installed in $ProjectRoot\.cursor\rules\"
        } catch {
            Write-Warn "Cursor adapter installation failed: $_"
        }
    }
}

# ---------------------------------------------------------------------------
# Print MCP configuration examples
# ---------------------------------------------------------------------------

Write-Step "MCP Configuration"
Write-Host ""
Write-Host "  Copy one of the following blocks into your editor's MCP settings." -ForegroundColor Cyan
Write-Host ""

# Escape backslashes for JSON
$repoRootJson = $RepoRoot -replace '\\', '\\'

# Cursor (~/.cursor/mcp.json or project .cursor/mcp.json)
Write-Host "  -- Cursor (~/.cursor/mcp.json or .cursor/mcp.json) --" -ForegroundColor White
Write-Host @"
  {
    "mcpServers": {
      "memory-engine": {
        "command": "uv",
        "args": [
          "run",
          "--directory",
          "$repoRootJson",
          "memory-engine-mcp",
          "--project-root",
          "<PATH_TO_YOUR_PROJECT>"
        ]
      }
    }
  }
"@ -ForegroundColor Gray

Write-Host ""

# Claude Code (~/.claude/claude.json or project .claude/settings.json)
Write-Host "  -- Claude Code (~/.claude/claude.json mcpServers block) --" -ForegroundColor White
Write-Host @"
  {
    "mcpServers": {
      "memory-engine": {
        "type": "stdio",
        "command": "uv",
        "args": [
          "run",
          "--directory",
          "$repoRootJson",
          "memory-engine-mcp",
          "--project-root",
          "<PATH_TO_YOUR_PROJECT>"
        ],
        "env": {}
      }
    }
  }
"@ -ForegroundColor Gray

# ---------------------------------------------------------------------------
# Next steps
# ---------------------------------------------------------------------------

Write-Step "Next Steps"
Write-Host ""
Write-Host "  1. Copy the MCP configuration block above into your editor settings." -ForegroundColor Cyan
Write-Host "     Replace <PATH_TO_YOUR_PROJECT> with the project you want to give memory." -ForegroundColor Cyan
Write-Host ""
Write-Host "  2. Paste into Claude Code (~\.claude\claude.json) or Cursor (settings > MCP)." -ForegroundColor Cyan
Write-Host ""
Write-Host "  3. Restart your coding agent client." -ForegroundColor Cyan
Write-Host ""
Write-Host "  4. Agent workflow policy (CLAUDE.md / .cursor/rules):" -ForegroundColor Cyan
Write-Host "     On first use, Memory Engine will automatically write a workflow policy" -ForegroundColor White
Write-Host "     into your project's CLAUDE.md. This tells the agent exactly when to call" -ForegroundColor White
Write-Host "     retrieve_agent_context and reflect_and_write." -ForegroundColor White
Write-Host "     To install it immediately (before first use):" -ForegroundColor White
Write-Host "     uv run --directory $RepoRoot memory policy install ``" -ForegroundColor Gray
Write-Host "       --project-root <YOUR_PROJECT> --client claude-code" -ForegroundColor Gray
Write-Host ""
Write-Host "  Documentation: README.md" -ForegroundColor Cyan
Write-Host ""

Write-Host "+-----------------------------------------------------+" -ForegroundColor Green
Write-Host "|    Agent Memory Engine installed successfully!      |" -ForegroundColor Green
Write-Host "+-----------------------------------------------------+" -ForegroundColor Green
Write-Host ""

# Optional semantic retrieval prompt (skipped in CI / non-interactive environments)
if (-not $env:CI -and [Environment]::UserInteractive) {
    Write-Host ""
    Write-Host "Optional: Enable semantic retrieval (Phase 13)?" -ForegroundColor Cyan
    Write-Host "  Installs sentence-transformers + sqlite-vec for cross-wording search." -ForegroundColor White
    Write-Host "  Model download (~130 MB) happens on first use, not now." -ForegroundColor White
    $semReply = Read-Host "  Install and enable? [y/N]"
    if ($semReply -match "^[Yy]$") {
        Write-Host ""
        Write-Host "Installing memory-engine[semantic-transformers]..." -ForegroundColor Cyan
        uv pip install --directory $RepoRoot "memory-engine[semantic-transformers]"
        Write-Host "Dependencies installed." -ForegroundColor Green
        Write-Host ""
        Write-Host "  To activate for a project, run once per project:" -ForegroundColor White
        Write-Host "  uv run --directory $RepoRoot memory semantic status --enable --project-root <YOUR_PROJECT>" -ForegroundColor Gray
    } else {
        Write-Host "  Skipped. Enable later with:" -ForegroundColor White
        Write-Host "    uv pip install 'memory-engine[semantic-transformers]'" -ForegroundColor Gray
        Write-Host "    memory semantic status --enable --project-root <YOUR_PROJECT>" -ForegroundColor Gray
    }
    Write-Host ""
}
