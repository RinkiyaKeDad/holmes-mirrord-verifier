# holmes-mirrord-verifier

The reference implementation that backs **Part II** of MetalBear's AI-SRE
verification post:

- Part I: *Auto-verifying your AI-SRE's fixes against your real cluster, with mirrord* (link TBD)
- Part II: *HolmesGPT, end-to-end on a real cluster: what passed, what didn't* (link TBD)

**The thesis.** AI-SREs (HolmesGPT, Resolve AI, incident.io's Investigator,
NeuBird, Datadog Bits, etc.) can suggest fixes. None of them can *prove* the fix
actually clears the alert before a human reviews it. This repo plugs
`mirrord exec` in as the proving step: take the AI-SRE's recommendation, run the
patched code against the **real cluster** (no deploy), and emit a PASS/REJECT
verdict tied to the alert's actual SLO.

> **This demo only runs against a real Kubernetes cluster.** It steers the
> candidate's traffic into the live cluster with mirrord — there is no local or
> simulated mode. You need a cluster with the mirrord operator installed.

---

## How the loop works

```
            ┌─────────────┐   alert fires    ┌──────────────┐
 Prometheus │  checkout   │ ───────────────► │ Alertmanager │
   scrapes  │  service    │                  └──────┬───────┘
            │ (has a bug) │                         │ investigates
            └──────┬──────┘                         ▼
                   │ calls               ┌────────────────────┐
                   ▼                     │     HolmesGPT       │  "root cause +
            ┌─────────────┐              │   (the AI-SRE)      │   a recommendation"
            │   pricing   │              └──────────┬──────────┘   (markdown report)
            │ (slow tail) │                         │
            └─────────────┘                         ▼
                                         ┌────────────────────┐
                                         │   bridge (Claude)   │  turns the prose
                                         │   ~80 lines         │  report into a patch
                                         └──────────┬──────────┘
                                                    │ patch
                                                    ▼
                                         ┌────────────────────┐
                                         │     verifier        │  baseline vs. patched
                                         │   (mirrord exec)    │  against the REAL cluster
                                         └──────────┬──────────┘
                                                    ▼
                                            PASS  /  REJECT
```

The key trick is **mirrord**. The verifier runs the *patched code* as an
ordinary process, but `mirrord exec` gives that process the network identity of
the real `checkout` pod. So when the patched code calls `pricing`, it hits the
**real, live `pricing` service** — same latency, same data — without deploying
anything. That's what makes the "does the fix actually work?" comparison
trustworthy.

### The components

| Component | What it is | Why it's here |
|---|---|---|
| `checkout` | Toy Python/Flask service with a planted bug | The service that's "broken" |
| `pricing` | Downstream service `checkout` calls; a fixed subset of items is slow (~1.5s) | The "live cluster state" that makes verification real |
| `loadgen` | Pods that POST to `checkout` continuously | Generates traffic so metrics/alerts are meaningful |
| kube-prometheus-stack | Prometheus + Alertmanager + Grafana | Scrapes metrics, fires SLO alerts |
| HolmesGPT | Open-source AI-SRE, runs as a pod | Investigates alerts, suggests fixes |
| verifier | This repo's engine | Bridges the report to a patch and runs it under mirrord |
| mirrord operator | MetalBear's cluster component | Lets the verifier steer traffic into the live cluster |

### The two scenarios

| Scenario | Planted bug | Alert | What the AI-SRE does | Verdict |
|---|---|---|---|---|
| **2 — latency** (ships by default) | `checkout` calls `pricing` with no timeout, and some pricing calls take ~1.5s | `CheckoutP99High` (p99 ≈ 2s vs 0.3s SLO) | Recommends caching — improves the average but not the worst-case tail | **REJECT** |
| **1 — error rate** (you plant it) | requests for an `item_id` ending in `-3` raise `ValueError` → 500s | `CheckoutErrorRateHigh` (error rate > 5%) | Correctly diagnoses item-3; the fix clears the alert | **PASS** |

