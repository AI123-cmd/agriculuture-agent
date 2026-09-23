"""临时脚本：创建角色、库与扩展（用完即删，不入提交物）。"""
import asyncio

import asyncpg

ADMIN = dict(host="localhost", port=5432, user="postgres", password="postgres", timeout=10)
DBS = ("huanong", "huanong_test")


async def main() -> None:
    conn = await asyncpg.connect(database="postgres", **ADMIN)
    for db in DBS:
        exists = await conn.fetchval("select 1 from pg_database where datname=$1", db)
        if exists:
            print("db exists:", db)
        else:
            await conn.execute(f'CREATE DATABASE "{db}"')
            print("created db:", db)
    role = await conn.fetchval("select 1 from pg_roles where rolname='huanong'")
    if role:
        await conn.execute("ALTER ROLE huanong LOGIN PASSWORD 'huanong'")
        print("role exists, password reset")
    else:
        await conn.execute("CREATE ROLE huanong LOGIN PASSWORD 'huanong'")
        print("created role: huanong")
    await conn.close()

    for db in DBS:
        conn = await asyncpg.connect(database=db, **ADMIN)
        await conn.execute(f'ALTER DATABASE "{db}" OWNER TO huanong')
        await conn.execute("GRANT ALL ON SCHEMA public TO huanong")
        await conn.execute("ALTER SCHEMA public OWNER TO huanong")
        await conn.execute("GRANT CREATE ON DATABASE " + f'"{db}"' + " TO huanong")
        await conn.close()
        print("ownership granted:", db)

    for db in DBS:
        conn = await asyncpg.connect(database=db, user="huanong", password="huanong", host="localhost", port=5432, timeout=10)
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        rows = await conn.fetch("select extname, extversion from pg_extension order by extname")
        print(db, "extensions:", [(r["extname"], r["extversion"]) for r in rows])
        await conn.close()


asyncio.run(main())
