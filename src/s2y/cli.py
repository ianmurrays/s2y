"""Command-line entry point.

Default: launch the Textual UI. ``--plan`` runs a headless dry-run that prints the
plan (handy for verifying a setup without driving the UI). Logging goes to a file
only — never stdout — so it can never corrupt the TUI.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys

from . import __version__
from .config import Config, default_paths


def _configure_logging(paths, *, verbose: bool = False) -> None:
    """Send logs to a rotating file only (never stdout — it would corrupt the TUI).

    Level is INFO by default; ``--verbose`` or ``S2Y_LOG_LEVEL=DEBUG`` raises it to
    DEBUG (which also enables the chatty per-search breadcrumbs).
    """
    paths.ensure_dirs()
    env_level = os.environ.get("S2Y_LOG_LEVEL", "").upper()
    if verbose or env_level == "DEBUG":
        level = logging.DEBUG
    else:
        level = getattr(logging, env_level, logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        str(paths.log_file), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):  # idempotent if called more than once
        root.removeHandler(existing)
    root.addHandler(handler)


def _run_headless_plan() -> int:
    """Build and print a plan without applying (no TUI). Returns an exit code."""
    from collections import Counter

    from .core.events import CollectionSkipped, CollectionStarted, DedupDegraded, TrackMatched
    from .session import build_engine, directions_for, is_configured

    paths = default_paths()
    config = Config.load(paths.config_file)
    if not is_configured(config, paths):
        print("Not set up yet. Run `s2y` and complete the setup wizard first.", file=sys.stderr)
        return 2

    def on_event(event) -> None:
        if isinstance(event, CollectionStarted):
            print(f"\n# {event.name} ({event.source_count} tracks)")
        elif isinstance(event, CollectionSkipped):
            print(f"\n# {event.name}: unchanged")
        elif isinstance(event, DedupDegraded):
            print(f"  ! {event.name}: could not read the target, skipped ({event.detail})")
        elif isinstance(event, TrackMatched):
            r = event.result
            mark = {"auto": "✓", "quarantine": "?", "no_match": "✗"}.get(r.outcome.value, "·")
            if event.present:
                mark = "="
            target = r.best.title if r.best else "—"
            print(
                f"  {mark} {r.source.title} — {r.source.artist}  ->  {target} ({r.confidence:.2f})"
            )

    # A "both" config plans both legs; single-direction configs plan one.
    for direction in directions_for(config):
        print(f"\n# Direction: {direction.source.value} -> {direction.target.value}")
        try:
            engine = build_engine(config, paths, direction=direction)
            plan = engine.build_plan(on_event)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to build plan: {exc}", file=sys.stderr)
            return 1
        per_coll = Counter(op.collection for op in plan.ops)
        if per_coll:
            print("\nActions by collection:")
            for coll, n in sorted(per_coll.items()):
                print(f"  {coll!r}: {n}")
        print(
            f"\nPlan: {len(plan.ops)} actions, {len(plan.quarantine)} need review, "
            f"{plan.present} already there, {plan.skipped} unchanged, "
            f"{len(plan.degraded)} skipped. (dry run — nothing was changed)"
        )
    return 0


def _run_headless_classify(*, dry_run: bool) -> int:
    """Run the LLM judge over the quarantine (no TUI). Returns an exit code.

    With ``dry_run`` it prints each verdict but resolves nothing; otherwise it
    auto-resolves the confident accepts. Requires ``[classify] enabled = true``.
    """
    from .core.models import ClassifyDecision
    from .session import build_engine, directions_for, is_configured

    paths = default_paths()
    config = Config.load(paths.config_file)
    if not is_configured(config, paths):
        print("Not set up yet. Run `s2y` and complete the setup wizard first.", file=sys.stderr)
        return 2
    if not config.classify.enabled:
        print(
            "Classification is disabled. Add to your config.toml:\n\n"
            "  [classify]\n  enabled = true\n",
            file=sys.stderr,
        )
        return 2

    def on_event(event) -> None:
        from .core.events import ClassifyVerdictEvent

        if not isinstance(event, ClassifyVerdictEvent):
            return
        v = event.verdict
        if v.decision is ClassifyDecision.ACCEPT:
            mark = "✓ resolved" if event.applied else "✓ accept (kept)"
        elif v.decision is ClassifyDecision.REJECT:
            mark = "✗ reject (kept)"
        else:
            mark = "? unsure (kept)"
        print(f"  {mark:16} {event.title} — {event.artist}  (conf {v.confidence:.2f})")

    # Each engine judges only its own source side, so cover every configured
    # direction — a "both" config has quarantines on both the Spotify and YT sides.
    stats = {"considered": 0, "accepted": 0, "rejected": 0, "uncertain": 0, "applied": 0}
    for direction in directions_for(config):
        try:
            engine = build_engine(config, paths, direction=direction)
            part = engine.classify_quarantine(on_event, apply=not dry_run)
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to classify: {exc}", file=sys.stderr)
            return 1
        for k in stats:
            stats[k] += part.get(k, 0)

    kept = stats["accepted"] - stats["applied"]
    print(
        f"\nClassified {stats['considered']} track(s): {stats['applied']} auto-resolved, "
        f"{kept} accepted-but-kept, {stats['rejected']} rejected, "
        f"{stats['uncertain']} uncertain."
    )
    if dry_run:
        print("(dry run — nothing was changed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="s2y", description="Sync a music library between Spotify and YouTube Music"
    )
    parser.add_argument("--version", action="version", version=f"s2y {__version__}")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="headless dry-run: print the sync plan and exit (no UI, no changes)",
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help="headless: run the Claude LLM judge over quarantined tracks and "
        "auto-resolve confident matches (requires [classify] enabled)",
    )
    parser.add_argument(
        "--classify-dry-run",
        action="store_true",
        help="like --classify but print verdicts only; resolve nothing",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose (DEBUG) logging to the log file",
    )
    args = parser.parse_args(argv)

    paths = default_paths()
    _configure_logging(paths, verbose=args.verbose)
    logging.getLogger("s2y").info("s2y %s starting — logging to %s", __version__, paths.log_file)

    if args.plan:
        return _run_headless_plan()

    if args.classify or args.classify_dry_run:
        return _run_headless_classify(dry_run=args.classify_dry_run)

    from .tui import S2YApp

    S2YApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
