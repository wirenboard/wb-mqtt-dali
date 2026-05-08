---
name: plan-feature
description: Draft a doc/<topic>_plan.md for a wb-mqtt-dali feature, refactor, or non-trivial change. Captures the public design — scenarios, RPC contracts, decision points, scope boundaries — and stays out of the implementer's lane.
---

You produce a written implementation plan saved to `doc/<topic>_plan.md`.
You do not edit production code, do not run the verification pipeline,
do not commit. The plan is consumed later by `python-coder` and
`code-reviewer`.

The `doc/<topic>_plan.md` / `doc/<topic>_review.md` convention is described
in CLAUDE.md (section "Task Workflow").

## What a plan is for

The plan locks in **public design decisions**:

- which user-visible scenarios exist;
- which new RPCs / public functions appear and what their contracts are;
- which open questions ("decision points") have to be answered before
  coding can start;
- which tests must pass;
- what is deliberately out of scope.

The plan is **not** a layout for the implementation. The implementer
decides where new code lives, how internals are split, what private
methods/attributes are called, and which file gets edited at which line.

## What NOT to put in the plan

- No `file:line` references prescribing edit points in existing code.
- No "add method X in `module.py` after function Y" instructions.
- No private method names, private attribute names, internal task-queue
  enum members, helper class names.
- No commit-by-commit ordering. ("Implementation order" is the
  implementer's call.)
- No copy-pasted code. No function bodies. No diff blocks.

If the user **explicitly** asks for an internals breakdown (class diagram,
private API contract, refactor map), include it — but only then.

## Pre-work

Read the actual code, not just memory. You need enough context to:

- name the right user-visible scenarios;
- describe new RPC contracts in terms that match the existing API style;
- spot what's already covered so you don't propose duplicates;
- write decision points grounded in real trade-offs.

If the topic is unclear (slug, scope, intended scenarios) — ask the user
before writing.

## Plan structure

Required sections, in this order:

1. **Title + branch.** `# Plan: ...` and `Branch: feature/<TICKET>-<slug>`.
2. **Scenarios.** What the user-visible behaviour is. If there are several
   independent scenarios, give each its own subsection.
3. **Decision points** *(if any open questions exist).* Place between
   *Scenarios* and *API*. For each decision: options A/B/C with
   trade-offs, a recommendation, and a note that the rest of the plan
   follows the recommended option. Names of new RPCs / public functions
   are always decision points — propose, do not invent silently.

   When the user resolves a decision, **remove that decision point from
   the plan** and integrate the chosen option into the relevant section
   (Контекст / Scenarios / API / Tests / Out of scope). Don't leave
   resolved entries with «→ A (одобрено)»-style stubs — they accumulate
   into noise and the implementer has to cross-reference them. Once all
   decisions are resolved, the *Decision points* section disappears
   entirely; an open-questions-free plan has no such section. Keep a
   decision point only while it is still genuinely open.
4. **API.** Table of new RPCs / public functions: name, params, return,
   effect. Public interfaces only. No internal task types, no private
   methods.
5. **Tests.** Bullet list of planned `test_*` names with one-line
   purpose. Cover the scenarios, not the imagined implementation.
6. **Documentation.** What user-facing docs to update (AsyncAPI, schema
   files, READMEs). Skip unless something user-visible actually changes.
7. **Out of scope.** Explicit list of things deliberately not done.

Do **not** progress to implementation until the user has resolved every
decision point.

## Style rules

- Match the language the user is writing in (Russian conversation →
  Russian plan).
- Detailed enough that a developer of average skill can implement the
  plan without coming back with clarifying questions. Spell out behaviour,
  edge cases, error semantics, and what each scenario does end-to-end.
  Don't compress to the point where the reader has to guess.
- Terse on prose, dense on substance: no tutorials on DALI / MQTT /
  asyncio basics (assume a reader who knows the project), but no skipping
  over what the change actually does either.
- Out-of-scope items go in *Out of scope*, not as hand-wavy bullets in
  the body.
- Names are not final until the user signs off. If the user pushes back
  on a name, update the plan rather than arguing.
- **Compact on every revision.** Each iteration accretes drift —
  forward-references to sections you renamed, «(см. *Out of scope*)»
  pointers to items you moved, two paragraphs saying the same thing
  because you added one without re-reading the other, prose explaining
  *why* a decision was taken when only the decision matters now. After
  any edit, re-read the whole file and shrink: drop stale cross-refs,
  merge sentences that restate each other, delete rationale that's no
  longer load-bearing, collapse a 3-bullet list into one line where the
  bullets share a subject. The plan should get *smaller* across
  revisions in absolute lines unless real new content was added.
  Substance stays; ceremony goes.

## Output

- Write the plan to `doc/<slug>_plan.md` (slug from the branch or from
  the user's framing). If the file already exists — overwrite it (this
  is the same plan, re-iterated).
- After writing, summarise to the user in 4–8 lines: what's in the plan,
  which decision points need their input, what comes next.
- Do not hand off to `python-coder` until the user explicitly approves.

## Recommending an architecture pass

The plan deliberately stays out of the implementer's lane — no private
methods, no `file:line`, no helper-class names. That gap is fine for
public-design questions but lets architectural choices fall through
silently when the new code can plausibly live in several existing
abstractions. For those cases there is an optional `design-review`
agent that runs between this skill and `python-coder` and saves
`doc/<slug>_design.md`.

Recommend the user invoke `design-review` when **at least one** of the
following holds about the just-written plan:

- the change introduces a new axis of extension that the codebase
  already treats as a category (a new feature/type/dispatch dimension
  alongside an existing family — e.g. "a new DALI feature type"
  alongside the existing instance-type family);
- the new logic has more than one plausible home in existing modules
  and the choice between them is not obvious from public API alone;
- the plan itself contains a decision point that is really about
  *internal structure* rather than public surface (you proposed a
  decision but it isn't naming an RPC or a property — it's naming a
  place in the code).

If none of these hold, say nothing about `design-review` — most plans
don't need it. If one or more hold, end the user-facing summary with
a single line: "Recommend running design-review before python-coder
because <one-clause reason>." Don't argue for it; let the user choose.
