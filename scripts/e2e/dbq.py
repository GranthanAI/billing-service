"""Run one SQL statement against the dev auth DB (needs the auth-service venv: asyncpg). Used only to activate throwaway
users (the e-mail OTP exists only in the e-mail) and to give one of them a staff role. Prints the rows."""
import asyncio
import os
import sys

import asyncpg


async def main() -> None:
    url = os.environ.get("AUTH_DB_URL", "postgresql://postgres:1234@localhost:5432/auth_db")
    conn = await asyncpg.connect(url)
    for row in await conn.fetch(sys.argv[1]):
        print(" ".join(str(v) for v in row.values()))
    await conn.close()


asyncio.run(main())
