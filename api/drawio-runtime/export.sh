#!/bin/sh
# Run the official draw.io Desktop CLI behind a disposable X server.
#
# xvfb-run relies on xauth resolving the sandbox hostname. gVisor deliberately
# does not provide that host identity, so use Xvfb's local, access-control-free
# mode instead. The display exists only for this process and is torn down after
# the export finishes.
set -eu

# Let Xvfb allocate a free display instead of deriving one from the wrapper PID.
# A PID-derived display can collide with a socket left by a previously killed
# Electron process and make every later feedback render in the Chat fail.
display_file="$(mktemp /tmp/flowork-drawio-display.XXXXXX)"
xvfb_log="$(mktemp /tmp/flowork-drawio-xvfb.XXXXXX.log)"

Xvfb -displayfd 3 -screen 0 1920x1080x24 -ac -nolisten tcp \
  3>"${display_file}" >"${xvfb_log}" 2>&1 &
xvfb_pid=$!

cleanup() {
  kill "${xvfb_pid}" 2>/dev/null || true
  wait "${xvfb_pid}" 2>/dev/null || true
  rm -f "${display_file}" "${xvfb_log}"
}
trap cleanup EXIT HUP INT TERM

attempt=0
while [ ! -s "${display_file}" ]; do
  if ! kill -0 "${xvfb_pid}" 2>/dev/null; then
    cat "${xvfb_log}" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  if [ "${attempt}" -ge 100 ]; then
    cat "${xvfb_log}" >&2
    echo "Timed out waiting for Xvfb to allocate a display" >&2
    exit 1
  fi
  sleep 0.05
done

display=":$(cat "${display_file}")"

# The runtime owns a cancellable process group containing this wrapper,
# Xvfb and Electron. Do not add a fixed export deadline or split that group.
set +e
DISPLAY="${display}" drawio --no-sandbox --disable-gpu "$@"
status=$?
set -e

exit "${status}"
