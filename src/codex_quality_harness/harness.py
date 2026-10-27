"""Local Controller state and development loop. Workers never write this state."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
import tempfile
import shutil
from pathlib import Path

from . import doctor

STAGES = ("draft_mr", "code_ready", "test_accepted", "production_ready")
KINDS = {"contract", "developer", "independent"}
IDENTITY = "严骏 <yanjun@fuzhi.ai>"
MAX_SOURCE = 256 * 1024 * 1024


class RunError(Exception):
    def __init__(self, message, result="blocked"):
        super().__init__(message)
        self.result = result


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def runtime_paths(contract):
    values = []
    for raw in contract.get("runtime_read_paths", []):
        path = Path(raw).expanduser().resolve()
        if not path.is_dir() or not (path / "pyvenv.cfg").is_file() or not (path / "lib/python3.10/site-packages").is_dir():
            raise RunError("extra runtime reads must be explicit Python 3.10 virtual environments")
        values.append(path)
    return values


def environment_identity(runtime, contract):
    executable = Path(runtime) / "bin/python3.10"
    if not executable.is_file():
        raise RunError("Python 3.10 runtime executable is missing")
    # Hash imported/runtime bytes, not only package versions: editable code,
    # .pth and cached bytecode can change behavior without changing METADATA.
    roots = [Path(runtime).resolve(), *runtime_paths(contract)]
    dependencies, count, total = [], 0, 0
    for root in roots:
        records = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                target = path.resolve()
                if not any(target.is_relative_to(allowed) for allowed in roots):
                    raise RunError("runtime symlink escapes the approved runtime")
                records.append([str(path.relative_to(root)), "link", str(target)])
                continue
            if not path.is_file():
                continue
            count += 1
            size = path.stat().st_size
            total += size
            if count > 100000 or size > 128 * 1024 * 1024 or total > 2 * 1024 ** 3:
                raise RunError("runtime content fingerprint budget exceeded")
            records.append([str(path.relative_to(root)), bool(path.stat().st_mode & 0o111), digest(path.read_bytes())])
        dependencies.append([str(root), records])
    version = doctor.version_check()
    if version["result"] != "pass":
        raise RunError("Codex CLI version is not available")
    return digest(encoded([dependencies, version["version"]]))


def rules_digest():
    root = Path(__file__).parent
    return digest(b"".join((root / name).read_bytes() for name in ("harness.py", "doctor.py", "adapters.py", "cli.py")))


def git(repo, *args, timeout=45):
    data = doctor.execute(["git", "-C", str(repo), *args], timeout=timeout)
    if data["exit_code"]:
        raise RunError("Git operation failed: " + args[0])
    return data["stdout"]


def relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RunError("invalid repository-relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise RunError("path must stay inside its declared workspace")
    return path


def fingerprint(repo, depth=0):
    """Hash bytes and executable bits, including untracked source and submodules."""
    if depth > 5:
        raise RunError("submodule depth exceeds fingerprint budget")
    paths = sorted(set(git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")) - {b""})
    if len(paths) > 20000:
        raise RunError("source file-count budget exceeded")
    records, total = [], 0
    for raw in paths:
        name = os.fsdecode(raw)
        path = repo / relative(name)
        if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
            raise RunError("source symlink must be resolved explicitly: " + name)
        if not path.exists():
            records.append([name, "deleted"])
        elif path.is_dir():
            records.append([name, "submodule", fingerprint(path, depth + 1)])
        else:
            size = path.stat().st_size
            total += size
            if size > 32 * 1024 * 1024 or total > MAX_SOURCE:
                raise RunError("source byte budget exceeded")
            records.append([name, bool(path.stat().st_mode & 0o111), digest(path.read_bytes())])
    return digest(encoded(records))


def validate_contract(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise RunError("contract version must be 1")
    if not isinstance(value.get("goal"), str) or not value["goal"].strip():
        raise RunError("contract goal is required")
    architecture = value.get("architecture", {})
    if not isinstance(architecture, dict) or any(not architecture.get(k) for k in ("scope", "reuse", "data_model", "performance")):
        raise RunError("architecture must define scope, reuse, data_model and performance")
    if not isinstance(architecture["scope"], list) or not architecture["scope"]:
        raise RunError("architecture scope must enumerate authorized repository paths")
    for path in architecture["scope"]:
        relative(path)
    rubric, tests = value.get("rubric"), value.get("tests")
    if not isinstance(rubric, list) or not rubric or not isinstance(tests, list) or not tests or len(tests) > 100:
        raise RunError("nonempty rubric and bounded tests are required")
    if value.get("risk") not in {"low", "medium", "high"}:
        raise RunError("risk must be low, medium or high")
    ids = set()
    for test in tests:
        if not isinstance(test, dict) or not re.fullmatch(r"[\w.-]{1,80}", test.get("id", "")) or test["id"] in ids:
            raise RunError("test identifiers must be unique")
        ids.add(test["id"])
        if test.get("kind") not in KINDS:
            raise RunError("test kind must be contract, developer or independent")
        if test.get("negative_argv") and (type(test.get("negative_exit", 1)) is not int or not 1 <= test.get("negative_exit", 1) <= 125):
            raise RunError("known-bad negative control must fail with a nonzero ordinary exit")
        for key in ("argv", "negative_argv"):
            if key not in test and key == "negative_argv":
                continue
            argv = test.get(key)
            if not isinstance(argv, list) or not argv or len(argv) > 200 or any(not isinstance(a, str) or "\x00" in a for a in argv):
                raise RunError("test commands must be argv arrays, never shell interpolation")
        if test["kind"] != "developer" and not test.get("inputs"):
            raise RunError("protected Eval must declare tests, fixtures, mocks and selection inputs")
        for path in test.get("inputs", []):
            relative(path)
        for report in ("junit", "coverage"):
            if test.get(report):
                relative(test[report])
        minimum = test.get("coverage_min")
        if minimum is not None and (not isinstance(minimum, (int, float)) or not 0 <= minimum <= 100 or not test.get("coverage")):
            raise RunError("coverage gate requires an XML path and a 0..100 minimum")
    for item in rubric:
        if not isinstance(item, dict) or not item.get("expected") or item.get("id") not in ids or not isinstance(item.get("critical"), bool):
            raise RunError("each rubric must reference an Eval and define its expectation and critical flag")
        test = next(t for t in tests if t["id"] == item["id"])
        if item["critical"] and (test["kind"] == "developer" or not test.get("negative_argv")):
            raise RunError("critical Eval needs a protected known-bad negative control")
    budget = value.get("budget", {})
    if not isinstance(budget, dict) or not 1 <= budget.get("rounds", 3) <= 20 or not 1 <= budget.get("seconds", 1800) <= 86400:
        raise RunError("invalid development budget")
    for path in value.get("developer_test_paths", []) + value.get("protected_paths", []):
        relative(path)
    protected = [relative(p) for test in tests if test["kind"] != "developer" for p in test.get("inputs", [])]
    for raw in value.get("developer_test_paths", []):
        path = relative(raw)
        if any(path == p or path in p.parents or p in path.parents for p in protected):
            raise RunError("developer tests cannot overlap protected Eval inputs")
    for raw in architecture["scope"]:
        path = relative(raw)
        for fixed in (".git", ".gitignore", ".gitmodules", ".gitlab-ci.yml", ".gitlab", ".pre-commit-config.yaml", "tests", "quality/contracts", "pytest.ini", "pyproject.toml", ".mise.toml", ".coveragerc", "requirements.txt", "requirements-dev.txt"):
            fixed = Path(fixed)
            if path == fixed or path in fixed.parents or fixed in path.parents:
                raise RunError("business scope cannot authorize protected infrastructure")
        if any(path == p or path in p.parents or p in path.parents for p in protected):
            raise RunError("business scope overlaps protected Eval inputs")
    if value.get("delivery", {}).get("gitlab_project") == 1678:
        ci = value.get("ci", {})
        if ci.get("unit_min", 86) < 86 or ci.get("integration_min", 30) < 30:
            raise RunError("Backend coverage thresholds cannot be lowered")
    return value


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        for name in ("blobs", "runs", "worktrees"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=5)
        os.chmod(self.root / "state.sqlite", 0o600)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("pragma user_version").fetchone()[0] not in (0, 1):
            self.db.close()
            raise RunError("unsupported Controller state schema version")
        self.db.execute("pragma journal_mode=WAL")
        self.db.execute("pragma foreign_keys=ON")
        self.db.executescript("""
            create table if not exists runs (
                id text primary key, repo text not null, task text not null,
                contract_digest text not null, base_sha text not null, worktree text not null,
                branch text not null, lifecycle text not null, stage text not null,
                created real not null, data text not null,
                unique(repo, task, contract_digest, base_sha)
            );
            create table if not exists evidence (
                id integer primary key, run text not null references runs(id), kind text not null,
                result text not null, fingerprint text not null, rules text not null,
                digest text not null, created real not null
            );
            create table if not exists operations (
                id text primary key, run text not null references runs(id), kind text not null,
                request text not null, state text not null, pid integer, outcome text,
                created real not null, unique(run, kind, request)
            );
            create table if not exists approvals (
                id text primary key, run text not null references runs(id), scope text not null,
                digest text not null, created real not null
            );
        """)
        self.db.commit()
        self.db.execute("pragma user_version=1")

    def close(self):
        self.db.close()

    def backup(self, target):
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with sqlite3.connect(target) as destination:
            self.db.backup(destination)

    def blob(self, content):
        if len(content) > doctor.MAX_ARCHIVE:
            raise RunError("evidence exceeds byte budget")
        key = digest(content)
        path = self.root / "blobs" / key
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            self.read_blob(key)
        else:
            with os.fdopen(fd, "wb") as out:
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
        return key

    def read_blob(self, key):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise RunError("invalid evidence reference")
        path = self.root / "blobs" / key
        if path.is_symlink() or not path.is_file() or path.stat().st_size > doctor.MAX_ARCHIVE:
            raise RunError("missing or invalid evidence blob")
        data = path.read_bytes()
        if digest(data) != key:
            raise RunError("evidence digest mismatch")
        return data

    def run(self, run_id):
        row = self.db.execute("select * from runs where id=?", (run_id,)).fetchone()
        if row is None:
            raise RunError("run not found")
        run = dict(row)
        run["data"] = json.loads(run["data"])
        return run

    def contract(self, run):
        return validate_contract(json.loads(self.read_blob(run["contract_digest"])))

    def update(self, run_id, *, lifecycle=None, stage=None, **data):
        # Cancellation can arrive from another CLI while a worker is running.
        # Serialize the read/merge/write so unrelated updates cannot erase it.
        with self.db:
            self.db.execute("begin immediate")
            run = self.run(run_id)
            run["data"].update(data)
            self.db.execute("update runs set lifecycle=?,stage=?,data=? where id=?", (
                lifecycle or run["lifecycle"], stage or run["stage"], encoded(run["data"]).decode(), run_id))

    def evidence(self, run_id, kind, result, fp, content):
        if result not in {"pass", "fail", "blocked", "skipped", "not_applicable"}:
            raise RunError("invalid verification result")
        key = self.blob(content)
        with self.db:
            cursor = self.db.execute("insert into evidence(run,kind,result,fingerprint,rules,digest,created) values(?,?,?,?,?,?,?)",
                                     (run_id, kind, result, fp, self.evidence_rules(run_id), key, time.time()))
        return {"id": cursor.lastrowid, "kind": kind, "result": result, "digest": key}

    def evidence_rules(self, run_id, environment=None):
        current = environment or self.run(run_id)["data"].get("environment_identity")
        return digest(encoded([rules_digest(), current])) if current else rules_digest()

    def current_evidence(self, run_id, kind, fp):
        rows = self.db.execute("select * from evidence where run=? and kind=? and fingerprint=? and rules=? order by id",
                               (run_id, kind, fp, self.evidence_rules(run_id))).fetchall()
        results = [dict(row) for row in rows]
        for item in results:
            self.read_blob(item["digest"])
        return results

    @contextlib.contextmanager
    def lock(self, run_id):
        self.run(run_id)
        root = self.root / "runs" / run_id
        root.mkdir(mode=0o700, exist_ok=True)
        with (root / "writer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunError("worktree already has a live Controller writer")
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def begin_operation(self, run_id, kind, request):
        payload = encoded(request).decode()
        existing = self.db.execute("select * from operations where run=? and kind=? and request=?", (run_id, kind, payload)).fetchone()
        if existing:
            raise RunError("operation already recorded; reconcile " + existing["id"], "uncertain")
        op_id = secrets.token_hex(12)
        with self.db:
            self.db.execute("insert into operations(id,run,kind,request,state,created) values(?,?,?,?,?,?)",
                            (op_id, run_id, kind, payload, "intent", time.time()))
        return op_id

    @contextlib.contextmanager
    def model_slot(self):
        handles = []
        try:
            for number in range(2):
                handle = (self.root / ("model-slot-" + str(number))).open("a")
                handles.append(handle)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                pending = self.db.execute("select count(*) from operations where kind in ('builder','reviewer') and state!='confirmed'").fetchone()[0]
                if pending >= 2:
                    raise RunError("two model operations active or unresolved; reconcile before dispatch")
                yield
                return
            raise RunError("model concurrency limit reached")
        finally:
            for handle in handles:
                handle.close()

    def finish_operation(self, op_id, outcome):
        with self.db:
            self.db.execute("update operations set state='confirmed',outcome=? where id=?", (encoded(outcome).decode(), op_id))

    def execute(self, run_id, kind, argv, *, stdin=None, env=None, timeout=45, isolated_source=None):
        request = {"argv": argv, "input": digest((stdin or "").encode()), "nonce": secrets.token_hex(4)}
        if isolated_source is not None:
            request["isolated_source"] = str(isolated_source)
        op = self.begin_operation(run_id, kind, request)
        launched = False
        def started(pid):
            nonlocal launched
            launched = True
            with self.db:
                self.db.execute("update operations set state='running',pid=? where id=?", (pid, op))
        try:
            value = doctor.execute(argv, stdin=stdin, env=env, timeout=timeout, on_start=started,
                                   should_cancel=lambda: self.run(run_id)["data"].get("cancel_requested", False))
        except (KeyboardInterrupt, subprocess.TimeoutExpired, ValueError) as error:
            outcome = {"interrupted": type(error).__name__, "evidence": getattr(error, "evidence_files", [])}
            restricted = kind == "reviewer" or kind.startswith(("eval:", "negative:")) or (kind == "builder" and isolated_source is not None)
            if restricted:
                self.finish_operation(op, outcome)
            else:
                # A general Adapter can retain external authority in a detached
                # descendant. Neither cancellation nor a read-back proves it dead.
                with self.db:
                    self.db.execute("update operations set state='uncertain',outcome=? where id=?", (encoded(outcome).decode(), op))
            raise RunError("worker interrupted: " + type(error).__name__, "blocked" if restricted else "uncertain")
        except OSError as error:
            if not launched:
                self.finish_operation(op, {"not_started": type(error).__name__})
            raise RunError("worker execution error; inspect operation " + op, "uncertain" if launched else "blocked")
        stdout, stderr = self.blob(value["stdout"]), self.blob(value["stderr"])
        result = {"exit_code": value["exit_code"], "stdout": stdout, "stderr": stderr, "seconds": value["elapsed_seconds"]}
        self.finish_operation(op, result)
        return result


def prepare(store, repo, task, contract_file, approval=None):
    repo = Path(repo).resolve()
    if store.root.is_relative_to(repo) or repo.is_relative_to(store.root):
        raise RunError("Controller state and source repository must be separate")
    if not re.fullmatch(r"[\w.-]{1,100}", task):
        raise RunError("invalid task identifier")
    content = Path(contract_file).read_bytes()
    if len(content) > 1024 * 1024:
        raise RunError("contract exceeds byte budget")
    contract = validate_contract(json.loads(content))
    base = git(repo, "rev-parse", "--verify", contract.get("base_ref", "HEAD") + "^{commit}").decode().strip()
    contract_digest = store.blob(encoded(contract))
    old = store.db.execute("select id from runs where repo=? and task=? and contract_digest=? and base_sha=?",
                           (str(repo), task, contract_digest, base)).fetchone()
    if old:
        existing = store.run(old["id"])
        if not Path(existing["worktree"]).is_dir() or "protected" not in existing["data"]:
            raise RunError("previous preparation outcome is unresolved; use qh resume --reconcile", "uncertain")
        return existing
    previous = store.db.execute("select id from runs where repo=? and task=? order by created desc limit 1", (str(repo), task)).fetchone()
    if previous:
        previous_run = store.run(previous["id"])
        previous_contract = store.contract(previous_run)
        old_tests = {t["id"]: t for t in previous_contract["tests"]}
        new_tests = {t["id"]: t for t in contract["tests"]}
        changed_standard = any(key not in new_tests or new_tests[key] != value for key, value in old_tests.items())
        changed_standard |= any(item not in contract["rubric"] for item in previous_contract["rubric"])
        changed_standard |= previous_contract["goal"] != contract["goal"]
        # Policy and adapter checks also define acceptance. Conservatively ask
        # for an exact version approval whenever these existing standards change.
        for key in ("ci", "operations", "production", "delivery"):
            changed_standard |= previous_contract.get(key) != contract.get(key)
        changed_standard |= {"low": 0, "medium": 1, "high": 2}[contract["risk"]] < {"low": 0, "medium": 1, "high": 2}[previous_contract["risk"]]
        if changed_standard:
            from .adapters import verify_approval
            scope = {"run_id": previous["id"], "kind": "contract_change", "environment": "controller", "target": task,
                     "source_sha": base, "script_digest": digest(encoded([previous_run["contract_digest"], contract_digest]))}
            try:
                verify_approval(approval, scope)
            except RunError:
                raise RunError("existing acceptance changed; explicit approval required for scope " + encoded(scope).decode())
    run_id = "QH-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
    work = store.root / "worktrees" / run_id
    branch = "qh/" + task.lower() + "/" + run_id.lower()
    with store.db:
        store.db.execute("insert into runs values(?,?,?,?,?,?,?,?,?,?,?)", (
            run_id, str(repo), task, contract_digest, base, str(work), branch,
            "waiting", "draft_mr", time.time(), encoded({"rounds": 0, "seconds": 0, "tokens": 0}).decode()))
    op = store.begin_operation(run_id, "worktree", {"branch": branch, "base": base, "path": str(work)})
    git(repo, "worktree", "add", "-b", branch, str(work), base)
    store.finish_operation(op, {"worktree": str(work), "head": base})
    # A frozen snapshot protects fixtures, mocks and selection configuration too.
    protected = protected_files(work, contract)
    store.update(run_id, protected=protected, source=fingerprint(work))
    if previous:
        store.update(run_id, previous_run=previous["id"], contract_change_approval=store.blob(encoded(approval)) if approval else None)
        if previous_contract.get("runtime_read_paths") == contract.get("runtime_read_paths") and previous_run["data"].get("environment_identity"):
            store.update(run_id, environment_identity=previous_run["data"]["environment_identity"], runtime=previous_run["data"].get("runtime"))
        # Reuse only unchanged protected Eval on exactly the same source/rules.
        fp = fingerprint(work)
        for test in contract["tests"]:
            if old_tests.get(test["id"]) == test:
                for prefix in ("eval:", "negative:"):
                    for item in store.current_evidence(previous["id"], prefix + test["id"], fp):
                        store.evidence(run_id, item["kind"], item["result"], fp, store.read_blob(item["digest"]))
    return store.run(run_id)


def protected_files(work, contract):
    roots = ["tests", "quality/contracts", ".git", ".gitignore", ".gitmodules", ".gitlab-ci.yml", ".gitlab", ".pre-commit-config.yaml", "pytest.ini", "pyproject.toml", ".mise.toml", ".coveragerc", "requirements.txt", "requirements-dev.txt"]
    roots += [os.fsdecode(p) for p in git(work, "ls-files", "-z", "--", "**/.gitignore").split(b"\0") if p]
    roots += contract.get("protected_paths", [])
    roots += [p for test in contract["tests"] if test["kind"] != "developer" for p in test.get("inputs", [])]
    developer = [relative(p) for p in contract.get("developer_test_paths", [])]
    files = {}
    for root in roots:
        path = work / root
        candidates = list(path.rglob("*")) if path.is_dir() else [path]
        for candidate in candidates:
            rel = candidate.relative_to(work)
            if any(rel == d or d in rel.parents for d in developer):
                continue
            if candidate.is_symlink():
                raise RunError("protected input cannot be a symlink")
            if candidate.is_file():
                files[str(rel)] = digest(candidate.read_bytes())
    return files


def assert_protected(store, run):
    if protected_files(Path(run["worktree"]), store.contract(run)) != run["data"].get("protected", {}):
        raise RunError("protected contract/fixture/mock/selection baseline changed")
    if git(Path(run["worktree"]), "ls-files", "-z", "--others", "--ignored", "--exclude-standard"):
        raise RunError("ignored worktree outputs are not reproducible source; use the dedicated output directory")


def response(store, run_id, result, blockers=None, stage=None):
    run = store.run(run_id)
    return {"protocol_version": 1, "run_id": run_id, "task": run["task"], "stage": stage or run["stage"],
            "result": result, "lifecycle": run["lifecycle"], "blockers": blockers or [],
            "quality_result": run["data"].get("quality_result", "not_evaluated"),
            "worktree": run["worktree"], "branch": run["branch"], "data": run["data"],
            "evidence": [dict(row) for row in store.db.execute("select id,kind,result,digest from evidence where run=? order by id", (run_id,))],
            "next": "resolve blockers and use qh resume" if blockers else "use qh gate for the next stage"}


def gate(store, run_id, stage, _locked=False):
    if not _locked:
        with store.lock(run_id):
            return gate(store, run_id, stage, _locked=True)
    if stage not in STAGES:
        raise RunError("unknown stage")
    run = store.run(run_id)
    contract = store.contract(run)
    assert_protected(store, run)
    fp = fingerprint(Path(run["worktree"]))
    head = git(Path(run["worktree"]), "rev-parse", "HEAD")
    required = ["eval:" + t["id"] for t in contract["tests"]]
    required += ["negative:" + t["id"] for t in contract["tests"] if any(r["id"] == t["id"] and r["critical"] for r in contract["rubric"])]
    required += ["review"]
    delivery = contract.get("delivery", {})
    blockers = []
    if STAGES.index(stage) >= 1:
        isolation = store.current_evidence(run_id, "isolation", run["data"].get("isolation_fingerprint", "not-run"))
        if not isolation or any(item["result"] != "pass" for item in isolation):
            blockers.append("runtime isolation is missing, failed or stale")
        if run["data"].get("runtime") and run["data"].get("environment_identity") != environment_identity(Path(run["data"]["runtime"]), contract):
            blockers.append("Codex/runtime/dependency environment changed; prior verification is stale")
    if stage == "draft_mr":
        required = []
    if delivery and STAGES.index(stage) >= 0:
        required += ["publication"]
    if delivery and STAGES.index(stage) >= 1:
        from .adapters import check_gitlab
        blockers += check_gitlab(store, run)
    if STAGES.index(stage) >= 2:
        required += ["test_deployment", "test_regression", "test_report"]
        from .adapters import run_operation
        for kind in ("test_deployment", "test_regression", "test_report"):
            names = [name for name, spec in contract.get("operations", {}).items() if spec.get("kind") == kind]
            if len(names) != 1:
                blockers.append("one reviewed adapter/read-back required for " + kind)
                continue
            try:
                run_operation(store, run_id, names[0], reconcile=True, _locked=True)
            except RunError as error:
                blockers.append(str(error))
    if STAGES.index(stage) >= 3:
        required += ["production_approval", "capacity", "production_observation", "recovery"]
        from .adapters import production_approval, verify_approval
        approvals = store.current_evidence(run_id, "production_approval", fp)
        try:
            if not approvals:
                raise RunError("separate production readiness approval is required")
            approval = json.loads(store.read_blob(approvals[-1]["digest"]))
            verify_approval(approval["approval"], production_approval(store, run_id))
        except RunError as error:
            blockers.append(str(error))
        names = [name for name, spec in contract.get("operations", {}).items() if spec.get("kind") == "production_observation"]
        if len(names) != 1:
            blockers.append("one specified-version production read-back adapter is required")
        else:
            try:
                run_operation(store, run_id, names[0], reconcile=True, _locked=True)
            except RunError as error:
                blockers.append(str(error))
    if fingerprint(Path(run["worktree"])) != fp or git(Path(run["worktree"]), "rev-parse", "HEAD") != head:
        blockers.append("source changed during gate verification")
    for kind in required:
        evidence = store.current_evidence(run_id, kind, fp)
        if not evidence or evidence[-1]["result"] != "pass" or any(item["result"] == "fail" for item in evidence):
            blockers.append("missing, failed or stale evidence: " + kind)
    if contract["risk"] == "high" and STAGES.index(stage) >= 1:
        for kind in ("performance", "risk"):
            records = store.current_evidence(run_id, kind, fp)
            if not records or any(r["result"] != "pass" for r in records):
                blockers.append("critical high-risk verification missing: " + kind)
    result = "blocked" if blockers else "pass"
    store.update(run_id, quality_result=result)
    if result == "pass":
        store.update(run_id, stage=stage, lifecycle="completed", accepted_fingerprint=fp, accepted_rules=store.evidence_rules(run_id))
    return response(store, run_id, result, blockers, stage)


def resume(store, run_id, reconcile=False):
    with store.lock(run_id):
        run = store.run(run_id)
        if reconcile:
            from .adapters import reconcile_operations
            reconcile_operations(store, run)
        unresolved = store.db.execute("select * from operations where run=? and state!='confirmed' order by created", (run_id,)).fetchall()
        blockers = []
        for operation in unresolved:
            alive = False
            if operation["pid"]:
                try:
                    os.kill(operation["pid"], 0)
                    alive = True
                except ProcessLookupError:
                    pass
                except PermissionError:
                    alive = True
            blockers.append(operation["id"] + (": worker still alive; writer not replaced" if alive else ": outcome unknown; reconcile before replay"))
        if blockers:
            return response(store, run_id, "uncertain", blockers)
        assert_protected(store, run)
        return response(store, run_id, "blocked", ["safe to continue with qh run; no action was replayed"])


def worker_args(work, runtime, contract, role, temp, contract_path):
    args = doctor.permission_args(work, runtime, role)
    paths = {str(work / ".git"): "read", str(contract_path): "read", str(temp): "write"}
    for path in runtime_paths(contract):
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
        if contract_path.is_relative_to(path) or codex_home.is_relative_to(path) or path == Path.home():
            raise RunError("runtime permission would expose Controller state or credentials")
        paths[str(path)] = "read"
    for path in ("tests", "quality/contracts", ".gitignore", ".gitmodules", ".gitlab-ci.yml", ".gitlab", ".pre-commit-config.yaml", "pytest.ini", "pyproject.toml", ".mise.toml", ".coveragerc", "requirements.txt", "requirements-dev.txt"):
        paths[str(work / path)] = "read"
    if (work / ".git").exists():
        for path in git(work, "ls-files", "-z", "--", "**/.gitignore").split(b"\0"):
            if path:
                paths[str(work / os.fsdecode(path))] = "read"
    for test in contract["tests"]:
        if test["kind"] != "developer":
            for path in test.get("inputs", []):
                paths[str(work / relative(path))] = "read"
    for path in contract.get("protected_paths", []):
        paths[str(work / relative(path))] = "read"
    if role == "builder":
        for path in contract["architecture"]["scope"]:
            paths[str(work / relative(path))] = "write"
        for path in contract.get("developer_test_paths", []):
            paths[str(work / relative(path))] = "write"
    # Last override supplies the complete map, retaining M0's root/default denials.
    base = {}
    base.update({":root": "deny", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny",
                 str(runtime): "read", str(work): "read"})
    base.update(paths)
    base[str(temp / "schema.json")] = "read"
    base[str(temp / "result.json")] = "read"
    args += ["-c", 'permissions.qh.extends=":read-only"', "-c", "permissions.qh.filesystem=" + doctor.json_value(base)]
    return args


def evaluate(store, run, runtime, *, remaining):
    work, contract = Path(run["worktree"]), store.contract(run)
    fp, results = fingerprint(work), []
    for test in contract["tests"]:
        for negative in (False, True) if test.get("negative_argv") else (False,):
            kind = ("negative:" if negative else "eval:") + test["id"]
            prior = store.current_evidence(run["id"], kind, fp)
            failed = [item for item in prior if item["result"] == "fail"]
            if failed:
                results += failed
                continue
            if prior and prior[-1]["result"] == "pass":
                results.append(prior[-1])
                continue
            # macOS Unix socket paths have a small OS limit. Keep pytest/test
            # runtime sockets short; accepted artifacts are copied to blobs.
            temp = Path(tempfile.mkdtemp(prefix="qh-eval-", dir="/tmp")).resolve()
            contract_path = store.root / "blobs" / run["contract_digest"]
            # Tests may create reports/caches, but cannot change existing source.
            args = worker_args(work, runtime, contract, "reviewer", temp, contract_path)
            argv = [arg.replace("{output}", str(temp)) for arg in test["negative_argv" if negative else "argv"]]
            env = {"PATH": str(runtime / "bin") + ":/usr/bin:/bin", "HOME": str(temp), "TMPDIR": str(temp),
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(work), "COVERAGE_FILE": str(temp / ".coverage"),
                   "PYTEST_ADDOPTS": "-o cache_dir=" + str(temp / ".pytest_cache")}
            command = ["codex", "sandbox", "-P", "qh", "-C", str(work), *args, "--allow-unix-socket", str(temp), "/usr/bin/env", "-i", *[k + "=" + v for k, v in env.items()], *argv]
            output = store.execute(run["id"], kind, command, timeout=min(test.get("timeout", 300), remaining()))
            ok = output["exit_code"] == (test.get("negative_exit", 1) if negative else 0)
            body = {**output, "argv": argv, "negative": negative}
            if not negative:
                for report in ("junit", "coverage"):
                    if test.get(report):
                        path = temp / relative(test[report])
                        if not path.is_file() or path.is_symlink() or path.stat().st_size > doctor.MAX_ARCHIVE:
                            ok = False
                            body[report + "_error"] = "required report missing"
                        else:
                            content = path.read_bytes()
                            body[report] = store.blob(content)
                            try:
                                xml = doctor.parse_xml(content, {"testsuite", "testsuites"} if report == "junit" else {"coverage"})
                                if report == "junit":
                                    cases, failures, errors, skipped = doctor.junit_counts(xml)
                                    ok = ok and cases > 0 and not (failures or errors or skipped)
                                else:
                                    valid, covered = int(xml.get("lines-valid", "0")), int(xml.get("lines-covered", "-1"))
                                    ok = ok and 0 <= covered <= valid and valid > 0 and 100 * covered / valid >= test.get("coverage_min", 0)
                            except (ValueError, TypeError, KeyError):
                                ok = False
            assert_protected(store, store.run(run["id"]))
            if fingerprint(work) != fp:
                raise RunError("Eval changed source; evidence rejected")
            unavailable = output["exit_code"] in test.get("infrastructure_exit_codes", [77])
            results.append(store.evidence(run["id"], kind, "blocked" if unavailable else "pass" if ok else "fail", fp, encoded(body)))
            shutil.rmtree(temp)
    return results


def builder_workspace(store, run):
    """Give each Builder new path authority; it never gets the accepted tree.

    A detached descendant may survive its process group. Its sandbox still only
    authorizes this one disposable path, so it cannot race later verification.
    """
    canonical = Path(run["worktree"])
    work = store.root / "worktrees" / (run["id"] + "-builder-" + secrets.token_hex(5))
    head = git(canonical, "rev-parse", "HEAD").decode().strip()
    op = store.begin_operation(run["id"], "builder_workspace", {"path": str(work), "head": head})
    git(canonical, "worktree", "add", "--detach", str(work), head)
    paths = git(canonical, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    for raw in paths:
        if not raw:
            continue
        source, target = canonical / relative(os.fsdecode(raw)), work / relative(os.fsdecode(raw))
        if source.is_dir():
            raise RunError("Builder submodules need an explicit isolated checkout; not silently shared")
        if source.is_symlink():
            raise RunError("Builder source symlinks are unsupported")
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists():
            target.unlink()
    if fingerprint(work) != fingerprint(canonical):
        raise RunError("isolated Builder snapshot differs from the accepted workspace")
    store.finish_operation(op, {"path": str(work), "head": head, "isolated": True})
    return work


def accept_builder_source(store, run, isolated, baseline, before):
    """Capture bounded bytes, then import them. No worker can write the target."""
    canonical, contract = Path(run["worktree"]), store.contract(run)
    probe = {**run, "worktree": str(isolated), "data": {**run["data"], "protected": baseline}}
    assert_protected(store, probe)
    expected = fingerprint(isolated)
    paths = set(git(isolated, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    paths.update(git(canonical, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    allowed = [relative(p) for p in contract["architecture"]["scope"] + contract.get("developer_test_paths", [])]
    captured = []
    for raw in paths:
        if not raw:
            continue
        rel = relative(os.fsdecode(raw))
        source, target = isolated / rel, canonical / rel
        content = source.read_bytes() if source.is_file() else None
        mode = bool(source.stat().st_mode & 0o111) if content is not None else False
        old = target.read_bytes() if target.is_file() else None
        old_mode = bool(target.stat().st_mode & 0o111) if old is not None else False
        if content == old and mode == old_mode:
            continue
        if not any(rel == root or root in rel.parents for root in allowed):
            raise RunError("Builder changed a path outside its authorized scope")
        captured.append((target, content, mode))
    if fingerprint(isolated) != expected or fingerprint(canonical) != before:
        raise RunError("source changed while capturing Builder output; no result accepted")
    for target, content, executable in captured:
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o755 if executable else 0o644)
    if fingerprint(canonical) != expected:
        raise RunError("accepted snapshot differs; rerun verification before any release")


def codex_worker(store, run, runtime, role, feedback, remaining):
    canonical, contract = Path(run["worktree"]), store.contract(run)
    before = fingerprint(canonical)
    work = builder_workspace(store, run) if role == "builder" else canonical
    baseline = protected_files(work, contract)
    root = store.root / "runs" / run["id"]
    temp = root / (role + "-" + secrets.token_hex(5))
    temp.mkdir(mode=0o700)
    schema = {"type": "object", "properties": {
        "summary": {"type": "string"}, "findings": {"type": "array", "items": {"type": "object", "properties": {
            "severity": {"type": "string"}, "file": {"type": "string"}, "line": {"type": "integer"}, "description": {"type": "string"}},
            "required": ["severity", "file", "line", "description"], "additionalProperties": False}}},
        "required": ["summary", "findings"], "additionalProperties": False}
    schema_path, result_path = temp / "schema.json", temp / "result.json"
    schema_path.write_bytes(encoded(schema))
    args = doctor.codex_command(work, runtime, role, schema_path, result_path)
    args += worker_args(work, runtime, contract, role, temp, store.root / "blobs" / run["contract_digest"])
    configs = {"shell_environment_policy.set": {"PATH": str(runtime / "bin") + ":/usr/bin:/bin", "HOME": str(temp), "TMPDIR": str(temp), "PYTHONDONTWRITEBYTECODE": "1"}}
    args += [arg for k, v in configs.items() for arg in ("-c", k + "=" + doctor.json_value(v))]
    instructions = "实现契约要求的最小完整修复，只修改授权业务代码。先搜索已有实现并复用。" if role == "builder" else "独立只读 CR；检查正确性、安全、实际负载、测试、性能、必要性及相关调用链。不得修改文件。"
    instructions += " 按 Quality Harness/Ponytail 执行：最小必要改动、优先复用、默认复用数据模型、评估性能证据和缺口、按冻结架构与 Rubrics 验证；不降要求，不引入重复实现或无关重构。"
    prompt = instructions + " 不得修改受保护的契约、Fixture、Mock、测试选择或权限；不得执行 Git、部署、数据库或外部写操作。生产风险和验证缺口写入 summary。只返回 schema JSON。\n契约：\n" + json.dumps(contract, ensure_ascii=False) + "\n验证/审查反馈：\n" + json.dumps(feedback, ensure_ascii=False)
    if role == "reviewer":
        diff = git(work, "diff", "--no-ext-diff", run["base_sha"]).decode(errors="replace")
        if len(diff) > 512 * 1024:
            raise RunError("review diff exceeds context budget; split the task")
        prompt += "\n最终差异（相关调用方也须读取核对）：\n" + diff
    with store.model_slot():
        output = store.execute(run["id"], role, args + ["-"], stdin=prompt, timeout=remaining(),
                               isolated_source=work if role == "builder" else None)
    if output["exit_code"] or not result_path.is_file():
        raise RunError(role + " did not produce a completed structured result")
    result = json.loads(result_path.read_text())
    if set(result) != {"summary", "findings"} or not isinstance(result["summary"], str) or not isinstance(result["findings"], list):
        raise RunError("invalid worker result")
    events = [json.loads(line) for line in store.read_blob(output["stdout"]).decode().splitlines() if line.strip()]
    usage = [event.get("usage", {}) for event in events if event.get("type") == "turn.completed"]
    if len(usage) != 1:
        raise RunError("worker turn completion/usage is missing or ambiguous")
    if role == "builder":
        accept_builder_source(store, run, work, baseline, before)
    assert_protected(store, store.run(run["id"]))
    after = fingerprint(canonical)
    if role == "reviewer" and before != after:
        raise RunError("Reviewer changed source")
    data = {**output, "result": result, "usage": usage[0], "source_before": before, "source_after": after}
    evidence = store.evidence(run["id"], "review" if role == "reviewer" else "builder",
                              "pass" if role == "builder" or not result["findings"] else "fail", after, encoded(data))
    current = store.run(run["id"])["data"]
    store.update(run["id"], tokens=current.get("tokens", 0) + usage[0].get("input_tokens", 0) + usage[0].get("output_tokens", 0))
    return result, evidence


def run_loop(store, run_id, runtime, until="code_ready", publish=False, pipeline=None):
    runtime = Path(runtime).resolve()
    with store.lock(run_id):
        unresolved = store.db.execute("select id from operations where run=? and state!='confirmed'", (run_id,)).fetchall()
        if unresolved:
            raise RunError("unfinished operation; use qh resume", "uncertain")
        run, start = store.run(run_id), time.monotonic()
        contract = store.contract(run)
        assert_protected(store, run)
        budget = contract.get("budget", {})
        spent = run["data"].get("seconds", 0)
        def remaining():
            value = budget.get("seconds", 1800) - spent - (time.monotonic() - start)
            if value <= 0 or (budget.get("tokens") and store.run(run_id)["data"].get("tokens", 0) >= budget["tokens"]):
                raise RunError("development budget exhausted")
            return value
        store.update(run_id, lifecycle="running")
        result = None
        try:
            checks = [doctor.version_check(), doctor.authentication_check()]
            def permissions(work, runtime, role):
                probe_contract = {"architecture": {"scope": ["business"]}, "tests": [], "protected_paths": ["contracts"]}
                return worker_args(work, runtime, probe_contract, role, work / "scratch", work / "contracts/baseline")
            checks += [doctor.sandbox_check(runtime, role, permissions) for role in ("builder", "reviewer")]
            isolation_fp = digest(encoded([run["contract_digest"], str(runtime), checks[0], rules_digest()]))
            store.update(run_id, isolation_fingerprint=isolation_fp, runtime=str(runtime), environment_identity=environment_identity(runtime, contract))
            store.evidence(run_id, "isolation", doctor.aggregate(checks), isolation_fp, encoded(checks))
            if doctor.aggregate(checks) != "pass":
                raise RunError("runtime isolation/authentication preflight failed")
            feedback, previous, unchanged = [], None, 0
            while True:
                remaining()
                run = store.run(run_id)
                results = evaluate(store, run, runtime, remaining=remaining)
                if any(item["result"] == "blocked" for item in results):
                    raise RunError("Eval infrastructure unavailable; repair the environment and resume without consuming a code round")
                bad = [item for item in results if item["result"] != "pass"]
                fp = fingerprint(Path(run["worktree"]))
                if not bad:
                    reviews = store.current_evidence(run_id, "review", fp)
                    if reviews and all(item["result"] == "pass" for item in reviews):
                        break
                    if reviews:
                        bad = [item for item in reviews if item["result"] != "pass"]
                    else:
                        review, _ = codex_worker(store, run, runtime, "reviewer", feedback, remaining)
                        if not review["findings"]:
                            break
                        bad = review["findings"]
                signature = []
                for item in bad:
                    if "digest" in item:
                        detail = json.loads(store.read_blob(item["digest"]))
                        if item["kind"] == "review":
                            signature.extend([[f.get("file"), f.get("description")] for f in detail.get("result", {}).get("findings", [])])
                        else:
                            tail = store.read_blob(detail["stderr"]).decode(errors="replace").strip().splitlines()[-1:] if detail.get("stderr") else []
                            signature.append([item["kind"], detail.get("exit_code"), tail])
                    else:
                        signature.append([item.get("file"), item.get("description")])
                state = digest(encoded(signature))
                unchanged = unchanged + 1 if state == previous else 0
                previous = state
                if unchanged >= 1:
                    raise RunError("same failure without improvement for two rounds")
                if run["data"]["rounds"] >= budget.get("rounds", 3):
                    raise RunError("code repair round budget exhausted")
                feedback = []
                for item in bad:
                    if "digest" not in item:
                        feedback.append(item)
                        continue
                    detail = json.loads(store.read_blob(item["digest"]))
                    for stream in ("stdout", "stderr"):
                        if detail.get(stream):
                            detail[stream + "_excerpt"] = store.read_blob(detail[stream]).decode(errors="replace")[-4000:]
                    feedback.append({"kind": item["kind"], "result": item["result"], **detail})
                codex_worker(store, run, runtime, "builder", feedback, remaining)
                store.update(run_id, rounds=run["data"]["rounds"] + 1)
            if publish:
                from .adapters import publish_draft
                publish_draft(store, store.run(run_id))
            if pipeline is not None:
                store.update(run_id, pipeline=pipeline)
            result = gate(store, run_id, until, _locked=True)
            return result
        except RunError as error:
            cancelled = store.run(run_id)["data"].get("cancel_requested", False)
            store.update(run_id, lifecycle="cancelled" if cancelled else "waiting", last_error=str(error))
            result = response(store, run_id, error.result, [str(error)], until)
            return result
        except KeyboardInterrupt:
            store.update(run_id, lifecycle="cancelled", last_error="Controller interrupted")
            result = response(store, run_id, "blocked", ["Controller interrupted; inspect pending operations before resuming"], until)
            return result
        except (ValueError, OSError, KeyError, RuntimeError, sqlite3.Error) as error:
            store.update(run_id, lifecycle="waiting", last_error=type(error).__name__)
            result = response(store, run_id, "blocked", ["Controller error: " + type(error).__name__], until)
            return result
        finally:
            elapsed = spent + time.monotonic() - start
            store.update(run_id, seconds=elapsed)
            if result is not None:
                result["data"]["seconds"] = elapsed
