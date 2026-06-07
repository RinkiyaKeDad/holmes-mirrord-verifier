#!/usr/bin/env bash
# Runs both scenarios end-to-end. Used by `make demo`.
#
# Scenario 1 calls Claude — requires ANTHROPIC_API_KEY.
# Scenario 2 uses a prepared patch — works offline.
#
# `mirrord` on PATH: real verification (against MIRRORD_TARGET).
# No mirrord: simulated runs (so the scaffold is greppable end-to-end).

set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p demo/out

if command -v uv >/dev/null 2>&1; then
  RUN=(uv run python -m verifier.cli)
elif [[ -x .venv/bin/python ]]; then
  RUN=(.venv/bin/python -m verifier.cli)
else
  echo "no venv found — run 'make install' first" >&2
  exit 1
fi

echo
echo "================================================================"
echo "scenario 1 (offline): hand-crafted GOOD patch — verifier should PASS"
echo "================================================================"
"${RUN[@]}" run scenarios/scenario-1-fix-works-offline.json \
  --bundle-out demo/out/scenario-1-offline.json || true

echo
echo "================================================================"
echo "scenario 1 (live):    Claude proposes the patch — should also PASS"
echo "================================================================"
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "(skipped — ANTHROPIC_API_KEY not set)"
else
  "${RUN[@]}" run scenarios/scenario-1-fix-works.json \
    --bundle-out demo/out/scenario-1.json || true
fi

echo
echo "================================================================"
echo "scenario 2:           deliberately bad patch — verifier should REJECT"
echo "================================================================"
"${RUN[@]}" run scenarios/scenario-2-fix-fails.json \
  --bundle-out demo/out/scenario-2.json || true

echo
echo "proof bundles written to demo/out/"
