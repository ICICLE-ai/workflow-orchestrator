import os
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, create_engine
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    
    id = Column(Integer, primary_key=True)
    workflow_id = Column(String(100), unique=True, index=True)
    dataset_url = Column(String(255))
    tapis_job_uuid = Column(String(100), nullable=True)
    status = Column(String(50))  # PENDING, PREPROCESSING, TRAINING, INFERENCE, COMPLETED, FAILED
    model_accuracy = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

DATABASE_URL = os.environ.get("DBOS_SYSTEM_DATABASE_URL", "postgresql+psycopg2://dbos:dbos_password@localhost:5433/dbos_db")
engine = create_engine(DATABASE_URL)

def init_db():
    Base.metadata.create_all(bind=engine)
