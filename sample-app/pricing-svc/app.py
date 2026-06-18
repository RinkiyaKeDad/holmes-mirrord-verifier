"""pricing-svc: the downstream service that checkout calls.

A fixed subset of items has a heavy tail (~1.5s); every other item is fast.
The tail is keyed on the item_id (not random), so the latency a caller sees is
fully deterministic and reproducible. That determinism is what lets the
verifier compare baseline vs. patched runs and reach the same verdict every
time, instead of depending on which calls happened to hit a random tail.

This is the "live cluster state" the verifier targets: mirrord steers
checkout's HTTP call here, and the verifier classifies a checkout patch by
whether it survives this latency profile, not by what's in the code.
"""

from __future__ import annotations

import time

from flask import Flask, jsonify

app = Flask(__name__)

# Items whose pricing call is slow (~1.5s), keyed on the numeric suffix of the
# item_id. With the loadgen's item-0..item-9 rotation, exactly these three of
# ten items are slow. A client-side cache only hides *repeat* calls, so the p99
# (worst ~1%) still lands on a slow first-touch — which is why caching the
# pricing client does NOT clear the p99 latency SLO (Scenario 2 -> REJECT).
SLOW_SUFFIXES = {3, 6, 9}
BASE_LATENCY_S = 0.03
TAIL_LATENCY_S = 1.5


def _is_slow(item_id: str) -> bool:
    suffix = item_id.rsplit("-", 1)[-1]
    return suffix.isdigit() and int(suffix) % 10 in SLOW_SUFFIXES


@app.route("/price/<item_id>")
def price(item_id: str):
    time.sleep(BASE_LATENCY_S + (TAIL_LATENCY_S if _is_slow(item_id) else 0.0))
    return jsonify({"price": 9.99 + len(item_id) % 100 / 10})


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    # threaded=True so concurrent callers (loadgen + the verifier's candidate)
    # don't queue behind one another and add nondeterministic latency.
    app.run(host="0.0.0.0", port=8080, threaded=True)
