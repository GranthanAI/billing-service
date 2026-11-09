"""Object storage: an S3-compatible client (MinIO, SeaweedFS, S3) and an in-memory stand-in."""

from typing import Any

import aioboto3  # type: ignore[import-untyped]


class S3Storage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self._session = aioboto3.Session(
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name=region
        )
        self._endpoint = endpoint_url
        self._bucket = bucket

    def _client(self) -> Any:
        return self._session.client("s3", endpoint_url=self._endpoint or None)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        async with self._client() as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    async def get(self, key: str) -> bytes | None:
        async with self._client() as s3:
            try:
                obj = await s3.get_object(Bucket=self._bucket, Key=key)
            except s3.exceptions.NoSuchKey:
                return None
            data: bytes = await obj["Body"].read()
            return data

    async def presigned_get(self, key: str, expires_s: int) -> str:
        async with self._client() as s3:
            url: str = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_s,
            )
            return url


class InMemoryStorage:
    """For tests and local runs without an object store."""

    def __init__(self, base_url: str = "https://objects.test") -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.presigned: list[tuple[str, int]] = []
        self._base = base_url

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes | None:
        found = self.objects.get(key)
        return found[0] if found else None

    async def presigned_get(self, key: str, expires_s: int) -> str:
        self.presigned.append((key, expires_s))
        return f"{self._base}/{key}?expires={expires_s}"
