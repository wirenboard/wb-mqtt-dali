---
name: code-review-orchestrator
description: >-
  Orchestrate a multi-aspect code review by spawning one specialized subagent per
  review dimension — security, stability, conventions/structure conformance,
  documentation conformance, regressions, test coverage of new code, simplification
  (over-complex/verbose code and heavyweight deps used trivially), and an architect that
  proposes structural improvements as options with pros/cons — then act as a coordinator
  that deduplicates, re-categorizes, filters noise, surfaces valid architecture options
  to the user, and assembles one structured review with severity ratings and a merge
  verdict. Use whenever the user asks to review a diff, PR, MR, branch, or changes;
  mentions "code review", "review my changes", "review this PR/branch", "is this safe to
  merge", "did I break anything", "can this be simplified", "is this over-engineered", or
  wants a thorough multi-angle review. Use it even when the user only says "take a look
  at my changes" if the intent is review rather than feature work.
---

# Code Review Orchestrator

Review a set of code changes by running several **specialized reviewers in parallel**,
each looking at exactly one dimension, then consolidating everything into **one**
report. This beats a single generic "find bugs in this diff" prompt because a focused
reviewer with explicit "what to flag / what NOT to flag" boundaries produces far less
noise, and a coordinator pass removes the duplication and false positives that come
from running several reviewers at once.

Two design commitments make the output usable:

- **Structured findings, not prose.** Every sub-reviewer returns machine-readable
  findings — `severity` ∈ {critical, warning, suggestion}, `confidence`, category,
  `file:line`, and a concrete suggested fix — never a paragraph of commentary. This is
  what lets the coordinator dedupe, re-rank, and filter reliably.
- **One consolidated comment, not a stream of inline notes.** The final output is a
  single structured review. A flood of inline comments adds cognitive load to threads
  that are already noisy; one organized report is far easier to act on.

The design follows a coordinator/sub-reviewer split: sub-reviewers find candidate
issues; the coordinator decides what is real, where it belongs, and how serious it is.
The bias is toward **signal over noise** and toward **approval** — a clean change with
one minor nit should still pass.

## The review aspects

| Aspect | Brief | What it owns |
| --- | --- | --- |
| Security | `references/security.md` | Exploitable/dangerous issues + secret & credential detection |
| Stability | `references/stability.md` | Crashes, data loss, leaks, concurrency/races, correctness, performance, observability |
| Conventions | `references/conventions.md` | Conformance to the project's structure, patterns, and practices |
| Documentation | `references/documentation.md` | Code vs. its docs/comments/API contracts being out of sync |
| Regressions | `references/regressions.md` | Changes that break or alter existing behavior |
| Test coverage | `references/test-coverage.md` | New/changed logic that ships without tests |
| Simplification | `references/simplification.md` | Code that's more complex/verbose/heavyweight than needed (incl. heavy deps for trivial use) |
| Architecture | `references/architecture.md` | Wider-angle structural improvements — consolidation, abstractions, when a design has run its course |

All reviewers share the rules in `references/shared-reviewer-rules.md` (severity
definitions, the structured output format, and global "do not flag" rules). Every
sub-reviewer prompt MUST include the shared rules plus its own aspect brief.

**The architect is special.** Its brief overrides two of the shared rules: it may look
beyond the diff at the surrounding module, and it returns `<proposal>` blocks (options
with pros/cons) instead of `<finding>` blocks. Treat its output differently in the judge
pass and the report (see below).

## Workflow

### Step 1 — Establish scope

Figure out exactly what changed. Do not review the whole repository.

1. Determine the base. If the user named a base branch / PR / commit range, use it.
   Otherwise default to the merge target; fall back to `main`, then `master`.
   ```bash
   git diff --merge-base <base> --stat      # overview
   git diff --merge-base <base>             # full patch
   git diff --merge-base <base> --name-only # file list
   ```
   If there is no git context (e.g. the user pasted a diff or pointed at files),
   review what was provided and say so in the verdict.
