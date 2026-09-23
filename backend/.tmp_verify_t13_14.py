import psycopg

with psycopg.connect("postgresql://huanong:huanong@localhost:5432/huanong_test") as c:
    with c.cursor() as cur:
        cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','timescaledb')")
        print("1 extensions:", cur.fetchall())

        cur.execute("SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name='env_reading'")
        print("2 hypertable count:", cur.fetchone()[0])

        cur.execute(
            "SELECT column_name, udt_name FROM information_schema.columns "
            "WHERE table_schema='knowledge' AND table_name='knowledge_item' AND column_name='embedding'"
        )
        print("3 embedding:", cur.fetchall())

        cur.execute("SELECT version_num FROM public.alembic_version")
        print("4 alembic_version:", cur.fetchall())

        cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN "
            "('user','farmer_profile','crop','field','device','location','broadcast_log',"
            "'ai_conversation','ai_message','memory_summary','user_profile_tag')"
        )
        print("5 baseline tables:", cur.fetchone()[0])

        cur.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
            "ON a.attrelid=i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid='env_reading'::regclass AND i.indisprimary ORDER BY a.attname"
        )
        print("8 env_reading pk:", cur.fetchall())

        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='ai_conversation'")
        print("6 ai_conversation idx:", cur.fetchall())
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='ai_message'")
        print("7 ai_message idx:", cur.fetchall())