Scenario 2 is the one that makes the verifier *credible*: "AI suggests, AI
verifies" is only a closed loop if the verifier is willing to **reject** a
plausible-but-wrong fix.

---

## Prerequisites

- A Kubernetes cluster with the **mirrord operator** installed
  ([install docs](https://metalbear.com/mirrord/docs/operator/setup/installation)).
  The operator requires a license (MetalBear employees / trial users have one).
- `kubectl`, `helm`, `docker` (with `buildx`), and `uv` installed locally.
- A container registry you can push to and the cluster can pull from
  (Docker Hub, GHCR, etc.).
- An **Anthropic API key** (used by both HolmesGPT and the bridge).

```bash
cp .env.example .env       # then put your key in ANTHROPIC_API_KEY
```

Every `kubectl`/`helm` command below assumes your kubeconfig is already
pointed at the cluster (e.g. `export KUBECONFIG=/path/to/kubeconfig`).

---

## Step 0 — Confirm the cluster and operator

Check the cluster is reachable and the mirrord operator is healthy. **This is
the one prerequisite the demo can't install for you.**

```bash
kubectl get nodes
kubectl get pods -n mirrord
kubectl get crds | grep mirrord
```

Sample output:

```
NAME                                            STATUS   ROLES    AGE     VERSION
k3s-homesgpt-9e4b-7b72b3-node-pool-6188-gi6lm   Ready    <none>   3h30m   v1.35.0+k3s1
k3s-homesgpt-9e4b-7b72b3-node-pool-6188-mszry   Ready    <none>   3h30m   v1.35.0+k3s1

NAME                                READY   STATUS    RESTARTS   AGE
mirrord-operator-7ff88665cd-frlw2   1/1     Running   0          166m
```

---

## Step 1 — Build and push the images

There are three images: `checkout`, `pricing`, and the `verifier`. Build them
for your registry.

> **Why `--platform linux/amd64`?** The cluster nodes are amd64, and the
> verifier image bakes in the linux/x86_64 mirrord binary. If you build on an
> Apple-Silicon Mac (arm64) without this flag, the verifier pod will crash-loop.

```bash
export REGISTRY=docker.io/<your-username>   # a registry you can push to

docker buildx build --platform linux/amd64 --provenance=false \
  -t $REGISTRY/mirrord-sre-verifier-pricing:dev --push sample-app/pricing-svc
docker buildx build --platform linux/amd64 --provenance=false \
  -t $REGISTRY/mirrord-sre-verifier-sample:dev  --push sample-app
docker buildx build --platform linux/amd64 --provenance=false \
  -t $REGISTRY/mirrord-sre-verifier:dev         --push .
```

The committed manifests point at a sample registry. Repoint them at yours:

```bash
grep -rl 'docker.io/rinkiyakedad' sample-app/k8s deploy \
  | xargs perl -pi -e "s#docker.io/rinkiyakedad#${REGISTRY}#g"
```

> If your registry repos are private, the cluster also needs an
> `imagePullSecret`. Public repos (the default on Docker Hub) need nothing extra.

---

## Step 2 — Deploy the workload

This creates the `verifier-poc` namespace and deploys the three app components.
All manifests are already namespaced to `verifier-poc`.

```bash
kubectl create namespace verifier-poc

kubectl apply -f sample-app/k8s/pricing.yaml \
              -f sample-app/k8s/deployment.yaml \
              -f sample-app/k8s/loadgen.yaml
```

- **`pricing`** is the downstream dependency. A fixed subset of item_ids
  (`item-3`, `item-6`, `item-9`) is deliberately slow (~1.5s); everything else
  is fast. The latency is a deterministic function of the item_id (not random),
  which is what makes the verifier's verdict reproducible.
- **`checkout`** calls `pricing` on every request. As shipped it has the
  **latency bug**: no client-side timeout, so it inherits pricing's slow tail.
- **`loadgen`** (3 replicas) POSTs to `checkout` continuously, rotating
  `item-0`..`item-9`, so ~30% of requests hit a slow pricing call.

Confirm everything is running and traffic is flowing:

```bash
kubectl get pods -n verifier-poc
kubectl logs -n verifier-poc deploy/checkout --tail=4
```

Sample output:

```
NAME                                READY   STATUS    RESTARTS   AGE
checkout-5dd5495b7c-rfnkk           1/1     Running   0          12m
loadgen-548947b959-cpdnn            1/1     Running   0          122m
loadgen-548947b959-gjk92            1/1     Running   0          122m
loadgen-548947b959-qfkf5            1/1     Running   0          122m
pricing-5b7fb56968-kp9vk            1/1     Running   0          12m

... httpx — HTTP Request: GET http://pricing/price/item-7 "HTTP/1.1 200 OK"
... werkzeug — 10.0.1.205 - - "POST /checkout HTTP/1.1" 200 -
```

You can confirm the deterministic latency directly from any pod with `curl`:

```bash
# item-3 is always slow, item-1 is always fast — every time:
kubectl exec -n verifier-poc deploy/loadgen -- \
  curl -s -o /dev/null -w '%{time_total}s\n' http://pricing/price/item-3   # ~1.53s
kubectl exec -n verifier-poc deploy/loadgen -- \
  curl -s -o /dev/null -w '%{time_total}s\n' http://pricing/price/item-1   # ~0.03s
```

---

## Step 3 — Install Prometheus + Alertmanager and the SLO rules

Install kube-prometheus-stack. The two `...NilUsesHelmValues=false` flags tell
Prometheus to discover *our* ServiceMonitor/PrometheusRule regardless of labels.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update prometheus-community

helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false

kubectl apply -f sample-app/k8s/monitoring/servicemonitor.yaml \
              -f sample-app/k8s/monitoring/prometheus-rule.yaml
```

- **`servicemonitor.yaml`** tells Prometheus to scrape `checkout` at `/metrics`.
- **`prometheus-rule.yaml`** defines the two SLO alerts: `CheckoutP99High`
  (p99 > 0.3s) and `CheckoutErrorRateHigh` (error rate > 5%).

Give it ~2–3 minutes (the rules use a 2-minute rate window plus a 30s `for:`),
then confirm the latency alert is firing:

```bash
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090 &

# p99 latency — expect ~1.9s, well above the 0.3s SLO:
curl -s 'http://localhost:9090/api/v1/query?query=histogram_quantile(0.99,sum(rate(checkout_request_duration_seconds_bucket%5B2m%5D))by(le))'

# alert state — expect CheckoutP99High = firing:
curl -s 'http://localhost:9090/api/v1/alerts'
```

Sample (trimmed): `p99 ≈ 1.94s`, `CheckoutP99High = firing`.

---

## Step 4 — Deploy the verifier

The verifier needs three things: its image, an Anthropic secret, and RBAC that
lets it talk to the mirrord operator. `deploy/verifier.yaml` contains the
ServiceAccount, RBAC bindings, Deployment, and Service (all in `verifier-poc`).

```bash
# secret holding the Anthropic key (read from your .env)
KEY=$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)
kubectl create secret generic verifier-secrets -n verifier-poc \
  --from-literal=anthropic-api-key="$KEY"

kubectl apply -f deploy/verifier.yaml
kubectl get pods -n verifier-poc -l app=verifier
```

The RBAC in that file (the `mirrord-operator-user` and `-basic` cluster-role
bindings) is what lets the verifier ask the operator to steer traffic. Without
it, `mirrord exec` inside the pod would fail.

---

## Step 5 — Install HolmesGPT (the AI-SRE)

Following the
[official Helm install](https://holmesgpt.dev/latest/installation/kubernetes-installation/?tab=anthropic),
configured for Anthropic.

```bash
KEY=$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)
kubectl create secret generic holmes-secrets -n verifier-poc \
  --from-literal=anthropic-api-key="$KEY"

