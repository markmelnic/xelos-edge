#!/usr/bin/env sh
# Xelos Edge installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/markmelnic/xelos-edge/main/install.sh | sh
#
# What it does:
#   1. Auto-installs missing dependencies (Python 3.11+, Node 18+,
#      Claude Code CLI) via the system package manager when possible.
#   2. Creates an isolated venv at ~/.xelos/runtime.
#   3. Installs the `xelos-edge` package into that venv.
#   4. Drops a thin `xelos` launcher into ~/.local/bin so it's on PATH.
#   5. Prompts to log in to Claude Code so the daemon can spawn agents
#      out of the box.
#
# Re-running is safe: existing venvs are upgraded in place.
#
# Env knobs:
#   XELOS_NONINTERACTIVE=1   skip all `read` prompts (use defaults)
#   XELOS_SKIP_DEPS=1        don't try to auto-install missing deps
#   XELOS_SKIP_CLAUDE=1      don't install or log in to Claude Code

set -eu

XELOS_HOME="${XELOS_HOME:-$HOME/.xelos}"
RUNTIME_DIR="$XELOS_HOME/runtime"
BIN_DIR="${XELOS_BIN_DIR:-$HOME/.local/bin}"
PACKAGE_SPEC="${XELOS_PACKAGE_SPEC:-git+https://github.com/markmelnic/xelos-edge.git@main}"
NONINTERACTIVE="${XELOS_NONINTERACTIVE:-0}"
SKIP_DEPS="${XELOS_SKIP_DEPS:-0}"
SKIP_CLAUDE="${XELOS_SKIP_CLAUDE:-0}"

# Pretty printing -----------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    BOLD=$(tput bold)
    DIM=$(tput dim)
    RED=$(tput setaf 1)
    GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3)
    RESET=$(tput sgr0)
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

info()  { printf "%s==>%s %s\n" "$BOLD" "$RESET" "$1"; }
warn()  { printf "%s!!%s %s\n" "$YELLOW$BOLD" "$RESET" "$1" >&2; }
fail()  { printf "%sx%s %s\n" "$RED$BOLD" "$RESET" "$1" >&2; exit 1; }
ok()    { printf "%s✓%s %s\n" "$GREEN$BOLD" "$RESET" "$1"; }
note()  { printf "  %s%s%s\n" "$DIM" "$1" "$RESET"; }

confirm() {
    # confirm "Question?" → returns 0 on yes (default), 1 on no.
    if [ "$NONINTERACTIVE" = "1" ]; then
        return 0
    fi
    if [ ! -t 0 ]; then
        # No tty (curl … | sh) — go ahead with the safe default.
        return 0
    fi
    printf "%s? %s [Y/n] " "$BOLD" "$1"; printf "%s" "$RESET"
    read -r reply
    case "$reply" in
        n|N|no|NO) return 1 ;;
        *) return 0 ;;
    esac
}

# Package-manager helpers ---------------------------------------------------
OS_NAME=$(uname -s)
PKG_INSTALL=""
case "$OS_NAME" in
    Darwin)
        if command -v brew >/dev/null 2>&1; then
            PKG_INSTALL="brew install"
        fi
        ;;
    Linux)
        if   command -v apt    >/dev/null 2>&1; then PKG_INSTALL="sudo apt-get install -y"
        elif command -v dnf    >/dev/null 2>&1; then PKG_INSTALL="sudo dnf install -y"
        elif command -v pacman >/dev/null 2>&1; then PKG_INSTALL="sudo pacman -S --noconfirm"
        elif command -v apk    >/dev/null 2>&1; then PKG_INSTALL="sudo apk add"
        elif command -v zypper >/dev/null 2>&1; then PKG_INSTALL="sudo zypper install -y"
        fi
        ;;
esac

pkg_install() {
    # pkg_install "python3 python3-venv" "brew formula" — first arg for
    # apt/dnf/pacman/apk/zypper, second for brew. Empty second arg falls
    # back to the first.
    if [ -z "$PKG_INSTALL" ]; then
        return 1
    fi
    case "$OS_NAME" in
        Darwin)
            $PKG_INSTALL ${2:-$1}
            ;;
        *)
            $PKG_INSTALL $1
            ;;
    esac
}

