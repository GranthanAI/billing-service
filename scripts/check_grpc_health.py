"""gRPC health check (grpc.health.v1) of a running billing-api:  python scripts/check_grpc_health.py [127.0.0.1:50061]

Prints SERVING / NOT_SERVING and exits 0 only when the overall service is SERVING. The health service needs no token.
"""

import sys

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:50061"
    try:
        with grpc.insecure_channel(target) as channel:
            stub = health_pb2_grpc.HealthStub(channel)
            resp = stub.Check(health_pb2.HealthCheckRequest(service=""), timeout=3)
    except grpc.RpcError as exc:
        print(f"{target}: {exc.code().name}")
        return 1
    status = health_pb2.HealthCheckResponse.ServingStatus.Name(resp.status)
    print(f"{target}: {status}")
    return 0 if status == "SERVING" else 1


if __name__ == "__main__":
    raise SystemExit(main())
