"""
Executing an APPROVED proposal. These are intentionally inert stubs that
just record what would have happened -- wire each one to your real draft
queue / internal DB / notification system before this counts as a live
integration. Keeping them behind one function makes it easy to guarantee
"only an approved proposal may execute": this is the only place in the
codebase allowed to cause a side effect.
"""
from __future__ import annotations

from . import store


def execute(call_id: str, action: str, target: dict, payload: dict) -> str:
    """Run the effect for an approved action. Returns a short human-readable
    detail string. Must never raise for a well-formed action; catch and
    convert integration errors to a detail string upstream if needed."""

    if action == "no_action":
        detail = "no effect required"

    elif action == "quarantine_item":
        detail = f"isolated (reason: {payload.get('reason', 'unspecified')})"

    elif action == "request_confirmation":
        detail = f"routed to approval queue (reason: {payload.get('reason', 'unspecified')})"

    elif action == "create_draft":
        queue = target.get("queue", "default")
        # TODO: call your real draft-creation API here.
        detail = f"draft created in queue '{queue}' (not sent)"

    elif action == "update_internal_record":
        record_id = target.get("recordId")
        field = target.get("field")
        # TODO: call your real internal-record update API here.
        detail = f"updated field '{field}' on record '{record_id}'"

    elif action == "send_approved_notice":
        recipient = target.get("recipient")
        template = target.get("template")
        # TODO: call your real outbound-send API here. By the time we get
        # here, decision.py has already required an explicit trust marker
        # AND the grader has already approved this exact proposal via
        # receipt -- do not add any other outbound path.
        detail = f"notice sent to '{recipient}' using template '{template}'"

    else:  # pragma: no cover - guarded by schema validation upstream
        detail = f"unrecognized action '{action}', treated as no-op"

    store.record_effect(call_id, action, {"target": target, "payload": payload, "detail": detail})
    return detail
