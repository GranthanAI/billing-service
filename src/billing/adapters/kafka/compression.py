"""Kafka compression codecs: checked when the worker starts, not on the first compressed message.

Producers in this platform use lz4 (llm-service) and gzip (billing); aiokafka decodes lz4, snappy
and zstd with `cramjam`. A worker without a codec cannot read a message that uses it, so it
refuses to start instead.
"""

from collections.abc import Callable


def _aiokafka_checks() -> dict[str, Callable[[], bool]]:
    from aiokafka import codec

    return {
        "gzip": codec.has_gzip,
        "snappy": codec.has_snappy,
        "lz4": codec.has_lz4,
        "zstd": codec.has_zstd,
    }


def missing_codecs(checks: dict[str, Callable[[], bool]] | None = None) -> list[str]:
    try:
        checks = checks if checks is not None else _aiokafka_checks()
    except Exception:  # noqa: BLE001  aiokafka itself missing
        return ["gzip", "snappy", "lz4", "zstd"]
    missing = []
    for name, available in checks.items():
        try:
            ok = bool(available())
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            missing.append(name)
    return missing


def require_codecs(checks: dict[str, Callable[[], bool]] | None = None) -> None:
    """Raise RuntimeError naming the missing codecs if any is unavailable."""
    missing = missing_codecs(checks)
    if missing:
        raise RuntimeError(
            f"Kafka compression codec(s) not available: {', '.join(missing)}. "
            "Install the codec library (`uv add cramjam`): without it the worker could not read "
            "messages compressed that way."
        )
