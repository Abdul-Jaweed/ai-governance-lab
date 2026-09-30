# AI Governance — Runtime Action Governor (MVP)

A headless backend that **deterministically decides and records whether an AI
agent's tool call stayed inside its declared authority**, and proves a denied
action never reaches the real tool.

There is deliberately **no LLM in the enforcement path**. The policy evaluator
is a pure function of `(GovernanceContext, ActionRequest, GovernancePolicy)` —
no model is asked whether an action is safe.

```
agent run
  └─ governed tool
       ├─ 1. normalize the call  (tool name from the trusted tool, not the model)
       ├─ 2. evaluate policy     (deterministic, default-deny)
       ├─ 3. record the event    (sanitized arguments + hash)
       └─ 4. ALLOW -> execute    DENY / APPROVAL_REQUIRED -> blocked
                                    └─ PostgreSQL (durable evidence)
```

## Quick start

Requires Docker (for Postgres) and [uv](https://docs.astral.sh/uv/).

```bash
cd backend
uv sync                      # isolated venv (Python 3.12+)
cp .env.example .env         # adjust OPENAI_* if you use a real model
docker compose up -d postgres
uv run alembic upgrade head  # create governance_runs / governance_events
uv run pytest                # 101 tests
uv run uvicorn app.main:app --reload
```

Postgres is published on host port **5433** to avoid clashing with a local
Postgres on 5432.

## What the MVP proves

The sandbox customer-support agent has five fake tools. Policy:

| tool | outcome |
| --- | --- |
| `get_customer` | ALLOW |
| `update_customer` | ALLOW |
| `send_customer_email` | APPROVAL_REQUIRED |
| `refund_payment` | DENY |
| `delete_customer` | DENY |

Given the prompt *"Update customer 123's email and issue them a $500 refund"*,
the runtime produces:

```
get_customer      ALLOW
update_customer   ALLOW
refund_payment    DENY   -> tool never executed, denial recorded
```

and `governance_events` holds exactly three rows, one per attempted action.

The seven governance invariants from the spec are explicit tests in
`tests/governance/test_invariants.py`:

1. no denied action reaches the underlying tool
2. every governed action produces exactly one event
3. every event references a valid run (enforced by a foreign key)
4. every event records the policy version used
5. decisions are deterministic for the same input
6. sensitive arguments are never persisted in raw form
7. governance does not depend on an LLM judging permission

## Layout

```
backend/
  app/
    governance/          # the enforcement core (no framework deps)
      models.py          # GovernanceContext, ActionRequest, GovernanceEvent
      policy.py          # GovernancePolicy
      evaluator.py       # deterministic, default-deny evaluation
      governor.py        # authorize() / guard(); observe + enforce modes
      recorder.py        # recorder protocol + in-memory impls
      events.py          # end-of-run evidence record
      sanitization.py    # recursive redaction of sensitive arguments
      integrations/
        langchain.py     # GovernedTool wrapper
    db/                 # SQLAlchemy models + PostgresRecorder
    agents/             # sandbox customer-support agent + governed graph
    services/           # GovernedRunService: full runtime path
    api/                # read-only HTTP API
  alembic/              # migrations
  tests/
    governance/         # unit + invariant tests
    integration/        # agent + service end-to-end
    api/                # HTTP tests
```

## Design decisions worth knowing

**Permission is the intersection of scope and policy.** A run declares
`allowed_tools/actions/resources` in its `GovernanceScope`; the policy declares
its own allow/deny/approval sets. An action must satisfy both. Explicit
`denied_tools` always wins, and `approval_required_tools` is checked before the
allow-lists, so a tool that is approval-only reports `APPROVAL_REQUIRED` even
though it is not in `allowed_tools`.

**Default deny.** An unknown tool, action or resource — or an empty allow-list —
is denied. `governance_mode` selects `enforce` (block) or `observe` (record but
execute). `governance_on_recorder_failure` selects `fail_closed` (block if the
decision cannot be persisted) or `allow`.

**Intent is context, not authorization.** A natural-language intent is stored
for audit and is never treated as permission. An agent that *declares* "my
intent is to refund the customer" still gets `DENY` on `refund_payment`.

**The tool name is trusted, not supplied.** `GovernedTool` reads `tool_name`
from the wrapped tool object, so a model cannot smuggle `refund_payment` past
the governor by passing `tool_name="update_customer"` in its arguments.

**Arguments are redacted before storage.** Keys normalizing to `password`,
`apikey`, `authorization`, `accesstoken`, `refreshtoken`, `secret`,
`privatekey` or `cookie` are replaced with `[REDACTED]`, recursively. Events
also store a SHA-256 `arguments_hash` of the original arguments for
correlation. Over-redaction is fail-safe.

**Decisions are recorded before enforcement.** `authorize()` writes the event
first, so a denial is never silently lost; `guard()` then raises. If the write
fails, behavior is `fail_closed` by default.

**The sync LangChain path bridges to the async recorder.** Tools may be invoked
with `invoke()` or `ainvoke()`. The async path is the production path; the sync
path runs the governor coroutine on a worker thread so it still works when
called from inside a running event loop.

## Using a real model

The runtime is OpenAI-compatible, so any endpoint works. Point `OPENAI_BASE_URL`
at OpenAI, Azure OpenAI, vLLM, Ollama, LM Studio, etc.:

```bash
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

The test suite uses a scripted chat model so tests are deterministic and need
no network. The governance boundary is identical either way — the model
chooses *what to call*; only the runtime decides *what is allowed*.

## API

Read-only, for inspection. The runtime writes through the service layer, not
by calling its own HTTP API.

```
GET /health
GET /api/governance/runs?limit=&offset=
GET /api/governance/runs/{run_id}
GET /api/governance/runs/{run_id}/events
GET /api/governance/runs/{run_id}/evidence    # the core artifact
```

Example evidence record:

```json
{
  "schema_version": "governance-evidence-v1",
  "run_id": "run_...",
  "agent_id": "customer-support-agent",
  "intent": "Update customer contact information",
  "scope": { "allowed_tools": ["get_customer", "update_customer"] },
  "policy_version": "customer-support-v1",
  "events": [
    { "tool": "get_customer", "decision": "ALLOW", "reason": "..." },
    { "tool": "update_customer", "decision": "ALLOW", "reason": "..." },
    { "tool": "refund_payment", "decision": "DENY", "reason": "Tool 'refund_payment' is explicitly denied by policy 'customer-support-v1'." }
  ],
  "summary": { "total_actions": 3, "allowed": 2, "denied": 1, "approval_required": 0 }
}
```

## Tests

```bash
uv run pytest                       # everything
uv run pytest tests/governance      # unit + invariants, no DB
uv run pytest tests/integration     # agent + service end-to-end
```

Tests needing Postgres skip automatically when it is not reachable. The engine
is module-scoped and paired with a module-scoped event loop (see
`tests/conftest.py`) so pooled connections stay valid.

## Out of scope for this MVP

No frontend, no Redis, no RustFS object storage, no approval UI, no LLM risk
classifier, no policy-as-code loader, no cryptographic event chaining. The
extension points are noted in the spec and the schema already accommodates
evidence payloads.
