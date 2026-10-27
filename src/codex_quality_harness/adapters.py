"""Controller-owned Git/GitLab operations. No credentials are passed to workers."""

import json
import re
import operator
import time
import shutil
import math
import sys
from pathlib import Path

from . import doctor
from .harness import IDENTITY, RunError, encoded, fingerprint, git, digest, relative

OPERATION_KINDS = {"test_deployment", "test_regression", "test_report", "database_write", "production_observation", "performance", "capacity", "risk", "recovery"}


def verify_approval(approval, scope):
    """Local authorization record, not a cryptographic proof of human intent."""
    if not isinstance(approval, dict) or approval.get("approved_by") != "yanjun@fuzhi.ai":
        raise RunError("explicit user approval is required")
    expires = approval.get("expires_at", 0)
    if not isinstance(expires, (int, float)) or not time.time() < expires <= time.time() + 86400:
        raise RunError("approval expired or exceeds the one-day scope")
    if any(approval.get(key) != value for key, value in scope.items()):
        raise RunError("approval does not match this environment, commit, script and target")


def production_approval(store, run_id, approval=None):
    run = store.run(run_id)
    contract = store.contract(run)
    policy = contract.get("production", {})
    if not isinstance(policy.get("target"), str) or not policy["target"]:
        raise RunError("production readiness needs an explicit target and approved capacity/recovery policy")
    work = Path(run["worktree"])
    if git(work, "status", "--porcelain").strip():
        raise RunError("production readiness approval requires a committed clean version")
    scope = {"run_id": run_id, "kind": "production_ready", "environment": "production",
             "target": policy["target"], "source_sha": git(work, "rev-parse", "HEAD").decode().strip(), "script_digest": digest(encoded(policy))}
    if approval is None:
        return scope
    verify_approval(approval, scope)
    proof = {"scope": scope, "approval": approval}
    store.evidence(run_id, "production_approval", "pass", fingerprint(work), encoded(proof))
    return scope


def measurement_comparison(before, after):
    """No baseline or mismatched workloads means no improvement percentage."""
    keys = ("metric", "unit", "method", "environment", "target", "request_digest")
    if not isinstance(before, dict) or not isinstance(after, dict) or any(not before.get(k) or before.get(k) != after.get(k) for k in keys):
        return {"verified": False, "reason": "missing comparable baseline/workload"}
    for sample in (before, after):
        if type(sample.get("samples")) is not int or sample["samples"] < 1 or type(sample.get("value")) not in (int, float) or not math.isfinite(sample["value"]):
            return {"verified": False, "reason": "measurement value/sample count missing"}
    if before["value"] <= 0:
        return {"verified": False, "reason": "baseline must be positive"}
    return {"verified": True, "metric": before["metric"], "unit": before["unit"], "before": before["value"],
            "after": after["value"], "delta": after["value"] - before["value"],
            "reduction_percent": (before["value"] - after["value"]) * 100 / before["value"],
            "samples_before": before["samples"], "samples_after": after["samples"]}


def select(value, path):
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise RunError("required observed fact is missing: " + path)
        value = value[part]
    return value


def check_observation(observation, spec):
    checks = spec.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 100:
        raise RunError("operation needs predeclared bounded factual checks")
    operations = {"eq": operator.eq, "ne": operator.ne, "lte": operator.le, "gte": operator.ge}
    for check in checks:
        try:
            actual = select(observation, check["path"])
            expected = check["value"]
            if isinstance(expected, bool) and not isinstance(actual, bool):
                raise RunError("observed boolean fact has the wrong type")
            if isinstance(expected, (int, float)) and not isinstance(expected, bool):
                if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual):
                    raise RunError("observed numeric fact is invalid")
            ok = operations[check["op"]](actual, expected)
        except (KeyError, TypeError):
            raise RunError("invalid observation check")
        if not ok:
            raise RunError("observed fact failed: " + check["path"])


