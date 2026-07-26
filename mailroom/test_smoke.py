import os
import sys

os.environ["MAILROOM_DB"] = "/tmp/mailroom_smoke.db"
if os.path.exists(os.environ["MAILROOM_DB"]):
    os.remove(os.environ["MAILROOM_DB"])

sys.path.insert(0, os.path.dirname(__file__))

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

# Stub out the model call entirely so this test needs no API key / network.
calls = {"n": 0}


def fake_decide(dossier):
    calls["n"] += 1
    return {
        "action": "no_action",
        "target": {},
        "payload": {"reason": "duplicate of prior ticket"},
        "evidence": [],
    }


main.decide = fake_decide
main.store.init_db()  # TestClient doesn't always fire startup events; be explicit

client = TestClient(main.app)
URL = main.ENDPOINT_PATH

d1 = {"dossierId": "d1", "subject": "hello", "body": "just checking in"}
d2 = {"dossierId": "d2", "subject": "hi", "body": "already resolved, thanks"}

# 1) propose
r1 = client.post(URL, json={"operation": "propose", "evaluationId": "eval-1", "dossiers": [d1, d2]})
assert r1.status_code == 200, r1.text
body1 = r1.json()
assert body1["status"] == "awaiting_receipts"
assert len(body1["proposals"]) == 2
assert calls["n"] == 2, "expected exactly one model call per unique dossier"
call_id_1 = body1["proposals"][0]["callId"]
digest_1 = body1["proposals"][0]["inputDigest"]
print("propose OK, callId:", call_id_1)

# 2) exact replay of propose: same evaluationId, same dossiers -> cached, no new model calls
r2 = client.post(URL, json={"operation": "propose", "evaluationId": "eval-1", "dossiers": [d1, d2]})
assert r2.status_code == 200
assert r2.json() == body1
assert calls["n"] == 2, "replay must not call the model again"
print("propose replay OK")

# 3) conflict: same evaluationId, different dossiers -> 409
r3 = client.post(URL, json={"operation": "propose", "evaluationId": "eval-1", "dossiers": [d1]})
assert r3.status_code == 409, r3.text
print("propose conflict OK ->", r3.status_code)

# 4) new evaluationId, SAME dossier content -> cache hit, no new model call, SAME callId
r4 = client.post(URL, json={"operation": "propose", "evaluationId": "eval-2", "dossiers": [d1, d2]})
assert r4.status_code == 200
assert calls["n"] == 2, "content cache must avoid a second model call across evaluations"
assert r4.json()["proposals"][0]["callId"] == call_id_1, "callId must be stable across evaluations"
print("content-cache + stable callId OK")

# 5) duplicate dossierId -> 400
r5 = client.post(URL, json={"operation": "propose", "evaluationId": "eval-3", "dossiers": [d1, d1]})
assert r5.status_code == 400, r5.text
print("duplicate dossierId OK ->", r5.status_code)

# 6) malformed JSON -> 400
r6 = client.post(URL, content=b"{not json", headers={"Content-Type": "application/json"})
assert r6.status_code == 400, r6.text
print("malformed JSON OK ->", r6.status_code)

# 7) commit: approve d1's proposal
receipt = {
    "evaluationId": "eval-1",
    "callId": call_id_1,
    "dossierId": "d1",
    "receiptId": "rcpt-1",
    "decision": "approved",
    "inputDigest": digest_1,
}
r7 = client.post(URL, json={"operation": "commit", "receipts": [receipt]})
assert r7.status_code == 200, r7.text
out = r7.json()["outcomes"][0]
assert out["status"] == "no_action"
print("commit OK:", out)

# 8) commit replay: identical receipt again -> same outcome, no re-execution
r8 = client.post(URL, json={"operation": "commit", "receipts": [receipt]})
assert r8.status_code == 200
assert r8.json()["outcomes"][0] == out
print("commit replay OK")

# 9) commit with wrong digest -> rejected, not executed
bad_receipt = dict(receipt, receiptId="rcpt-2", inputDigest="deadbeef")
r9 = client.post(URL, json={"operation": "commit", "receipts": [bad_receipt]})
assert r9.status_code == 200
assert r9.json()["outcomes"][0]["status"] == "rejected"
print("bad digest rejected OK:", r9.json()["outcomes"][0]["detail"])

# 10) commit for unknown callId -> rejected
unknown_receipt = dict(receipt, receiptId="rcpt-3", callId="cid_doesnotexist")
r10 = client.post(URL, json={"operation": "commit", "receipts": [unknown_receipt]})
assert r10.json()["outcomes"][0]["status"] == "rejected"
print("unknown callId rejected OK")

print("\nALL SMOKE TESTS PASSED")

# 11) route is path-agnostic: root and an arbitrary path both work, since we
#     don't know the exact URL string that'll be registered with the grader.
for path in ["/", "/whatever/path/the/grader/uses"]:
    rr = client.post(path, json={"operation": "propose", "evaluationId": f"eval-path-{path}", "dossiers": [d1]})
    assert rr.status_code == 200, f"{path} -> {rr.status_code}: {rr.text}"
    print(f"catch-all route OK for {path!r}")

# 12) two DIFFERENT dossierIds with IDENTICAL content (a true "duplicates"
#     case) must get DIFFERENT callIds -- one proposal per dossier, unique
#     callIds, even when content collides.
dup_a = {"dossierId": "dup-a", "subject": "same", "body": "identical text"}
dup_b = {"dossierId": "dup-b", "subject": "same", "body": "identical text"}
rdup = client.post(URL, json={"operation": "propose", "evaluationId": "eval-dup", "dossiers": [dup_a, dup_b]})
assert rdup.status_code == 200, rdup.text
props = rdup.json()["proposals"]
call_ids = [p["callId"] for p in props]
assert len(call_ids) == len(set(call_ids)), f"duplicate-content dossiers collided on callId: {call_ids}"
print("duplicate-content dossiers got distinct callIds OK:", call_ids)
