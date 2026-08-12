# Project Rules

Normative rules for the `wb-mqtt-dali` project. This file is the **single source of
truth** for the agent workflow rules and the code style below. It is imported into
`CLAUDE.md` (via `@project-rules.md`) so it loads in every session, and it is read
directly by the `code-review-orchestrator` skill's project-rules reviewer. Edit the rules **here**;
both the author side (code being written) and the review side stay in sync.

## Agent Workflow Rules

- **No commits without approval** — never create a git commit without explicit user approval in the current conversation.
- **No editing existing tests without approval.**
- **No gratuitous renames** — do not rename existing identifiers (locals, params, functions, methods, classes, module-level constants) unless functionally required (old name became misleading after a behavior change, or a real name clash). Subjective "consistency"/"better naming" doesn't count; expanding a signature does not justify renaming.
- **No gratuitous comment rewrites** — the same rule applies to existing comments and docstrings: leave them alone unless the code they describe changed and made them wrong or incomplete. Rewording, re-wrapping, or "clarifying" a still-accurate comment is diff noise that costs reviewer attention. When a comment does have to change, edit the part that went stale, don't rewrite the whole block.
- **No throwaway temp vars** — do not introduce a temporary local variable for 1–2 uses; only if used 3+ times or it materially improves readability.
- **No silencing tests or linters** — do not disable/skip tests; do not add `# pylint: disable` / `# noqa` / `# type: ignore` without a concrete reason. Fix the underlying issue.
- **Never force-push a PR** — no `--force` / `--force-with-lease` to update a PR. Add new commits — reviewers need incremental changes.
- **No new private access from tests** — tests must not **add new** access to private attributes (`_underscore`) of production classes. If a test can't be written against the public API, **stop and ask the user** — the fix usually requires widening the API or rethinking the test. Pre-existing private access in untouched test code is tolerated debt.
- **Scope `protected-access` disables** — `# pylint: disable=protected-access` must scope to a single function or line, never a whole module.
- **Changelog is written from the user's side** — a `debian/changelog` entry says what changed for someone running the service: what used to go wrong, what happens now, what to expect in MQTT or on the bus. Module, class and function names, RPC internals and refactoring details belong in the commit message, not there.

## Code Style & Notes

- **Style/lint config** — `pyproject.toml` (baseline: `https://github.com/wirenboard/codestyle/blob/master/python/config/pyproject.toml`).
- **Async I/O & test base** — all I/O is `asyncio`; tests use `unittest.IsolatedAsyncioTestCase`.
- **Docstrings on non-trivial tests** — start the test with a short docstring describing the scenario being tested (what's set up, what's exercised, what's expected). Trivial one-liners (single assertion against a pure function) don't need it; anything with multi-step setup, async interactions, or non-obvious expectations does.
- **Comments carry what the code can't** — a comment or docstring earns its place by stating the non-obvious: why this way, what breaks otherwise, which external contract forces it. Don't restate what the next line already says, and don't spend three sentences where one does. Keep a comment at a call site to a line or two; when a mechanism genuinely needs several paragraphs, state it once in the module or class docstring and let the call sites refer to it by name instead of each repeating the context.
- **Enums over string/int constants** — when a value has a small, fixed set of options (status, kind, mode, action), model it with `enum.Enum` rather than string or integer literals.
  - Plain `Enum` with descriptive values is the default; reach for `IntEnum`/`StrEnum`/`Flag` only when there's a concrete reason (interop, bitwise ops).
  - Anti-pattern: a dataclass field typed `status: str` with conventional literals `"ok"`/`"error"` — make it a typed enum.
- **Units in the names of numeric constants** — a numeric constant holding a physical quantity (time, length, temperature, current, …) names its unit: `RESET_SETTLE_TIME_S`, `WB_MQTT_SERIAL_PORT_LOAD_TOTAL_TIMEOUT_MS`. Without the suffix the unit is the one thing a reader has to guess, and guessing wrong is a factor-of-1000 bug.
- **Structures over `dict`/`tuple` soup when the shape is known** — if you know the keys and types, declare a `@dataclass` (frozen for immutable records, mutable for in-place state) or `NamedTuple` and use it as the field/parameter type.
  - Avoid `Optional[dict]`, `dict[str, list[str]]`, `tuple[tuple[str, tuple[str, ...]], ...]`, or several parallel dicts keyed by the same value — they hide what each string means and force readers to reverse-engineer the shape from assignment sites.
  - When several dicts share the same key set (`a[k]`, `b[k]`, `c[k]` always read together), that's a missing dataclass — collapse them into one `dict[Key, RecordType]`.
  - Type aliases (`ControlId = str`) are cheap and worth using to document intent in signatures.
  - Do not type a field `Optional[T]` defensively if every code path that constructs the parent already supplies a non-`None` value — narrow it to `T` so callers and the type-checker see the real contract.
- **No bare class-level annotations for instance state** — a name annotated in the class body with no value (`next_due_at: Optional[float]`) creates nothing at runtime: it only describes an attribute some *other* method assigns later, and its practical effect is silencing pylint's `attribute-defined-outside-init` instead of fixing the shape. Two legitimate forms, nothing in between:
  - a real class attribute — annotate **and** assign the default (`read_error: bool = False`), which is right when instances share the default and only some of them ever rebind it;
  - or all instance state assigned in `__init__`. A mixin is not an exception: give it its own `__init__` and have the host invoke it — `super().__init__(...)` with a single base, or an explicit `Base.__init__(self, ...)` per base when the bases are independent and take different arguments — rather than an `_init_*` method the host has to remember to call.
  - `@dataclass`, `Protocol`, `NamedTuple`, `TypedDict` and `Enum` bodies are unaffected — there the bare annotation *is* the declaration the class is built from.

### Class method ordering

Within every class body, methods are grouped in this order, with `# --- ... ---` dividers between groups (groups that are empty are omitted, dividers too):

1. `__init__` and other dunder methods.
2. Public methods and `@property`s — the class's external API.
3. `# --- Hooks for subclasses ---` — methods intended to be overridden (typically named with a leading underscore, e.g. `_initialize_impl`, `_build_mqtt_controls`).
4. `# --- Private ---` — internal helpers not intended for subclasses to override.

Within each group, order is by relevance/call sequence, not alphabetical.