def operation_scope(store, run, name):
    contract, work = store.contract(run), Path(run["worktree"])
    spec = contract.get("operations", {}).get(name)
    if not isinstance(spec, dict) or spec.get("kind") not in OPERATION_KINDS:
        raise RunError("operation is not registered in the frozen contract")
    if not isinstance(spec.get("target"), str) or not re.fullmatch(r"[\w.:/-]{1,250}", spec["target"]):
        raise RunError("operation needs an exact target")
    if spec.get("environment") not in {"test", "production", "isolated"}:
        raise RunError("operation environment must be explicit")
    if spec["kind"].startswith("test_") and spec["environment"] != "test":
        raise RunError("TEST authority cannot be used for production")
    path = None
    script_digest = "direct-read-only-gateway"
    if spec["kind"] != "production_observation":
        path = work / relative(spec.get("script", ""))
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(work) or path.stat().st_size > 1024 * 1024:
            raise RunError("adapter script must be a bounded reviewed repository file")
        git(work, "ls-files", "--error-unmatch", spec["script"])
        script_digest = digest(path.read_bytes())
    sha = git(work, "rev-parse", "HEAD").decode().strip()
    if git(work, "status", "--porcelain").strip():
        raise RunError("operational evidence requires a committed clean source version")
    for key in ("argv", "verify_argv"):
        argv = spec.get(key)
        if not isinstance(argv, list) or not argv or len(argv) > 100 or any(not isinstance(a, str) or "\x00" in a for a in argv):
            raise RunError("operation and read-back must use structured argv")
    scope = {"run_id": run["id"], "kind": spec["kind"], "environment": spec["environment"], "source_sha": sha,
             "target": spec["target"], "script_digest": digest(encoded([script_digest, spec]))}
    return spec, scope, path


