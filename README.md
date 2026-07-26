# mailroom-agent

Skeleton for the propose/commit mail-triage endpoint. Runs locally, tested
end-to-end with a stubbed decision function (see `test_smoke.py`) — no
model calls needed to verify the propose/commit/replay/conflict plumbing.

## Layout

```
app/
  main.py       single POST endpoint, routes "propose" / "commit"
  models.py     request/response schemas  <-- fix field names, see below
  hashing.py    canonical JSON + fingerprints (replay/conflict/cache keys)
  store.py      SQLite persistence         <-- see deployment note below
  decision.py   prompt, model call, schema + safety validation
  actions.py    effect execution stubs     <-- wire to real systems
api/index.py    Vercel ASGI entrypoint
vercel.json     Vercel routing
test_smoke.py   offline end-to-end test (propose/commit/replay/409/etc.)
```

## Before you submit: two things I could not verify from the pasted spec

1. **Exact field names.** Your doc referenced an "Exact propose request and
   response" / "Exact commit request and terminal response" spec that
   wasn't in what you pasted (probably a collapsed section on the page).
   I built `models.py` from the prose description, and marked every guess
   with `# ASSUMPTION` — mainly:
   - `receiptVerificationKey` on the propose request
   - `decision: "approved"|"rejected"` and `verificationKey` on each receipt
   - the `target`/`payload` field names per action

   Since "exact target/payload fields" is explicitly graded, get the real
   schema and update `models.py` — everything else (hashing, caching,
   replay, conflict handling) is keyed off `dossierId`/`evaluationId`/
   `callId`/`receiptId`, which the transcript confirms, so it shouldn't need
   to change.

2. **Storage durability on Vercel.** `store.py` uses SQLite on local disk.
   Vercel functions have an ephemeral filesystem — the DB file will not
   survive between invocations/cold starts, which will silently break
   caching and replay across separate Check runs (exactly the thing the
   grader is testing). Before deploying there, either:
   - point `MAILROOM_DB` at a real external database (Vercel
     Postgres/KV, Supabase, Neon, etc.) and swap the `sqlite3` calls in
     `store.py` for that client — every other module only calls the
     functions in `store.py`, so this is a contained change; or
   - deploy to a host with a persistent disk instead (Fly.io, Railway,
     Render, a small VPS) and keep SQLite as-is.

## Config (env vars)

```
MAILROOM_DB        path to the SQLite file (default: mailroom.db)
MAILROOM_MODEL      model name (default: gpt-4o-mini)
MAILROOM_API_BASE   OpenAI-compatible base URL (unset = official OpenAI;
                     point this at OpenRouter/Groq/local Ollama/etc.)
MAILROOM_API_KEY    API key for that provider
```

## Run locally

```
pip install -r requirements.txt
uvicorn app.main:app --reload
python3 test_smoke.py   # offline, no API key required
```

## Try it with curl (against the fallback/no-model path)

The smoke test monkeypatches `decide()`; against a live server you'll need
`MAILROOM_API_KEY` set so the real model call in `decision.py` runs. If it's
unset or fails, `decide()` deliberately falls back to `request_confirmation`
rather than crashing or guessing — see `decision.SAFE_FALLBACK`.

## What's already handled, per the grading rules

- **Replay**: identical `evaluationId` + identical dossiers on propose, or
  identical `receiptId` on commit, return the byte-identical cached
  response and never repeat a model call or a tool effect.
- **Conflict**: same `evaluationId`, different dossier content → `409`.
- **Schema/malformed**: bad JSON, missing `operation`, failed Pydantic
  validation, or duplicate `dossierId`/`receiptId` → `400`/`422` before any
  AI or tool work runs.
- **Content-keyed caching**: the model is called once per unique dossier
  *content* (`hashing.dossier_content_fingerprint`), not once per
  evaluation — a `callId` derived from that same fingerprint, so a stable
  dossier gets the same `callId` across evaluations and later Checks.
- **Receipt verification**: commit checks the evaluation exists, the
  `callId`→proposal lookup succeeds and matches `dossierId`, the
  `proposalDigest` matches, and (if the evaluation stored one) the
  `verificationKey` matches, before any effect runs. Any mismatch produces
  a `rejected` outcome rather than executing anything or raising.
- **Prompt-injection posture**: `decision.py`'s system prompt explicitly
  frames the dossier as data, requires evidence to be a verbatim substring
  of the dossier (checked in code, not trusted from the model), and gates
  the one outbound-effect action (`send_approved_notice`) on an explicit
  structured trust marker in the dossier rather than the model's own
  claim — a request lacking that gets downgraded to `request_confirmation`.

## What's still a stub, on purpose

- `actions.py` logs intended effects instead of calling real systems —
  wire in your actual draft/record/notification APIs.
- `decision._find_trust_marker` looks for `dossier["approval"]["trusted"]`
  — replace with whatever your synthetic dossiers actually use to signal
  "this outbound send is pre-approved."
