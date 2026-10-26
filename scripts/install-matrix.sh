#!/bin/bash
# Lore local container install matrix: build the wheel ONCE in a throwaway
# container, then run scripts/smoke_install.py against a fleet of images in
# parallel. No Python needed on the host, only docker.
#
# Usage: [JOBS=4] [ONLY=name,name] scripts/install-matrix.sh [--worktree] [--no-arm] [--keep]
# Exit code = number of failed targets.
#
# ponytail: smallest working version; only covers the targets listed below.
set -euo pipefail

SCRIPT="${0##*/}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
readonly SMOKE="${REPO_ROOT}/scripts/smoke_install.py"

usage() {
  printf 'usage: %s [-h] [--worktree] [--no-arm] [--keep]\n' "$SCRIPT"
  printf '  -h, --help      show this help\n'
  printf '  --worktree      export the working tree (git ls-files --cached --others --exclude-standard)\n'
  printf '                  instead of HEAD\n'
  printf '  --no-arm        skip the linux/arm64 target\n'
  printf '  --keep          keep temp workdir and lore-matrix-* images (skip cleanup)\n'
  printf 'env: JOBS (default 4)  parallel smoke runs\n'
  printf '     ONLY=name,name    run only these target names\n'
  printf 'targets: py311 py312 py313 py314 py312-semantic alpine313 debian12 ubuntu24 fedora arm64-py313\n'
}

worktree=0
no_arm=0
keep=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help) usage; exit 0 ;;
    --worktree) worktree=1 ;;
    --no-arm) no_arm=1 ;;
    --keep) keep=1 ;;
    *)
      printf 'error: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

readonly JOBS="${JOBS:-4}"
readonly ONLY="${ONLY:-}"

want() { [[ -z "$ONLY" ]] || [[ ",${ONLY}," == *",${1},"* ]]; }

