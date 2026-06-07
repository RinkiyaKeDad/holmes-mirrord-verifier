"""Bridge HolmesGPT v2 recommendation (handle item-3) into a code patch."""
import json, os, re, sys
from pathlib import Path
from anthropic import Anthropic

REPO = Path(__file__).resolve().parents[2] / "sample-app"
HOLMES_OUT = Path("/tmp/holmes-out-v2.txt")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/holmes-bridged-scenario-v2.json")


def read_holmes_analysis() -> str:
    txt = HOLMES_OUT.read_text()
    idx = txt.find("Analysis:")
    if idx == -1:
        idx = txt.find("Root Cause")
    return txt[idx : idx + 4000] if idx != -1 else txt[-4000:]


def read_source() -> str:
    chunks = []
    for f in sorted(REPO.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        rel = f.relative_to(REPO)
        chunks.append(f"\n--- {rel} ---\n{f.read_text()}\n")
    return "".join(chunks)


SYSTEM = """You are a faithful translator. Your job is to convert an AI-SRE tool's
prose recommendation into a concrete code patch — implementing exactly what
the AI-SRE recommended, not what you think the right fix should be.

You will receive HolmesGPT's investigation output and the candidate service's
source. HolmesGPT diagnosed a `ValueError: unsupported catalog shape for
item_id=item-3` and recommended "fix application code to handle item-3
catalog shape or add proper validation."

Implement HolmesGPT's recommendation as a minimal code change in
`app/checkout.py`. The fix should make checkout return a valid response for
item-3 rather than raising. Use the simplest reasonable defensive handling
HolmesGPT's words allow (return a price of 0, or skip the pricing call and
return a default). Do not add unrelated improvements.

Output ONLY a JSON object with this exact schema:
{
  "summary": "<one-line>",
  "hypothesis": "<paraphrase HolmesGPT's diagnosis>",
  "repro_steps": ["<step 1>", "<step 2>"],
  "files": [{"path": "<relative path>", "before": "<EXACT original>", "after": "<patched>"}],
  "confidence": 0.0-1.0,
  "expected_signal_change": "<expected metric movement>",
  "model": "claude-bridge-from-holmes"
}

Rules:
- 'before' must contain the FULL ORIGINAL FILE CONTENT (every byte, top to bottom — module docstring, imports, every function, every comment). Not a snippet.
- 'after' must contain the FULL PATCHED FILE CONTENT (every byte, top to bottom). Not a diff, not a snippet.
- Patches must be syntactically valid Python.
- No commentary outside the JSON.
"""


def main():
    analysis = read_holmes_analysis()
    source = read_source()
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
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
        "event_id": "holmes-bridged-v2-001",
        "alert_title": "[holmes-bridged] checkout 5xx rate above SLO",
        "title": "[holmes-bridged] checkout 5xx rate above SLO",
        "event_msg": "HolmesGPT investigated CheckoutErrorRateHigh and recommended handling item-3 in checkout code.",
        "body": analysis,
        "service": "checkout",
        "metric": "checkout_request_errors_total",
        "alert_threshold": 0.05,
        "last_value": 0.20,
        "alert_transition": "error",
        "tags": {"service": "checkout", "env": "staging", "team": "payments"},
    }
    scenario = {
        "name": "holmes-bridged-error-rate",
        "description": "Real HolmesGPT recommendation for CheckoutErrorRateHigh, bridged through Claude.",
        "repo_path": "sample-app",
        "use_claude": False,
        "alert": alert,
        "alert_slo": {
            # CheckoutErrorRateHigh fires when error_rate > 0.05 (5%).
            # The CLI loads this onto Alert.slo so the engine can do the
            # alert-aware classification instead of generic comparison.
            "signal": "error_rate",
            "operator": ">",
            "threshold": 0.05,
        },
        "prepared_patch": patch,
    }
    OUT.write_text(json.dumps(scenario, indent=2))
    print(f"wrote {OUT}")
    print(f"\npatch summary: {patch['summary']}")
    print(f"files: {[f['path'] for f in patch['files']]}")
    print(f"confidence: {patch['confidence']}")
    print(f"expected: {patch['expected_signal_change']}")


if __name__ == "__main__":
    main()
