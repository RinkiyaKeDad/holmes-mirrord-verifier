"""Bridge: turn a HolmesGPT investigation report into a concrete code patch.

HolmesGPT outputs prose ("root cause… recommendation: add caching"). The
verifier can't run prose — it needs a before/after code patch to apply and run.
This module is that translator: one Claude call that reads Holmes' report plus
the candidate source and emits a structured Patch.

It is deliberately a *faithful translator*: it implements exactly what Holmes
recommended, not what Claude thinks the best fix is. That faithfulness is what
keeps the verifier honest — if the bridge silently wrote a correct fix, the
REJECT case (Holmes recommends something that doesn't clear the SLO) could never
happen.

This is the generic, production-shaped version of the scenario-specific scripts
under walkthroughs/holmes-*/bridge_holmes.py (which hardcode each scenario's
expected recommendation). Here the recommendation is whatever Holmes actually
said.
"""

from __future__ import annotations

import logging
import os

from anthropic import Anthropic

from .models import Alert, Patch, PatchFile
from .orchestrator import _extract_json, _snapshot_repo

log = logging.getLogger("verifier.bridge")

DEFAULT_MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are a faithful translator. You convert an AI-SRE tool's prose
investigation report into a concrete code patch that implements EXACTLY what the
AI-SRE recommended — not what you think the best fix is.

You receive HolmesGPT's investigation report and the candidate service's source.
Read the report's recommendation / remediation and implement the single smallest
code change that carries it out. If the report lists several recommendations,
implement the first one that is a concrete code change to the candidate source;
skip pure infrastructure, config, scaling, or "monitor it" advice that isn't a
code edit. Do NOT add improvements, extra error handling, fallbacks, or fixes the
report did not call for. Translate, don't improve.

Respond with exactly one JSON object matching this schema:

{
  "summary": "<one-line description of the change you made>",
  "hypothesis": "<paraphrase of HolmesGPT's stated root cause>",
  "repro_steps": ["<step 1>", "<step 2>"],
  "files": [{"path": "<rel path>", "before": "<exact original content>",
             "after": "<exact patched content>"}],
  "confidence": 0.0-1.0,
  "expected_signal_change": "<what HolmesGPT expected to improve, and how>"
}

Rules:
- 'before' must be the EXACT current content of each file you change (every byte,
  top to bottom — docstring, imports, every function). Not a snippet, not a diff.
- 'after' must be the full patched file content.
- Patches must be syntactically valid in the file's language.
- Do not add commentary outside the JSON.
"""


class Bridge:
    """Wraps Claude to translate a Holmes report into a Patch."""

    def __init__(self, model: str | None = None) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY missing — the bridge needs a Claude key.")
        self.client = Anthropic(api_key=api_key)
        self.model = model or os.environ.get("VERIFIER_MODEL", DEFAULT_MODEL)

    def report_to_patch(self, alert: Alert, report: str) -> Patch:
        source = _snapshot_repo(alert.repo_path) if alert.repo_path else ""
        user_msg = (
            f"# HolmesGPT investigation report\n{report}\n\n"
            f"# Candidate source\n{source}\n\n"
            f"Respond with the JSON patch."
        )
        log.info("bridging holmes report for alert=%s via %s", alert.id, self.model)
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        payload = _extract_json(text)
        return Patch(
            summary=payload["summary"],
            hypothesis=payload["hypothesis"],
            repro_steps=payload["repro_steps"],
            files=[PatchFile(**f) for f in payload["files"]],
            confidence=float(payload["confidence"]),
            expected_signal_change=payload["expected_signal_change"],
            model=f"holmes-bridge:{self.model}",
        )
