"""Device-owned capture transaction, process identity checks and recovery."""
import re
import shlex

LOCK = '/data/local/tmp/adreno-capture.lock'


def validate_state(state):
    if not re.fullmatch(r'/data/local/tmp/adreno-capture-[0-9a-f]{32}', state['remote']):
        raise ValueError('Invalid recovery directory')
    if state['config'] not in ('/data/vendor/gpu/qgl_config.txt', '/data/misc/gpu/qgl_config.txt'):
        raise ValueError('Invalid configuration path')
    if not re.fullmatch(r'[A-Za-z0-9_.]+', state['package']):
        raise ValueError('Invalid recovery package')


def common(state):
    validate_state(state)
    return _common(state)


def _common(state):
    # Keep the shell source literal: Python must never expand shell PID syntax.
    prefix = '\nset -eu\n' + '\n'.join(k+'='+shlex.quote(v) for k,v in [('R',state['remote']),('C',state['config']),('P',state['package']),('L',LOCK)])+'\n'
    return prefix + r'''
alive() {
    test -f "$1" || return 1
    read -r pid ticks < "$1"
    case "$pid" in ''|*[!0-9]*) return 1;; esac
    test -r /proc/"$pid"/stat || return 1
    test "$(awk '{print $22}' /proc/"$pid"/stat)" = "$ticks" || return 1
    test "$(awk '{print $3}' /proc/"$pid"/stat)" != Z
}
record() { pid=$1; printf '%s %s\n' "$1" "$(awk '{print $22}' /proc/"$1"/stat)" > "$R/.$2.tmp"; mv "$R/.$2.tmp" "$R/$2"; }
signature() {
    if test -e "$C"; then
        sha256sum "$C" | awk '{print $1}'
        stat -c '%a %u %g' "$C"
        ls -Zd "$C" | awk '{print $1}'
    else echo CONFIG_ABSENT; fi
}
'''


def recovery_script(state):
    return common(state) + r'''
if test "${1:-}" = --stop; then touch "$R/cancelled"; fi
if alive "$R/worker" && test "$PPID" != "$pid"; then
    if test "${1:-}" != --stop; then echo CAPTURE_RUNNING >&2; exit 3; fi
    # PID + start ticks prevent signalling an unrelated process after PID reuse.
    tr '\000' ' ' < /proc/"$pid"/cmdline | grep -F "$R/capture.sh" >/dev/null || exit 4
    kill -TERM "$pid"
    for n in 1 2 3 4 5; do alive "$R/worker" || break; sleep 1; done
    if alive "$R/worker"; then kill -KILL "$pid"; sleep 1; fi
fi
# Serialize EXIT trap, explicit recovery and the independent device watchdog.
if ! mkdir "$R/restoring" 2>/dev/null; then
    for n in 1 2 3 4 5; do
        test -d "$R/restoring" || exec sh "$R/restore.sh" "${1:-}"
        sleep 1
    done
    echo RECOVERY_BUSY >&2; exit 5
fi
trap 'rmdir "$R/restoring"' EXIT
if test -f "$R/armed"; then
    test "$(cat "$L/owner" 2>/dev/null)" = "$R" || { echo OWNER_MISMATCH >&2; exit 6; }
    am force-stop "$P"
    for child in sleeper logcat vprobe; do
        if alive "$R/$child"; then kill -TERM "$pid" 2>/dev/null || true; fi
    done
    if test -f "$R/existed"; then
        cp --preserve=all "$R/config.before" "$C"
        cmp "$R/config.before" "$C"
    else rm -f "$C"; fi
    signature > "$R/config.after"
    cmp "$R/config.signature" "$R/config.after"
    rm -f "$R/armed"
fi
if test "$(cat "$L/owner" 2>/dev/null)" = "$R"; then
    rm -f "$L/owner"
    rmdir "$L"
fi
test ! -e "$R/armed"
if test -f "$R/config.signature"; then
    signature > "$R/config.after"
    cmp "$R/config.signature" "$R/config.after"
fi
touch "$R/done"
echo RESTORED
'''


def worker_script(state, component, extras, seconds, binlog, uid, probe_ready):
    config = '0x0=0x8675309\nenableshaderlog=True\n' + ('enablebinlog=True\n' if binlog else '')
    launch = 'am start -W -n ' + shlex.quote(component) + ' ' + ' '.join(shlex.quote(x) for x in extras)
    return common(state) + r'''
mkdir "$L"
printf '%s' "$R" > "$L/owner"
record $$ worker
finish() {
    code=$?
    trap - EXIT INT TERM HUP
    if alive "$R/sleeper"; then kill -TERM "$pid" 2>/dev/null || true; fi
    if sh "$R/restore.sh"; then echo "$code" > "$R/exitcode"; else echo 90 > "$R/exitcode"; fi
}
trap finish EXIT
trap 'exit 130' INT TERM HUP
test ! -f "$R/cancelled"
test ! -L "$C"
signature > "$R/config.signature"
if test -e "$C"; then cp --preserve=all "$C" "$R/config.before"; touch "$R/existed"; fi
touch "$R/armed"
if test -f "$R/existed"; then cat "$R/config.before"; fi > "$C"
printf '\n' >> "$C"
''' + 'printf %s ' + shlex.quote(config) + r''' >> "$C"
chmod 644 "$C"
am force-stop "$P"
''' + f'logcat -b main -b system -b crash -v threadtime -T 1 --uid={int(uid)} > "$R/logcat-window.txt" 2> "$R/logcat-error.txt" &\n' + r'''
record $! logcat
sleep 1 & record $! sleeper; wait "$pid"
alive "$R/logcat" || { echo LOGCAT_STREAM_FAILED >&2; exit 7; }
''' + launch + '\n' + f'for n in $(seq 1 {seconds}); do\n' + r'''
    ps -A -o PID,NAME | awk -v p="$P" '$2==p || index($2,p ":")==1 {print $1}' >> "$R/pids"
    sleep 1 & record $! sleeper; wait "$pid"
done
am force-stop "$P"
''' + (r'''
for run in 0 1; do
    mkdir -p "$R/probe/$run"
    (cd "$R/probe/$run"; exec "$R/qglprobe") > "$R/probe/$run/stdout.txt" 2> "$R/probe/$run/stderr.txt" &
    record $! vprobe
    echo "$pid" > "$R/probe/$run/pid"
    if wait "$pid"; then echo 0 > "$R/probe/$run/exitcode"; else echo $? > "$R/probe/$run/exitcode"; fi
done
''' if probe_ready else '')


def watchdog_script(state, seconds):
    return common(state) + f'for n in $(seq 1 {seconds+60}); do\n' + r'''
    test ! -f "$R/done" || exit 0
    if test -f "$R/worker" && ! alive "$R/worker"; then
        sh "$R/restore.sh" --stop
        exit $?
    fi
    sleep 1
done
sh "$R/restore.sh" --stop
'''
