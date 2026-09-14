# ADR 0011 — Model vendors per persona (`codex exec` beside `claude -p`)

- Status: accepted
- Date: 2026-09-13
- Deciders: Operator (direction, 2026-09-13), Shohoku (design + execution)

## Context

Every place the harness launched an agent session assumed one vendor. The
agent SDK had a working `claude_p` backend and an empty `openai_sdk` stub;
the Slack bridge, the external idle-compaction pass, and the (inert)
tiger-memory summarizer all hard-coded `get_backend("claude_p")`; autodrive
took `--backend`/`--model` flags but nothing fed them from team config. A
persona's identity lived in `configs/personas.yaml`, but which model it ran
on did not — `AgentConfig.model` was set in exactly one place in the whole
package (the autodrive runner).

The Operator now has OpenAI's Codex CLI working on the team host and asked
for three things:

1. `codex exec` usable by the harness the same way `claude -p` is.
2. Each persona able to pin its own model vendor and model, with a team
   default that a persona inherits when it says nothing — and the persona
   then always runs on that choice, for tasks and for memory sweeping.
3. `tigerharness init` asking which vendor a new team defaults to.

Both CLIs bill a *subscription* (Claude Pro/Max through `claude -p`, ChatGPT
through `codex exec`), so the subscription rail
([subscription-backend.md](../subscription-backend.md)) is unchanged: the
harness still holds no API key for either vendor.

## Decision

### 1. A `codex_exec` backend, the same shape as `claude_p`

