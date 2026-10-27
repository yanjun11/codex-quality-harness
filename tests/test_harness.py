import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_quality_harness.harness import Store, RunError, prepare, fingerprint, gate, resume, validate_contract, worker_args, builder_workspace, accept_builder_source, protected_files, environment_identity
from codex_quality_harness.adapters import verify_approval, check_observation, measurement_comparison, run_operation, publish_draft, operation_scope


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "严骏")
        git(self.repo, "config", "user.email", "yanjun@fuzhi.ai")
        git(self.repo, "config", "user.useConfigOnly", "true")
        git(self.repo, "config", "commit.gpgsign", "false")
        for role in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
            self.assertTrue(git(self.repo, "var", role).startswith("严骏 <yanjun@fuzhi.ai> "))
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests/check.py").write_text("from calc import add\nassert add(2, 3) == 5\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "test fixture")
        self.store = Store(self.root / "state")
        self.addCleanup(self.store.close)
        self.contract = {
            "version": 1, "goal": "add must sum two integers",
            "architecture": {k: "fixture scope" for k in ("scope", "reuse", "data_model", "performance")},
            "rubric": [{"id": "sum", "expected": "2+3=5", "critical": True}],
            "tests": [{"id": "sum", "kind": "contract", "argv": ["python3.10", "tests/check.py"],
                       "inputs": ["tests/check.py"], "negative_argv": ["python3.10", "-c", "raise SystemExit(1)"]}],
            "risk": "low", "base_ref": "HEAD", "budget": {"rounds": 3, "seconds": 1800},
        }
        self.contract["architecture"]["scope"] = ["calc.py"]
        self.file = self.root / "contract.json"
        self.file.write_text(json.dumps(self.contract))

    def make_run(self):
        return prepare(self.store, self.repo, "DEMO-1", self.file)

    def test_contract_missing_boundaries_and_known_bad_control_blocks(self):
        for field in ("architecture", "rubric", "goal"):
            value = dict(self.contract)
            value.pop(field)
            with self.assertRaises(RunError):
                validate_contract(value)
        value = json.loads(json.dumps(self.contract))
        value["tests"][0].pop("negative_argv")
        with self.assertRaises(RunError):
            validate_contract(value)

    def test_authorized_scope_cannot_grant_access_to_tests_or_git(self):
        for scope in (["."], ["../outside"], ["tests"], [".git"], ["requirements.txt"]):
            value = json.loads(json.dumps(self.contract))
            value["architecture"]["scope"] = scope
            with self.assertRaises(RunError):
                validate_contract(value)

    def test_worker_only_writes_authorized_scope_not_entire_repository(self):
        run = self.make_run()
        work = Path(run["worktree"])
        args = worker_args(work, Path('/runtime'), self.contract, 'builder', self.root / 'scratch', self.root / 'contract.json')
        self.assertIn('permissions.qh.extends=":read-only"', args)
        rule = [v for v in args if v.startswith('permissions.qh.filesystem=')][-1]
        self.assertIn(json.dumps(str(work)) + '="read"', rule)
        self.assertIn(json.dumps(str(work / 'calc.py')) + '="write"', rule)
        self.assertIn(json.dumps(str(work / 'tests')) + '="read"', rule)

    def test_model_concurrency_is_bounded_across_controllers(self):
        with self.store.model_slot():
            with self.store.model_slot():
                with self.assertRaises(RunError):
                    with self.store.model_slot():
                        pass

    def test_prepare_preserves_original_and_protects_contract(self):
        (self.repo / "local-note").write_text("preserve me")
        run = self.make_run()
        self.assertNotEqual(run["worktree"], str(self.repo))
        self.assertEqual((self.repo / "local-note").read_text(), "preserve me")
        self.assertEqual(self.store.contract(run)["goal"], self.contract["goal"])
        self.assertEqual(run["stage"], "draft_mr")
        self.assertEqual(gate(self.store, run["id"], "code_ready")["result"], "blocked")

    def test_new_source_and_fixture_change_expire_review(self):
        run = self.make_run()
        work = Path(run["worktree"])
        before = fingerprint(work)
        self.store.evidence(run["id"], "review", "pass", before, b'{"findings":[]}')
        (work / "calc.py").write_text("def add(a,b): return a+b\n")
        self.assertNotEqual(fingerprint(work), before)
        self.assertFalse(self.store.current_evidence(run["id"], "review", fingerprint(work)))
        after = fingerprint(work)
        (work / "tests/check.py").write_text("assert True\n")
        self.assertNotEqual(fingerprint(work), after)

    def test_commit_hook_cannot_publish_bytes_not_covered_by_review(self):
        self.contract["delivery"] = {"gitlab_project": 123, "remote": "origin", "target": "main"}
        self.file.write_text(json.dumps(self.contract))
        git(self.repo, "remote", "add", "origin", "https://gitlab.deepwisdomai.com/example/repo.git")
        run = self.make_run()
        work = Path(run["worktree"])
        (work / "calc.py").write_text("def add(a,b): return a+b\n")
        self.store.evidence(run["id"], "review", "pass", fingerprint(work), b'{"result":{"summary":"reviewed"}}')
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "post-commit"
        hook.write_text("#!/bin/sh\nprintf '\n# changed by local hook\n' >> calc.py\n")
        hook.chmod(0o700)
        git(work, "config", "core.hooksPath", str(hooks))
        with patch('codex_quality_harness.adapters.doctor.api', return_value={"path_with_namespace": "example/repo"}):
            with self.assertRaisesRegex(RunError, 'source changed during commit'):
                publish_draft(self.store, run)
        self.assertFalse(self.store.db.execute("select id from operations where kind='push'").fetchall())

    def test_required_ci_cannot_be_satisfied_by_local_model_pass(self):
        run = self.make_run()
        fp = fingerprint(Path(run["worktree"]))
        self.store.evidence(run["id"], "model", "pass", fp, b'{"status":"PASS"}')
        result = gate(self.store, run["id"], "code_ready")
        self.assertEqual(result["result"], "blocked")
        self.assertIn("review", " ".join(result["blockers"]))

    def test_artifacts_are_immutable_and_tampering_blocks_read(self):
        run = self.make_run()
        evidence = self.store.evidence(run["id"], "eval:sum", "pass", "fingerprint", b"original")
        with self.assertRaises(RunError):
            self.store.read_blob("../outside")
        path = self.store.root / "blobs" / evidence["digest"]
        path.write_bytes(b"changed")
        with self.assertRaises(RunError):
            self.store.read_blob(evidence["digest"])

    def test_live_writer_not_replaced_when_lease_expired(self):
        run = self.make_run()
        with self.store.lock(run["id"]):
            with self.assertRaises(RunError):
                with self.store.lock(run["id"]):
                    pass

    def test_unknown_operation_blocks_resume_instead_of_duplicate_dispatch(self):
        run = self.make_run()
        operation = self.store.begin_operation(run["id"], "push", {"sha": run["base_sha"]})
        result = resume(self.store, run["id"])
        self.assertEqual(result["result"], "uncertain")
        self.assertIn(operation, " ".join(result["blockers"]))
        self.assertEqual(self.store.db.execute("select count(*) from operations where kind='push'").fetchone()[0], 1)

    def test_first_failure_cannot_be_replaced_by_unchanged_retry(self):
        run = self.make_run()
        fp = fingerprint(Path(run["worktree"]))
        self.store.evidence(run["id"], "eval:sum", "fail", fp, b"first failure")
        self.store.evidence(run["id"], "eval:sum", "pass", fp, b"later success")
        results = self.store.current_evidence(run["id"], "eval:sum", fp)
        self.assertIn("fail", [e["result"] for e in results])
        self.assertEqual(gate(self.store, run["id"], "code_ready")["result"], "blocked")

    def test_untracked_source_and_symlink_changes_are_not_invisible(self):
        run = self.make_run()
        work = Path(run["worktree"])
        before = fingerprint(work)
        (work / "new.py").write_text("bad change\n")
        self.assertNotEqual(fingerprint(work), before)
        (work / "escape").symlink_to(self.root / "contract.json")
        with self.assertRaises(RunError):
            fingerprint(work)

    def test_backup_uses_sqlite_consistent_snapshot(self):
        run = self.make_run()
        target = self.root / "backup.sqlite"
        self.store.backup(target)
        import sqlite3
        with sqlite3.connect(target) as backup:
            self.assertEqual(backup.execute("select id from runs").fetchone()[0], run["id"])

    def test_test_approval_cannot_authorize_production_or_changed_script(self):
        import time
        scope = {"run_id": "run-1", "kind": "test_deployment", "environment": "test", "source_sha": "a" * 40,
                 "target": "personal-lane", "script_digest": "b" * 64}
        approval = {**scope, "approved_by": "yanjun@fuzhi.ai", "expires_at": time.time() + 300}
        verify_approval(approval, scope)
        for key, changed in (("environment", "production"), ("source_sha", "c" * 40), ("script_digest", "d" * 64)):
            with self.assertRaises(RunError):
                verify_approval(approval, {**scope, key: changed})
        with self.assertRaises(RunError):
            verify_approval({**approval, "expires_at": 0}, scope)

    def test_observation_checks_facts_not_a_pass_string(self):
        spec = {"checks": [{"path": "route.source_sha", "op": "eq", "value": "abc"},
                           {"path": "scenarios.timeout.status", "op": "eq", "value": "passed"}]}
        with self.assertRaises(RunError):
            check_observation({"result": "PASS"}, spec)
        check_observation({"route": {"source_sha": "abc"}, "scenarios": {"timeout": {"status": "passed"}}}, spec)
        with self.assertRaises(RunError):
            check_observation({"route": {"source_sha": "old"}, "scenarios": {"timeout": {"status": "passed"}}}, spec)

    def test_no_performance_improvement_without_comparable_measurements(self):
        before = {"metric": "median", "unit": "ms", "method": "same requests", "environment": "test", "target": "lane", "request_digest": "a", "samples": 30, "value": 200}
        after = {**before, "value": 50}
        self.assertEqual(measurement_comparison(before, after)["reduction_percent"], 75)
        self.assertFalse(measurement_comparison(None, after)["verified"])
        self.assertFalse(measurement_comparison(before, {**after, "request_digest": "changed"})["verified"])

    def test_changed_existing_business_acceptance_needs_explicit_approval(self):
        self.make_run()
        self.contract["goal"] = "accept any integer result"
        self.file.write_text(json.dumps(self.contract))
        with self.assertRaisesRegex(RunError, "explicit approval"):
            self.make_run()
        self.assertEqual(self.store.db.execute("select count(*) from runs").fetchone()[0], 1)

    def test_added_check_reuses_only_unchanged_eval_not_old_review(self):
        run = self.make_run()
        fp = fingerprint(Path(run["worktree"]))
        self.store.evidence(run["id"], "eval:sum", "pass", fp, b"original result")
        self.store.evidence(run["id"], "review", "pass", fp, b"old review")
        extra = {**self.contract["tests"][0], "id": "extra"}
        self.contract["tests"].append(extra)
        self.contract["rubric"].append({"id": "extra", "expected": "additional check", "critical": True})
        self.file.write_text(json.dumps(self.contract))
        new = self.make_run()
        self.assertNotEqual(new["id"], run["id"])
        self.assertTrue(self.store.current_evidence(new["id"], "eval:sum", fp))
        self.assertFalse(self.store.current_evidence(new["id"], "review", fp))
        self.assertFalse(self.store.current_evidence(new["id"], "eval:extra", fp))

    def test_cancellation_terminates_worker_and_keeps_nonpassing_result(self):
        import sys
        run = self.make_run()
        self.store.update(run["id"], cancel_requested=True)
        with self.assertRaises(RunError):
            self.store.execute(run["id"], "eval:sum", [sys.executable, "-c", "import time; time.sleep(30)"], timeout=5)
        row = self.store.db.execute("select * from operations where kind='eval:sum'").fetchone()
        self.assertEqual(row["state"], "confirmed")
        self.assertIn("interrupted", json.loads(row["outcome"]))
        with self.assertRaises(ProcessLookupError):
            os.kill(row["pid"], 0)

    def test_dead_restricted_worker_is_reconciled_without_inventing_a_pass(self):
        run = self.make_run()
        op = self.store.begin_operation(run["id"], "builder", {"isolated_source": str(self.root / "disposable")})
        with self.store.db:
            self.store.db.execute("update operations set pid=99999999,state='running' where id=?", (op,))
        with patch('os.kill', side_effect=ProcessLookupError):
            result = resume(self.store, run["id"], reconcile=True)
        self.assertEqual(result["result"], "blocked")
        row = self.store.db.execute("select * from operations where id=?", (op,)).fetchone()
        self.assertEqual(json.loads(row["outcome"])["result"], "unknown")
        self.assertIsNone(json.loads(row["outcome"])["quality_verdict"])

    def test_external_adapter_without_approval_never_executes(self):
        self.contract["operations"] = {"deploy": {"kind": "test_deployment", "environment": "test", "target": "lane",
            "script": "tests/check.py", "argv": ["python3.10", "{script}"], "verify_argv": ["python3.10", "{script}"],
            "checks": [{"path": "route_hit", "op": "eq", "value": True}]}}
        self.file.write_text(json.dumps(self.contract))
        run = self.make_run()
        with patch.object(self.store, 'execute') as execute:
            with self.assertRaisesRegex(RunError, 'approval missing'):
                run_operation(self.store, run["id"], 'deploy')
            execute.assert_not_called()

    def test_interrupted_general_adapter_is_not_confirmed_on_process_exit(self):
        import sys
        run = self.make_run()
        self.store.update(run['id'], cancel_requested=True)
        with self.assertRaises(RunError) as error:
            self.store.execute(run['id'], 'adapter:deploy', [sys.executable,'-c','import time; time.sleep(30)'], timeout=5)
        self.assertEqual(error.exception.result, 'uncertain')
        row = self.store.db.execute("select * from operations where kind='adapter:deploy'").fetchone()
        self.assertEqual(row['state'], 'uncertain')
        self.assertEqual(resume(self.store, run['id'], reconcile=True)['result'], 'uncertain')

    def test_unknown_unstarted_worker_is_not_assumed_dead(self):
        run = self.make_run()
        self.store.begin_operation(run["id"], "builder", {"request": "unknown"})
        self.assertEqual(resume(self.store, run["id"], reconcile=True)["result"], "uncertain")

    def test_unisolated_dead_writer_is_not_reconciled_on_pid_alone(self):
        run = self.make_run()
        op = self.store.begin_operation(run["id"], "builder", {"request": "legacy unsafe workspace"})
        with self.store.db:
            self.store.db.execute("update operations set pid=99999999,state='running' where id=?", (op,))
        with patch('os.kill', side_effect=ProcessLookupError):
            self.assertEqual(resume(self.store, run["id"], reconcile=True)["result"], "uncertain")

    def test_disposable_builder_cannot_change_accepted_source_after_capture(self):
        run = self.make_run()
        canonical = Path(run["worktree"])
        before = fingerprint(canonical)
        isolated = builder_workspace(self.store, run)
        baseline = protected_files(isolated, self.contract)
        (isolated / "calc.py").write_text("def add(a,b): return a+b\n")
        accept_builder_source(self.store, run, isolated, baseline, before)
        accepted = fingerprint(canonical)
        # Simulate a late orphan writer retaining authority to the old path.
        (isolated / "calc.py").write_text("def add(a,b): return 0\n")
        self.assertEqual(fingerprint(canonical), accepted)
        args = worker_args(isolated, Path('/runtime'), self.contract, 'builder', self.root / 'scratch', self.root / 'contract.json')
        rule = [v for v in args if v.startswith('permissions.qh.filesystem=')][-1]
        self.assertNotIn(json.dumps(str(canonical)) + "=", rule)

    def test_runtime_module_or_pth_change_invalidates_identity_without_metadata_change(self):
        runtime = self.root / 'runtime'
        (runtime / 'bin').mkdir(parents=True)
        (runtime / 'bin/python3.10').write_bytes(b'fixture runtime')
        module = runtime / 'module.py'
        module.write_text('value=1')
        with patch('codex_quality_harness.doctor.version_check', return_value={'result':'pass','version':'fixture'}):
            before = environment_identity(runtime, {})
            module.write_text('value=2')
            after = environment_identity(runtime, {})
            self.assertNotEqual(before, after)
            (runtime / 'editable.pth').write_text('/changed')
            self.assertNotEqual(after, environment_identity(runtime, {}))

    def test_ci_or_regression_policy_cannot_be_weakened_without_approval(self):
        self.contract['ci'] = {'require_merge_result': True}
        self.file.write_text(json.dumps(self.contract))
        self.make_run()
        self.contract['ci']['require_merge_result'] = False
        self.file.write_text(json.dumps(self.contract))
        with self.assertRaisesRegex(RunError, 'explicit approval'):
            self.make_run()

    def test_external_operations_wait_for_other_uncertain_effects(self):
        self.contract['operations'] = {'db': {'kind': 'database_write', 'environment': 'test', 'target': 'db-a',
            'script': 'tests/check.py', 'argv': ['python3.10', '{script}'], 'verify_argv': ['python3.10', '{script}'],
            'checks': [{'path':'ok', 'op':'eq', 'value':True}]}}
        self.file.write_text(json.dumps(self.contract))
        run = self.make_run()
        self.store.begin_operation(run['id'], 'external:old-deploy', {'request':'unknown'})
        with patch.object(self.store, 'execute') as execute:
            with self.assertRaisesRegex(RunError, 'unfinished operation'):
                run_operation(self.store, run['id'], 'db')
            execute.assert_not_called()

    def test_deployment_rejects_database_evidence_from_another_target(self):
        import time
        from codex_quality_harness.harness import encoded
        common = {'environment':'test', 'script':'tests/check.py', 'argv':['python3.10','{script}'],
                  'verify_argv':['python3.10','{script}'], 'checks':[{'path':'ok','op':'eq','value':True}]}
        self.contract['operations'] = {'db-a':{**common,'kind':'database_write','target':'database-a'},
            'db-b':{**common,'kind':'database_write','target':'database-b'},
            'deploy':{**common,'kind':'test_deployment','target':'lane','database_operations':['db-b']}}
        self.file.write_text(json.dumps(self.contract))
        run = self.make_run()
        fp = fingerprint(Path(run['worktree']))
        wrong_scope = operation_scope(self.store, run, 'db-a')[1]
        self.store.evidence(run['id'], 'database_write', 'pass', fp, encoded({'scope':wrong_scope}))
        scope = operation_scope(self.store, run, 'deploy')[1]
        approval = {**scope,'approved_by':'yanjun@fuzhi.ai','expires_at':time.time()+300}
        with patch('codex_quality_harness.harness.gate', return_value={'result':'pass'}), patch.object(self.store,'execute') as execute:
            with self.assertRaisesRegex(RunError, 'exact-target/script'):
                run_operation(self.store, run['id'], 'deploy', approval)
            execute.assert_not_called()

    def test_dependency_environment_change_invalidates_existing_review(self):
        run = self.make_run()
        fp = fingerprint(Path(run["worktree"]))
        self.store.update(run["id"], environment_identity="before")
        self.store.evidence(run["id"], 'review', 'pass', fp, b'old review')
        self.store.update(run["id"], environment_identity="after")
        self.assertFalse(self.store.current_evidence(run["id"], 'review', fp))

    def test_known_bad_control_cannot_be_configured_to_succeed(self):
        self.contract['tests'][0]['negative_exit'] = 0
        with self.assertRaises(RunError):
            validate_contract(self.contract)


if __name__ == "__main__":
    unittest.main()
