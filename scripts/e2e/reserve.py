"""Reserve-only calls straight to billing's gRPC, through conversation-service's own client (no model is called).
usage (from conversation-service/, with its venv):  reserve.py <user_id> <model|-> <count> [message|youtube|manim]
Prints one JSON object per call."""
import asyncio
import json
import sys
import uuid

sys.path.insert(0, ".")
from app.clients.billing_client import DENY_REASONS, GROUP_NAMES, billing_client  # noqa: E402
from app.clients.billing_grpc import billing_pb2 as pb  # noqa: E402

KINDS = {"message": pb.RESERVE_KIND_MESSAGE, "youtube": pb.RESERVE_KIND_YOUTUBE_VIDEO, "manim": pb.RESERVE_KIND_MANIM_VIDEO}


async def main() -> None:
    uid, model, count = sys.argv[1], ("" if sys.argv[2] == "-" else sys.argv[2]), int(sys.argv[3])
    kind = KINDS[sys.argv[4] if len(sys.argv) > 4 else "message"]
    for _ in range(count):
        req = pb.CheckAndReserveRequest(user_id=uid, idempotency_key=str(uuid.uuid4()), kind=kind, requested_model=model)
        r = await billing_client._call("CheckAndReserve", req, 2.0)
        print(json.dumps({
            "allowed": r.allowed, "deny_reason": DENY_REASONS.get(r.deny_reason, "" if r.allowed else "unknown"),
            "message_key": r.message_key, "group": GROUP_NAMES.get(r.granted_group), "model": r.granted_model,
            "downgraded": r.downgraded, "funded_by": r.funded_by, "remaining_today": r.remaining_today, "hold_id": r.hold_id}))


asyncio.run(main())
