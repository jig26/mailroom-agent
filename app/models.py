"""
Request/response schemas.

IMPORTANT: the task doc referenced an "Exact propose request and response"
and "Exact commit request and terminal response" spec that wasn't included
in what you pasted. Everything marked "# ASSUMPTION" below is my best
reasonable guess from the prose description, not a confirmed field name.
Since "exact target/payload fields" is a graded category, swap these for
the platform's real field names before you submit -- that's the single
highest-leverage fix once you have the real spec in hand.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ActionType = Literal[
    "create_draft",
    "update_internal_record",
    "send_approved_notice",
    "request_confirmation",
    "quarantine_item",
    "no_action",
]

ALLOWED_ACTIONS: set[str] = {
    "create_draft",
    "update_internal_record",
    "send_approved_notice",
    "request_confirmation",
    "quarantine_item",
    "no_action",
}


class Dossier(BaseModel):
    """A single mail record. Content fields beyond dossierId are left open
    (`extra="allow"`) since the exact mail-record schema wasn't provided --
    the decision layer treats the whole object as untrusted data anyway."""

    model_config = ConfigDict(extra="allow")

    dossierId: str


class ProposeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    operation: Literal["propose"]
    evaluationId: str
    dossiers: list[Dossier]
    # ASSUMPTION: a per-evaluation secret used to authenticate receipts at
    # commit time. Rename/relocate once you have the real field.
    receiptVerificationKey: Optional[str] = None


class Proposal(BaseModel):
    dossierId: str
    callId: str
    action: ActionType
    target: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    # Fingerprint of the dossier's CONTENT that produced this proposal
    # (== hashing.dossier_content_fingerprint). Lets the grader/receipt
    # verify the proposal was made against a specific, stable input.
    inputDigest: str


class ProposeResponse(BaseModel):
    status: Literal["awaiting_receipts"]
    proposals: list[Proposal]


class Receipt(BaseModel):
    model_config = ConfigDict(extra="allow")

    evaluationId: str
    callId: str
    dossierId: str
    receiptId: str
    # ASSUMPTION: grader's approve/reject verdict for this proposal.
    decision: Literal["approved", "rejected"]
    # ASSUMPTION: should match the proposal's inputDigest.
    inputDigest: Optional[str] = None
    # ASSUMPTION: matched against the evaluation's receiptVerificationKey.
    verificationKey: Optional[str] = None


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    operation: Literal["commit"]
    receipts: list[Receipt]


class Outcome(BaseModel):
    receiptId: str
    callId: str
    dossierId: str
    status: Literal["executed", "no_action", "rejected", "error"]
    action: Optional[str] = None
    detail: Optional[str] = None


class CommitResponse(BaseModel):
    status: Literal["completed"]
    outcomes: list[Outcome]