def run_operation(store, run_id, name, approval=None, reconcile=False, _locked=False):
    """Run an explicitly selected existing adapter, then verify its actual read-back.

    Never invoked by Builder/Reviewer. The operator must review the frozen script,
    argv, target and checks; arbitrary model-produced JSON is not an adapter.
    """
    if not _locked:
        with store.lock(run_id):
            return run_operation(store, run_id, name, approval, reconcile, _locked=True)
    from .harness import assert_protected, gate
    run = store.run(run_id)
    assert_protected(store, run)
    spec, scope, script = operation_scope(store, run, name)
    if not reconcile and store.db.execute("select id from operations where run=? and state!='confirmed' limit 1", (run_id,)).fetchone():
        raise RunError("unfinished operation; reconcile all outstanding effects before another external action", "uncertain")
    fp = fingerprint(Path(run["worktree"]))
    if spec["kind"] == "production_observation":
        # Only the existing observation gateway is allowed for production reads.
        for key in ("argv", "verify_argv"):
            argv = spec[key]
            actual = shutil.which("atoms-obs-prod")
            requested = shutil.which(argv[0])
            leaves = {("snapshot",), ("investigate",), ("db", "chat"), ("vm", "query"), ("jaeger", "trace"), ("jaeger", "services")}
            if not actual or not requested or Path(actual).resolve() != Path(requested).resolve() or not any(tuple(argv[1:1 + len(leaf)]) == leaf for leaf in leaves):
                raise RunError("production observations must use an allowlisted read-only atoms-obs-prod command")
            if "--envelope" not in argv or spec.get("read_only") is not True:
                raise RunError("production observations require a complete audited envelope")
        if spec["environment"] != "production":
            raise RunError("production observation environment mismatch")
    else:
        previous_approval = store.db.execute("select digest from approvals where id=?", (digest(encoded(scope)),)).fetchone()
        if reconcile and previous_approval:
            store.read_blob(previous_approval["digest"])
        elif not approval:
            raise RunError("explicit operation approval missing; no deployment/database write was attempted")
        else:
            verify_approval(approval, scope)
            approval_digest = store.blob(encoded(approval))
            with store.db:
                store.db.execute("insert or ignore into approvals values(?,?,?,?,?)", (
                    digest(encoded(scope)), run_id, encoded(scope).decode(), approval_digest, time.time()))
    if spec["kind"] == "test_deployment":
        ready = gate(store, run_id, "code_ready", _locked=True)
        if ready["result"] != "pass":
            raise RunError("code_ready must pass before TEST deployment")
        changed = git(Path(run["worktree"]), "diff", "--name-only", run["base_sha"], "HEAD").decode().splitlines()
        database_change = any(p.endswith(".sql") or re.search(r"(^|/)(migrate|migrations|dao/orm/model)(/|$)", p) for p in changed)
        required = spec.get("database_operations", [])
        if not isinstance(required, list):
            raise RunError("database_operations must list exact registered dependencies")
        if (database_change or spec.get("startup_migrations")) and not required:
            raise RunError("deployment must declare its exact database_operations before startup")
        # Explicit dependencies always apply, including nonstandard migration
        # paths that conservative automatic detection cannot recognize.
        for dependency in required:
            db_spec, db_scope, _ = operation_scope(store, run, dependency)
            if db_spec["kind"] != "database_write" or db_scope["environment"] != scope["environment"]:
                raise RunError("database dependency has the wrong kind or environment")
            records = store.current_evidence(run_id, "database_write", fp)
            matching = [item for item in records if json.loads(store.read_blob(item["digest"])).get("scope") == db_scope]
            if not matching or any(item["result"] != "pass" for item in matching):
                raise RunError("database/startup migration lacks successful exact-target/script evidence: " + dependency)
    if spec["kind"] in {"test_regression", "test_report"}:
        deployed = store.current_evidence(run_id, "test_deployment", fp)
        if not deployed or any(item["result"] != "pass" for item in deployed):
            raise RunError("actual deployment identity must be verified first")
    substitution = {"{workspace}": run["worktree"], "{script}": str(script), "{sha}": scope["source_sha"], "{target}": scope["target"]}
    def command(key):
        return [substitution.get(arg, arg) for arg in spec[key]]
    request = {"name": name, "scope": scope}
    existing = store.db.execute("select * from operations where run=? and kind=? and request=?", (
        run_id, "external:" + name, encoded(request).decode())).fetchone()
    if existing and existing["state"] == "confirmed" and not reconcile:
        return json.loads(existing["outcome"])
    if existing and not reconcile:
        raise RunError("external result is unresolved; read back before repeating " + existing["id"], "uncertain")
    op = existing["id"] if existing else store.begin_operation(run_id, "external:" + name, request)
    if not reconcile:
        if spec["kind"] != "production_observation":
            sys.stderr.write("线上运行风险提醒：仅执行已批准范围 " + spec["kind"] + "；目标 " + scope["target"] +
                             "，提交 " + scope["source_sha"][:12] + "。异常或结果不明时先回读，不自动重复写入或反向迁移。\n")
        output = store.execute(run_id, "adapter:" + name, command("argv"), timeout=min(spec.get("timeout", 120), 600))
        if output["exit_code"]:
            raise RunError("adapter result unknown; do not replay the operation", "uncertain")
    verification = store.execute(run_id, "readback:" + name, command("verify_argv"), timeout=45)
    if verification["exit_code"]:
        raise RunError("external read-back failed; operation remains unresolved", "uncertain")
    observation = json.loads(store.read_blob(verification["stdout"]))
    if spec["kind"] == "production_observation":
        if observation.get("ok") is not True or observation.get("complete") is not True or observation.get("env") != "prod":
            raise RunError("production observation is incomplete, unaudited or from the wrong environment")
        if select(observation, spec.get("source_path", "data.source_sha")) != scope["source_sha"]:
            raise RunError("production observation does not prove the specified deployed version")
    elif observation.get("source_sha") != scope["source_sha"] or observation.get("target") != scope["target"] or observation.get("environment") != scope["environment"]:
        raise RunError("observed deployment/environment/source identity mismatch", "uncertain")
    try:
        check_observation(observation, spec)
    except RunError:
        scenarios = observation.get("scenarios", {})
        failed = isinstance(scenarios, dict) and any(isinstance(s, dict) and s.get("status") == "failed" for s in scenarios.values())
        store.evidence(run_id, spec["kind"], "fail" if failed else "blocked", fp,
                       encoded({"scope": scope, "adapter": verification, "observation": observation}))
        raise
    captured_at = observation.get("captured_at")
    if not isinstance(captured_at, (int, float)) or not 0 <= time.time() - captured_at <= min(spec.get("freshness_seconds", 600), 3600):
        raise RunError("observation freshness is not proven")
    if spec["kind"] == "test_deployment":
        for field in ("lane_id", "config_selector"):
            if not observation.get(field):
                raise RunError("missing actual deployment fact: " + field)
        if observation.get("route_hit") is not True:
            raise RunError("actual route hit is not confirmed")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", observation.get("image_digest", "")):
            raise RunError("actual image digest is required")
    if spec["kind"] == "test_regression":
        scenarios = spec.get("required_scenarios", [])
        if not scenarios or any(select(observation, "scenarios." + item + ".status") != "passed" for item in scenarios):
            raise RunError("all predeclared business/fault/recovery scenarios must pass")
        if any(not isinstance(observation.get(key), list) or not observation[key] for key in ("screenshots", "requests")):
            raise RunError("regression must retain screenshots and request evidence")
        observation["comparison"] = measurement_comparison(observation.get("before"), observation.get("after"))
    if spec["kind"] == "test_report":
        if not observation.get("document_url") or observation.get("text_saved") is not True or observation.get("image_refs_verified") is not True:
            raise RunError("Feishu report text/images must be read back")
    if fingerprint(Path(run["worktree"])) != fp:
        raise RunError("source changed during external operation", "uncertain")
    proof = {"scope": scope, "adapter": verification, "observation": observation}
    store.evidence(run_id, spec["kind"], "pass", fp, encoded(proof))
    store.finish_operation(op, proof)
    return proof


