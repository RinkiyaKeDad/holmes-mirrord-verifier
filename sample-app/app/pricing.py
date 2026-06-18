"""Pricing client. HTTP call to `pricing-svc` in the cluster.

Requires `PRICING_URL`. The latency comes from the in-cluster pricing pod that
the verifier steers the candidate's traffic to via mirrord — there is no local
simulator; this demo only runs against a real cluster.
"""

from __future__ import annotations

import os

import httpx


class PricingError(Exception):
    pass


def fetch_price(item_id: str, timeout_ms: int | None = None) -> float:
    """Returns price. Raises PricingError on timeout or downstream error."""
    url = os.environ.get("PRICING_URL")
    if not url:
        raise PricingError(
            "PRICING_URL is not set — this demo runs the candidate against a real "
            "in-cluster pricing service via mirrord, not a local simulator."
        )

    timeout_s = timeout_ms / 1000 if timeout_ms else None
    try:
        r = httpx.get(f"{url.rstrip('/')}/price/{item_id}", timeout=timeout_s)
        r.raise_for_status()
        return float(r.json()["price"])
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        raise PricingError(str(e)) from e
