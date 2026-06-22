import os
import sys

import psycopg2
from dbos import DBOS
from app.models import Base, engine, DATABASE_URL, StepType
from sqlalchemy.orm import sessionmaker

def main():
    print("--- 1. Resetting DBOS System Database (Dropping dbos_db) ---")
    try:
        config = {
            "name": "dbos-example",
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
        conn = psycopg2.connect("postgresql://dbos:dbos_password@localhost:5433/postgres")
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
    """
    print("\n--- 3. Creating Application Database Tables ---")
    try:
        # Re-create all application tables defined in models.py
        Base.metadata.create_all(bind=engine)
        print("[SUCCESS] Created all application database tables.")
    except Exception as e:
        print(f"[ERROR] Creating application database tables failed: {e}")
        sys.exit(1)

    print("\n--- 4. Seeding Predefined Step Types ---")
    try:
        Session = sessionmaker(bind=engine)
        with Session() as session:
            types_data = [
                {"key": "preprocess", "app": "preprocessing-pipeline", "name": "Preprocessing"},
                {"key": "train", "app": "training-pipeline", "name": "Training"},
                {"key": "inference", "app": "inference-pipeline", "name": "Inference"}
            ]
            for t in types_data:
                session.add(StepType(
                    step_type_key=t["key"],
                    tapis_app_id=t["app"],
                    display_name=t["name"]
                ))
            session.commit()
        print("[SUCCESS] Predefined step types seeded successfully.")
    except Exception as e:
        print(f"[ERROR] Seeding predefined step types failed: {e}")
        sys.exit(1)
    """

    print("\n--- 5. Clearing Mock Tapis Jobs ---")
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

    print("\n[COMPLETE] DBOS and application environments have been completely cleared, reset, and seeded!")

if __name__ == "__main__":
    main()
