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
    from .core.events import CollectionSkipped, CollectionStarted, TrackMatched
    from .session import build_engine, is_configured

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
        elif isinstance(event, TrackMatched):
            r = event.result
            mark = {"auto": "✓", "quarantine": "?", "no_match": "✗"}.get(r.outcome.value, "·")
            target = r.best.title if r.best else "—"
            print(f"  {mark} {r.source.title} — {r.source.artist}  ->  {target} ({r.confidence:.2f})")

    try:
        engine = build_engine(config, paths)
        plan = engine.build_plan(on_event)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to build plan: {exc}", file=sys.stderr)
        return 1

    print(
        f"\nPlan: {len(plan.ops)} actions, {len(plan.quarantine)} need review, "
        f"{plan.skipped} unchanged. (dry run — nothing was changed)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="s2y", description="Sync Spotify to YouTube Music")
    parser.add_argument("--version", action="version", version=f"s2y {__version__}")
    parser.add_argument(
        "--plan", action="store_true",
        help="headless dry-run: print the sync plan and exit (no UI, no changes)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="verbose (DEBUG) logging to the log file",
    )
    args = parser.parse_args(argv)

    paths = default_paths()
    _configure_logging(paths, verbose=args.verbose)
    logging.getLogger("s2y").info("s2y %s starting — logging to %s", __version__, paths.log_file)

    if args.plan:
        return _run_headless_plan()

    from .tui import S2YApp

    S2YApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
