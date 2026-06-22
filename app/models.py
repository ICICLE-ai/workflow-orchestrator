import os
from datetime import datetime
from sqlalchemy import Integer, String, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import create_async_engine

class Base(DeclarativeBase):
    pass

class StepType(Base):
    """
    Represents a step type definition mapping to a Tapis application.

    Attributes:
        step_type_key (str): The unique key for this step type (e.g., 'preprocess', 'train').
        tapis_app_id (str): The Tapis application ID mapped to this step.
        display_name (str): The display name for the UI.
        config_schema (str, optional): A JSON-serialized schema detailing configuration parameters. Optional for development.
    """
    __tablename__ = "step_types"
    step_type_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    tapis_app_id: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(100))
    config_schema: Mapped[str | None] = mapped_column(Text)

class Workflow(Base):
    """
    Represents a DAG (Directed Acyclic Graph) workflow template definition.

    Attributes:
        workflow_id (int): Primary key for the workflow template.
        name (str): The name of the workflow template.
        description (str, optional): A brief explanation or description of the workflow template.
        wf_nodes (relationship): A collection of WFNode defined in the workflow.
        wf_edges (relationship): A collection of WFEdge linking nodes.
        wf_runs (relationship): The history of WFRun triggered from this workflow template.
    """
    __tablename__ = "workflows"
    workflow_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(255))
    
    wf_nodes: Mapped[list["WFNode"]] = relationship(back_populates="workflow", cascade="all, delete-orphan")
    wf_edges: Mapped[list["WFEdge"]] = relationship(back_populates="workflow", cascade="all, delete-orphan")
    wf_runs: Mapped[list["WFRun"]] = relationship(back_populates="workflow", cascade="all, delete-orphan")

class WFNode(Base):
    """
    Represents a specific task node for a step within a workflow DAG.

    Attributes:
        wf_node_id (int): Primary key for the node configuration.
        workflow_id (int): Foreign key referencing the parent Workflow.
        step_type_key (str): Foreign key referencing the StepType definition of the node.
        node_label (str): The unique label identifier of the node within the DAG.
        workflow (relationship): Parent Workflow instance.
        step_type (relationship): The StepType associated with this node.
        run_steps (relationship): A collection of RunStep associated with this node.
    """
    __tablename__ = "wf_nodes"
    wf_node_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.workflow_id", ondelete="CASCADE"), index=True)
    step_type_key: Mapped[str] = mapped_column(ForeignKey("step_types.step_type_key", ondelete="RESTRICT"), index=True)
    node_label: Mapped[str] = mapped_column(String(100))
    
    workflow: Mapped["Workflow"] = relationship(back_populates="wf_nodes")
    step_type: Mapped["StepType"] = relationship()
    run_steps: Mapped[list["RunStep"]] = relationship(back_populates="wf_node", cascade="all, delete-orphan")

class WFEdge(Base):
    """
    Represents a directional dependency edge connecting two task nodes in a workflow DAG.

    Attributes:
        wf_edge_id (int): Primary key for the edge configuration.
        workflow_id (int): Foreign key referencing the parent Workflow.
        source_wf_node_id (int): Foreign key referencing the source (dependency) WFNode.
        target_wf_node_id (int): Foreign key referencing the target (dependent) WFNode.
        workflow (relationship): Parent Workflow instance.
        source_wf_node (relationship): Source WFNode instance.
        target_wf_node (relationship): Target WFNode instance.
    """
    __tablename__ = "wf_edges"
    wf_edge_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.workflow_id", ondelete="CASCADE"), index=True)
    source_wf_node_id: Mapped[int] = mapped_column(ForeignKey("wf_nodes.wf_node_id", ondelete="CASCADE"), index=True)
    target_wf_node_id: Mapped[int] = mapped_column(ForeignKey("wf_nodes.wf_node_id", ondelete="CASCADE"), index=True)
    
    workflow: Mapped["Workflow"] = relationship(back_populates="wf_edges")
    source_wf_node: Mapped["WFNode"] = relationship("WFNode", foreign_keys=[source_wf_node_id])
    target_wf_node: Mapped["WFNode"] = relationship("WFNode", foreign_keys=[target_wf_node_id])
    
    __table_args__ = (
        UniqueConstraint("workflow_id", "source_wf_node_id", "target_wf_node_id", name="uix_wf_edge"),
    )

class WFRun(Base):
    """
    Represents a run instance of a workflow template.

    Attributes:
        wf_run_id (int): Primary key for the workflow run record.
        workflow_id (int): Foreign key referencing the template Workflow.
        dbos_workflow_id (str): Unique DBOS workflow orchestrator execution ID.
        status (str): Current status of the run (e.g., 'PENDING', 'RUNNING', 'COMPLETED', 'FAILED').
        created_at (datetime): The timestamp when the workflow execution record was created.
        updated_at (datetime): The timestamp when the workflow execution record was last modified.
        workflow (relationship): The parent Workflow template instance.
        run_steps (relationship): The collection of RunStep associated with this node.
    """
    __tablename__ = "wf_runs"
    wf_run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.workflow_id", ondelete="CASCADE"), index=True)
    dbos_workflow_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(50), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    
    workflow: Mapped["Workflow"] = relationship(back_populates="wf_runs")
    run_steps: Mapped[list["RunStep"]] = relationship(back_populates="wf_run", cascade="all, delete-orphan")

class RunStep(Base):
    """
    Represents the execution state and parameters of a specific node within a workflow run.

    Attributes:
        run_step_id (int): Primary key for the step run record.
        wf_run_id (int): Foreign key referencing the parent WFRun instance.
        wf_node_id (int): Foreign key referencing the executed task node (WFNode).
        status (str): The current status of this task step (e.g., 'pending', 'running', 'completed', 'failed').
        tapis_job_uuid (str, optional): The unique identifier of the corresponding Tapis job, if applicable.
        tapis_job_status (str, optional): The status reported from polling the Tapis application.
        inputs (str, optional): A JSON-serialized string representing the resolved parameters/inputs.
        outputs (str, optional): A JSON-serialized string representing the outputs/results of the step execution.
        error_message (str, optional): Details of any exception or failure encountered during execution.
        created_at (datetime): The timestamp when the step run was initialized.
        updated_at (datetime): The timestamp when the step run state was last modified.
        wf_run (relationship): The parent WFRun instance.
        wf_node (relationship): The executed task WFNode instance.
    """
    __tablename__ = "run_steps"
    run_step_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wf_run_id: Mapped[int] = mapped_column(ForeignKey("wf_runs.wf_run_id", ondelete="CASCADE"), index=True)
    wf_node_id: Mapped[int] = mapped_column(ForeignKey("wf_nodes.wf_node_id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    tapis_job_uuid: Mapped[str | None] = mapped_column(String(100))
    tapis_job_status: Mapped[str | None] = mapped_column(String(50))
    inputs: Mapped[str | None] = mapped_column(Text)
    outputs: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    
    wf_run: Mapped["WFRun"] = relationship(back_populates="run_steps")
    wf_node: Mapped["WFNode"] = relationship(back_populates="run_steps")

DATABASE_URL = os.environ.get("DBOS_SYSTEM_DATABASE_URL", "postgresql+asyncpg://dbos:dbos_password@localhost:5433/dbos_db")

engine = create_async_engine(DATABASE_URL)

async def init_db():
    """
    Initializes the database schema by creating all defined tables.

    Inputs:
        None

    Outputs:
        None
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
