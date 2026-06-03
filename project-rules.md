# Project Rules

Normative rules for the `wb-mqtt-dali` project. This file is the **single source of
truth** for the agent workflow rules and the code style below. It is imported into
`CLAUDE.md` (via `@project-rules.md`) so it loads in every session, and it is read
directly by the `code-review-orchestrator` skill's conventions reviewer. Edit the rules **here**;
both the author side (code being written) and the review side stay in sync.

## Agent Workflow Rules

- Never create a git commit without explicit user approval in the current conversation.
- Never modify existing tests without explicit user approval.
- Do not rename existing identifiers (locals, params, functions, methods, classes, module-level constants) unless functionally required (old name became misleading after a behavior change, or a real name clash). Subjective "consistency"/"better naming" doesn't count. Expanding a signature does not justify renaming.
- Do not introduce temporary local variables for 1–2 uses; only if used 3+ times or they materially improve readability.
- Do not disable/skip tests; do not add `# pylint: disable` / `# noqa` / `# type: ignore` without a concrete reason. Fix the underlying issue.
- Never force-push (`--force` / `--force-with-lease`) to update a PR. Add new commits — reviewers need incremental changes.
- Tests must not **add new** access to private attributes (`_underscore`) of production classes. If a test can't be written against the public API, **stop and ask the user** — the fix usually requires widening the API or rethinking the test. Pre-existing private access in untouched test code is tolerated debt.
- `# pylint: disable=protected-access` must scope to a single function or line, never a whole module.

## Code Style & Notes

- Style/lint config: `pyproject.toml` (baseline: `https://github.com/wirenboard/codestyle/blob/master/python/config/pyproject.toml`).
- All I/O is `asyncio`; tests use `unittest.IsolatedAsyncioTestCase`.
- Non-trivial tests start with a short docstring describing the scenario being tested (what's set up, what's exercised, what's expected). Trivial one-liners (single assertion against a pure function) don't need it; anything with multi-step setup, async interactions, or non-obvious expectations does.
- **Enums over string/int constants**: when a value has a small, fixed set of options (status, kind, mode, action), model it with `enum.Enum` rather than string or integer literals. Plain `Enum` with descriptive values is the default; reach for `IntEnum`/`StrEnum`/`Flag` only when there's a concrete reason (interop, bitwise ops). Anti-pattern: a dataclass field typed `status: str` with conventional literals `"ok"`/`"error"` — make it a typed enum.
- **Structures over `dict`/`tuple` soup when the shape is known**: avoid `Optional[dict]`, `dict[str, list[str]]`, `tuple[tuple[str, tuple[str, ...]], ...]`, or several parallel dicts keyed by the same value — they hide what each string means and force readers to reverse-engineer the shape from assignment sites. If you know the keys and types, declare a `@dataclass` (frozen for immutable records, mutable for in-place state) or `NamedTuple` and use it as the field/parameter type. When several dicts share the same key set (`a[k]`, `b[k]`, `c[k]` always read together), that's a missing dataclass — collapse them into one `dict[Key, RecordType]`. Type aliases (`ControlId = str`) are cheap and worth using to document intent in signatures. Do not type a field `Optional[T]` defensively if every code path that constructs the parent already supplies a non-`None` value — narrow it to `T` so callers and the type-checker see the real contract.

### Class method ordering

Within every class body, methods are grouped in this order, with `# --- ... ---` dividers between groups (groups that are empty are omitted, dividers too):

1. `__init__` and other dunder methods.
2. Public methods and `@property`s — the class's external API.
3. `# --- Hooks for subclasses ---` — methods intended to be overridden (typically named with a leading underscore, e.g. `_initialize_impl`, `_build_mqtt_controls`).
4. `# --- Private ---` — internal helpers not intended for subclasses to override.

Within each group, order is by relevance/call sequence, not alphabetical.