helm repo add robusta https://robusta-charts.storage.googleapis.com
helm repo update robusta

# values.yaml: wire the API key from the secret and register a model.
cat > /tmp/holmes-values.yaml <<'YAML'
additionalEnvVars:
  - name: ANTHROPIC_API_KEY
    valueFrom:
      secretKeyRef:
        name: holmes-secrets
        key: anthropic-api-key
modelList:
  claude-sonnet:
    api_key: "{{ env.ANTHROPIC_API_KEY }}"
    model: anthropic/claude-sonnet-4-6
    temperature: 0
YAML

helm upgrade --install holmesgpt robusta/holmes -n verifier-poc -f /tmp/holmes-values.yaml
kubectl get pods -n verifier-poc | grep holmes   # wait for 1/1 Running
```

Finally, set up the local virtualenv — needed only to run the **bridge** step (a
small Claude call); the verifier itself runs in-cluster:

```bash
make install
```

---

## Step 6 — Run Scenario 2 (latency → REJECT)

This is the scenario that ships by default. The loop is: **Holmes investigates →
bridge turns the report into a patch → verifier runs it under mirrord.**

### 6a. HolmesGPT investigates the alert

```bash
kubectl exec -n verifier-poc deploy/holmesgpt-holmes -- sh -c \
 'cd /app && HOME=/tmp python holmes_cli.py investigate alertmanager \
    --alertmanager-url http://monitoring-kube-prometheus-alertmanager.monitoring:9093 \
    --alertmanager-alertname CheckoutP99High \
    --model "anthropic/claude-sonnet-4-6"' \
 > /tmp/holmes-out-v3.txt
