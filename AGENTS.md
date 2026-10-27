# Codex Quality Harness

Read README.md before changing behavior. M0 must pass
before implementing the unattended loop or expanding the general framework.
Do not turn missing evidence into success or substitute fixtures for live acceptance.
Use Python 3.10 and the standard library. Run `PYTHONPATH=src python3.10 -m unittest discover -s tests`.

## Fixed Quality Harness constraints

These requirements apply to design, implementation and CR. A model's PASS does not waive them.

1. **Minimum necessary change:** declare the changed scope first. Justify each new file,
   module or dependency; reject unrelated edits and speculative abstractions in CR.
2. **Reuse first:** search existing implementations, callers and shared capabilities before
   implementing. Prefer direct reuse or a small extension. Record the specific gap when
   a new implementation is required.
3. **Reuse data models by default:** reuse existing tables, fields and relationships.
   A necessary model change must explain why reuse is insufficient, compatibility,
   migration order and recovery limits. This does not authorize database writes.
4. **Assess production performance:** changes to queries, concurrency, queues, caches,
   external calls or resource use require a report covering the change, evidence,
   verification gaps, risks and recovery limits. Critical unverified high-risk paths
   cannot receive an unconditional CR pass.
5. **Define before building:** define architectural boundaries, key behavior, acceptance
   cases and Rubrics before implementation; validate against the same version afterward.
   Lowering or deleting acceptance requirements requires the user's confirmation.

During CR, assess all five against the actual diff and callers; cite evidence and keep
unverified items visible. Reuse the existing review pass and report, without a second
ritual review or duplicate document.