[[ "$(uname -s)" == "Linux" ]] || { printf 'error: Linux with docker required\n' >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { printf 'error: docker not found\n' >&2; exit 1; }
# preflight: the docker bind-mount source must be a file or docker creates a directory
[[ -f "$SMOKE" ]] || { printf 'error: %s not found\n' "$SMOKE" >&2; exit 1; }

WORK="$(mktemp -d)"
# invoked indirectly via the EXIT trap below
# shellcheck disable=SC2329
cleanup() {
  local rc=$?
  if [[ "$keep" == 1 ]]; then
    printf 'kept workdir: %s\n' "$WORK"
    printf 'kept images; clean with: docker image prune -af --filter label=lore-matrix=1\n'
  else
    # label filter only; never a bare prune
    docker image prune -af --filter label=lore-matrix=1 >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
  exit "$rc"
}
trap cleanup EXIT

mkdir -p "$WORK/src" "$WORK/dist" "$WORK/logs"

# --- 1. export the sources -------------------------------------------------
cd "$REPO_ROOT"
if [[ "$worktree" == 1 ]]; then
  # includes staged + untracked (non-ignored) files; symlinks are followed
  while IFS= read -r -d '' f; do
    mkdir -p "$WORK/src/$(dirname "$f")"
    cp "$f" "$WORK/src/$f"
  done < <(git ls-files -z --cached --others --exclude-standard)
else
  git archive HEAD | tar -x -C "$WORK/src"
fi

# --- 2. build the wheel ONCE in a throwaway container (no host Python) -----
if ! docker run --rm -v "$WORK/src:/src:ro" -v "$WORK/dist:/dist" python:3.12-slim \
  sh -c 'pip install -q build && cp -r /src /tmp/s && cd /tmp/s && python -m build --wheel --outdir /dist'; then
  printf 'error: wheel build failed\n' >&2
  exit 1
fi
if ! ls "$WORK"/dist/*.whl >/dev/null 2>&1; then
  printf 'error: no wheel produced\n' >&2
  exit 1
fi

# --- 3. per-target runner, written into the workdir ------------------------
RUNNER="$WORK/run.sh"
cat >"$RUNNER" <<'RUNNER_EOF'
#!/bin/bash
# $1=WORK $2=smoke script path $3=target name; spec line read from $1/targets.tsv
set -uo pipefail
WORK="$1"
SMOKE="$2"
name="$3"
line="$(grep -m1 "^${name}|" "$WORK/targets.tsv")"
IFS='|' read -r name _ image mode <<<"$line"
log="$WORK/logs/$name.log"
# --read-only works: pip/pipx write only under HOME=/tmp/home on the /tmp tmpfs.
flags=(--rm --user 1000:1000 -e HOME=/tmp/home --tmpfs "/tmp:rw,exec,size=1g"
  --read-only --cpus 2 --memory 3g
  -v "$WORK/dist:/dist:ro" -v "$SMOKE:/smoke/smoke_install.py:ro"
  --cap-drop ALL --security-opt no-new-privileges)
tmo=600
smoke_inner='whl=$(ls /dist/*.whl 2>/dev/null | head -n1)
[ -n "$whl" ] || { printf "FAIL wheel: no wheel in /dist\n"; exit 1; }
python /smoke/smoke_install.py --wheel "$whl"'
case "$mode" in
  smoke) inner="$smoke_inner" ;;
  semantic) inner="${smoke_inner} --extra semantic" ;;
  arm)
    flags+=(--platform linux/arm64)
    tmo=1800
    inner="$smoke_inner"
    ;;
  pipx) inner='whl=$(ls /dist/*.whl 2>/dev/null | head -n1)
[ -n "$whl" ] || { printf "FAIL wheel: no wheel in /dist\n"; exit 1; }
export PATH="$HOME/.local/bin:$PATH"
pipx install "$whl" && python3 /smoke/smoke_install.py --installed' ;;
  *)
    printf 'FAIL config: unknown mode %s\n' "$mode" >"$log"
    printf '1\n' >"$WORK/logs/$name.exit"
    exit 0
    ;;
esac
timeout "$tmo" docker run "${flags[@]}" "$image" sh -c "$inner" >"$log" 2>&1
printf '%s\n' "$?" >"$WORK/logs/$name.exit"
RUNNER_EOF
chmod +x "$RUNNER"

# --- 4. distro images that need root for package install: built via stdin --
build_image() {
  local name="$1" df="$2"
  printf 'building lore-matrix-%s ...\n' "$name"
  if ! printf '%s\n' "$df" |
    docker build -q --label lore-matrix=1 -t "lore-matrix-$name" - >/dev/null; then
    printf 'error: image build failed for lore-matrix-%s\n' "$name" >&2
    return 1
  fi
}

# An image counts as available when it is already local or can be pulled. `docker manifest
# inspect` is not used: it hangs under Docker Hub's anonymous rate limit.
image_available() {
  local ref="$1"
  docker image inspect "$ref" >/dev/null 2>&1 && return 0
  timeout 300 docker pull -q "$ref" >/dev/null
}

# name|python column|image|mode|base-ref-to-check
declare -a TARGETS=()
add_target() {
  local name="$1" py="$2" image="$3" mode="$4" ref="$5"
  want "$name" || return 0
  if [[ -n "$ref" ]]; then
    local pulltxt=""
    pulltxt="$(image_available "$ref" 2>&1)" || {
      # record WHY (rate limit vs actually missing) so throttled runs are obvious
      printf 'SKIP tag: cannot pull %s: %s\n' \
        "$ref" "$(printf '%s' "$pulltxt" | tail -n1)" >"$WORK/logs/$name.log"
      printf '0\n' >"$WORK/logs/$name.exit"
      TARGETS+=("${name}|${py}|${image}|skip")
      return 0
    }
  fi
  TARGETS+=("${name}|${py}|${image}|${mode}")
}

add_target py311 "3.11" "python:3.11-slim" smoke "python:3.11-slim"
add_target py312 "3.12" "python:3.12-slim" smoke "python:3.12-slim"
add_target py313 "3.13" "python:3.13-slim" smoke "python:3.13-slim"
add_target py314 "3.14" "python:3.14-slim" smoke "python:3.14-slim"
add_target py312-semantic "3.12 +semantic" "python:3.12-slim" semantic "python:3.12-slim"
# musl: base install only, no semantic extra expected there
add_target alpine313 "3.13 (musl)" "python:3.13-alpine" smoke "python:3.13-alpine"
if [[ "$no_arm" == 0 ]]; then
  add_target arm64-py313 "3.13 (arm64)" "python:3.13-slim" arm "python:3.13-slim"
fi

if want debian12 && image_available debian:12 >/dev/null 2>&1; then
  build_image debian12 'FROM debian:12
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv python3-pip pipx && rm -rf /var/lib/apt/lists/*'
  add_target debian12 "3 (apt pipx)" "lore-matrix-debian12" pipx ""
fi
if want ubuntu24 && image_available ubuntu:24.04 >/dev/null 2>&1; then
  build_image ubuntu24 'FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 python3-venv python3-pip pipx && rm -rf /var/lib/apt/lists/*'
  add_target ubuntu24 "3 (apt pipx)" "lore-matrix-ubuntu24" pipx ""
fi
if want fedora && image_available fedora:latest >/dev/null 2>&1; then
  build_image fedora 'FROM fedora:latest
RUN dnf -y install python3 python3-pip pipx && dnf clean all'
  add_target fedora "3 (dnf pipx)" "lore-matrix-fedora" pipx ""
fi

if [[ "${#TARGETS[@]}" -eq 0 ]]; then
  printf 'error: no targets selected (check ONLY=%s)\n' "$ONLY" >&2
  exit 2
fi

# --- 5. run the smoke suite in parallel ------------------------------------
# --read-only works for every target here: pip/pipx write only under HOME/tmpfs.
printf '%s\n' "${TARGETS[@]}" >"$WORK/targets.tsv"
printf '%s\n' "${TARGETS[@]}" | grep -v '|skip$' | cut -d'|' -f1 |
  xargs -r -P "$JOBS" -I{} bash "$RUNNER" "$WORK" "$SMOKE" "{}" || true

# --- 6. report --------------------------------------------------------------
printf '\n%-15s %-16s %-6s %s\n' target python result "failed steps"
printf '%s\n' "-------------------------------------------------------------------------------"
rc=0
for line in "${TARGETS[@]}"; do
  IFS='|' read -r name py image mode <<<"$line"
  log="$WORK/logs/$name.log"
  if [[ "$mode" == skip ]]; then
    printf '%-15s %-16s %-6s %s\n' "$name" "$py" "SKIP" "-"
    continue
  fi
  code="$(cat "$WORK/logs/$name.exit" 2>/dev/null || printf '1')"
  fails="$(grep -c '^FAIL' "$log" 2>/dev/null || true)"
  [[ -n "$fails" ]] || fails=0
  if [[ "$code" == 0 && "$fails" == 0 ]]; then
    steps="$(grep '^SKIP' "$log" 2>/dev/null | cut -d' ' -f2 | cut -d: -f1 | paste -sd, - || true)"
    printf '%-15s %-16s %-6s %s\n' "$name" "$py" "PASS" "${steps:--}"
  else
    rc=$((rc + 1))
    steps="$(grep '^FAIL' "$log" 2>/dev/null | cut -d' ' -f2 | cut -d: -f1 | paste -sd, - || true)"
    [[ -n "$steps" ]] || steps="exit=$code (see log)"
    printf '%-15s %-16s %-6s %s\n' "$name" "$py" "FAIL" "$steps"
  fi
done
exit "$rc"
