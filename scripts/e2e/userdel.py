"""Publish user.deleted for ids that exist only in billing (never in auth). Run with billing's venv (aiokafka)."""
import asyncio
import datetime
import json
import sys
import uuid

from aiokafka import AIOKafkaProducer


async def main() -> None:
    p = AIOKafkaProducer(bootstrap_servers="127.0.0.1:9092")
    await p.start()
    for uid in sys.argv[1:]:
        ev = {"event_id": str(uuid.uuid4()), "occurred_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "user_id": uid}
        await p.send_and_wait("user.deleted", json.dumps(ev).encode(), key=uid.encode())
    await p.stop()


asyncio.run(main())