2. Filter out noise before reviewing: lockfiles (`package-lock.json`, `yarn.lock`,
   `pnpm-lock.yaml`, `Cargo.lock`, `go.sum`, `poetry.lock`, etc.), vendored deps,
   minified/bundled assets (`*.min.js`, `*.bundle.js`, `*.map`), and generated files
   (first lines contain markers like `@generated` / `DO NOT EDIT`). **Exception:** keep
   database migrations even if marked generated — they contain real schema changes.
3. Detect the stack (languages, frameworks, test runner, build tool) and load project
   conventions if present: `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING*`, `docs/`,
   linter/formatter configs, and the test directory layout. Pass relevant pieces to
   the reviewers that need them (conventions, documentation, test coverage).
4. Write a short shared-context note (changed files, base, stack, where the conventions
   and docs live, anything the user flagged as important) so reviewers don't each
   re-derive it.

### Step 2 — Classify the diff into a risk tier

Classify **every** review into a tier at the orchestrator level before spawning
anything. This is what keeps cost and noise down — don't run eight frontier-model agents
at a typo fix. Compute total changed lines (added + removed) and file count, and check
whether any changed path is security-sensitive or a system/critical path.

| Tier | Trigger | Agents | Who runs |
| --- | --- | --- | --- |
| **Trivial** | ≤10 changed lines **and** ≤20 files, no sensitive paths | 2 | A single generalized pass (stability + conventions inline, no subagents needed) |
| **Lite** | ≤100 changed lines, ≤20 files, no sensitive paths | 4 | Stability, conventions, test coverage, simplification |
| **Full** | >100 lines **or** >50 files **or** *any* security-sensitive or system/critical path | 7+ (all 8) | Everything, incl. security and architecture |

Definitions and rules:

- **Security-sensitive / system / critical paths** always force **Full**, regardless of
  size: anything under `auth/`, `crypto/`, `security/`, payments/billing, session or
  token handling, access control, input parsing at trust boundaries, infra/deploy
  config, migrations, or anything whose path name sounds even remotely security- or
  infra-related. When unsure whether a path qualifies, treat it as sensitive.
- The **Lite** set is the sensible default 4; adapt it to what the diff touches (e.g.
  swap in documentation if the change is largely docs/API surface). Architecture rarely
  earns its keep below Full — include it only if a small change is structurally
  significant.
- **When in doubt, escalate a tier.** The cost of one extra agent is far smaller than a
  missed security or stability bug.

### Step 3 — Spawn one subagent per aspect (in parallel)

For each selected aspect, spawn a subagent in the **same turn** so they run
concurrently. Each subagent prompt is assembled as:

```
<shared-reviewer-rules.md contents>

<the aspect brief, e.g. references/security.md contents>

## Shared context
<base, changed files, stack, conventions/docs locations, user notes>

## Your task
Review the changed code for your assigned aspect. Read whatever source files you need
for context — definitions, callers, types, related tests, the diff itself — BEFORE
judging anything; do not review a hunk in isolation. But anchor every finding to a
changed line/hunk and report only issues the change introduces or newly exposes. Return
findings in the structured format from the shared rules (including confidence). If you
find nothing worth flagging, return exactly: LGTM.
```

Tell each subagent which files are in scope and where the full diff lives. Subagents
are free to read source, grep, and trace callers — they decide what context they need.
They return structured findings (or `LGTM`); they do NOT post anything or talk to the
user.

**Architect exception:** the architecture subagent's brief overrides the task text
above — it may read the surrounding module (not just the diff) and returns `<proposal>`
blocks instead of `<finding>` blocks. Spawn it the same way; just don't expect the
`<finding>` format from it.

If subagents are unavailable (e.g. plain Claude.ai), run the aspects sequentially in
this one context instead — same briefs, same output format, one aspect at a time — then
proceed to Step 4.

### Step 4 — Coordinate: the judge pass

Collect every reviewer's findings and consolidate. This is the most important step
and the reason the output stays clean. Do it in order:

1. **Deduplicate.** The same underlying issue often shows up from two reviewers (e.g.
   security and stability both flag an unvalidated input). Keep it **once**, in the
   section where it fits best, with the highest severity assigned by any reviewer.
