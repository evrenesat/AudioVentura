#!/usr/bin/env bash
set -euo pipefail

av_app_pid=""
av_queue_pid=""

# ShellCheck cannot infer that this function is reached through the trap below.
# shellcheck disable=SC2317
av_cleanup() {
  trap - EXIT INT TERM
  for av_child_pid in "$av_queue_pid" "$av_app_pid"; do
    if [[ -n "$av_child_pid" ]] && kill -0 "$av_child_pid" 2>/dev/null; then
      kill -TERM "$av_child_pid" 2>/dev/null || true
    fi
  done
  for av_child_pid in "$av_queue_pid" "$av_app_pid"; do
    if [[ -n "$av_child_pid" ]]; then
      wait "$av_child_pid" 2>/dev/null || true
    fi
  done
}
trap av_cleanup EXIT INT TERM

uvicorn salad.worker_api:app --host 0.0.0.0 --port 8080 &
av_app_pid="$!"

av_ready=0
for _ in $(seq 1 900); do
  if ! kill -0 "$av_app_pid" 2>/dev/null; then
    wait "$av_app_pid"
    exit "$?"
  fi
  if curl --silent --show-error --fail --output /dev/null \
    http://127.0.0.1:8080/ready; then
    av_ready=1
    break
  fi
  sleep 2
done

if [[ "$av_ready" -ne 1 ]]; then
  echo "AudioVentura runtime readiness deadline exceeded" >&2
  exit 1
fi

/usr/local/bin/salad-http-job-queue-worker &
av_queue_pid="$!"

set +e
wait -n "$av_app_pid" "$av_queue_pid"
av_exit_status="$?"
set -e
exit "$av_exit_status"
