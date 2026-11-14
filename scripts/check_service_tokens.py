r"""Checks that each calling service's gRPC token is accepted by the RUNNING billing.

    .venv\Scripts\python scripts\check_service_tokens.py [--env .env.dev] [--target 127.0.0.1:50061]

Reads CONV/FILE/LLM/MEM_BILLING_TOKEN from .env.dev (a value is everything after `=` up to the end of the line: it is
stripped of whitespace and quotes, and a trailing ` # comment` is dropped, so even an old-style file works), calls
GetEntitlements for a random user id with each token and prints OK or the gRPC status. Never prints a token.
Exit code 0 only when all four are OK.
"""

import argparse
import re
import sys
import uuid
from pathlib import Path

import grpc

from billing.gen.granthan.billing.v1 import billing_pb2, billing_pb2_grpc

SERVICES = [("conversation", "CONV_BILLING_TOKEN"), ("file", "FILE_BILLING_TOKEN"),
            ("llm", "LLM_BILLING_TOKEN"), ("memory", "MEM_BILLING_TOKEN")]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            raw = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip("\"'")
            values[m.group(1)] = raw
    return values


def check(target: str, token: str) -> str:
    try:
        with grpc.insecure_channel(target) as channel:
            billing_pb2_grpc.BillingInternalStub(channel).GetEntitlements(
                billing_pb2.GetEntitlementsRequest(user_id=str(uuid.uuid4())), timeout=5,
                metadata=[("authorization", f"Bearer {token}")])
        return "OK"
    except grpc.RpcError as exc:
        return exc.code().name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=str(Path(__file__).resolve().parents[1] / ".env.dev"))
    ap.add_argument("--target", default="127.0.0.1:50061")
    args = ap.parse_args()
    env = read_env(Path(args.env))
    ok = True
    for name, key in SERVICES:
        token = env.get(key, "")
        result = check(args.target, token) if token else "MISSING_IN_ENV_FILE"
        ok &= result == "OK"
        print(f"{name:<13} {key:<18} (len {len(token):>2})  {result}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
