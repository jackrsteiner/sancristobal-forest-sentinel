# Delivery model: epics, slices, and beads

This document defines how agentic work is organized in this repository. It is
intentionally **project-agnostic**: the three-layer model, the workflow, and the
definition of done below can be copied into any repository that adopts this
agentic workflow. Project-specific content — the actual epics and slices — lives
in `docs/work-plan.md`.

## Three layers of work

Work is organized along three layers. **Epics** and **vertical slices** are
*orthogonal axes*, not competitors: every bead belongs to exactly one epic *and*
one slice.

| Layer | Question it answers | Sliced by | Demonstrable on its own? | Lifespan |
|-------|---------------------|-----------|--------------------------|----------|
| **Epic** | *Where does this code live?* | System component / layer | No — a layer alone does nothing | Long-lived bucket |
| **Vertical slice** | *What working capability did we just ship?* | End-to-end, user-visible behavior | **Yes — this is the unit of "is it functional yet"** | A delivery milestone |
| **Bead** | *What can one agent do in one run?* | A single PR's worth of work | Part of a slice | One agent run |

### Epics

An epic is a **horizontal bucket** that groups related work by system component
(for example: "database", "imagery access", "dashboard"). Epics organize the
codebase; they are *not* implemented directly and a finished epic is not, by
itself, a demonstrable capability. Epics are tracked as GitHub issues labelled
`epic`, decomposed into beads attached as native sub-issues. An epic is closed
only when all of its beads are closed and its acceptance criteria are met.

### Vertical slices

A vertical slice is a **thin thread that cuts through many epics** to deliver one
small but *complete*, hallway-testable capability. A slice is the unit that
answers "is the system functional yet?" — after a slice ships, you can run
something and a person can try it. Slices are tracked as GitHub **milestones**;
each bead is assigned to the milestone for its slice.

The same bead can belong to epic "indices" *and* to slice "optical change
detection". Epics organize; slices deliver. Prefer building the thinnest
end-to-end slice first (a "walking skeleton") and deepening it with later slices,
rather than completing one horizontal layer at a time.

### Beads

A bead is a **small, agent-sized unit of work** that can be picked up,
implemented, tested, and shipped in a single agent run. A bead is an issue that
satisfies all of the following:

1. **Small enough for one agent run.** If a bead cannot be completed in one
   focused pass, split it.
2. **Belongs to exactly one epic and one slice.** The epic says where the code
   lives; the slice says which delivery milestone it serves.
3. **Has explicit acceptance criteria.** Observable, testable outcomes. The bead
   is not done until every criterion is checked.
4. **Has explicit dependencies.** Beads it depends on are recorded with
   `Depends on #NNN`; beads it unblocks use `Blocks #NNN`. GitHub renders these
   cross-references automatically.
5. **Ships with tests.** New and changed code is covered by tests, and all tests
   pass locally and in CI before the bead is closed.

## How the layers relate

- Epics are tracking issues; slices are milestones; beads are the issues that
  actually get implemented.
- Prefer **GitHub native sub-issues** to attach a bead to its epic. If sub-issues
  are not appropriate (for example, when a bead spans two epics), reference the
  epic from the bead's body and add the bead to the epic's task list.
- Assign every bead to the **milestone** for its vertical slice.
- A slice is "done" when every bead in its milestone is closed and the slice's
  hallway test passes. An epic is "done" when every bead under it is closed.

## Filing a bead

Open a new issue using the **Agent bead** template
(`.github/ISSUE_TEMPLATE/agent-bead.yml`). The template enforces:

- a parent epic reference,
- the vertical slice the bead serves,
- in-scope and out-of-scope sections,
- an acceptance-criteria checklist,
- explicit `Depends on` / `Blocks` dependency references,
- a test plan,
- a definition-of-done checklist that cannot be skipped.

If a field does not apply, say so explicitly rather than leaving it blank.

## Dependencies

Beads must record the issues they depend on using the GitHub issue tracker's
native cross-reference syntax:

- `Depends on #NNN` — this bead cannot ship until `#NNN` is merged.
- `Blocks #NNN` — `#NNN` cannot ship until this bead is merged.

GitHub renders these references in the timeline and in linked-issue panels, so
dependency state is visible without leaving the tracker. A bead with no
dependencies must say so explicitly (for example, "No upstream dependencies —
foundational bead under epic #NN").

## Tests and coverage

A bead is not done unless:

- every code path it adds or changes is covered by tests,
- those tests run in CI on the pull request that closes the bead, and
- the full test suite passes.

If tests cannot be added for a particular reason (for example, a bead that only
edits documentation), the bead must justify it explicitly in its test plan.
"I'll add tests later" is not acceptable; that work belongs in a follow-up bead
linked with `Blocks`.

## Definition of done

The agent-bead template includes a definition-of-done checklist. Every box must
be checked before the closing pull request is merged:

- [ ] Acceptance criteria are all checked.
- [ ] New and changed code is fully covered by tests.
- [ ] All tests pass locally and in CI.
- [ ] Linked to the parent epic via sub-issue or task list.
- [ ] Assigned to the milestone for its vertical slice.
- [ ] Bead dependencies are recorded with `Depends on` / `Blocks` references.
- [ ] Documentation is updated where the change is user- or operator-visible.

## Sizing

If a bead grows past a single agent run during implementation, stop and split it.
The preferred split is along the natural seams of the pipeline, not arbitrary
file boundaries. Each resulting bead must independently satisfy the definition of
done and stay within a single vertical slice where possible.
