"""Trigger a HolmesGPT investigation from inside the cluster.

HolmesGPT's HTTP API (image 0.33.0) only exposes /api/chat — there is no
endpoint that reproduces the CLI's `investigate alertmanager`, which pulls the
specific firing alert from Alertmanager and runs a structured investigation. So
the verifier execs the CLI inside the Holmes pod, exactly as a human would:

    cd /app && HOME=/tmp python holmes_cli.py investigate alertmanager \
      --alertmanager-url <url> --alertmanager-alertname <name> --model <model>

HOME=/tmp works around the pod's read-only root filesystem (Holmes writes a
toolset cache to ~/.holmes). The verifier's Role already grants pods/exec in the
namespace, so no extra RBAC is needed.
"""

from __future__ import annotations

import logging
import os
import time

from kubernetes import client, config
from kubernetes.stream import stream

from .models import Alert

log = logging.getLogger("verifier.holmes")

# The Holmes investigation runs an agent loop (kubectl, logs, LLM calls); give
# it room. The exec stream is drained in small slices until the command exits or
# this deadline trips.
_INVESTIGATE_TIMEOUT_S = 300


class HolmesInvestigator:
    """Execs the HolmesGPT CLI in its pod and returns the markdown report."""

    def __init__(
        self,
        namespace: str | None = None,
        label_selector: str | None = None,
        alertmanager_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.namespace = namespace or os.environ.get("HOLMES_NAMESPACE", "verifier-poc")
        self.label_selector = label_selector or os.environ.get(
            "HOLMES_LABEL_SELECTOR", "app=holmes"
        )
        self.alertmanager_url = alertmanager_url or os.environ.get(
            "ALERTMANAGER_URL",
            "http://monitoring-kube-prometheus-alertmanager.monitoring:9093",
        )
        self.model = model or os.environ.get("HOLMES_MODEL", "anthropic/claude-sonnet-4-6")
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.core = client.CoreV1Api()

    def investigate(self, alert: Alert) -> str:
        """Run an Alertmanager investigation for `alert` and return the report.

        `alert.metric` carries the Prometheus alertname (set by the webhook
        parser from the alert's `alertname` label).
        """
        alertname = alert.metric or alert.title
        pod = self._find_pod()
        cli = (
            "cd /app && HOME=/tmp python holmes_cli.py investigate alertmanager "
            f"--alertmanager-url {self.alertmanager_url} "
            f"--alertmanager-alertname {alertname} "
            f"--model {self.model}"
        )
        log.info("holmes investigate [%s] in pod %s", alertname, pod)
        raw = self._exec(pod, ["sh", "-c", cli])
        report = _extract_report(raw)
        log.info("holmes report for %s: %d chars", alertname, len(report))
        return report

    def _find_pod(self) -> str:
        pods = self.core.list_namespaced_pod(
            namespace=self.namespace, label_selector=self.label_selector
        )
        running = [
            p for p in pods.items
            if p.status and p.status.phase == "Running"
        ]
        if not running:
            raise RuntimeError(
                f"no running Holmes pod in {self.namespace} matching "
                f"{self.label_selector!r}"
            )
        return running[0].metadata.name

    def _exec(self, pod: str, command: list[str]) -> str:
        resp = stream(
            self.core.connect_get_namespaced_pod_exec,
            pod,
            self.namespace,
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
        chunks: list[str] = []
        deadline = time.monotonic() + _INVESTIGATE_TIMEOUT_S
        while resp.is_open():
            if time.monotonic() > deadline:
                resp.close()
                raise RuntimeError(
                    f"holmes investigation exceeded {_INVESTIGATE_TIMEOUT_S}s"
                )
            resp.update(timeout=5)
            if resp.peek_stdout():
                chunks.append(resp.read_stdout())
            if resp.peek_stderr():
                chunks.append(resp.read_stderr())
        resp.close()
        return "".join(chunks)


def _extract_report(raw: str) -> str:
    """Pull the final analysis out of the CLI's chatty output.

    The CLI streams its agent loop (tool calls, progress) and then prints the
    final answer after the last `AI:` marker. We keep that tail; if no marker is
    found we fall back to the whole transcript so the bridge still has context.
    """
    marker = raw.rfind("AI:")
    return raw[marker:].strip() if marker != -1 else raw.strip()
