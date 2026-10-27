import argparse
import json
import os
import sys
from pathlib import Path

from .doctor import run_doctor


EXIT_CODES = {"pass": 0, "fail": 1, "blocked": 2, "uncertain": 3}


def main():
    parser = argparse.ArgumentParser(description="Quality Harness M0 capability acceptance; subsequent phases are gated.")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Run real, fail-closed M0 capability checks")
    doctor.add_argument("--repo", type=Path, required=True)
    doctor.add_argument("--runtime", type=Path, default=Path(sys.base_prefix), help="Standalone Python 3.10 runtime root (not the tool virtualenv)")
    doctor.add_argument("--output-dir", type=Path, default=Path.home() / ".local/state/codex-quality-harness")
    doctor.add_argument("--gitlab-project", type=int)
    doctor.add_argument("--pipeline", type=int)
    doctor.add_argument("--live-codex", action="store_true", help="Exercise actual Builder and Reviewer calls (consumes model usage)")
    doctor.add_argument("--wait-for-ci-seconds", type=int, default=0, help="Bounded read-only wait for the selected pipeline (0..5400; no job retry)")
    doctor.add_argument("--json", action="store_true", help="Versioned JSON (also the default)")
    args = parser.parse_args()
    if not args.repo.is_dir():
        parser.error("--repo must be an existing directory")
    if not 0 <= args.wait_for_ci_seconds <= 5400:
        parser.error("--wait-for-ci-seconds must be between 0 and 5400")
    os.umask(0o077)
    result = run_doctor(args.repo, args.runtime.resolve(), args.output_dir, args.gitlab_project, args.pipeline, args.live_codex, args.wait_for_ci_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_CODES[result["result"]]
