from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def make_engine(url: str, *, pool_size: int = 10) -> AsyncEngine:
    """Async engine; every connection runs with search_path = billing and UTC."""
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": "billing", "timezone": "UTC"}},
    )
