import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from codex_quality_harness.doctor import aggregate, assess_reports, permission_args, inspect_archive, safe_environment, execute, wait_for_pipeline, run_doctor, exact_probe_command


def archive(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return b.getvalue()


class AcceptanceTests(unittest.TestCase):
    def test_no_vacuous_success(self):
        self.assertEqual(aggregate([]), "blocked")
        self.assertEqual(aggregate([{"result": "pass"}, {"result": "blocked"}]), "blocked")
        self.assertEqual(aggregate([{"result": "pass"}]), "pass")

    def test_summary_or_model_pass_is_not_independent_report(self):
        files = {"app-pipeline-integration-test-result.json": json.dumps({"status": "passed"}),
                 "app-pipeline-integration-coverage.json": json.dumps({"coverage": 100})}
        r = assess_reports("integration_test", "success", inspect_archive(archive(files)), 30)
        self.assertEqual(r["result"], "blocked")
        self.assertIn("JUnit", " ".join(r["blockers"]))

    def test_valid_xml_and_coverage(self):
        files = {"junit.xml": '<testsuites><testsuite tests="1"><testcase name="contract"/></testsuite></testsuites>',
                 "coverage.xml": '<coverage line-rate="0.86" lines-covered="86" lines-valid="100"/>'}
        self.assertEqual(assess_reports("unit_test", "success", inspect_archive(archive(files)), 86)["result"], "pass")

    def test_coverage_uses_counts_not_claimed_percentage(self):
        files = {"junit.xml": '<testsuite><testcase name="c"/></testsuite>',
                 "coverage.xml": '<coverage line-rate="1.0" lines-covered="1" lines-valid="100"/>'}
        self.assertEqual(assess_reports("unit_test", "success", inspect_archive(archive(files)), 86)["result"], "fail")

    def test_skipped_and_failed_tests_do_not_pass(self):
        for body in ['<testcase name="c"><skipped/></testcase>', '<testcase name="c"><failure/></testcase>', '']:
            files = {"junit.xml": '<testsuite>' + body + '</testsuite>',
                     "coverage.xml": '<coverage lines-covered="100" lines-valid="100"/>'}
            self.assertNotEqual(assess_reports("unit_test", "success", inspect_archive(archive(files)), 86)["result"], "pass")

    def test_job_absence_manual_skipped_and_failure_block_or_fail(self):
        for status in [None, "manual", "skipped", "running", "failed", "canceled"]:
            self.assertNotEqual(assess_reports("unit_test", status, {}, 86)["result"], "pass")

    def test_wrong_names_and_duplicate_archives_rejected(self):
        for name in ["../../junit.xml", "/junit.xml", "nested/junit.xml"]:
            self.assertEqual(inspect_archive(archive({name: '<testsuite/>'})), {})
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("junit.xml", "a")
            with self.assertWarns(UserWarning):
                z.writestr("junit.xml", "b")
        with self.assertRaises(ValueError):
            inspect_archive(b.getvalue())

    def test_malformed_untrusted_xml_rejected(self):
        for xml in ['<!DOCTYPE x [<!ENTITY x "hi">]><testsuite/>', '<testsuite>', '<not-junit/>']:
            r = assess_reports("unit_test", "success", {"junit.xml": xml.encode(), "coverage.xml": b'<coverage/>'}, 86)
            self.assertNotEqual(r["result"], "pass")

    def test_declared_suite_counts_cannot_hide_failures_or_deleted_cases(self):
        for attrs in ['tests="2" failures="1"', 'tests="2"', 'errors="1"', 'skipped="1"', 'tests="-1"']:
            xml = ('<testsuite ' + attrs + '><testcase name="c"/></testsuite>').encode()
            r = assess_reports("unit_test", "success", {"junit.xml": xml, "coverage.xml": b'<coverage lines-valid="100" lines-covered="100"/>'}, 86)
            self.assertEqual(r["result"], "blocked")

    def test_nested_suite_totals_checked(self):
        xml = b'<testsuites tests="2"><testsuite tests="1"><testcase name="c"/></testsuite></testsuites>'
        r = assess_reports("unit_test", "success", {"junit.xml": xml, "coverage.xml": b'<coverage lines-valid="100" lines-covered="100"/>'}, 86)
        self.assertEqual(r["result"], "blocked")

    def test_suite_error_outside_testcase_cannot_disappear(self):
        xml = b'<testsuite tests="1"><testcase name="c"/><error message="collection failure"/></testsuite>'
        r = assess_reports("unit_test", "success", {"junit.xml": xml, "coverage.xml": b'<coverage lines-valid="100" lines-covered="100"/>'}, 86)
        self.assertEqual(r["result"], "blocked")

    def test_utf16_dtd_and_unknown_encoding_do_not_escape_json_result(self):
        xmls = ['<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY x "hi">]><testsuite><testcase name="&x;"/></testsuite>'.encode("utf-16"),
                b'<?xml version="1.0" encoding="unknown-encoding"?><testsuite/>']
        for xml in xmls:
            r = assess_reports("unit_test", "success", {"junit.xml": xml, "coverage.xml": b'<coverage/>'}, 86)
            self.assertEqual(r["result"], "blocked")

    def test_reviewer_extends_read_only(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            args = permission_args(p, p / "runtime", "reviewer")
            self.assertIn('permissions.qh.extends=":read-only"', args)
            self.assertNotIn('permissions.qh.extends=":workspace"', args)
            self.assertIn('permissions.qh.network.enabled=false', args)

    def test_no_credentials_inherited_by_test_processes(self):
        with patch.dict("os.environ", {"PROD_PASSWORD": "dummy", "CI_JOB_TOKEN": "dummy", "HOME": "/synthetic"}, clear=True):
            env = safe_environment()
            self.assertNotIn("PROD_PASSWORD", env)
            self.assertNotIn("CI_JOB_TOKEN", env)
            self.assertEqual(env["HOME"], "/synthetic")

    def test_process_budget_terminates_instead_of_waiting_forever(self):
        with patch("codex_quality_harness.doctor.os.killpg", wraps=os.killpg) as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                execute([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.15)
            self.assertEqual(kill.call_count, 1)

    def test_output_budget_enforced_during_process(self):
        with patch("codex_quality_harness.doctor.MAX_ARCHIVE", 1024):
            with self.assertRaises(ValueError):
                execute([sys.executable, "-c", "import os,time; os.write(1,b'x'*2048); time.sleep(10)"], timeout=3)

    def test_ci_wait_reads_only_selected_pipeline_until_terminal(self):
        with patch("codex_quality_harness.doctor.api", side_effect=[{"status": "running"}, {"status": "success"}]) as get:
            with patch("codex_quality_harness.doctor.time.sleep"):
                wait_for_pipeline(1678, 123, 60)
        self.assertEqual(get.call_count, 2)
        self.assertTrue(all(c.args == ("projects/1678/pipelines/123",) for c in get.call_args_list))

    def test_cancellation_is_json_evidence_and_stops_later_calls(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            with patch("codex_quality_harness.doctor.version_check", side_effect=KeyboardInterrupt):
                with patch("codex_quality_harness.doctor.api") as network:
                    r = run_doctor(p, p, p, 1678, 123, True, 60)
                    network.assert_not_called()
            self.assertTrue(r["cancelled"])
            self.assertEqual(r["result"], "blocked")
            self.assertTrue(Path(r["evidence"][0]).is_file())

    def test_model_echo_cannot_replace_exact_probe_execution(self):
        runtime, work = Path('/runtime'), Path('/work')
        good = '/runtime/bin/python3.10 -I /work/probe.py'
        self.assertTrue(exact_probe_command(good, runtime, work))
        self.assertTrue(exact_probe_command("/bin/zsh -c '" + good + "'", runtime, work))
        for bad in ["echo PASS", good + "; echo PASS", "/runtime/bin/python3.10 /work/probe.py", good + " extra"]:
            self.assertFalse(exact_probe_command(bad, runtime, work))


if __name__ == "__main__":
    unittest.main()
