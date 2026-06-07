# holmes-mirrord-verifier

The reference implementation that backs **Part II** of MetalBear's AI-SRE
verification post:

- Part I: *Auto-verifying your AI-SRE's fixes against your real cluster, with mirrord* (link TBD)
- Part II: *HolmesGPT, end-to-end on a real cluster: what passed, what didn't* (link TBD)

**The thesis.** AI-SREs (HolmesGPT, Resolve AI, incident.io's
Investigator, NeuBird, Datadog Bits, etc.) can suggest fixes. None of
them can prove the fix actually clears the alert before a human reviews
it. This repo shows how to plug `mirrord exec` in as the proving step:
take the AI-SRE's recommendation, run the patched code against the real
cluster (without a deploy), and emit a PASS/REJECT verdict tied to the
alert's actual SLO.

## What's in the repo

- `sample-app/` — toy `checkout` + `pricing` services with two planted
  bugs (error-rate, p99 latency).
- `src/verifier/` — the verifier: receives Alertmanager webhooks, calls
  a bridge wrapper to turn an AI-SRE recommendation into a code patch,
  runs the patch under `mirrord exec`, and emits a verdict.
- `walkthroughs/holmes-error-rate/`, `walkthroughs/holmes-latency/` —
  the two demo runs from Part II, with the actual HolmesGPT
  transcripts, the bridged patches, and the verification bundles.
- `scenarios/` — synthetic alert payloads + planted-bug seeds for
  `make demo`.
- `deploy/verifier.yaml` — Kubernetes manifest for running the verifier
  in-cluster.

## Quick start

```bash
make install          # creates a venv with dependencies
make demo             # runs the bundled scenarios end-to-end
```

`make demo` runs:

- **scenario-1-fix-works-offline** — hand-crafted good patch
  (`timeout_ms=200`). Verifier applies it, runs both versions, p99
  drops ~86%, bundle = **PASS**. Runs without an API key — good for CI.
- **scenario-1-fix-works** — same alert, but Claude proposes the
  patch. Requires `ANTHROPIC_API_KEY`. Skipped silently if unset.
- **scenario-2-fix-fails** — same alert, hand-crafted bad patch
  (`timeout_ms=5000`, too generous to trip the 1.5s tail). Verifier
  observes p99 unchanged, bundle = **REJECT** with a clean rationale.

Scenario 2 matters more than scenario 1: it's what makes the verifier
credible. "AI suggests, AI verifies" is a closed loop only if the
verifier is willing to reject.

## Run against a real cluster

Without `MIRRORD_TARGET`, the runner executes the patched code
in-process and emits a `LOCAL-ONLY` warning per run. To verify against
a real cluster:

```bash
export MIRRORD_TARGET=deploy/checkout
export MIRRORD_NAMESPACE=staging
make demo
```

You'll need the mirrord operator installed in the cluster. See the
[mirrord operator install docs](https://metalbear.com/mirrord/docs/operator/setup/installation).

## Architecture

| Path | What it is |
|---|---|
| `src/verifier/webhook.py` | FastAPI receiver for Alertmanager (and Datadog) webhooks |
| `src/verifier/orchestrator.py` | Claude wrapper that emits structured patch + hypothesis |
| `src/verifier/engine.py` | Verification orchestrator: apply → run via mirrord → compare → bundle |
| `src/verifier/mirrord_runner.py` | Wraps `mirrord exec`, captures stdout/metrics |
| `src/verifier/proof_bundle.py` | Markdown + JSON evidence emitter |
| `src/verifier/poster.py` | Posts the verdict back to PR / Slack / Datadog |
| `sample-app/` | Toy `checkout`/`pricing` services with planted bugs |
| `walkthroughs/` | Recorded artifacts from the Part II demo runs |

## License

MIT.
