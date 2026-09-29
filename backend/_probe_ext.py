import asyncio

import asyncpg


async def main() -> None:
    c = await asyncpg.connect("postgresql://huanong:huanong@localhost:5432/huanong_test")
    rows = await c.fetch("SELECT extname FROM pg_extension ORDER BY 1")
    print("installed:", [r["extname"] for r in rows])
    rows = await c.fetch(
        "SELECT name, installed_version FROM pg_available_extensions "
        "WHERE name = ANY($1) ORDER BY 1",
        ["zhparser", "pg_trgm", "pg_bigm", "vector", "unaccent"],
    )
    print("available:", [(r["name"], r["installed_version"]) for r in rows])
    await c.close()


asyncio.run(main())
