"""
Canonical JSON encoding and content-fingerprinting.

Everything that needs to be compared for replay/conflict detection or used
as a cache key goes through here, so there is exactly one definition of
"the same content" in the whole service.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace, stable
    across process restarts and across Python versions."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def fingerprint(obj: Any) -> str:
    """Content fingerprint of any JSON-serializable object."""
    return sha256_hex(canonical_json(obj))


def dossier_content_fingerprint(dossier: dict) -> str:
    """Fingerprint of a dossier's CONTENT only (dossierId excluded).

    This is the cache key for AI decisions and the key used to derive a
    stable callId, per the requirement that identical dossier content must
    produce the identical proposal/callId across different evaluationIds.
    """
    body = {k: v for k, v in dossier.items() if k != "dossierId"}
    return fingerprint(body)


def dossier_call_id(dossier_id: str) -> str:
    """Deterministic callId derived from dossierId alone (never from
    content). This guarantees uniqueness even when two distinct dossiers
    share identical content (the "duplicates" case type), and stability
    across evaluations/Checks since dossierId itself is stable."""
    return f"cid_{sha256_hex(dossier_id)[:24]}"


def dossiers_batch_fingerprint(dossiers: list[dict]) -> str:
    """Fingerprint of an entire propose request's dossier set, used to tell
    an exact replay (same evaluationId + same dossiers) apart from a
    changed-content conflict (same evaluationId + different dossiers)."""
    ordered = sorted(dossiers, key=lambda d: d["dossierId"])
    return fingerprint(ordered)
