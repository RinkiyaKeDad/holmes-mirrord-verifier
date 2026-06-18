"""Bridge HolmesGPT v3 (latency alert) into a code patch with p99-ms SLO."""
import json, os, re, sys
from pathlib import Path
from anthropic import Anthropic

REPO = Path(__file__).resolve().parents[2] / "sample-app"
HOLMES_OUT = Path("/tmp/holmes-out-v3.txt")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/holmes-bridged-scenario-v3.json")


def read_holmes_analysis() -> str:
    txt = HOLMES_OUT.read_text()
    idx = txt.find("Root Cause Analysis")
    if idx == -1:
        idx = txt.find("Analysis")
    return txt[idx : idx + 4000] if idx != -1 else txt[-4000:]


def read_source() -> str:
    chunks = []
    for f in sorted(REPO.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        rel = f.relative_to(REPO)
        chunks.append(f"\n--- {rel} ---\n{f.read_text()}\n")
    return "".join(chunks)


SYSTEM = """You are a faithful translator. Convert HolmesGPT's prose recommendation
into a concrete code patch the verification pipeline can apply and run.

HolmesGPT investigated a p99 latency alert and recommended (in order):
1. Implement caching for pricing data
2. Add async processing for non-critical pricing lookups
3. Monitor pricing service performance and scaling
4. Consider request batching to pricing service

Implement the FIRST code-shaped recommendation: add an in-memory TTL cache
around the pricing client so repeated calls for the same item_id return cached
values. Implement caching in pricing.py.

You MUST NOT:
- Add a timeout to fetch_price (HolmesGPT did not recommend this).
- Add error handling, fallbacks, or default prices (not recommended).
- Combine caching with any other optimization HolmesGPT didn't suggest.
- "Course-correct" HolmesGPT's recommendation. Translate, don't improve.

Output ONLY a JSON object with this schema:
{
  "summary": "<one-line>",
  "hypothesis": "<HolmesGPT's stated reasoning, paraphrased>",
  "repro_steps": ["<step 1>", "<step 2>"],
  "files": [{"path": "<relative path>", "before": "<EXACT original full file>", "after": "<full patched file>"}],
  "confidence": 0.0-1.0,
  "expected_signal_change": "<what HolmesGPT expected to happen>",
  "model": "claude-bridge-from-holmes"
}

Rules:
- 'before' must be the EXACT full original file content (every byte, top to bottom).
- 'after' must be the EXACT full patched file content (every byte, top to bottom).
- Patches must be syntactically valid Python.
- No commentary outside the JSON.
"""


def main():
    analysis = read_holmes_analysis()
    source = read_source()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM,
        messages=[{"role": "user", "content":
            f"# HolmesGPT investigation\n{analysis}\n\n"
            f"# Candidate source\n{source}\n\n"
            f"Respond with the JSON patch."
        }],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    blob = fence.group(1) if fence else text[text.find("{") : text.rfind("}") + 1]
    patch = json.loads(blob)

    alert = {
        "event_id": "holmes-bridged-v3-001",
        "alert_title": "[holmes-bridged] checkout p99 latency above SLO",
        "title": "[holmes-bridged] checkout p99 latency above SLO",
        "event_msg": "HolmesGPT investigated CheckoutP99High and recommended caching.",
        "body": analysis,
        "service": "checkout",
        "metric": "checkout_request_duration_seconds_p99",
        "alert_threshold": 0.3,
        "last_value": 1.86,
        "alert_transition": "error",
        "tags": {"service": "checkout", "env": "staging", "team": "payments"},
    }
    scenario = {
        "name": "holmes-bridged-latency",
        "description": "Real HolmesGPT recommendation for CheckoutP99High (caching), bridged through Claude.",
        "repo_path": "sample-app",
        "use_claude": False,
        "alert": alert,
        "alert_slo": {
            # CheckoutP99High fires when p99 > 0.3s (300ms).
            "signal": "p99_ms",
            "operator": ">",
            "threshold": 300.0,
        },
        "prepared_patch": patch,
    }
    OUT.write_text(json.dumps(scenario, indent=2))
    print(f"wrote {OUT}")
    print(f"\npatch summary: {patch['summary']}")
    print(f"files: {[f['path'] for f in patch['files']]}")
    print(f"expected: {patch['expected_signal_change']}")


if __name__ == "__main__":
    main()
