import argparse
import json
import os
import sys
import sqlite3
from pathlib import Path

from .doctor import run_doctor
from .harness import Store, RunError, prepare, gate, response, fingerprint, resume, run_loop


EXIT_CODES = {"pass": 0, "fail": 1, "blocked": 2, "uncertain": 3}


def main():
    parser = argparse.ArgumentParser(description="Local Codex Quality Harness; integration and release gates remain independent.")
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
    prepare_parser = sub.add_parser("prepare", help="Freeze a contract and create an isolated persistent worktree")
    prepare_parser.add_argument("--repo", type=Path, required=True)
    prepare_parser.add_argument("--task", required=True)
    prepare_parser.add_argument("--contract", type=Path, required=True)
    prepare_parser.add_argument("--approval", type=Path, help="Explicit approval for a changed existing acceptance standard")
    from .harness import STAGES
    for name in ("run", "gate", "status", "resume"):
        command = sub.add_parser(name)
        command.add_argument("--run", required=True)
        command.add_argument("--json", action="store_true")
        command.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/codex-quality-harness/controller")
        if name in {"run", "resume"}:
            command.add_argument("--until", choices=STAGES, default="code_ready")
            command.add_argument("--runtime", type=Path, default=Path(sys.base_prefix))
            command.add_argument("--publish", action="store_true", help="Commit/push a reviewed change and create a Draft MR; no merge or deploy")
            command.add_argument("--pipeline", type=int, help="Read this actual MR pipeline; never infer success from logs")
            command.add_argument("--reconcile", action="store_true", help="Read back uncertain operations; never blindly resend")
        if name == "run":
            command.add_argument("--cancel", action="store_true", help="Request cancellation; retain all evidence")
            command.add_argument("--operation", help="Explicitly invoke a reviewed adapter registered in the contract")
            command.add_argument("--approval", type=Path, help="Exact operation approval record; never create one without the user's authorization")
            command.add_argument("--approve-stage", choices=["production_ready"], help="Record separately authorized readiness approval; never deploy or merge")
        if name == "gate":
            command.add_argument("--stage", choices=STAGES, required=True)
        if name == "status":
            command.add_argument("--backup", type=Path, help="Create an exclusive consistent SQLite backup")
    prepare_parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/codex-quality-harness/controller")
    prepare_parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command in {"doctor", "prepare"} and not args.repo.is_dir():
        parser.error("--repo must be an existing directory")
    if args.command == "doctor" and not 0 <= args.wait_for_ci_seconds <= 5400:
        parser.error("--wait-for-ci-seconds must be between 0 and 5400")
    os.umask(0o077)
    if args.command == "doctor":
        result = run_doctor(args.repo, args.runtime.resolve(), args.output_dir, args.gitlab_project, args.pipeline, args.live_codex, args.wait_for_ci_seconds)
    else:
        store = None
        try:
            store = Store(args.state_dir)
            if args.command == "prepare":
                approval = json.loads(args.approval.read_text()) if args.approval else None
                run = prepare(store, args.repo, args.task, args.contract, approval)
                result = response(store, run["id"], "pass")
            elif args.command == "run":
                if args.cancel:
                    store.update(args.run, cancel_requested=True)
                    result = response(store, args.run, "blocked", ["cancellation requested; inspect status for confirmed worker termination"])
                elif args.operation or args.approve_stage:
                    from .adapters import run_operation, production_approval
                    approval = None
                    if args.approval:
                        if args.approval.stat().st_size > 1024 * 1024:
                            raise RunError("approval record exceeds size budget")
                        approval = json.loads(args.approval.read_text())
                    if args.operation:
                        proof = run_operation(store, args.run, args.operation, approval, args.reconcile)
                        result = response(store, args.run, "pass")
                        result["operation"] = proof["scope"]["kind"]
                    else:
                        if not approval:
                            raise RunError("stage approval record is required")
                        with store.lock(args.run):
                            production_approval(store, args.run, approval)
                        result = response(store, args.run, "pass")
                else:
                    result = run_loop(store, args.run, args.runtime, args.until, args.publish, args.pipeline)
            elif args.command == "gate":
                result = gate(store, args.run, args.stage)
            elif args.command == "resume":
                result = resume(store, args.run, args.reconcile)
                if result["result"] == "blocked" and result["blockers"] == ["safe to continue with qh run; no action was replayed"]:
                    store.update(args.run, cancel_requested=False)
                    result = run_loop(store, args.run, args.runtime, args.until, args.publish, args.pipeline)
            else:
                run = store.run(args.run)
                fp = fingerprint(Path(run["worktree"]))
                result = response(store, args.run, "pass")
                from .harness import environment_identity
                current_environment = environment_identity(Path(run["data"]["runtime"]), store.contract(run)) if run["data"].get("runtime") else None
                expected_rules = store.evidence_rules(args.run, current_environment)
                if run["data"].get("accepted_fingerprint") != fp or run["data"].get("accepted_rules") != expected_rules:
                    result["quality_result"] = "stale" if run["data"].get("accepted_fingerprint") else "not_evaluated"
                for evidence in result["evidence"]:
                    row = store.db.execute("select fingerprint,rules from evidence where id=?", (evidence["id"],)).fetchone()
                    expected = run["data"].get("isolation_fingerprint") if evidence["kind"] == "isolation" else fp
                    evidence["validity"] = "valid" if row["fingerprint"] == expected and row["rules"] == expected_rules else "stale"
                result["pending_operations"] = [dict(row) for row in store.db.execute(
                    "select id,kind,state,pid from operations where run=? and state!='confirmed' order by created", (args.run,))]
                from .adapters import operation_scope
                result["operation_scopes"] = {}
                for name in store.contract(run).get("operations", {}):
                    try:
                        result["operation_scopes"][name] = operation_scope(store, run, name)[1]
                    except RunError as error:
                        result["operation_scopes"][name] = {"blocked": str(error)}
                from .adapters import efficiency_report, production_approval
                result["efficiency"] = efficiency_report(store, run)
                if store.contract(run).get("production"):
                    try:
                        result["production_approval_scope"] = production_approval(store, args.run)
                    except RunError as error:
                        result["production_approval_scope"] = {"blocked": str(error)}
                if args.backup:
                    store.backup(args.backup)
                    result["backup"] = str(args.backup)
        except RunError as error:
            result = {"protocol_version": 1, "run_id": getattr(args, "run", None), "result": error.result,
                      "stage": getattr(args, "stage", None), "blockers": [str(error)], "evidence": [], "next": "resolve blockers; no implicit approval or replay"}
        except (ValueError, OSError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
            result = {"protocol_version": 1, "run_id": getattr(args, "run", None), "result": "blocked",
                      "blockers": ["Controller input or environment error: " + type(error).__name__], "evidence": []}
        finally:
            if store is not None:
                store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_CODES[result["result"]]
