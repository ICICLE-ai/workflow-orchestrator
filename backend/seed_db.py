import os
from sqlalchemy.orm import Session
from main import SessionLocal
from models import PortDataType, StepTypeRegistry, StepTypePort, Team, AppUser, PipelineRun

def seed_database():
    db = SessionLocal()
    
    try:
        # 1. Define Port Data Types
        data_types = [
            {"type_key": "image_dir", "description": "Directory containing images"},
            {"type_key": "video_file", "description": "Video file (.mp4, .avi, etc)"},
            {"type_key": "json_results", "description": "JSON file with results/annotations"},
            {"type_key": "pytorch_model", "description": "PyTorch model weights (.pth)"},
            {"type_key": "csv_data", "description": "CSV dataset"},
            {"type_key": "shapefile", "description": "Geospatial shapefile"},
            {"type_key": "heatmap_image", "description": "Generated heatmap image"}
        ]
        
        for dt in data_types:
            existing = db.query(PortDataType).filter(PortDataType.type_key == dt["type_key"]).first()
            if not existing:
                db.add(PortDataType(**dt))
        
        db.commit()

        # 2. Define Step Types
        step_types = [
            {
                "step_type_key": "preprocess",
                "display_name": "Preprocessing",
                "description": "Process CSV and Shapefile data",
                "category": "Data Processing",
                "icon": "database"
            },
            {
                "step_type_key": "extractframe",
                "display_name": "Extract Frames",
                "description": "Extract image frames from video",
                "category": "Data Processing",
                "icon": "video"
            },
            {
                "step_type_key": "imgcrop",
                "display_name": "Crop Images",
                "description": "Crop a directory of images",
                "category": "Data Processing",
                "icon": "crop"
            },
            {
                "step_type_key": "training",
                "display_name": "Model Training",
                "description": "Train a new model on annotated images",
                "category": "Machine Learning",
                "icon": "activity"
            },
            {
                "step_type_key": "inference",
                "display_name": "Model Inference",
                "description": "Run inference using a trained model",
                "category": "Machine Learning",
                "icon": "cpu"
            },
            {
                "step_type_key": "object_detector",
                "display_name": "Object Detection",
                "description": "Detect objects in images",
                "category": "Machine Learning",
                "icon": "target"
            },
            {
                "step_type_key": "classifier",
                "display_name": "Image Classifier",
                "description": "Classify images or bounding boxes",
                "category": "Machine Learning",
                "icon": "tag"
            },
            {
                "step_type_key": "heatmap",
                "display_name": "Heatmap Generation",
                "description": "Generate spatial heatmaps from results",
                "category": "Visualization",
                "icon": "map"
            },
            {
                "step_type_key": "visualization",
                "display_name": "Visualization",
                "description": "Generate visualization artifacts",
                "category": "Visualization",
                "icon": "eye"
            }
        ]
        
        for st in step_types:
            existing = db.query(StepTypeRegistry).filter(StepTypeRegistry.step_type_key == st["step_type_key"]).first()
            if not existing:
                db.add(StepTypeRegistry(**st))
        
        db.commit()

        # 2.5 Define Mock Team and User
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

        # 3. Define Step Type Ports
        ports = [
            # Preprocess
            {"step_type_key": "preprocess", "port_name": "csv_input", "direction": "input", "data_type": "csv_data"},
            {"step_type_key": "preprocess", "port_name": "shapefile_input", "direction": "input", "data_type": "shapefile"},
            {"step_type_key": "preprocess", "port_name": "geospatial_out", "direction": "output", "data_type": "json_results"},
            
            # Extract Frame
            {"step_type_key": "extractframe", "port_name": "video_input", "direction": "input", "data_type": "video_file"},
            {"step_type_key": "extractframe", "port_name": "images_out", "direction": "output", "data_type": "image_dir"},
            
            # Image Crop
            {"step_type_key": "imgcrop", "port_name": "images_input", "direction": "input", "data_type": "image_dir"},
            {"step_type_key": "imgcrop", "port_name": "cropped_out", "direction": "output", "data_type": "image_dir"},
            
            # Training
            {"step_type_key": "training", "port_name": "images_input", "direction": "input", "data_type": "image_dir"},
            {"step_type_key": "training", "port_name": "annotations_input", "direction": "input", "data_type": "json_results"},
            {"step_type_key": "training", "port_name": "model_out", "direction": "output", "data_type": "pytorch_model"},
            
            # Inference
            {"step_type_key": "inference", "port_name": "images_input", "direction": "input", "data_type": "image_dir"},
            {"step_type_key": "inference", "port_name": "model_input", "direction": "input", "data_type": "pytorch_model"},
            {"step_type_key": "inference", "port_name": "results_out", "direction": "output", "data_type": "json_results"},
            
            # Object Detector
            {"step_type_key": "object_detector", "port_name": "images_input", "direction": "input", "data_type": "image_dir"},
            {"step_type_key": "object_detector", "port_name": "detections_out", "direction": "output", "data_type": "json_results"},
            
            # Classifier
            {"step_type_key": "classifier", "port_name": "images_input", "direction": "input", "data_type": "image_dir"},
            {"step_type_key": "classifier", "port_name": "regions_input", "direction": "input", "data_type": "json_results", "is_required": False},
            {"step_type_key": "classifier", "port_name": "classifications_out", "direction": "output", "data_type": "json_results"},
            
            # Heatmap
            {"step_type_key": "heatmap", "port_name": "results_input", "direction": "input", "data_type": "json_results"},
            {"step_type_key": "heatmap", "port_name": "heatmap_out", "direction": "output", "data_type": "heatmap_image"},
            
            # Visualization
            {"step_type_key": "visualization", "port_name": "results_input", "direction": "input", "data_type": "json_results"},
        ]
        
        for p in ports:
            existing = db.query(StepTypePort).filter(
                StepTypePort.step_type_key == p["step_type_key"],
                StepTypePort.port_name == p["port_name"],
                StepTypePort.direction == p["direction"]
            ).first()
            if not existing:
                db.add(StepTypePort(**p))
        
        db.commit()
        print("Database seeded successfully with step types and ports!")

    except Exception as e:
        db.rollback()
        print(f"Error seeding database: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_database()
