#!/usr/bin/env bash
# scripts/install.sh — Agent Memory Engine local installer
#
# Usage:
#   bash scripts/install.sh
#
# Checks Git, Python 3.11+, and uv; installs dependencies via uv sync;
# runs a lightweight health check; prints ready-to-copy MCP config for
# Cursor and Claude Code.
#
# Supports: macOS, Linux
# Does NOT: edit any config files, require Docker, make network calls
#           beyond the uv/Astral installer (if uv is missing).

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

# ── Globals ───────────────────────────────────────────────────────────────────

OS=""
REPO_ROOT=""

# ── Helper functions ──────────────────────────────────────────────────────────

print_header() {
    echo ""
    echo -e "${BOLD}┌─────────────────────────────────────────────────────┐${NC}"
    echo -e "${BOLD}│          Agent Memory Engine  ·  Installer          │${NC}"
    echo -e "${BOLD}│   Local-first persistent memory for coding agents   │${NC}"
    echo -e "${BOLD}└─────────────────────────────────────────────────────┘${NC}"
    echo ""
}

print_success() { echo -e "  ${GREEN}✓${NC}  $1"; }
print_warning() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
print_error()   { echo -e "  ${RED}✗${NC}  $1" >&2; }
print_info()    { echo -e "  ${BLUE}→${NC}  $1"; }
print_step()    { echo ""; echo -e "${BOLD}$1${NC}"; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

# ── OS Detection ──────────────────────────────────────────────────────────────

detect_os() {
    print_step "Detecting operating system..."
    case "$(uname -s)" in
        Darwin)
            OS="macos"
            print_success "macOS detected"
            ;;
        Linux)
            OS="linux"
            print_success "Linux detected"
            ;;
        *)
            print_error "Unsupported operating system: $(uname -s)"
            echo ""
            echo "  Agent Memory Engine installer currently supports macOS and Linux."
            echo "  Windows support is planned as future work."
            echo "  See: https://github.com/uudam42/agent-memory-engine"
            exit 1
            ;;
    esac
}

# ── Git Check ─────────────────────────────────────────────────────────────────

check_git() {
    print_step "Checking Git..."
    if ! command_exists git; then
        print_error "Git is not installed."
        echo ""
        if [ "$OS" = "macos" ]; then
            echo "  Install with:  xcode-select --install"
            echo "  Or Homebrew:   brew install git"
        else
            echo "  Debian/Ubuntu: sudo apt-get install git"
            echo "  Fedora/RHEL:   sudo dnf install git"
        fi
        exit 1
    fi
    print_success "Git: $(git --version)"
}

# ── Python Check ──────────────────────────────────────────────────────────────

check_python() {
    print_step "Checking Python 3.11+..."
    local found_version=""

    for cmd in python3 python; do
        if command_exists "$cmd"; then
            local version
            version=$("$cmd" --version 2>&1 | awk '{print $2}')
            local major minor
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                print_success "Python $version  ($cmd)"
                return 0
            else
                found_version="$version"
                print_warning "Found Python $version via '$cmd' — need 3.11+"
            fi
        fi
    done

    print_error "Python 3.11 or newer is required."
    if [ -n "$found_version" ]; then
        echo "  Detected: Python $found_version"
    fi
    echo ""
    echo "  Install from: https://www.python.org/downloads/"
    if [ "$OS" = "macos" ]; then
        echo "  Or Homebrew:  brew install python@3.11"
    else
        echo "  Or deadsnakes (Ubuntu): sudo add-apt-repository ppa:deadsnakes/ppa"
        echo "                          sudo apt-get install python3.11"
    fi
    exit 1
}

# ── uv Check / Install ────────────────────────────────────────────────────────

check_or_install_uv() {
    print_step "Checking uv..."
    if command_exists uv; then
        print_success "uv: $(uv --version)"
        return 0
    fi

    print_info "uv not found — installing via Astral installer..."
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        # Extend PATH to include common uv install locations
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        if command_exists uv; then
            print_success "uv installed: $(uv --version)"
        else
            print_error "uv was installed but is not yet in PATH."
            echo ""
            echo "  Run one of the following to activate it, then re-run this installer:"
            echo "    source \"\$HOME/.local/bin/env\""
            echo "    source \"\$HOME/.cargo/env\""
            echo "  Or open a new terminal and try again."
            exit 1
        fi
    else
        print_error "Automatic uv installation failed."
        echo ""
        echo "  Install manually, then re-run this script:"
        echo "    curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
}