`agent_sdk/backends/codex_exec.py` spawns `codex exec --json` per call with
the prompt on stdin, and `codex exec resume <thread-id> --json` for a later
turn of the same session (the id comes from Codex's `thread.started` event).
Its JSONL vocabulary (`item.*` events wrapping `agent_message`, `reasoning`,
`command_execution`, `file_change`, `mcp_tool_call`, `web_search`;
`turn.completed` with usage; `turn.failed`) maps onto the SDK's existing
`Event` union, so every consumer sees the same `RunStart` / `MessageComplete`
/ `ToolCall` / `ToolResult` / `RunDone` stream it sees from Claude.

Two translations carry the persona model:

- `AgentConfig.instructions` (the persona prompt) becomes Codex's
  `developer_instructions` config override, passed as `-c
  developer_instructions=<TOML basic string>`. Codex layers it over its own
  base instructions, which is the role `--system-prompt` plays for `claude
  -p`. `json.dumps` renders the string: JSON's escapes are a subset of
  TOML's, so a multi-paragraph prompt with quotes and `=` survives.
  Verified live against Codex CLI 0.154.
- `cfg.extra["permission_mode"]` keeps the Claude Code vocabulary the
  harness already speaks and maps it: `bypassPermissions` / `dontAsk` →
  `--dangerously-bypass-approvals-and-sandbox` (the unattended mode every
  daemon runs in), `acceptEdits` → `workspace-write` sandbox, `plan` →
  `read-only` sandbox.

Knobs Codex has no per-run flag for (`max_turns`, `max_budget_usd`,
`disallowed_tools`, `settings`, `builtin_tools`) are **logged and ignored**
rather than raised, so a config written for `claude_p` — the persona router's
`max_turns: 1` + `plan` mode, for instance — runs unchanged. `cost_usd` is
always `None`: Codex reports tokens, not dollars, and the spend is the
subscription anyway.

### 2. Vendors are declared in `configs/personas.yaml`, resolved in one module

```yaml
default_vendor: claude        # team default: claude | chatgpt
default_model: ""             # team default model; blank = the CLI's own

personas:
  - name: Rukawa
    vendor: chatgpt           # this persona's override (optional)
    model: gpt-6-astra        # this persona's override (optional)
```

`tigerharness/vendors.py` owns the mapping (`claude` → `claude_p`, `chatgpt`
→ `codex_exec`, with the obvious aliases) and the resolution rule:

1. **vendor** — the persona's own value, else the team's `default_vendor`,
   else `claude`. A blank value or the literal `default` means "not set".
2. **model** — the persona's own value if set; otherwise the team's
   `default_model` **only when the persona runs on the team's vendor**. A
   persona that switched vendor and named no model gets that vendor CLI's
   own default. A model id belongs to one vendor, and the team's default may
   name the other's — inheriting it across vendors would hand `codex` a
   Claude model id.

A malformed vendor (`chatgtp`) raises `ValueError`; a missing file, or a
stripped install without pyyaml, logs and falls back to `claude`. A typo
must be a loud startup failure, never a quiet switch to the other vendor's
bill.

### 3. Every launch site resolves through it

| Consumer | Before | Now |
|---|---|---|
| Slack bridge lane | one `claude_p` backend per lane | one backend instance per distinct vendor in the roster; each `PersonaSlot` carries its `backend_name` and its `agent_config.model`; the persona router and pre-routing threads use the team default's backend |
| thread store | `session_id` + persona | also records `backend`; on resume, a record whose backend differs from the persona's current one opens a **fresh** session instead of handing a foreign id to `--resume` (records written before the field existed read as `claude_p`) |
| in-bridge idle compaction (ADR 0004) | `/compact` turn on every heavy lane | skipped for personas not on `claude_p` — `/compact` is a Claude Code prompt turn; a Codex thread compacts itself and would just answer a message reading "/compact" |
| external `compact-idle` pass | `claude_p` for every record | records tagged with another backend are skipped (`vendor_unsupported`) |
| autodrive `start` / auto-start | `--backend` default `claude_p`, `--model` default none | flag > the **driver persona's** vendor/model from personas.yaml > `claude_p`; `--backend` also accepts a vendor name; an explicit `--backend` that differs from the persona's gets no model unless `--model` says which |
| `tigerharness init` | — | asks "Which model vendor should this team use by default?" (+ optional default model) for a **new** team; `--vendor` / `--model` flags; `--yes` takes `claude`; warns when the chosen vendor's CLI is not on `PATH` |

The tiger-memory summarizer's `backend`/`model` fields are untouched: they
are inert (the production sweep is model-free glue over in-session
sub-agents — see [tiger-memory.md](../tiger-memory.md)), and re-plumbing an
unused path would be motion, not progress.

## Consequences

**What this buys.** A team can put one persona on ChatGPT and the rest on
Claude by editing two lines of the roster file, and the Slack bridge honours
it per thread from the next restart. Autodrive honours it for its driver. A
new team picks its vendor at init instead of discovering `claude_p` is a
literal in three modules.

**What it does not yet do — stated so nobody assumes otherwise.**

1. **A journal task runs on the driver's vendor, not the task's persona's.**
   *Resolved by [ADR 0012](0012-drive-lanes.md) (drive lanes).* A drive
   now takes only work whose owner persona runs on its own vendor/model
   lane (`claim` and `step-done` enforce it; `sweep --driver` shows it),
   and autodrive fires one drive per lane with work, waking early on a
   clean completion so a workflow handoff between lanes does not wait out
   an interval. The one stated simplification: a workflow's compile loop
   runs as a unit on the captain's lane.
2. **The memory sweep runs on whichever session triggers it.** *Resolved
   by [ADR 0012](0012-drive-lanes.md) part 2.* A team sweep now restricts
   itself to the sweeping persona's lane (`sweep-plan --lane-of`), and
   autodrive's idle path fires one own-only sweep session per lane with
   pending personas, on that lane's vendor, before its maintenance drive.
   The executor rule is untouched: extraction still runs in the sweeping
   session's own helper sub-agents.
3. **The team's skills are written for Claude Code.** *Resolved the same
   day.* The bundled skills now use a vendor-neutral vocabulary ("helper
   session", "headless CLI session") defined once in the `AGENTS.md`
   runtime glossary, and `init` links `.agents/skills -> .claude/skills`
   so Codex discovers the very same files (Codex reads `.agents/skills/`
   and follows the symlink; verified on Codex CLI 0.154, as was its
   ability to spawn a helper sub-agent that writes a file -- the executor
   the sweep skill relies on). One copy of every skill, no per-vendor
   forks.
4. **Codex transcripts are not ingested by tiger-memory.** *Resolved the
   same day.* A `codex` source (`sources/codex_transcript.py`) reads
   `~/.codex/sessions/**/rollout-*.jsonl`, keeps the sessions opened in the
   team root, attributes them through the same threads.json reverse map
   (the bridge records the Codex thread id as the session id), skips
   helper-session rollouts, and shares the attribution / filtering /
   record shape with the Claude adapter through one base class. New
   personas get the source by default; existing configs add three lines.
5. **The Codex CLI must be on the daemon's `PATH`**, exactly as `claude`
   must (README, "Known limitations"). On this host it lives in
   `~/.local/bin`, which the bridge's systemd drop-in must include.

**Rejected alternatives.**

- *Wrap the `openai-agents` SDK instead of the Codex CLI.* That is
  API-billed and would need a key; it also cannot run the team's skills or
  tools the way an agentic CLI does. The stub stays a stub.
- *Put vendor/model in `.env` (`TIGERHARNESS_*_BACKEND`).* Env is per
  process, the choice is per persona; `personas.yaml` is already the roster
  and the only file every consumer reads.
- *Inherit the team's `default_model` across a vendor switch.* See the
  resolution rule: a model id is meaningless to the other vendor's CLI.
