#!/usr/bin/env bash
# THE documented command (organizer ruling, 2026-09-03: "they run the command
# we document"). Builds the judged image, runs one challenge inside a
# container with output/ and artifacts/ bind-mounted, copies result.json back
# to the host, and prints its headline fields. Optionally serves the
# generated app afterwards.
#
# Usage:
#   scripts/judge.sh [--platform linux/arm64|linux/amd64] [--image NAME] [--serve] [--idea-file PATH]
#
# Options:
#   --platform PLATFORM   Docker platform to build and run (default: linux/arm64,
#                          the judged platform on Apple Silicon; linux/amd64 for
#                          a fast local iteration loop -- timings differ, see README).
#   --image NAME           Image tag to build (default: agentcofounder:<arch-suffix>).
#   --idea-file PATH        Host path to a challenge idea file. Bind-mounted
#                          read-only into the container and passed through as
#                          --idea-file to `npm run challenge`. Default: the
#                          repository's own contract-public/development-idea.txt.
#   --serve                 After the run, serve output/app on http://localhost:3000
#                          (npm run serve) until interrupted with Ctrl-C.
#   -h, --help               Print this usage and exit.
#
# Env vars forwarded into the container when set on the host (never printed):
#   CHALLENGE_PROVIDER CHALLENGE_MODEL CHALLENGE_THINKING CHALLENGE_TIMEOUT_MS
#   BERGET_API_KEY CHALLENGE_API_KEY OPENAI_API_KEY
# A repository-root .env file, if present, is also passed with --env-file.
#
# Assumption: the host user is uid 1000, matching the image's "node" user
# (uid 1000) -- so files the container writes under the output/ and
# artifacts/ bind mounts come back host-owned, not root-owned. If your host
# uid differs, the mounted directories will be owned by a foreign uid inside
# the container instead; either accept that (Docker still lets that uid write
# to a bind mount it doesn't "own" as long as the host directory permissions
# allow it) or reclaim ownership afterwards with:
#   sudo chown -R "$(id -u):$(id -g)" output artifacts
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PLATFORM="linux/arm64"
IMAGE=""
SERVE=0
IDEA_FILE=""

print_usage() {
  sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --platform)
      PLATFORM="${2:?--platform requires a value}"
      shift 2
      ;;
    --image)
      IMAGE="${2:?--image requires a value}"
      shift 2
      ;;
    --serve)
      SERVE=1
      shift
      ;;
    --idea-file)
      IDEA_FILE="${2:?--idea-file requires a value}"
      shift 2
      ;;
    -h|--help)
      print_usage
      exit 0
      ;;
    *)
      echo "judge.sh: unknown argument: $1" >&2
      print_usage >&2
      exit 1
      ;;
  esac
done

ARCH_SUFFIX="${PLATFORM##*/}"
if [ -z "$IMAGE" ]; then
  IMAGE="agentcofounder:${ARCH_SUFFIX}"
fi
CONTAINER="agentcofounder-judge-$$"
# Reap the container no matter how this script exits (success, a failed
# command under `set -e`, or an interrupt) -- without this, a failure or
# Ctrl-C between `docker create` and the terminal `docker rm` below leaves it
# running/orphaned on the host.
trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true' EXIT

echo "judge.sh: building ${IMAGE} for ${PLATFORM}" >&2
docker buildx build --platform "$PLATFORM" --load -t "$IMAGE" .

mkdir -p output artifacts

CREATE_ARGS=(
  create
  --platform "$PLATFORM"
  --name "$CONTAINER"
)
if [ -f .env ]; then
  CREATE_ARGS+=(--env-file .env)
fi
for VAR in CHALLENGE_PROVIDER CHALLENGE_MODEL CHALLENGE_THINKING CHALLENGE_TIMEOUT_MS \
           BERGET_API_KEY CHALLENGE_API_KEY OPENAI_API_KEY; do
  if [ -n "${!VAR:-}" ]; then
    CREATE_ARGS+=(-e "$VAR")
  fi
done
CREATE_ARGS+=(
  -v "$PWD/output:/challenge/output"
  -v "$PWD/artifacts:/challenge/artifacts"
)

RUN_ARGS=()
if [ -n "$IDEA_FILE" ]; then
  MOUNTED="/challenge/judge-idea/$(basename "$IDEA_FILE")"
  CREATE_ARGS+=(-v "$(cd "$(dirname "$IDEA_FILE")" && pwd)/$(basename "$IDEA_FILE"):${MOUNTED}:ro")
  RUN_ARGS+=(--idea-file "$MOUNTED")
fi

CREATE_ARGS+=("$IMAGE")
CREATE_ARGS+=("${RUN_ARGS[@]}")

echo "judge.sh: creating container ${CONTAINER}" >&2
docker "${CREATE_ARGS[@]}" >/dev/null

echo "judge.sh: running (docker start -a ${CONTAINER})" >&2
set +e
docker start -a "$CONTAINER"
RUN_EXIT=$?
set -e
echo "judge.sh: container exited with status ${RUN_EXIT} (not fatal -- result.json is the source of truth)" >&2

if docker cp "${CONTAINER}:/challenge/result.json" ./result.json 2>/dev/null; then
  echo "judge.sh: copied result.json to $(pwd)/result.json" >&2
else
  echo "judge.sh: WARNING -- could not copy result.json out of the container" >&2
fi

docker rm "$CONTAINER" >/dev/null

if [ -f result.json ]; then
  node -e '
    const fs = require("fs");
    const r = JSON.parse(fs.readFileSync("result.json", "utf8"));
    const input = r.input_tokens ?? 0;
    const output = r.output_tokens ?? 0;
    const cacheRead = r.cache_read_tokens ?? 0;
    // Efficiency points formula (docs/PHASE1_READINESS.md #6): not a result.json
    // field -- derived here for a quick eyeball, official scoring is external.
    const points = input + 3 * output + 0.1 * cacheRead;
    console.log(`status: ${r.status}`);
    console.log(`model_calls: ${r.model_calls ?? "n/a"}`);
    console.log(`points (input + 3*output + 0.1*cache_read): ${points}`);
  '
fi

if [ "$SERVE" -eq 1 ]; then
  echo "judge.sh: serving output/app on http://localhost:3000 (Ctrl-C to stop)" >&2
  docker run --rm -p 3000:3000 -v "$PWD/output:/challenge/output" \
    --entrypoint npm "$IMAGE" run serve
fi
