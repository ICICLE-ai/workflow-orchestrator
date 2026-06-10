from main import SessionLocal
from models import WorkflowTemplate, WfNode, WfEdge, PipelineRun

def clear_db():
    db = SessionLocal()
    try:
        print("Deleting WfEdge...")
        db.query(WfEdge).delete()
        print("Deleting WfNode...")
        db.query(WfNode).delete()
        print("Deleting PipelineRuns (to avoid foreign key constraints if any)...")
        db.query(PipelineRun).delete()
        print("Deleting WorkflowTemplate...")
        db.query(WorkflowTemplate).delete()
        db.commit()
        print("Successfully cleared all workflow templates!")
    except Exception as e:
        db.rollback()
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    clear_db()