# ── Repository Root Resolution ────────────────────────────────────────────────

resolve_repo_root() {
    print_step "Resolving repository root..."

    # Prefer git (works from any subdirectory)
    if command_exists git && git rev-parse --show-toplevel >/dev/null 2>&1; then
        REPO_ROOT=$(git rev-parse --show-toplevel)
    else
        # Fallback: resolve relative to this script's location
        local script_dir
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        REPO_ROOT="$(cd "$script_dir/.." && pwd)"
    fi

    # Validate: pyproject.toml must exist at repo root
    if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
        print_error "Could not locate the repository root."
        echo "  Expected pyproject.toml at: $REPO_ROOT"
        echo "  Run this script from inside the cloned agent-memory-engine directory."
        exit 1
    fi

    print_success "Repository root: $REPO_ROOT"
}

# ── Install Dependencies ──────────────────────────────────────────────────────

install_dependencies() {
    print_step "Installing dependencies..."
    print_info "Running: uv sync  (in $REPO_ROOT)"
    cd "$REPO_ROOT"
    uv sync
    print_success "Dependencies installed."
}

# ── Health Check ──────────────────────────────────────────────────────────────

run_health_check() {
    print_step "Running health checks..."
    cd "$REPO_ROOT"

    # Python version inside venv
    local py_version
    py_version=$(uv run python --version 2>&1)
    print_success "Runtime Python:  $py_version"

    # uv
    print_success "uv:              $(uv --version)"

    # SQLite (always available in CPython stdlib)
    local sqlite_version
    sqlite_version=$(uv run python -c "import sqlite3; print(sqlite3.sqlite_version)")
    print_success "SQLite:          $sqlite_version"

    # MCP server entrypoint (--version exits immediately, no server started)
    local mcp_version
    mcp_version=$(uv run memory-engine-mcp --version 2>&1)
    print_success "MCP entrypoint:  $mcp_version"

    print_success "All health checks passed."
}

# ── MCP Configuration ─────────────────────────────────────────────────────────

print_mcp_config() {
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  Ready-to-copy MCP configuration                            ${NC}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  Replace  /absolute/path/to/your-project  with the project you"
    echo "  want to give persistent memory, then restart your client."
    echo ""

    # ── Cursor ──────────────────────────────────────────────────────────────
    echo -e "${BOLD}  Cursor${NC}  →  .cursor/mcp.json  (or global Cursor MCP settings)"
    echo ""
    cat <<EOF
  {
    "mcpServers": {
      "memory-engine": {
        "command": "uv",
        "args": [
          "run",
          "--directory",
          "${REPO_ROOT}",
          "memory-engine-mcp",
          "--project-root",
          "/absolute/path/to/your-project"
        ]
      }
    }
  }
EOF
    echo ""

    # ── Claude Code ─────────────────────────────────────────────────────────
    echo -e "${BOLD}  Claude Code${NC}  →  ~/.claude.json  (or project-level config)"
    echo ""
    cat <<EOF
  {
    "mcpServers": {
      "memory-engine": {
        "command": "uv",
        "args": [
          "run",
          "--directory",
          "${REPO_ROOT}",
          "memory-engine-mcp"
        ],
        "env": {
          "MEMORY_ENGINE_PROJECT_ROOT": "/absolute/path/to/your-project"
        }
      }
    }
  }
EOF
    echo ""
}

# ── Final Summary ─────────────────────────────────────────────────────────────

print_final_summary() {
    echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}${BOLD}  Installation complete.                                      ${NC}"
    echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  Repository:  $REPO_ROOT"
    echo ""
    echo "  Next steps:"
    echo "    1. Copy the MCP configuration block above for your client."
    echo "    2. Replace /absolute/path/to/your-project with a real project path."
    echo "    3. Paste into your Cursor or Claude Code config file."
    echo "    4. Restart your coding agent client."
    echo ""
    echo "  Further reading:"
    echo "    README:      $REPO_ROOT/README.md"
    echo "    Quick start: $REPO_ROOT/docs/guides/quickstart.md"
    echo "    Config:      $REPO_ROOT/docs/guides/configuration.md"
    echo ""
}

# ── Entry point ───────────────────────────────────────────────────────────────

main() {
    print_header
    detect_os
    check_git
    check_python
    check_or_install_uv
    resolve_repo_root
    install_dependencies
    run_health_check
    print_mcp_config
    print_final_summary
}

main "$@"
