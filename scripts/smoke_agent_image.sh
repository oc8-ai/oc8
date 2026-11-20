#!/bin/sh
# Smoke-tests an agent runtime image on one platform before CI publishes it
# (.github/workflows/agent-images.yml). arm64 runs under QEMU, so every check
# is bounded: a hung binary fails the job instead of stalling it.
#
#   smoke_agent_image.sh <image> <platform> <kind>
#
# kind: claude | codex | opencode   a CLI runtime image (its CLI's name)
#       isolated                    agent-isolated, the isolated runtime
#
# Every kind checks the tools oc8's sandbox contract execs inside a sandbox
# (oc8.sandbox.linger and oc8.sandbox.scratch: sh, tar, timeout, head, stat,
# GNU find -printf and head -z), as uid 1000, the user sandboxes run as.
set -eu

image=$1 platform=$2 kind=$3
limit=${SMOKE_TIMEOUT_S:-600}

contract='set -eu
for t in sh tar timeout head stat find id; do
  command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }
done
tar --version | head -n 1
timeout --version | head -n 1
stat --version | head -n 1
find --version | head -n 1
test "$(id -u)" = 1000 || { echo "runs as uid $(id -u), not 1000" >&2; exit 1; }
test "$(find / -maxdepth 0 -printf "%p %y")" = "/ d"
test "$(stat -c %u /)" = 0
test "$(printf "a\0b\0" | head -z -n 1 | tr "\0" "\n")" = a
timeout 1 sleep 0
echo "contract tools: ok"
'

case "$kind" in
  claude | codex | opencode)
    # The CLI images end in `USER node` (uid 1000): run as the image says.
    user_args=""
    checks="$contract
node --version
python3 --version
python3 -c 'import playwright, pandas, PIL, reportlab'
$kind --version
chrome=\$(find /opt/ms-playwright -type f -name chrome -perm -u+x | head -n 1)
test -n \"\$chrome\" || { echo 'no Playwright Chromium' >&2; exit 1; }
timeout 120 \"\$chrome\" --version
echo '$kind runtime: ok'
"
    ;;
  isolated)
    # The shell imports, run_program's python3 is the venv's, and the venv
    # holds the oc8 package only, no capas.
    user_args="--user 1000:1000"
    checks="$contract
/opt/venv/bin/python -c 'import oc8.isolated_shell, httpx'
test \"\$(command -v python3)\" = /opt/venv/bin/python3
/opt/venv/bin/python - <<'PY'
import importlib.metadata as md
names = {d.metadata['Name'].lower() for d in md.distributions()}
extra = sorted(n for n in names if n.startswith('oc8') and n != 'oc8')
assert not extra, extra
PY
test ! -e /app/capas
echo 'isolated runtime: ok'
"
    ;;
  *)
    echo "unknown kind: $kind" >&2
    exit 2
    ;;
esac

# shellcheck disable=SC2086  # user_args is empty or two words
exec timeout "$limit" docker run --rm --platform "$platform" $user_args \
  --entrypoint /bin/sh "$image" -c "$checks"
