import os
from sqlalchemy.orm import Session
from main import SessionLocal
from models import PortDataType, Team, AppUser, PipelineRun

def seed_database():
    db = SessionLocal()

    try:
        # 1. Define Port Data Types
        # These are the only step-related definitions seeded here. Step types and
        # their input/output ports are NOT defined in this file — they are the
        # single source of truth in backend/steps/*/step.json and are synced into
        # the database by sync_step_registry() on backend startup. Keeping a second
        # hardcoded copy here is what previously caused stale ports to reappear.
        data_types = [
            {"type_key": "image_dir", "description": "Directory containing images"},
            {"type_key": "video_file", "description": "Video file (.mp4, .avi, etc)"},
            {"type_key": "json_results", "description": "JSON file with results/annotations"},
            {"type_key": "pytorch_model", "description": "PyTorch model weights (.pth)"},
            {"type_key": "csv_data", "description": "CSV dataset"},
            {"type_key": "shapefile", "description": "Geospatial shapefile"},
            {"type_key": "geopackage", "description": "Geospatial GeoPackage (.gpkg) container"},
            {"type_key": "mission_files_dir", "description": "Directory of exported drone/GCS mission files, one subfolder per format"},
            {"type_key": "yolo_annotations_dir", "description": "Flat directory of YOLO-format label .txt files (one per image) plus classes.txt"},
            {"type_key": "heatmap_image", "description": "Generated heatmap image"},
            {"type_key": "file_path", "description": "File path"},
        ]

        for dt in data_types:
            existing = db.query(PortDataType).filter(PortDataType.type_key == dt["type_key"]).first()
            if not existing:
                db.add(PortDataType(**dt))

        db.commit()

        # 2. Define Mock Team and User
        team = db.query(Team).filter(Team.name == "default_team").first()
        if not team:
            team = Team(name="default_team", description="Default Testing Team")
            db.add(team)
            db.commit()
            db.refresh(team)

        user = db.query(AppUser).filter(AppUser.username == "mock_user").first()
        if not user:
            user = AppUser(username="mock_user", email="mock@example.com", team_id=team.team_id)
            db.add(user)
            db.commit()
            db.refresh(user)

        # 2.6 Define Mock Pipeline Runs
        runs = [
            {"user_id": user.user_id, "name": "Drone Crop Analysis Run #1", "status": "completed"},
            {"user_id": user.user_id, "name": "Model Training Nightly", "status": "running"},
            {"user_id": user.user_id, "name": "Failed Inference Pipeline", "status": "failed"},
        ]
        for run in runs:
            existing = db.query(PipelineRun).filter(PipelineRun.name == run["name"]).first()
            if not existing:
                db.add(PipelineRun(**run))
        
        db.commit()

        # NOTE: Step types and their input/output ports are intentionally NOT
        # seeded here. They are defined in backend/steps/*/step.json and synced
        # into the database by sync_step_registry() on backend startup, which is
        # the single source of truth. Run/restart the backend to populate them.
        print("Database seeded with port data types and mock team/user/runs.")

    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()
