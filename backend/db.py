"""Shared database configuration: connection URL, engine, and session factory.

Extracted so both the FastAPI app (main.py) and the DBOS execution engine
(engine/*) import the SAME engine and URL without a circular dependency on
main.py. DBOS stores its internal durable-execution state in this same database
(in its own schema), per the single-database design.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Database configuration (psycopg2 driver). Matches backend/docker-compose.yml:
# user=postgres password=password db=harvest on host port 5433.
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5433")
DB_NAME = os.getenv("DB_NAME", "harvest")
DB_USER = os.getenv("DB_USER", "wo")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")

DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
