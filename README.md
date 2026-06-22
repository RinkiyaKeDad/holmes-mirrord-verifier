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

It runs **fully automatically**: a Prometheus alert fires, Alertmanager webhooks
the verifier, and the verifier drives HolmesGPT → bridge → mirrord verification
on its own. You don't run any of the steps by hand — you watch them happen in the
verifier's logs.

---

## How the loop works

```
            ┌─────────────┐   alert fires    ┌──────────────┐
 Prometheus │  checkout   │ ───────────────► │ Alertmanager │
   scrapes  │  service    │                  └──────┬───────┘
            │ (has a bug) │             webhook on  │
            └──────┬──────┘             firing alert ▼
                   │ calls               ┌────────────────────┐
                   ▼                     │      verifier       │
            ┌─────────────┐   1. exec ─► │  (one pod, runs the │
            │   pricing   │ ◄─────────┐  │   whole pipeline)   │
            │ (slow tail) │           │  └─────────┬───────────┘
            └─────────────┘           │            │ 1. exec HolmesGPT CLI
                   ▲                  │            ▼
                   │ 3. mirrord exec  │   ┌────────────────────┐
                   │    baseline vs   │   │     HolmesGPT       │ investigates →
                   │    patched ──────┘   │   (the AI-SRE pod)  │ markdown report
                                          └─────────┬──────────┘
                                                    │ 2. bridge (Claude)
                                                    ▼  report → code patch
                                            PASS  /  REJECT  (in verifier logs)
```

The verifier does three things on every firing alert:

1. **Exec HolmesGPT** in its pod to investigate that specific alert and return a
   markdown root-cause report.
2. **Bridge** the report into a concrete code patch (one Claude call).
3. **Verify** the patch with `mirrord exec`: run the candidate baseline and
   patched, each steered into the live cluster, and compare against the alert's SLO.

The key trick is **mirrord**. The verifier runs the *patched code* as an ordinary
process, but `mirrord exec` gives that process the network identity of the real
`checkout` pod. So when the patched code calls `pricing`, it hits the **real, live
`pricing` service** — same latency, same data — without deploying anything. That's
what makes the "does the fix actually work?" comparison trustworthy.

### The components

| Component | What it is | Why it's here |
|---|---|---|
| `checkout` | Toy Python/Flask service with a planted bug | The service that's "broken" |
| `pricing` | Downstream service `checkout` calls; a fixed subset of items is slow (~1.5s) | The "live cluster state" that makes verification real |
| `loadgen` | Pods that POST to `checkout` continuously | Generates traffic so metrics/alerts are meaningful |
| kube-prometheus-stack | Prometheus + Alertmanager + Grafana | Scrapes metrics, fires SLO alerts, webhooks the verifier |
| HolmesGPT | Open-source AI-SRE, runs as a pod | Investigates alerts, suggests fixes |
| verifier | This repo's engine | Receives the alert, drives Holmes + bridge, runs mirrord, emits the verdict |
| mirrord operator | MetalBear's cluster component | Lets the verifier steer traffic into the live cluster |

### The two scenarios

| Scenario | Planted bug | Alert | What HolmesGPT does | Verdict |
|---|---|---|---|---|
| **Latency** (ships by default) | `checkout` calls `pricing` with no timeout; some pricing calls take ~1.5s | `CheckoutP99High` (p99 ≈ 2s vs 0.3s SLO) | Recommends caching (or a naive timeout) — helps the average, not the worst-case tail | **REJECT** |
| **Error rate** (you plant it) | requests for an `item_id` ending in `-3` raise `ValueError` → 500s | `CheckoutErrorRateHigh` (error rate > 5%) | Correctly diagnoses item-3; the fix clears the alert | **PASS** |

The latency scenario is the one that makes the verifier *credible*: "AI suggests,
AI verifies" is only a closed loop if the verifier is willing to **reject** a
plausible-but-wrong fix.

