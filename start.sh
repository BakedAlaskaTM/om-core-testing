#!/bin/bash

# OpenModeling launcher — runtime-first, then clients.
#
# Architecture:
#   1. Runtime (engine + bus + command service + transport) starts first.
#   2. Clients (GUI with splash, REPL, etc.) connect separately.
#   3. Default: GUI + REPL both start. GUI gets the splash screen.
#
# Requires uv. Install from https://docs.astral.sh/uv.

# Resolve script directory for background/foreground coordination
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the Python environment
source "$SCRIPT_DIR/scripts/resolve_python_env.sh"
resolve_python_env

# ---------------------------------------------------------------------------
# Extract transport args (--socket, --host, --port) and export as env vars
# so they propagate to main.py, _wait_for_transport, and sub-processes (TUI).
# ---------------------------------------------------------------------------
CARRY_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --socket)
            if [ -n "${2:-}" ]; then
                export OPENM_TRANSPORT_SOCKET="$2"
                shift 2
            else
                shift
            fi
            ;;
        --host)
            if [ -n "${2:-}" ]; then
                export OPENM_TRANSPORT_HOST="$2"
                shift 2
            else
                shift
            fi
            ;;
        --port)
            if [ -n "${2:-}" ]; then
                export OPENM_TRANSPORT_PORT="$2"
                shift 2
            else
                shift
            fi
            ;;
        *)
            # Non-transport arg — keep it for mode dispatch below.
            # Preserve positional args by shifting onto a carry list and
            # re-setting them after the loop.
            CARRY_ARGS+=("$1")
            # Track --log separately so it can be forwarded to the TUI.
            if [ "$1" = "--log" ]; then
                TUI_LOG_FLAG=1
            fi
            shift
            ;;
    esac
done
# Restore positional parameters (mode + its args) for dispatch below.
if [ "${#CARRY_ARGS[@]}" -gt 0 ]; then
    set -- "${CARRY_ARGS[@]}"
else
    set --
fi