```

What this command does, piece by piece:

- `kubectl exec -n verifier-poc deploy/holmesgpt-holmes -- …` — run a command
  *inside* the already-running HolmesGPT pod (kubectl resolves the Deployment to
  one of its pods). Everything after `--` is the command for the pod, not flags
  for kubectl.
- `sh -c '…'` — run the quoted string as a shell script in the pod (we need a
  shell because it uses `cd`, an env var, and `&&`).
- `cd /app && HOME=/tmp python holmes_cli.py …` — the CLI lives at
  `/app/holmes_cli.py` (it isn't on `PATH`). `HOME=/tmp` works around the pod's
  **read-only root filesystem**: Holmes writes a cache to `~/.holmes`, so we
  point `HOME` at a writable dir. Without it you get
  `Read-only file system: '/root/.holmes'`.
- `investigate alertmanager` — Holmes' mode for investigating a Prometheus/
  Alertmanager alert. It then runs its own agent loop (kubectl, pod logs,
  configs) and prints a markdown root-cause report.
- `--alertmanager-url http://monitoring-kube-prometheus-alertmanager.monitoring:9093`
  — the in-cluster DNS name of the Alertmanager Service (in the `monitoring`
  namespace, port 9093) to pull the alert from.
- `--alertmanager-alertname CheckoutP99High` — investigate only this alert.
- `--model "anthropic/claude-sonnet-4-6"` — the LLM Holmes reasons with (the
  Anthropic key is already an env var in the pod, set by the Helm chart).
- `> /tmp/holmes-out-v3.txt` — this redirect is *outside* the quotes, so it runs
  on **your** machine: kubectl streams the pod's stdout back, and `>` saves it to
  a local file that the bridge step reads next.

Holmes runs an agentic loop (kubectl, logs, config) for ~30–60s and produces a
markdown report. Sample (trimmed):

```
Root Cause: synchronous pricing calls with no caching; the pricing service has
a long tail (~1.5s) and checkout inherits it. p99 ≈ 1.9s vs the 0.3s SLO.

Recommendations:
  1. Implement caching for pricing data
  2. Add async processing for pricing lookups
  3. Consider request batching to the pricing service
```

