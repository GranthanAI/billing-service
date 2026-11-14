"""Creates the invoices bucket in the shared dev object store if it is missing (idempotent). Used by dev_up.sh.

    PYTHONPATH=src python scripts/ensure_dev_bucket.py          # reads OBJECT_STORE_* from the environment
"""
import asyncio
import os

import aioboto3


async def main() -> int:
    endpoint = os.environ.get("OBJECT_STORE_ENDPOINT", "")
    bucket = os.environ.get("OBJECT_STORE_BUCKET", "billing-invoices")
    if not endpoint:
        print("OBJECT_STORE_ENDPOINT is empty: invoices stay in memory (PDFs are not shared between api and worker)")
        return 0
    session = aioboto3.Session(aws_access_key_id=os.environ.get("OBJECT_STORE_ACCESS_KEY", ""),
                               aws_secret_access_key=os.environ.get("OBJECT_STORE_SECRET_KEY", ""),
                               region_name=os.environ.get("OBJECT_STORE_REGION", "us-east-1"))
    async with session.client("s3", endpoint_url=endpoint) as s3:
        existing = {b["Name"] for b in (await s3.list_buckets())["Buckets"]}
        if bucket not in existing:
            await s3.create_bucket(Bucket=bucket)
            print(f"created bucket {bucket}")
        else:
            print(f"bucket {bucket} exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
