from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import actions, store
from .decision import decide
from .hashing import (
    dossier_call_id,
    dossier_content_fingerprint,
    dossiers_batch_fingerprint,
)
from .models import (
    CommitRequest,
    CommitResponse,
    Outcome,
    Proposal,
    ProposeRequest,
    ProposeResponse,
    Receipt,
)

logger = logging.getLogger("mailroom")

ENDPOINT_PATH = "/v1/mailroom/actions"  # suggested path for your own reference/curl testing only --
                                         # the route below matches ANY path, so whatever URL you
                                         # actually register with the grader will still work.
MAX_REQUEST_BYTES = 2 * 1024 * 1024  # generous; tune to your traffic
MAX_RESPONSE_BYTES = 512 * 1024  # per spec

# The grader sends ~64 dossiers per propose within a 55s budget. Calling
# the model once per dossier sequentially blows that (64 * a few seconds).
# Fan the uncached decisions out across a bounded thread pool instead.
# Tune down if you hit provider rate limits, up if you have headroom.
PROPOSE_CONCURRENCY = int(os.environ.get("MAILROOM_CONCURRENCY", "12"))

app = FastAPI(title="mailroom-agent")


@app.on_event("startup")
def _startup() -> None:
    store.init_db()


# Belt-and-suspenders: some serverless ASGI wrappers (Vercel's included)
# don't reliably run the startup lifespan event, which would otherwise
# leave the DB uninitialized and turn the very first request into an
# uncaught "no such table" 500. Running it once at import time too is
# idempotent (CREATE TABLE IF NOT EXISTS) and cheap.
store.init_db()


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Any bug should still come back as valid JSON with the right
    # Content-Type, per the spec, and get logged with a full traceback so
    # it's diagnosable from server logs instead of a bare 500.
    logger.exception("unhandled error on %s", request.url.path)
    return JSONResponse(
        content={"error": "internal_error", "detail": str(exc)},
        status_code=500,
        media_type="application/json",
    )


def _json_response(payload: dict, status_code: int = 200) -> JSONResponse:
    body = json.dumps(payload).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        # Should not happen with the schemas above, but the spec is explicit
        # that an oversized successful body must be rejected -- fail loudly
        # in logs rather than silently ship a body the grader will bounce.
        logger.error("response exceeded %d bytes", MAX_RESPONSE_BYTES)
        raise HTTPException(500, "internal: response too large")
    return JSONResponse(content=payload, status_code=status_code, media_type="application/json")


@app.get("/health")
async def health() -> JSONResponse:
    """Confirms the deployment can actually reach its model. Hit this after
    setting env vars to verify config WITHOUT spending a Check attempt.
    Returns model_ok=true only if a real round-trip to the provider works."""
    from .decision import API_BASE, MODEL_NAME, call_model

    info: dict[str, Any] = {
        "model_name": MODEL_NAME,
        "api_base": API_BASE,
        "api_key_present": bool(__import__("os").environ.get("MAILROOM_API_KEY")),
    }
    try:
        call_model({"dossierId": "healthcheck", "body": "ping"})
        info["model_ok"] = True
    except Exception as exc:  # noqa: BLE001 - surface the real reason
        info["model_ok"] = False
        info["error"] = str(exc)[:500]
    return JSONResponse(content=info, media_type="application/json")


@app.post("/{full_path:path}")
async def mailroom_endpoint(full_path: str, request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=400, detail="request body too large")

    try:
        body: Any = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed JSON body")

    if not isinstance(body, dict) or "operation" not in body:
        raise HTTPException(status_code=400, detail="missing 'operation'")

    op = body.get("operation")
    if op == "propose":
        return handle_propose(body)
    if op == "commit":
        return handle_commit(body)
    raise HTTPException(status_code=400, detail=f"unknown operation {op!r}")


# ------------------------------------------------------------------- propose

