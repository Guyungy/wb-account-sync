"""CLI boundary: dry operations by default, explicit confirmation for writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from . import __version__, core
from .safety import SafetyError, atomic_json, require_plain_path

RETIRED = {"sync", "live", "daemon-install", "daemon-uninstall", "daemon-status",
           "daemon-log", "backup", "snapshots", "adopt", "revert"}


def parser():
    p = argparse.ArgumentParser(prog="wb-account-sync", description="Offline ordinary-session migration safety preview")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status"):
        cmd = sub.add_parser(name, help="Read-only session schema/account inspection")
        cmd.add_argument("--home", required=True)
        cmd.add_argument("--json", action="store_true")
    cmd = sub.add_parser("plan", help="Compute a read-only migration plan; stdout unless --output is explicit")
    cmd.add_argument("--home", required=True)
    cmd.add_argument("--source", required=True)
    cmd.add_argument("--target", required=True)
    cmd.add_argument("--output")
    cmd.add_argument("--json", action="store_true")
    cmd = sub.add_parser("apply", help="Apply a reviewed plan while client and old daemon are stopped")
    cmd.add_argument("--plan", required=True)
    cmd.add_argument("--state-dir", required=True)
    cmd.add_argument("--confirm", required=True, help="Full reviewed plan_id, not a generic --yes")
    cmd.add_argument("--json", action="store_true")
    cmd = sub.add_parser("verify", help="Verify ownership only, not UI/cloud/attachments")
    cmd.add_argument("--plan", required=True)
    cmd.add_argument("--json", action="store_true")
    cmd = sub.add_parser("restore", help="Undo recorded ownership changes without overwriting later edits")
    cmd.add_argument("--run-dir", required=True)
    cmd.add_argument("--confirm", required=True)
    cmd.add_argument("--json", action="store_true")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in RETIRED:
        print("This legacy command is disabled in the safety preview. Use doctor -> plan -> apply -> verify. "
              "Existing loaded daemons are NOT stopped by upgrading files. Quit the client and stop the "
              "old service through the normal macOS service controls. See docs/MIGRATION.md.", file=sys.stderr)
        return 2
    args = parser().parse_args(argv)
    try:
        if args.command in ("doctor", "status"):
            result = core.doctor(args.home)
        elif args.command == "plan":
            result = core.make_plan(args.home, args.source, args.target)
            if args.output:
                output = require_plain_path(args.output)
                home = Path(result["identity"]["home"])
                if output == home or home in output.parents:
                    raise SafetyError("Export the plan outside the client data directory.")
                atomic_json(output, result, exclusive=True)
        elif args.command == "apply":
            result = core.apply(core.load_plan(args.plan), args.state_dir, args.confirm)
        elif args.command == "verify":
            result = core.verify(core.load_plan(args.plan))
        else:
            result = core.restore(args.run_dir, args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.command == "verify" and not result["verified"]:
            return 3
        return 0
    except (SafetyError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        # SQL operational errors omit paths/statements to avoid accidental payload disclosure.
        message = str(exc) if isinstance(exc, SafetyError) else f"{type(exc).__name__}: operation failed; review local permissions and the run journal."
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": message}, ensure_ascii=False), file=sys.stderr)
        else:
            print("Refused: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