> **HolmesGPT is non-deterministic.** It's a real LLM agent, so its exact
> recommendation for the latency alert varies between runs (caching, a client-side
> timeout, "add async + a timeout", …). The bridge faithfully implements whichever
> one it gets, and in every case the verifier reaches the same **verdict**
> (REJECT for latency, PASS for error-rate) — but the exact patch and the patched
> metrics can differ run to run. The error-rate scenario is fully deterministic.

---

## Prerequisites

- A Kubernetes cluster with the **mirrord operator** installed
  ([install docs](https://metalbear.com/mirrord/docs/operator/setup/installation)).
  The operator requires a license (free trial, no credit card required).
- `kubectl`, `helm`, and `docker` (with `buildx`) installed locally.
- A container registry you can push to and the cluster can pull from
  (Docker Hub, GHCR, etc.).
- An **Anthropic API key** (used by both HolmesGPT and the bridge).

```bash
cp .env.example .env       # then put your key in ANTHROPIC_API_KEY
```

Every `kubectl`/`helm` command below assumes your kubeconfig is already pointed at
the cluster (e.g. `export KUBECONFIG=/path/to/kubeconfig`).

---

## Step 0 — Confirm the cluster and operator

Check the cluster is reachable and the mirrord operator is healthy. **This is the
one prerequisite the demo can't install for you.**

```bash
kubectl get nodes
kubectl get pods -n mirrord
kubectl get crds | grep mirrord
```

Sample output:

```
NAME                                                  STATUS   ROLES    AGE   VERSION
k3s-mirrord-holmes-282a-0b693b-node-pool-f49b-6zzbo   Ready    <none>   1h    v1.35.0+k3s1
k3s-mirrord-holmes-282a-0b693b-node-pool-f49b-v47k4   Ready    <none>   1h    v1.35.0+k3s1

NAME                                READY   STATUS    RESTARTS   AGE
mirrord-operator-7ff88665cd-frlw2   1/1     Running   0          30m
```

---

## Step 1 — Build and push the images

There are three images: `checkout`, `pricing`, and the `verifier`. Build them for
your registry.

> **Why `--platform linux/amd64`?** The cluster nodes are amd64, and the verifier
> image bakes in the linux/x86_64 mirrord binary. If you build on an Apple-Silicon
> Mac (arm64) without this flag, the verifier pod will crash-loop.

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

> If your registry repos are private, the cluster also needs an `imagePullSecret`.
> Public repos (the default on Docker Hub) need nothing extra.

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
  (`item-3`, `item-6`, `item-9`) is deliberately slow (~1.5s); everything else is
  fast. The latency is a deterministic function of the item_id (not random), which
  is what makes the verifier's verdict reproducible.
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
NAME                        READY   STATUS    RESTARTS   AGE
checkout-65995dd8cd-9h6vt   1/1     Running   0          4m
loadgen-548947b959-4sm6c    1/1     Running   0          4m
loadgen-548947b959-5988v    1/1     Running   0          4m
loadgen-548947b959-cqjpd    1/1     Running   0          4m
pricing-89569688f-6bnhm     1/1     Running   0          4m

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
- **`prometheus-rule.yaml`** defines the two SLO alerts (`CheckoutP99High`,
  `CheckoutErrorRateHigh`). Each alert carries annotations the verifier reads to
  know *what to do*: where the code is (`repo_path`), what to steer mirrord at
  (`verifier_target`, `verifier_namespace`), and the alert's SLO
  (`verifier_slo_signal/operator/threshold`) so the verdict is alert-aware.

Give it ~2–3 minutes (the rules use a 2-minute rate window plus a 30s `for:`),
then confirm the latency alert is firing:

```bash
kubectl -n monitoring port-forward svc/monitoring-kube-prometheus-prometheus 9090:9090 &

# p99 latency — expect ~1.9s, well above the 0.3s SLO:
curl -s 'http://localhost:9090/api/v1/query?query=histogram_quantile(0.99,sum(rate(checkout_request_duration_seconds_bucket%5B2m%5D))by(le))'

# alert state — expect CheckoutP99High = firing:
curl -s 'http://localhost:9090/api/v1/alerts'
```

Sample (trimmed): `p99 ≈ 1.94s`, `CheckoutP99High = firing route=verifier`.

---

## Step 4 — Deploy the verifier

The verifier needs: its image, an Anthropic secret, and RBAC. `deploy/verifier.yaml`
contains the ServiceAccount, RBAC bindings, Deployment, and Service (all in
`verifier-poc`).

```bash
# secret holding the Anthropic key (read from your .env)
KEY=$(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)
kubectl create secret generic verifier-secrets -n verifier-poc \
  --from-literal=anthropic-api-key="$KEY"

kubectl apply -f deploy/verifier.yaml
kubectl get pods -n verifier-poc -l app=verifier
```

What the RBAC and config in that file are for:

- **`mirrord-operator-user` / `-basic` ClusterRoleBindings** — let the verifier ask
  the operator to steer traffic. Without these, `mirrord exec` inside the pod fails.
- **`pods/exec` in the namespaced Role** — lets the verifier exec the HolmesGPT CLI
  in the Holmes pod (step 1 of the pipeline).
- **Env vars** — point the verifier at the Holmes pod (`HOLMES_NAMESPACE`,
  `HOLMES_LABEL_SELECTOR`), the in-cluster Alertmanager (`ALERTMANAGER_URL`), the
  Holmes model (`HOLMES_MODEL`), and force the candidate's pricing calls onto the
  HTTP path (`PRICING_URL`) so latency comes from the real pricing pod.

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

> The verifier finds this pod by label (`app=holmes`) and execs the CLI inside it.
> The Holmes HTTP API (image 0.33.0) only exposes `/api/chat` — it has no
> Alertmanager-investigation endpoint — which is why the verifier execs the CLI
> rather than calling an API.

---

## Step 6 — Wire Alertmanager to the verifier

This is the connection that makes the loop automatic: route firing alerts to the
verifier's webhook.

```bash
helm upgrade monitoring prometheus-community/kube-prometheus-stack \
  --namespace monitoring --reuse-values \
  --values sample-app/k8s/monitoring/alertmanager-values.yaml
```

`alertmanager-values.yaml` adds a receiver that POSTs to
`http://verifier.verifier-poc.svc.cluster.local:8000/webhook/alertmanager`, and a
route that sends only alerts labeled `route: verifier` there (our PrometheusRule
sets that label; everything else drops to a null sink). Confirm it landed:

```bash
kubectl exec -n monitoring alertmanager-monitoring-kube-prometheus-alertmanager-0 \
  -c alertmanager -- cat /etc/alertmanager/config_out/alertmanager.env.yaml \
  | grep -iE 'verifier|webhook|route ='
```

Sample output:

```
- receiver: verifier-webhook
  - route = "verifier"
- name: verifier-webhook
  webhook_configs:
    url: http://verifier.verifier-poc.svc.cluster.local:8000/webhook/alertmanager
```

---

## Step 7 — Watch the latency scenario verify itself (→ REJECT)

Everything is wired. `CheckoutP99High` is already firing, so Alertmanager will
deliver it to the verifier and the pipeline runs on its own. Watch the verifier's
logs:

```bash
kubectl logs -n verifier-poc deploy/verifier -f \
  | grep -E "alertmanager batch|holmes investigate|holmes report|bridging|verification:|Verdict:"
```

> **If nothing arrives within a few minutes:** Alertmanager already sent its one
> notification (e.g. before the verifier was up) and won't resend until
> `repeat_interval` (30m). Force a fresh delivery by restarting Alertmanager — on
> restart it re-notifies active alerts:
> ```bash
> kubectl delete pod -n monitoring alertmanager-monitoring-kube-prometheus-alertmanager-0
> ```

Sample log trace of one full automatic run:

```
verifier.webhook — alertmanager batch: 1 firing alerts
verifier.holmes  — holmes investigate [CheckoutP99High] in pod holmesgpt-holmes-555d45745d-wqjmm
verifier.holmes  — holmes report for CheckoutP99High: 2852 chars
verifier.bridge  — bridging holmes report for alert=032f3340646345bb via claude-sonnet-4-6
verifier.engine  — [baseline] metrics: p50=101ms p99=1633ms err=0%
verifier.engine  — [patched]  metrics: p50=2ms   p99=1636ms err=0%
```

…followed by the verdict:

```
## mirrord verification: REJECT — alert would still be firing

**SLO under verification:** `p99 latency (ms) > 300.0` (alert fires while this holds)

| | Baseline | Patched | SLO threshold | Status |
|---|---:|---:|---:|:---|
| p99 latency (ms) | 1632.5 | 1636.6 | 300.0 | ❌ violates |

**Regression watchlist** (other signals; not what the alert fired on):

| Signal | Baseline | Patched | Change |
|---|---:|---:|---:|
| p50 latency (ms) | 101.8 | 1.7  | -98.4% |
| Error rate       | 0.00% | 0.00% | +0.00 |

**Verdict:** Alert would still be firing post-merge.
SLO `p99_ms > 300.0` (baseline 1632.5, patched 1636.6).
```

**Read the verdict.** This run HolmesGPT recommended caching; the bridge added an
in-memory cache to the pricing client. Caching dropped **p50 by ~98%** (repeat
item_ids are served from memory) — it *looks* like a great fix. But the **p99 is
still ~1.6s**, because the 99th-percentile request is a cache *miss* that still
hits the 1.5s tail. The alert that fired would still be firing. **REJECT** — no
human in the loop.

> If your run shows a different patch or different patched metrics, that's
> expected: HolmesGPT is non-deterministic (see the note above). The verdict stays
> REJECT — none of its latency suggestions actually clear the p99 SLO.

---

## Step 8 — Watch the error-rate scenario verify itself (→ PASS)

This scenario uses a different planted bug, so you plant it and redeploy. Because
the verifier image bakes a copy of `sample-app/`, rebuild **both** the checkout
and verifier images (the bridge's patch "before" must byte-match the source the
verifier carries).

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

3. Watch the verifier logs again. As error rate climbs past 5%,
   `CheckoutErrorRateHigh` fires fresh (a brand-new firing alert, so Alertmanager
   delivers it immediately — no restart needed) and the pipeline runs:

   ```bash
   kubectl logs -n verifier-poc deploy/verifier -f \
     | grep -E "alertmanager batch|holmes investigate|holmes report|bridging|verification:|Verdict:"
   ```

Sample log trace:

```
verifier.webhook — alertmanager batch: 1 firing alerts
verifier.holmes  — holmes investigate [CheckoutErrorRateHigh] in pod holmesgpt-holmes-555d45745d-wqjmm
verifier.holmes  — holmes report for CheckoutErrorRateHigh: 2687 chars
verifier.bridge  — bridging holmes report for alert=4fafebebb792143d via claude-sonnet-4-6
verifier.engine  — patched app/checkout.py
verifier.engine  — [baseline] metrics: p50=94ms p99=1634ms err=10%
verifier.engine  — [patched]  metrics: p50=95ms p99=1633ms err=0%
```

…and the verdict:

```
## mirrord verification: PASS — alert condition no longer satisfied

**SLO under verification:** `error rate > 5.00%` (alert fires while this holds)

| | Baseline | Patched | SLO threshold | Status |
|---|---:|---:|---:|:---|
| Error rate | 10.00% | 0.00% | 5.00% | ✅ satisfies |

**Regression watchlist** (other signals; not what the alert fired on):

| Signal | Baseline | Patched | Change |
|---|---:|---:|---:|
| p99 latency (ms) | 1633.7 | 1633.4 | -0.0% |

**Verdict:** Alert condition no longer satisfied: `error_rate > 0.05`
(baseline 0.100, patched 0.000). Regression watchlist clean.
```

HolmesGPT pulled `ValueError: unsupported catalog shape for item_id=item-3`
straight out of the logs and recommended handling it; the bridge implemented the
fix; the verifier confirmed the error rate dropped **10% → 0%** with p99 unchanged
(no regression). **PASS** — again, no human in the loop.

> After this scenario, revert the item-3 edit in `checkout.py` (and rebuild) if you
> want to return to the latency-only default.

---

## What's happening under the hood

The whole pipeline lives in `src/verifier/` and is driven by the webhook:

| File | Role |
|---|---|
| `webhook.py` | FastAPI receiver. Parses the Alertmanager payload (incl. the SLO + target from rule annotations) and runs the pipeline per firing alert. |
| `holmes_client.py` | Execs `holmes_cli.py investigate alertmanager` in the Holmes pod (via the k8s API) and returns the markdown report. |
| `bridge.py` | One Claude call: Holmes' report + the candidate source → a structured before/after patch. A *faithful translator* — it implements exactly what Holmes recommended, not what it thinks is best. |
| `engine.py` | Copies the source, applies the patch, runs baseline and patched under `mirrord exec`, drives 100 requests each, compares, and emits the verdict. |
| `mirrord_runner.py` | Wraps `mirrord exec --target deploy/checkout`. |

**Why a "faithful" bridge?** If the bridge silently wrote a *correct* fix, the
latency scenario could never REJECT. It implements Holmes' actual suggestion
verbatim, and the verifier shows whether that suggestion clears the alert.

**How the verdict is computed** (`engine.py`), in two parts:

1. **SLO check** — would the alert that fired still be firing on the patched code?
   (e.g. `p99_ms > 300` or `error_rate > 0.05`). If yes → REJECT.
2. **Regression watchlist** — did any *other* signal degrade past tolerance
   (p50 +5%, p99 +10%, error rate +1pp)? If yes → REJECT, even if the alert cleared.

PASS requires both: the alert condition is gone *and* nothing else regressed.

### Driving a step by hand (debugging)

You normally never run the steps manually — the verifier does. But for debugging
you can reproduce any single step. The recorded Part II runs under
`walkthroughs/holmes-*/` include standalone bridge scripts and the captured Holmes
transcripts. For example, to investigate an alert yourself:

```bash
kubectl exec -n verifier-poc deploy/holmesgpt-holmes -- sh -c \
 'cd /app && HOME=/tmp python holmes_cli.py investigate alertmanager \
    --alertmanager-url http://monitoring-kube-prometheus-alertmanager.monitoring:9093 \
    --alertmanager-alertname CheckoutP99High --model "anthropic/claude-sonnet-4-6"'
```

(`HOME=/tmp` works around the Holmes pod's read-only root filesystem; the CLI is
at `/app/holmes_cli.py`, not on `PATH`.)

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
| `sample-app/k8s/monitoring/` | ServiceMonitor, PrometheusRule (SLO alerts), Alertmanager routing |
| `src/verifier/webhook.py` | Alertmanager/Datadog webhook receiver; drives the pipeline |
| `src/verifier/holmes_client.py` | Execs the HolmesGPT CLI in its pod |
| `src/verifier/bridge.py` | Translates a Holmes report into a code patch (one Claude call) |
| `src/verifier/engine.py` | Apply patch → run baseline/patched via mirrord → compare → verdict |
| `src/verifier/mirrord_runner.py` | Wraps `mirrord exec` |
| `src/verifier/proof_bundle.py` | Markdown + JSON evidence emitter |
| `deploy/verifier.yaml` | Verifier RBAC + Deployment + Service |
| `walkthroughs/` | Recorded artifacts + standalone bridge scripts from the Part II runs |

## License

MIT.