2. **Re-categorize.** Move each finding to the aspect it actually belongs to,
   regardless of which reviewer raised it.
3. **Reasonableness filter.** Drop speculative risks, nitpicks, style opinions the
   project doesn't hold, and anything that contradicts an established convention in
   this repo. Weight `confidence`: `low`-confidence findings need you to verify them in
   the source before they survive — drop the ones you can't confirm. If you are not sure
   a finding is real, **read the source to verify** before keeping or dropping it — do
   not pass through unverified guesses.
4. **Re-rate severity** against the definitions in the shared rules. A reviewer's
   `critical` that turns out to need unlikely preconditions becomes a `warning` or is
   dropped.
5. **Check coverage of new code explicitly.** New or changed logic without
   corresponding tests is a finding even if every other aspect is clean — this is a
   first-class requirement, not a nice-to-have.
6. **Handle architect proposals separately.** Architecture comes as `<proposal>` blocks,
   not findings. For each one: verify it's real and worthwhile by reading the code —
   drop speculative generality, over-engineering, and big risky rewrites that a small
   change doesn't justify. **Keep the valid ones with all their options and tradeoffs
   intact** and pass them through to the user in the Architecture section below; do NOT
   collapse the options into a single recommendation — surfacing the choice is the point.
   If a simplification finding and an architecture proposal overlap, keep the proposal
   (it carries the tradeoffs) and drop the duplicate finding.

   Architecture proposals are forward-looking and **do not change the merge verdict** by
   default — they're suggestions for the author to weigh, not merge blockers. The one
   exception: if the current architecture is actively *causing* a defect in this change,
   that defect also belongs in Critical/Warnings as a normal finding, and it can affect
   the verdict.

### Step 5 — Assemble the single report

Output **exactly one consolidated report** — not a stream of inline comments. One
organized comment is far easier to act on in a noisy thread. Group by severity, then by
aspect. Lead with the verdict so the reader sees the decision first. Every finding cites
`file:line` on a changed line. Keep each finding tight: what, where, why it matters,
suggested fix.

```markdown
## Code review — <short scope description>

**Verdict:** <one of: Approve · Approve with comments · Request changes · Block merge>
<one or two sentences explaining the verdict>

### Critical
<findings that will cause an outage/data loss or are exploitable — or "None">

### Warnings
<concrete risks or measurable regressions — or "None">

### Suggestions
<improvements worth considering — or "None">

### Test coverage
<new/changed logic and whether it's tested; list specifically what lacks tests — or
"All new logic is covered">

### Architecture & design (forward-looking, non-blocking)
<validated architect proposals, options preserved — or "None">
```

Finding entry format inside the severity/test-coverage sections:

```markdown
- **[<aspect>] <one-line title>** — `path/to/file.ext:120-135`
  <why it matters, concretely>. Suggested fix: <short fix>.
```

Architecture proposal format (preserve the options — don't collapse to one answer):

```markdown
- **<area>: <one-line summary of the opportunity>**
  <the observation — why the current shape limits things>. <impact / how urgent>.
  - *Option A — <summary>* (effort: <s/m/l>). Pros: <…>. Cons: <…>.
  - *Option B — <summary>* (effort: <s/m/l>). Pros: <…>. Cons: <…>.
  - **Recommendation:** <which and why, or "surfacing for you to decide">.
```

### Verdict rubric

Bias toward approval. Use this mapping:

| Situation | Verdict |
| --- | --- |
| All reviewers LGTM, or only trivial suggestions | Approve |
| Only suggestions, or warnings with no production risk | Approve with comments |
| Multiple warnings forming a risk pattern, or new logic with no tests | Request changes |
| Any critical issue, or a concrete production-safety / security risk | Block merge |

State the verdict plainly. If a single warning sits in an otherwise clean change,
that's still "Approve with comments", not a block.

## Notes

- Review only the change and what it touches — not pre-existing issues in unchanged
  code, unless the change makes them newly reachable.
- Don't restate the diff back to the user. Report findings, not a narration of what
  the code does.
- If scope was ambiguous (no clear base, pasted snippet), say what you reviewed in the
  verdict line so the user knows the boundaries.