Caching is a reasonable optimization — the question is whether it clears *this*
alert. That's what the verifier answers.

### 6b. Bridge the report into a code patch

The bridge is ~80 lines around the Anthropic SDK. It reads Holmes' report plus
the service source and emits a structured patch that *faithfully implements*
Holmes' top recommendation (here: an in-memory cache). It runs locally:

```bash
set -a; . ./.env; set +a    # export ANTHROPIC_API_KEY
uv run python walkthroughs/holmes-latency/bridge_holmes.py /tmp/holmes-bridged-latency.json
```

Sample output:

```
wrote /tmp/holmes-bridged-latency.json
patch summary: Add in-memory TTL cache to fetch_price to avoid redundant pricing service calls
files: ['app/pricing.py']
expected: Repeated requests for the same item_id are served from cache, reducing p99 ...
```

### 6c. Verify the patch against the live cluster

Copy the patch scenario into the verifier pod and run it. The verifier copies
the candidate source, applies the patch, then runs **baseline** and **patched**
each as a server under `mirrord exec --target deploy/checkout`, drives 100
requests at each, and compares.

```bash
POD=$(kubectl get pod -n verifier-poc -l app=verifier -o jsonpath='{.items[0].metadata.name}')
kubectl cp /tmp/holmes-bridged-latency.json verifier-poc/$POD:/tmp/scenario.json

kubectl exec -n verifier-poc $POD -- sh -c \
  'MIRRORD_TARGET=deploy/checkout MIRRORD_NAMESPACE=verifier-poc \
   python -m verifier.cli run /tmp/scenario.json --repo-root /scaffold'
```

Sample output:

```
## mirrord verification: REJECT — alert would still be firing

**SLO under verification:** `p99 latency (ms) > 300.0` (alert fires while this holds)

| | Baseline | Patched | SLO threshold | Status |
|---|---:|---:|---:|:---|
| p99 latency (ms) | 1636.4 | 1594.7 | 300.0 | ❌ violates |

**Regression watchlist** (other signals):

| Signal | Baseline | Patched | Change |
|---|---:|---:|---:|
| p50 latency (ms) | 132.5 | 1.7 | -98.7% |

**Verdict:** Alert would still be firing post-merge.
SLO `p99_ms > 300.0` (baseline 1636.4, patched 1594.7).
```

**Read the verdict.** Caching dropped **p50 by ~99%** (repeat item_ids are served
from memory) — it *looks* like a great fix. But the **p99 is still ~1.6s**,
because the 99th-percentile request is a cache *miss* that still hits the 1.5s
tail. The alert that fired would still be firing. **REJECT.**

This is deterministic: run the last command again and you get REJECT with the
same ~1595ms patched p99 every time. (Pricing's slow set is fixed, so the
measurement no longer depends on which calls randomly hit a tail.)

---

## Step 7 — Run Scenario 1 (error rate → PASS)

Scenario 1 uses a different planted bug, so you plant it and redeploy. Because
the verifier image bakes a copy of `sample-app/`, you rebuild **both** the
checkout and verifier images (the patch's "before" snippet must byte-match the
source the verifier sees).

1. Add the item-3 bug at the top of `checkout()` in `sample-app/app/checkout.py`:

   ```python
   if item_id.endswith("-3"):
       raise ValueError(f"unsupported catalog shape for item_id={item_id}")
   ```

2. Rebuild both images and roll the deployments:

   ```bash
   docker buildx build --platform linux/amd64 --provenance=false \
     -t $REGISTRY/mirrord-sre-verifier-sample:dev --push sample-app
   docker buildx build --platform linux/amd64 --provenance=false \
     -t $REGISTRY/mirrord-sre-verifier:dev       --push .
   kubectl rollout restart deployment/checkout deployment/verifier -n verifier-poc
   ```

