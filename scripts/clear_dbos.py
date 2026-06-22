import os
import sys

import psycopg2
from dbos import DBOS
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DBOS_DATABASE_URL")


def main():
    print("--- 1. Resetting DBOS System Database (Dropping dbos_db) ---")
    try:
        config = {
            "name": "workflow-orchestrator",
            "system_database_url": DATABASE_URL,
        }
        # Initialize DBOS configuration
        DBOS(config=config)
        # Reset DBOS system tables (drops dbos_db database)
        DBOS.reset_system_database()
        print("[SUCCESS] DBOS system database reset complete (dbos_db dropped).")
    except Exception as e:
        print(f"[ERROR] Resetting DBOS system database failed: {e}")
        sys.exit(1)
    finally:
        DBOS.destroy()

    print("\n--- 2. Re-creating dbos_db Database ---")

    try:
        # Connect to 'postgres' database to run CREATE DATABASE
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_catalog.pg_database WHERE datname = 'dbos_db';")
        exists = cur.fetchone()
        if not exists:
            cur.execute("CREATE DATABASE dbos_db;")
            print("[SUCCESS] Created dbos_db database.")
        else:
            print("[INFO] dbos_db database already exists.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Re-creating dbos_db database failed: {e}")
        sys.exit(1)

    print("\n--- 3. Clearing Mock Tapis Jobs ---")
    tapis_jobs_path = "tapis_jobs.json"
    if os.path.exists(tapis_jobs_path):
        try:
            os.remove(tapis_jobs_path)
            print(f"[SUCCESS] Removed {tapis_jobs_path}.")
        except Exception as e:
            print(f"[ERROR] Removing {tapis_jobs_path} failed: {e}")
            sys.exit(1)
    else:
        print(f"[INFO] {tapis_jobs_path} does not exist (already clean).")

    print("\n[COMPLETE] DBOS and application environments have been completely cleared!")


if __name__ == "__main__":
    main()