def efficiency_report(store, run):
    operations = store.db.execute("select kind,state,outcome from operations where run=?", (run["id"],)).fetchall()
    times = {}
    for operation in operations:
        if operation["state"] == "confirmed" and operation["outcome"]:
            value = json.loads(operation["outcome"])
            if isinstance(value.get("seconds"), (int, float)):
                times[operation["kind"]] = times.get(operation["kind"], 0) + value["seconds"]
    return {"seconds_by_operation": times, "development_seconds": run["data"].get("seconds", 0),
            "repair_rounds": run["data"].get("rounds", 0), "model_tokens": run["data"].get("tokens", 0),
            "unresolved_operations": sum(row["state"] != "confirmed" for row in operations),
            "money_cost": None, "improvement_percent": None,
            "limits": "No pricing or historical delivery baseline inferred; local/tool time is not production latency."}


def reconcile_operations(store, run):
    """Explicit recovery: observe an existing effect, never repeat its mutation."""
    from .harness import assert_protected, protected_files
    import os
    rows = store.db.execute("select * from operations where run=? and state!='confirmed' order by created", (run["id"],)).fetchall()
    work = Path(run["worktree"])
    for row in rows:
        if row["pid"]:
            try:
                os.kill(row["pid"], 0)
                continue
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
        request = json.loads(row["request"])
        kind, proof = row["kind"], None
        if kind == "worktree" and work.is_dir():
            if (git(work, "rev-parse", "HEAD").decode().strip() == request["base"]
                    and git(work, "symbolic-ref", "--short", "HEAD").decode().strip() == request["branch"]
                    and not git(work, "status", "--porcelain").strip()):
                store.update(run["id"], protected=protected_files(work, store.contract(run)), source=fingerprint(work))
                proof = {"reconciled": "existing clean worktree", "head": request["base"]}
        elif kind == "builder_workspace":
            isolated = Path(request["path"])
            if isolated.is_dir() and git(isolated, "rev-parse", "HEAD").decode().strip() == request["head"]:
                # Never resume a writer in this disposable directory.
                proof = {"reconciled": "disposable Builder path abandoned", "path": str(isolated), "result": "unknown"}
        elif kind == "push":
            remote = git(work, "ls-remote", "--heads", request["remote"], "refs/heads/" + request["branch"]).decode().split()
            if remote and remote[0] == request["sha"]:
                proof = {"reconciled": "remote branch read-back", "sha": remote[0]}
        elif kind == "commit":
            if fingerprint(work) == request["fingerprint"] and ("[" + run["id"] + "]") in git(work, "log", "-1", "--format=%B").decode():
                proof = {"reconciled": "local commit", "sha": git(work, "rev-parse", "HEAD").decode().strip()}
        elif kind == "create_mr":
            from urllib.parse import urlencode
            matches = doctor.api("projects/" + str(request["project"]) + "/merge_requests?" + urlencode({
                "source_branch": request["source_branch"], "target_branch": request["target_branch"], "state": "opened"}))
            if len(matches) == 1:
                proof = {"reconciled": "existing MR", "iid": matches[0]["iid"], "url": matches[0]["web_url"]}
        elif kind == "cr_comment":
            notes = doctor.api_pages("projects/" + str(request["project"]) + "/merge_requests/" + str(request["iid"]) + "/notes")
            marker = "<!-- qh-cr:" + run["id"] + ":" + request["sha"] + " -->"
            found = [note for note in notes if marker in note.get("body", "")]
            if len(found) == 1:
                proof = {"reconciled": "CR read-back", "note_id": found[0]["id"]}
        elif kind.startswith("external:"):
            try:
                run_operation(store, run["id"], request["name"], reconcile=True, _locked=True)
            except RunError:
                continue
            proof = {"reconciled": "registered adapter read-back"}
        elif row["pid"] and (kind == "reviewer" or kind.startswith(("eval:", "negative:"))
                               or (kind == "builder" and request.get("isolated_source")
                                   and Path(request["isolated_source"]) != work)):
            # Reviewer/Eval never write accepted source. A Builder only writes
            # its abandoned disposable path, never the tree a new round uses.
            # Generic adapters have no such isolation: only exact read-back can
            # reconcile their effects, even if their top-level PID disappeared.
            assert_protected(store, store.run(run["id"]))
            proof = {"reconciled": "worker no longer alive; current source captured", "result": "unknown",
                     "source": fingerprint(work), "quality_verdict": None}
        if proof is not None:
            store.finish_operation(row["id"], proof)