# Find a usable Python ------------------------------------------------------
find_python() {
    for cmd in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            ver=$("$cmd" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")
            case "$ver" in
                3.11|3.12|3.13|3.14|3.15)
                    echo "$cmd"
                    return 0
                    ;;
            esac
        fi
    done
    return 1
}

ensure_python() {
    PYTHON=$(find_python || true)
    if [ -n "${PYTHON:-}" ]; then
        ok "Found Python: $($PYTHON -V 2>&1) ($PYTHON)"
        return 0
    fi

    warn "No Python 3.11+ on PATH."
    if [ "$SKIP_DEPS" = "1" ]; then
        fail "Install Python 3.11+ manually and re-run (XELOS_SKIP_DEPS=1)."
    fi
    if [ -z "$PKG_INSTALL" ]; then
        warn "No supported package manager detected."
        note "Install Python 3.11+ from https://www.python.org/downloads/ and re-run."
        fail "Python missing."
    fi

    if confirm "Install Python 3.12 via your package manager now?"; then
        case "$OS_NAME" in
            Darwin) pkg_install "" "python@3.12" || fail "brew install python@3.12 failed" ;;
            *)      pkg_install "python3 python3-venv python3-pip" || fail "Python install failed" ;;
        esac
    else
        fail "Python missing — re-run after installing it."
    fi

    PYTHON=$(find_python || true)
    [ -n "${PYTHON:-}" ] || fail "Python install completed but interpreter still not found on PATH."
    ok "Installed Python: $($PYTHON -V 2>&1)"
}

ensure_node() {
    if command -v node >/dev/null 2>&1; then
        ok "Found Node: $(node -v)"
        return 0
    fi
    warn "Node.js not found (Claude Code needs it)."
    if [ "$SKIP_DEPS" = "1" ]; then
        warn "Skipping node install (XELOS_SKIP_DEPS=1)."
        return 0
    fi
    if [ -z "$PKG_INSTALL" ]; then
        warn "No supported package manager — install Node 18+ from https://nodejs.org and re-run."
        return 0
    fi
    if confirm "Install Node.js via your package manager now?"; then
        case "$OS_NAME" in
            Darwin) pkg_install "" "node" || warn "brew install node failed" ;;
            *)      pkg_install "nodejs npm" || warn "node install failed" ;;
        esac
    fi
    if command -v node >/dev/null 2>&1; then
        ok "Installed Node: $(node -v)"
    fi
}

ensure_claude_code() {
    if [ "$SKIP_CLAUDE" = "1" ]; then
        return 0
    fi
    if command -v claude >/dev/null 2>&1; then
        ok "Found Claude Code: $(claude --version 2>/dev/null || echo present)"
        return 0
    fi
    warn "Claude Code CLI (`claude`) not found."
    if ! command -v npm >/dev/null 2>&1; then
        warn "npm not available — skipping Claude Code install."
        return 0
    fi
    if confirm "Install Claude Code via npm (\`npm i -g @anthropic-ai/claude-code\`)?"; then
        if ! npm install -g @anthropic-ai/claude-code; then
            warn "Claude Code install failed — install manually from https://docs.anthropic.com/claude-code"
            return 0
        fi
    else
        return 0
    fi
    if command -v claude >/dev/null 2>&1; then
        ok "Claude Code installed."
    fi
}

claude_login_prompt() {
    if [ "$SKIP_CLAUDE" = "1" ]; then return 0; fi
    if ! command -v claude >/dev/null 2>&1; then return 0; fi
    if [ "$NONINTERACTIVE" = "1" ]; then return 0; fi
    if [ ! -t 0 ]; then
        note "Not running interactively — log in later with: claude login"
        return 0
    fi
    if confirm "Log in to Claude Code now? (opens a browser)"; then
        claude login || warn "claude login exited non-zero — try again with: claude login"
    fi
}

