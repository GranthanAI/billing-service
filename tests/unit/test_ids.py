from uuid import UUID

from billing.adapters.ids import SequentialIds, Uuid7Generator


def test_uuid7_layout_and_order() -> None:
    clock = iter([1_700_000_000_000] * 5 + [1_700_000_000_001])
    gen = Uuid7Generator(now_ms=lambda: next(clock))
    ids = [gen.new() for _ in range(6)]
    for u in ids:
        assert u.version == 7
        assert u.variant == "specified in RFC 4122"
        assert u.int >> 80 in (1_700_000_000_000, 1_700_000_000_001)
    assert ids == sorted(ids)
    assert len(set(ids)) == 6


def test_counter_overflow_borrows_from_the_clock() -> None:
    gen = Uuid7Generator(now_ms=lambda: 1_700_000_000_000)
    ids = [gen.new() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 5000


def test_real_clock_is_monotonic() -> None:
    gen = Uuid7Generator()
    ids = [gen.new() for _ in range(2000)]
    assert ids == sorted(ids)


def test_sequential_ids() -> None:
    ids = SequentialIds()
    assert [ids.new(), ids.new()] == [UUID(int=1), UUID(int=2)]