3. Wait for `CheckoutErrorRateHigh` to fire (~2–3 min; error rate climbs to
   ~10%), then run the same loop as Step 6, but with the error-rate alert and
   bridge:

   ```bash
   kubectl exec -n verifier-poc deploy/holmesgpt-holmes -- sh -c \
    'cd /app && HOME=/tmp python holmes_cli.py investigate alertmanager \
       --alertmanager-url http://monitoring-kube-prometheus-alertmanager.monitoring:9093 \
       --alertmanager-alertname CheckoutErrorRateHigh --model "anthropic/claude-sonnet-4-6"' \
    > /tmp/holmes-out-v2.txt

   set -a; . ./.env; set +a
   uv run python walkthroughs/holmes-error-rate/bridge_holmes.py /tmp/holmes-bridged-error.json

   POD=$(kubectl get pod -n verifier-poc -l app=verifier -o jsonpath='{.items[0].metadata.name}')
   kubectl cp /tmp/holmes-bridged-error.json verifier-poc/$POD:/tmp/scenario.json
   kubectl exec -n verifier-poc $POD -- sh -c \
     'MIRRORD_TARGET=deploy/checkout MIRRORD_NAMESPACE=verifier-poc \
      python -m verifier.cli run /tmp/scenario.json --repo-root /scaffold'
   ```

Holmes pulls the exception straight out of the logs:

```
Root Cause: Application Bug — Unsupported Catalog Shape for item-3
Every POST /checkout request for item_id=item-3 fails with HTTP 500
(ValueError: unsupported catalog shape for item_id=item-3). Error rate ~10%.
Fix: handle item-3 in checkout.py.
```

And the verifier signs off on the fix:

```
## mirrord verification: PASS — alert condition no longer satisfied

**SLO under verification:** `error rate > 5.00%`

| | Baseline | Patched | SLO threshold | Status |
|---|---:|---:|---:|:---|
| Error rate | 10.00% | 0.00% | 5.00% | ✅ satisfies |

**Verdict:** Alert condition no longer satisfied: `error_rate > 0.05`
(baseline 0.100, patched 0.000). Regression watchlist clean.
```

The fix dropped the error rate **10% → 0%** with no regression on latency. **PASS.**

---

## How the verdict is computed

For each run the verifier measures p50, p99, and error rate, then classifies in
two parts (`src/verifier/engine.py`):

1. **SLO check** — would the alert that fired still be firing on the patched
   code? (e.g. `p99_ms > 300` or `error_rate > 0.05`). If yes → REJECT.
2. **Regression watchlist** — did any *other* signal degrade past tolerance
   (p50 +5%, p99 +10%, error rate +1pp)? If yes → REJECT, even if the alert
   cleared.

PASS requires both: the alert condition is gone *and* nothing else regressed.

---

## Cleanup

```bash
helm uninstall holmesgpt -n verifier-poc
helm uninstall monitoring -n monitoring
kubectl delete namespace verifier-poc monitoring
# the mirrord operator and the cluster itself are left intact
```

---

## Repo map

| Path | What it is |
|---|---|
| `sample-app/` | Toy `checkout`/`pricing` services with planted bugs |
| `sample-app/k8s/` | Deployments + Services for checkout, pricing, loadgen |
| `sample-app/k8s/monitoring/` | ServiceMonitor + PrometheusRule (the SLO alerts) |
| `src/verifier/engine.py` | Apply patch → run baseline/patched via mirrord → compare → bundle |
| `src/verifier/mirrord_runner.py` | Wraps `mirrord exec` |
| `src/verifier/webhook.py` | FastAPI receiver for Alertmanager/Datadog webhooks |
| `src/verifier/orchestrator.py` | Claude wrapper that emits a structured patch |
| `src/verifier/proof_bundle.py` | Markdown + JSON evidence emitter |
| `deploy/verifier.yaml` | Verifier RBAC + Deployment + Service |
| `walkthroughs/` | Recorded artifacts + the bridge scripts from the Part II runs |

## License

MIT.