# Main ----------------------------------------------------------------------
info "Xelos Edge installer"

ensure_python
ensure_node
ensure_claude_code

# Some Linux distros split out the venv stdlib package; check it works.
if ! "$PYTHON" -m venv --help >/dev/null 2>&1; then
    warn "Python is missing the 'venv' module."
    if [ -n "$PKG_INSTALL" ] && confirm "Install python3-venv now?"; then
        pkg_install "python3-venv" "" || true
    fi
    "$PYTHON" -m venv --help >/dev/null 2>&1 || fail "Install python3-venv (or equivalent) and re-run."
fi

mkdir -p "$XELOS_HOME"
chmod 700 "$XELOS_HOME" 2>/dev/null || true

if [ ! -d "$RUNTIME_DIR" ]; then
    info "Creating runtime venv at $RUNTIME_DIR"
    "$PYTHON" -m venv "$RUNTIME_DIR"
else
    info "Reusing runtime venv at $RUNTIME_DIR"
fi

info "Installing xelos-edge..."
VENV_PY="$RUNTIME_DIR/bin/python"

# Ensure pip is available in the venv (some Linux distros ship Python without
# ensurepip wired up — fall back to the bundled module, then to system
# pip3/pip targeting the venv as a last resort).
if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    if ! "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
        for sys_pip in pip3 pip; do
            if command -v "$sys_pip" >/dev/null 2>&1; then
                "$sys_pip" install --quiet --upgrade --target="$RUNTIME_DIR/lib" pip || true
                break
            fi
        done
    fi
fi

"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet --upgrade "$PACKAGE_SPEC"
ok "Package installed"

# Drop launchers in ~/.local/bin -------------------------------------------
mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/xelos"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env sh
# Xelos Edge launcher (auto-generated by install.sh).
exec "$RUNTIME_DIR/bin/xelos" "\$@"
EOF
chmod +x "$LAUNCHER"
ok "Launcher: $LAUNCHER"

# `xelos-mcp` is the stdio MCP server Claude Code shells out to per run.
# Without a shim on PATH a Foreman / Workspace Dispatcher loses every
# cloud tool (`create_department`, `delegate_to_agent`, etc.) and looks
# broken to the user. The daemon also has a fallback that finds the
# binary inside its own venv, but the explicit shim keeps the install
# path uniform.
MCP_LAUNCHER="$BIN_DIR/xelos-mcp"
cat > "$MCP_LAUNCHER" <<EOF
#!/usr/bin/env sh
# Xelos MCP bridge (auto-generated by install.sh).
exec "$RUNTIME_DIR/bin/xelos-mcp" "\$@"
EOF
chmod +x "$MCP_LAUNCHER"
ok "MCP launcher: $MCP_LAUNCHER"

# PATH check ----------------------------------------------------------------
case ":$PATH:" in
    *":$BIN_DIR:"*)
        ;;
    *)
        warn "$BIN_DIR is not on your PATH yet."
        SHELL_NAME=$(basename "${SHELL:-}")
        case "$SHELL_NAME" in
            zsh)  RC="$HOME/.zshrc" ;;
            bash) RC="$HOME/.bashrc" ;;
            fish) RC="$HOME/.config/fish/config.fish" ;;
            *)    RC="" ;;
        esac
        if [ -n "$RC" ]; then
            note "Add this to $RC and reload your shell:"
            if [ "$SHELL_NAME" = "fish" ]; then
                note "    fish_add_path $BIN_DIR"
            else
                note "    export PATH=\"$BIN_DIR:\$PATH\""
            fi
        else
            note "Add $BIN_DIR to your PATH."
        fi
        ;;
esac

claude_login_prompt

ok "Done."
printf "\nNext steps:\n"
note "1. Generate a pair code in the Xelos UI under Devices."
note "2. Run:  xelos     (interactive menu — pair, serve, status, doctor)"
note "   Or:   xelos pair <CODE>   (one-shot, scriptable)"