def handle_propose(body: dict) -> JSONResponse:
    try:
        req = ProposeRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    dossier_ids = [d.dossierId for d in req.dossiers]
    if len(dossier_ids) != len(set(dossier_ids)):
        raise HTTPException(status_code=400, detail="duplicate dossierId in request")

    dossiers_raw = [d.model_dump() for d in req.dossiers]
    batch_fp = dossiers_batch_fingerprint(dossiers_raw)

    existing = store.get_evaluation(req.evaluationId)
    if existing is not None:
        if existing["dossiers_fingerprint"] == batch_fp:
            # Exact replay: return the byte-equivalent cached response,
            # no model calls, no re-persisting proposals.
            return _json_response(json.loads(existing["response_json"]))
        raise HTTPException(
            status_code=409,
            detail="evaluationId already used with different dossier content",
        )

    # --- Stage 1: resolve a decision for every dossier. Cache hits are
    # free; cache misses each need a model call, so run those misses
    # concurrently to stay within the per-request time budget. Writes to
    # the decision cache happen here; proposal persistence happens in
    # stage 2 so the response is built in a single deterministic pass.
    content_fps: dict[str, str] = {}
    decisions: dict[str, dict] = {}
    misses: list[dict] = []

    for d in dossiers_raw:
        did = d["dossierId"]
        content_fp = dossier_content_fingerprint(d)
        content_fps[did] = content_fp
        cached = store.get_cached_decision(content_fp)
        if cached is None:
            misses.append(d)
        else:
            decisions[did] = cached

    def _resolve(dossier: dict) -> tuple[str, dict]:
        # decide() never raises -- it returns SAFE_FALLBACK on failure --
        # so one bad dossier can't sink the whole batch.
        return dossier["dossierId"], decide(dossier)

    if misses:
        workers = max(1, min(PROPOSE_CONCURRENCY, len(misses)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for did, decision in pool.map(_resolve, misses):
                decisions[did] = decision
                store.save_decision(
                    content_fps[did],
                    decision["action"],
                    decision["target"],
                    decision["payload"],
                    decision["evidence"],
                )

    # --- Stage 2: build proposals in the original dossier order. Fully
    # deterministic, no model calls, one proposal per dossier.
    proposals: list[Proposal] = []
    for d in dossiers_raw:
        did = d["dossierId"]
        content_fp = content_fps[did]
        call_id = dossier_call_id(did)
        decision = decisions[did]

        input_digest = content_fp
        store.save_proposal(
            call_id, did, decision["action"], decision["target"], decision["payload"], input_digest
        )

        proposals.append(
            Proposal(
                dossierId=did,
                callId=call_id,
                action=decision["action"],
                target=decision["target"],
                payload=decision["payload"],
                evidence=decision["evidence"],
                inputDigest=input_digest,
            )
        )

    response = ProposeResponse(status="awaiting_receipts", proposals=proposals)
    response_dict = response.model_dump()
    store.save_evaluation(req.evaluationId, batch_fp, response_dict, req.receiptVerificationKey)
    return _json_response(response_dict)


# -------------------------------------------------------------------- commit

def handle_commit(body: dict) -> JSONResponse:
    try:
        req = CommitRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors())

    receipt_ids = [r.receiptId for r in req.receipts]
    if len(receipt_ids) != len(set(receipt_ids)):
        raise HTTPException(status_code=400, detail="duplicate receiptId in request")

    outcomes: list[dict] = []
    for r in req.receipts:
        cached = store.get_commit_outcome(r.receiptId)
        if cached is not None:
            # Exact replay of a commit: return the prior outcome, do not
            # re-execute any tool effect.
            outcomes.append(cached)
            continue

        outcome = _process_receipt(r)
        store.save_commit_outcome(r.receiptId, outcome)
        outcomes.append(outcome)

    response = CommitResponse(status="completed", outcomes=[Outcome(**o) for o in outcomes])
    return _json_response(response.model_dump())


def _rejected(r: Receipt, reason: str, action: str | None = None) -> dict:
    return {
        "receiptId": r.receiptId,
        "callId": r.callId,
        "dossierId": r.dossierId,
        "status": "rejected",
        "action": action,
        "detail": reason,
    }


def _process_receipt(r: Receipt) -> dict:
    evaluation = store.get_evaluation(r.evaluationId)
    if evaluation is None:
        return _rejected(r, "unknown evaluationId")

    proposal = store.get_proposal(r.callId)
    if proposal is None or proposal["dossier_id"] != r.dossierId:
        return _rejected(r, "unknown callId or dossierId does not match persisted proposal")

    if r.inputDigest and r.inputDigest != proposal["digest"]:
        return _rejected(r, "inputDigest does not match persisted proposal", proposal["action"])

    stored_key = evaluation["receipt_key"]
    if stored_key and r.verificationKey != stored_key:
        return _rejected(r, "invalid receipt verification key", proposal["action"])

    if r.decision != "approved":
        return {
            "receiptId": r.receiptId,
            "callId": r.callId,
            "dossierId": r.dossierId,
            "status": "rejected",
            "action": proposal["action"],
            "detail": "grader did not approve this proposal",
        }

    target = json.loads(proposal["target_json"])
    payload = json.loads(proposal["payload_json"])
    detail = actions.execute(r.callId, proposal["action"], target, payload)

    status = "no_action" if proposal["action"] == "no_action" else "executed"
    return {
        "receiptId": r.receiptId,
        "callId": r.callId,
        "dossierId": r.dossierId,
        "status": status,
        "action": proposal["action"],
        "detail": detail,
    }
