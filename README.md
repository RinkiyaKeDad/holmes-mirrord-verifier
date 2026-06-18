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

> **This demo runs only against a real Kubernetes cluster.** It steers the
> candidate's traffic into the live cluster with mirrord — there is no local /
> simulated mode. You need a cluster with the mirrord operator installed.

## What's in the repo

- `sample-app/` — toy `checkout` + `pricing` services with two planted
  bugs (error-rate, p99 latency), plus their Kubernetes manifests under
  `sample-app/k8s/` (checkout, pricing, loadgen) and the monitoring rules
  under `sample-app/k8s/monitoring/` (ServiceMonitor + PrometheusRule).
- `src/verifier/` — the verifier: receives Alertmanager webhooks, calls
  a bridge wrapper to turn an AI-SRE recommendation into a code patch,
  runs the patch under `mirrord exec`, and emits a verdict.
- `walkthroughs/holmes-error-rate/`, `walkthroughs/holmes-latency/` —
  the two demo runs from Part II, with the actual HolmesGPT
  transcripts, the bridged patches, and the bridge scripts.
- `deploy/verifier.yaml` — Kubernetes manifest (RBAC + Deployment) for
  running the verifier in-cluster.

## Prerequisites

- A Kubernetes cluster with the **mirrord operator** installed
  ([install docs](https://metalbear.com/mirrord/docs/operator/setup/installation)).
- `kubectl`, `helm`, `docker` (with `buildx`), and `uv` locally.
- An **Anthropic API key** (used by both HolmesGPT and the bridge). Copy
  `.env.example` to `.env` and fill it in.

## Running the demo

The loop: **HolmesGPT investigates an alert → a small Claude "bridge" turns the
report into a code patch → the verifier runs baseline vs. patched under
`mirrord exec` against the live cluster → PASS/REJECT.**

1. **Build & push the images** (`checkout`, `pricing`, and the verifier) to a
   registry your cluster can pull from, for `linux/amd64`. The verifier image
   bakes in the `linux/x86_64` mirrord binary, so it must be built for amd64.
2. **Deploy the workload + monitoring** into the `verifier-poc` namespace:
   ```bash
   kubectl create namespace verifier-poc
   kubectl apply -f sample-app/k8s/pricing.yaml \
                 -f sample-app/k8s/deployment.yaml \
                 -f sample-app/k8s/loadgen.yaml
   helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
     --namespace monitoring --create-namespace \
     --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
     --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false
   kubectl apply -f sample-app/k8s/monitoring/servicemonitor.yaml \
                 -f sample-app/k8s/monitoring/prometheus-rule.yaml
   ```
3. **Deploy the verifier** (needs an `anthropic-api-key` secret named
   `verifier-secrets`):
   ```bash
   kubectl apply -f deploy/verifier.yaml
   ```
4. **Install HolmesGPT** in-cluster (the AI-SRE), configured for Anthropic.
5. **Run a scenario**: let an alert fire, have HolmesGPT investigate it, run the
   bridge (`walkthroughs/holmes-*/bridge_holmes.py`) to produce a patch, then run
   the verifier against the live cluster:
   ```bash
   export MIRRORD_TARGET=deploy/checkout
   export MIRRORD_NAMESPACE=verifier-poc
   python -m verifier.cli run <bridged-scenario>.json --repo-root . \
     --bundle-out <out>.json
   ```

`make install` creates a local venv — needed only to run the bridge step
(a Claude call); the verifier itself runs in-cluster.

## Architecture

| Path | What it is |
|---|---|
| `src/verifier/webhook.py` | FastAPI receiver for Alertmanager (and Datadog) webhooks |
| `src/verifier/orchestrator.py` | Claude wrapper that emits structured patch + hypothesis |
| `src/verifier/engine.py` | Verification orchestrator: apply → run via mirrord → compare → bundle |
| `src/verifier/mirrord_runner.py` | Wraps `mirrord exec`, captures stdout/metrics |
| `src/verifier/proof_bundle.py` | Markdown + JSON evidence emitter |
| `src/verifier/poster.py` | Posts the verdict back to PR / Slack / Datadog |
| `sample-app/` | Toy `checkout`/`pricing` services with planted bugs + k8s manifests |
| `walkthroughs/` | Recorded artifacts from the Part II demo runs |

## License

MIT.