def verify_identity(repo):
    for role in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if not git(repo, "var", role).decode().startswith(IDENTITY + " "):
            raise RunError(role + " must resolve to " + IDENTITY)
    if git(repo, "config", "--get", "user.useConfigOnly").decode().strip() != "true":
        raise RunError("user.useConfigOnly must remain true")


def delivery_settings(contract):
    value = contract.get("delivery", {})
    if not isinstance(value.get("gitlab_project"), int) or value["gitlab_project"] <= 0:
        raise RunError("delivery requires a verified GitLab project ID")
    for key in ("remote", "target"):
        if not re.fullmatch(r"[\w./-]+", value.get(key, "")) or value[key].startswith("-"):
            raise RunError("delivery requires an existing remote and explicit target branch")
    return value


def publish_draft(store, run):
    work, contract = Path(run["worktree"]), store.contract(run)
    settings = delivery_settings(contract)
    fp = fingerprint(work)
    reviews = store.current_evidence(run["id"], "review", fp)
    if not reviews or any(item["result"] != "pass" for item in reviews):
        raise RunError("publish requires a valid final independent CR")
    verify_identity(work)
    remote = git(work, "remote", "get-url", "--push", settings["remote"]).decode().strip()
    from urllib.parse import urlparse
    parsed = urlparse(remote)
    if parsed.scheme:
        host, remote_path = parsed.hostname, parsed.path
        if parsed.scheme not in {"https", "ssh"} or parsed.password:
            raise RunError("remote must use supported credential auth without an embedded token")
    else:
        match = re.fullmatch(r"(?:[^@/]+@)?([^:/]+):(.+)", remote)
        host, remote_path = (match.group(1), match.group(2)) if match else (None, None)
    if host != doctor.HOST:
        raise RunError("GitLab publication target does not match the configured host")
    project = doctor.api("projects/" + str(settings["gitlab_project"]))
    if remote_path.strip("/").removesuffix(".git") != project["path_with_namespace"]:
        raise RunError("push remote does not match the authorized GitLab project")
    git(work, "rev-parse", "--verify", settings["target"] + "^{commit}")
    existing = run["data"].get("publication")
    if existing:
        if existing.get("fingerprint") != fp:
            raise RunError("published source changed; prepare a new version before publishing again")
        return existing
    if git(work, "status", "--porcelain").strip():
        op = store.begin_operation(run["id"], "commit", {"fingerprint": fp})
        git(work, "add", "--all")
        verify_identity(work)
        git(work, "commit", "-m", "fix: " + run["task"] + " [" + run["id"] + "]")
        sha = git(work, "rev-parse", "HEAD").decode().strip()
        store.finish_operation(op, {"sha": sha})
    sha = git(work, "rev-parse", "HEAD").decode().strip()
    # A hook or another local writer must not make unreviewed bytes publishable.
    if fingerprint(work) != fp or git(work, "status", "--porcelain").strip():
        raise RunError("source changed during commit; final CR is no longer valid")
    before = git(work, "ls-remote", "--heads", settings["remote"], "refs/heads/" + run["branch"]).decode().split()
    op = None
    if not before or before[0] != sha:
        op = store.begin_operation(run["id"], "push", {"remote": settings["remote"], "branch": run["branch"], "sha": sha})
        git(work, "push", settings["remote"], "HEAD:refs/heads/" + run["branch"], timeout=120)
    actual = git(work, "ls-remote", "--heads", settings["remote"], "refs/heads/" + run["branch"]).decode().split()
    if not actual or actual[0] != sha:
        raise RunError("remote push result is not confirmed", "uncertain")
    if op:
        store.finish_operation(op, {"sha": sha})
    base = "projects/" + str(settings["gitlab_project"])
    from urllib.parse import urlencode
    matches = doctor.api(base + "/merge_requests?" + urlencode({"source_branch": run["branch"], "target_branch": settings["target"], "state": "opened"}))
    if len(matches) > 1:
        raise RunError("ambiguous MR publication", "uncertain")
    if matches:
        mr = matches[0]
    else:
        request = {"source_branch": run["branch"], "target_branch": settings["target"],
                   "title": "Draft: " + run["task"] + " — " + contract["goal"][:120],
                   "description": "Quality Harness 本机独立 CR 完成。该 Draft MR 不代表 CI、TEST 或生产验收通过。\n\nRun: " + run["id"]}
        op = store.begin_operation(run["id"], "create_mr", {"project": settings["gitlab_project"], **request})
        path = store.root / "runs" / run["id"] / "mr-request.json"
        path.write_bytes(encoded(request))
        output = doctor.execute(["glab", "api", "--hostname", doctor.HOST, "--method", "POST", base + "/merge_requests", "--input", str(path)])
        if output["exit_code"]:
            raise RunError("MR creation result unknown; reconcile before retry", "uncertain")
        mr = json.loads(output["stdout"])
        store.finish_operation(op, {"iid": mr["iid"], "url": mr["web_url"]})
    if fingerprint(work) != fp or git(work, "status", "--porcelain").strip():
        raise RunError("source changed during publication; CR comment withheld", "uncertain")
    marker = "<!-- qh-cr:" + run["id"] + ":" + sha + " -->"
    notes = doctor.api_pages(base + "/merge_requests/" + str(mr["iid"]) + "/notes")
    if not any(marker in note.get("body", "") for note in notes):
        latest = doctor.api(base + "/merge_requests/" + str(mr["iid"]))
        if latest.get("sha") != sha:
            raise RunError("MR source changed before CR publication", "uncertain")
        review = json.loads(store.read_blob(reviews[-1]["digest"]))
        body = marker + "\n## 本机代码审查\n\n" + review.get("result", {}).get("summary", "见 Harness 证据")
        body += "\n\n本机 CR 绑定提交 `" + sha + "`。CI、TEST 与生产门禁另行核验；本评论不是平台审批。"
        op = store.begin_operation(run["id"], "cr_comment", {"project": settings["gitlab_project"], "iid": mr["iid"], "sha": sha})
        path = store.root / "runs" / run["id"] / "cr-request.json"
        path.write_bytes(encoded({"body": body}))
        output = doctor.execute(["glab", "api", "--hostname", doctor.HOST, "--method", "POST",
                                 base + "/merge_requests/" + str(mr["iid"]) + "/notes", "--input", str(path)])
        if output["exit_code"]:
            raise RunError("CR publication unknown; reconcile before retry", "uncertain")
        note = json.loads(output["stdout"])
        saved = doctor.api(base + "/merge_requests/" + str(mr["iid"]) + "/notes/" + str(note["id"]))
        if saved.get("body") != body:
            raise RunError("CR read-back mismatch", "uncertain")
        store.finish_operation(op, {"note_id": note["id"]})
    final = doctor.api(base + "/merge_requests/" + str(mr["iid"]))
    if final.get("sha") != sha:
        raise RunError("MR changed during publication", "uncertain")
    publication = {"project": settings["gitlab_project"], "iid": mr["iid"], "url": mr["web_url"], "sha": sha, "fingerprint": fp}
    store.update(run["id"], publication=publication)
    store.evidence(run["id"], "publication", "pass", fp, encoded(publication))
    return publication


