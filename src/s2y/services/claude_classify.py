"""LLM-judge adapter: classify a batch of quarantined tracks via the ``claude`` CLI.

This is the I/O half of the classifier — it shells out to ``claude -p`` in
headless mode and feeds the reply to the pure parser in :mod:`s2y.core.classify`.
It implements the :class:`s2y.core.ports.Classifier` Protocol.

A whole batch of tracks is judged in **one** ``claude -p`` call (the engine chunks
the quarantine, so very large backlogs split into a handful of calls instead of one
per track). By design it **never raises**: a missing binary, a non-zero exit, a
timeout, or unparseable output all become an all-``UNCERTAIN`` result aligned to the
batch, so the only consequence of any failure is that those tracks stay quarantined
for the human. Everything is logged to the file (the app routes logging there),
never to stdout.

Auth uses the user's existing Claude Code login — we do *not* pass ``--bare`` (which
would force an API key); a logged-in subscription works out of the box, and
``ANTHROPIC_API_KEY`` in the environment is honoured as an alternative.
"""

from __future__ import annotations

import json
import logging
import subprocess

from ..core.classify import build_batch_prompt, parse_batch_verdicts
from ..core.models import ClassifyDecision, ClassifyItem, ClassifyVerdict

log = logging.getLogger(__name__)


def _uncertain() -> ClassifyVerdict:
    return ClassifyVerdict(ClassifyDecision.UNCERTAIN, None, 0.0)


class ClaudeClassifier:
    """Classify quarantined tracks by invoking ``claude -p`` once per batch.

    Takes plain primitives (not the whole :class:`Config`) so it is trivial to
    construct in tests and stays decoupled from config loading.
    """

    def __init__(
        self, *, claude_path: str = "claude", model: str = "sonnet", timeout_s: float = 120.0
    ) -> None:
        self._path = claude_path
        self._model = model
        self._timeout = timeout_s

    def _command(self, prompt: str) -> list[str]:
        # `--tools ""` disables every built-in tool, so the run is a single, fast,
        # tool-free text completion. `--output-format json` wraps the reply in an
        # envelope whose `result` field holds the model's text.
        return [
            self._path,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self._model,
            "--tools",
            "",
        ]

    @staticmethod
    def _all_uncertain(items: tuple[ClassifyItem, ...]) -> tuple[ClassifyVerdict, ...]:
        return tuple(_uncertain() for _ in items)

    def classify_batch(self, items: tuple[ClassifyItem, ...]) -> tuple[ClassifyVerdict, ...]:
        if not items:
            return ()
        prompt = build_batch_prompt(items)
        try:
            proc = subprocess.run(
                self._command(prompt),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            log.warning(
                "claude binary not found at %r — leaving %d track(s) quarantined",
                self._path,
                len(items),
            )
            return self._all_uncertain(items)
        except subprocess.TimeoutExpired:
            log.warning(
                "claude classify timed out after %ss for a %d-track batch",
                self._timeout,
                len(items),
            )
            return self._all_uncertain(items)

        if proc.returncode != 0:
            log.warning("claude exited %d: %s", proc.returncode, (proc.stderr or "")[:500])
            return self._all_uncertain(items)

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            log.warning("claude produced no JSON envelope for a %d-track batch", len(items))
            return self._all_uncertain(items)
        if not isinstance(envelope, dict) or envelope.get("is_error"):
            log.warning("claude reported an error classifying a %d-track batch", len(items))
            return self._all_uncertain(items)
        result_text = envelope.get("result")
        if not isinstance(result_text, str):
            log.warning("claude envelope had no result text for a %d-track batch", len(items))
            return self._all_uncertain(items)

        return parse_batch_verdicts(result_text, items)