cleanup() {
    # Default-mode only: kill background GUI if REPL exited abnormally.
    # Client-only modes (--tui, --repl, etc.) have no background orphans.
    if [ -n "${GUI_PID:-}" ]; then
        kill "$GUI_PID" 2>/dev/null
        wait "$GUI_PID" 2>/dev/null
    fi
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_wait_for_transport() {
    # Block until the runtime transport endpoint is accepting connections.
    # This prevents the TUI client from launching before the runtime is ready.
    local max_wait=30
    local waited=0
    local socket_path="${OPENM_TRANSPORT_SOCKET:-/tmp/openm-${USER:-unknown}.sock}"
    local host="${OPENM_TRANSPORT_HOST:-127.0.0.1}"
    local port="${OPENM_TRANSPORT_PORT:-17391}"

    echo "Waiting for OM runtime to be ready..."
    while [ "$waited" -lt "$max_wait" ]; do
        if [ -n "$OPENM_TRANSPORT_SOCKET" ] || { [ -z "$OPENM_TRANSPORT_HOST" ] && [ -z "$OPENM_TRANSPORT_PORT" ]; }; then
            # Unix-domain socket (explicit or default)
            if [ -S "$socket_path" ]; then
                if $PYTHON -c "import socket, sys; s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(1); s.connect(sys.argv[1]); s.close()" "$socket_path" 2>/dev/null; then
                    echo "Runtime ready."
                    return 0
                fi
            fi
        else
            # TCP mode
            if $PYTHON -c "import socket, sys; s=socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.settimeout(1); s.connect((sys.argv[1], int(sys.argv[2]))); s.close()" "$host" "$port" 2>/dev/null; then
                echo "Runtime ready."
                return 0
            fi
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "Warning: OM runtime did not become ready within ${max_wait}s; TUI may fail to connect."
    return 1
}

# ---------------------------------------------------------------------------
# Mode dispatch
# ---------------------------------------------------------------------------

if [ "$1" = "--remote" ]; then
    # Launch with remote evaluation backend
    export OMENGINE_MODE=remote
    echo "Remote engine enabled (OMENGINE_MODE=remote)"
    shift
    # Re-dispatch with remaining args; if none remain, fall through to the
    # default mode (which prompts for TUI) instead of forcing --gui-only.
fi

if [ "$1" = "--runtime" ]; then
    # Standalone runtime host (no clients)
    echo "Starting OM runtime host..."
    $PYTHON -O main.py --runtime
    exit $?

elif [ "$1" = "--gui" ]; then
    # GUI client only (connects to existing --runtime)
    echo "Starting OM GUI client..."
    $PYTHON -O main.py --gui
    exit $?

elif [ "$1" = "--gui-only" ]; then
    # Runtime + GUI only (no REPL client). Splash screen lives here.
    echo "Starting OM runtime + GUI..."
    $PYTHON -O main.py --gui-only
    exit $?

elif [ "$1" = "--repl" ]; then
    # REPL client only (assumes runtime is already running)
    echo "Starting OM REPL client..."
    $PYTHON -O main.py --repl
    REPL_EXIT=$?
    # Guard: restore terminal echo if the REPL died abnormally (SIGTERM, etc.)
    # Python finally blocks don't run on SIGTERM, so the shell must clean up.
    stty echo 2>/dev/null || true
    exit $REPL_EXIT

elif [ "$1" = "--tui" ]; then
    # TUI client only (connects to existing --runtime)
    echo "Starting OM TUI client..."
    $PYTHON -O main.py --tui "${@:2}"
    TUI_EXIT=$?
    stty echo 2>/dev/null || true
    exit $TUI_EXIT

elif [ "$1" = "--gui-with-repl" ]; then
    # Legacy single-process combined mode
    echo "Starting OM GUI + REPL (single process)..."
    $PYTHON -O main.py --gui-with-repl
    exit $?

elif [ "$1" = "--batch" ] && [ -n "$2" ]; then
    # Headless batch execution (remote client, requires runtime)
    echo "Running OM in batch mode: $2"
    $PYTHON -O main.py --batch "$2"
    exit $?

else
    # DEFAULT: runtime + GUI (detached), plus TUI in a separate terminal.
    # Ask before the GUI starts so the prompt is not lost in engine output.
    read -r -p "Open a TUI in a separate terminal? [Y/n]: " answer
    answer=${answer:-Y}
    OPEN_TUI=0
    case "$answer" in
        [Yy]*)
            OPEN_TUI=1
            ;;
    esac

    echo "Starting OM runtime + GUI..."
    $PYTHON -O main.py --gui-only &
    GUI_PID=$!

    # Detach the GUI so it survives after this script exits.
    disown $GUI_PID 2>/dev/null || true
    GUI_PID=""

    if [ "$OPEN_TUI" -eq 1 ]; then
        # Wait for the runtime transport to be ready before opening the TUI.
        _wait_for_transport

        echo "Opening TUI in a separate terminal..."
        TUI_CMD="cd \"$SCRIPT_DIR\" && ./start.sh --tui"
        # Forward --log flag if present.
        if [ "${TUI_LOG_FLAG:-0}" = "1" ]; then
            TUI_CMD="$TUI_CMD --log"
        fi
        # Forward transport args so the TUI connects to the right endpoint.
        if [ -n "${OPENM_TRANSPORT_SOCKET:-}" ]; then
            TUI_CMD="$TUI_CMD --socket \"$OPENM_TRANSPORT_SOCKET\""
        fi
        if [ -n "${OPENM_TRANSPORT_HOST:-}" ]; then
            TUI_CMD="$TUI_CMD --host \"$OPENM_TRANSPORT_HOST\""
        fi
        if [ -n "${OPENM_TRANSPORT_PORT:-}" ]; then
            TUI_CMD="$TUI_CMD --port \"$OPENM_TRANSPORT_PORT\""
        fi
        # Build the macOS variant (no shell quoting needed inside AppleScript).
        TUI_CMD_MAC="cd \"$SCRIPT_DIR\" && ./start.sh --tui"
        if [ "${TUI_LOG_FLAG:-0}" = "1" ]; then
            TUI_CMD_MAC="$TUI_CMD_MAC --log"
        fi
        if [ -n "${OPENM_TRANSPORT_SOCKET:-}" ]; then
            TUI_CMD_MAC="$TUI_CMD_MAC --socket \"$OPENM_TRANSPORT_SOCKET\""
        fi
        if [ -n "${OPENM_TRANSPORT_HOST:-}" ]; then
            TUI_CMD_MAC="$TUI_CMD_MAC --host \"$OPENM_TRANSPORT_HOST\""
        fi
        if [ -n "${OPENM_TRANSPORT_PORT:-}" ]; then
            TUI_CMD_MAC="$TUI_CMD_MAC --port \"$OPENM_TRANSPORT_PORT\""
        fi

        if command -v osascript >/dev/null 2>&1 && [ "$(uname -s)" = "Darwin" ]; then
            # macOS: open a new Terminal.app window and run the TUI client there.
            # Escape backslashes and double quotes so the path is safe in AppleScript.
            SCRIPT_DIR_AE=$(printf '%s\n' "$SCRIPT_DIR" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\n')
            osascript <<EOF
tell application "Terminal"
    activate
    do script "$TUI_CMD_MAC"
end tell
EOF
        elif command -v gnome-terminal >/dev/null 2>&1; then
            gnome-terminal -- bash -c "$TUI_CMD" &
        elif command -v konsole >/dev/null 2>&1; then
            konsole -e bash -c "$TUI_CMD" &
        elif command -v xfce4-terminal >/dev/null 2>&1; then
            xfce4-terminal -e "bash -c '$TUI_CMD'" &
        elif command -v alacritty >/dev/null 2>&1; then
            alacritty -e bash -c "$TUI_CMD" &
        elif command -v kitty >/dev/null 2>&1; then
            kitty bash -c "$TUI_CMD" &
        elif command -v xterm >/dev/null 2>&1; then
            xterm -e bash -c "$TUI_CMD" &
        else
            echo "No supported terminal emulator found."
            echo "Please run this manually in another terminal:"
            echo "  $TUI_CMD"
        fi
    else
        echo "TUI not opened. Run './start.sh --tui' later if you want a command line."
    fi

    # Release the original terminal; the GUI and TUI run independently.
    trap - EXIT INT TERM
    exit 0
fi