def check_gitlab(store, run):
    """A model-written PASS never substitutes for the current MR and raw CI reports."""
    contract = store.contract(run)
    settings = delivery_settings(contract)
    publication, pipeline = run["data"].get("publication"), run["data"].get("pipeline")
    if not publication or not pipeline:
        return ["current MR publication and pipeline ID are required"]
    base = "projects/" + str(settings["gitlab_project"])
    mr = doctor.api(base + "/merge_requests/" + str(publication["iid"]))
    head = git(Path(run["worktree"]), "rev-parse", "HEAD").decode().strip()
    target = doctor.api(base + "/repository/branches/" + settings["target"].replace("/", "%2F"))
    actual_pipeline = doctor.api(base + "/pipelines/" + str(pipeline))
    blockers = []
    if mr.get("sha") != head or publication["sha"] != head:
        blockers.append("MR does not reference the current source commit")
    if mr.get("target_branch") != settings["target"] or actual_pipeline.get("status") != "success":
        blockers.append("target identity or required pipeline status is not accepted")
    head_pipeline = mr.get("head_pipeline") or {}
    if head_pipeline.get("id") != pipeline:
        blockers.append("pipeline is not the MR's current head pipeline")
    target_sha = target["commit"]["id"]
    previous = run["data"].get("ci_identity") or {}
    if previous.get("pipeline") == pipeline and previous.get("target") != target_sha:
        blockers.append("target branch advanced; previous merge validation is stale")
    if contract.get("ci", {}).get("require_merge_result", True):
        # A normal branch/MR pipeline does not prove a merged result.
        merged = doctor.api(base + "/repository/commits/" + actual_pipeline["sha"])
        parents = merged.get("parent_ids", [])
        if actual_pipeline.get("source") != "merge_request_event" or head not in parents or target_sha not in parents or len(parents) != 2:
            blockers.append("native merged-result pipeline identity is not proven; temporary merge is supplemental only")
    elif actual_pipeline.get("sha") != head:
        blockers.append("source pipeline commit does not match the reviewed code")
    report_dir = store.root / "runs" / run["id"]
    if settings["gitlab_project"] == 1678:
        report = doctor.backend_check(settings["gitlab_project"], pipeline, report_dir)
    else:
        required = contract.get("ci", {}).get("required_jobs", [])
        if not required or not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            raise RunError("risk policy must predeclare required CI Job names")
        jobs = doctor.api_pages(base + "/pipelines/" + str(pipeline) + "/jobs")
        checks = []
        for name in required:
            matches = [job for job in jobs if job.get("name") == name]
            if len(matches) != 1 or matches[0].get("status") != "success":
                checks.append({"name": name, "result": "blocked"})
                continue
            job = matches[0]
            raw = doctor.api(base + "/jobs/" + str(job["id"]) + "/artifacts", binary=True)
            files = doctor.inspect_archive(raw)
            try:
                cases, failures, errors, skipped = doctor.junit_counts(doctor.parse_xml(files["junit.xml"], {"testsuite", "testsuites"}))
                ok = bool(cases) and not (failures or errors or skipped)
                ok = ok and job.get("pipeline", {}).get("sha") == actual_pipeline["sha"]
                checks.append({"name": name, "result": "pass" if ok else "fail", "job_id": job["id"], "artifact_digest": digest(raw), "junit_digest": store.blob(files["junit.xml"])})
            except (ValueError, KeyError):
                checks.append({"name": name, "result": "blocked"})
        report = {"result": doctor.aggregate(checks), "jobs": checks}
    if report["result"] != "pass":
        blockers.append("required unit/integration Job reports or coverage failed")
    for check in report.get("jobs", []) if settings["gitlab_project"] == 1678 else []:
        name = check.get("name")
        expected = contract.get("ci", {}).get("unit_min", 86) if name == "unit_test" else contract.get("ci", {}).get("integration_min", 30)
        if check.get("coverage_percent", -1) < expected:
            blockers.append("coverage below risk policy: " + str(name))
    report.update(target_sha=target_sha, mr_iid=mr["iid"], pipeline=actual_pipeline)
    fp = fingerprint(Path(run["worktree"]))
    store.evidence(run["id"], "ci", "blocked" if blockers else "pass", fp, encoded(report))
    if not previous or previous.get("pipeline") != pipeline:
        store.update(run["id"], ci_identity={"pipeline": pipeline, "target": target_sha})
    return blockers
