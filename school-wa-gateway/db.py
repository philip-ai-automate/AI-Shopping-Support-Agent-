import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()


def get_db():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", 5432)),
        user=os.getenv("PG_USER", "phixtra_pg"),
        password=os.getenv("PG_PASSWORD", ""),
        dbname=os.getenv("PG_DB", "ai_support"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
