from sqlalchemy import Column, Integer, String, Boolean, Float, ForeignKey, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func
from geoalchemy2 import Geometry

Base = declarative_base()

class Team(Base):
    __tablename__ = 'team'
    
    team_id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    description = Column(String, default='')
    tapis_group_id = Column(String, unique=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    users = relationship("AppUser", back_populates="team")
    workflows = relationship("WorkflowTemplate", back_populates="team")


class AppUser(Base):
    __tablename__ = 'app_user'
    
    user_id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, nullable=False)
    email = Column(String)
    team_id = Column(Integer, ForeignKey('team.team_id'))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    team = relationship("Team", back_populates="users")
    workflows = relationship("WorkflowTemplate", back_populates="owner")
    pipeline_runs = relationship("PipelineRun", back_populates="user")


class StepTypeRegistry(Base):
    __tablename__ = 'step_type_registry'
    
    step_type_key = Column(String, primary_key=True)
    tapis_app_id = Column(String)
    display_name = Column(String, nullable=False)
    description = Column(String, default='')
    category = Column(String, default='general')
    icon = Column(String, default='default')
    config_schema = Column(JSON, nullable=False, default={})
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PortDataType(Base):
    __tablename__ = 'port_data_type'
    
    type_key = Column(String, primary_key=True)
    parent_type = Column(String, ForeignKey('port_data_type.type_key'), nullable=True)
    description = Column(String, default='')
    coerce_from = Column(JSON, default=[])


class StepTypePort(Base):
    __tablename__ = 'step_type_port'
    
    port_id = Column(Integer, primary_key=True, index=True)
    step_type_key = Column(String, ForeignKey('step_type_registry.step_type_key'), nullable=False)
    port_name = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    data_type = Column(String, ForeignKey('port_data_type.type_key'), nullable=False)
    is_required = Column(Boolean, default=True)
    description = Column(String, default='')

    __table_args__ = (
        UniqueConstraint('step_type_key', 'port_name', 'direction', name='uix_step_port'),
    )


class WorkflowTemplate(Base):
    __tablename__ = 'workflow_template'
    
    template_version_id = Column(Integer, primary_key=True, index=True)
    template_id = Column(Integer, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    name = Column(String, nullable=False)
    description = Column(String, default='')
    category = Column(String, default='Custom')
    owner_id = Column(Integer, ForeignKey('app_user.user_id'))
    team_id = Column(Integer, ForeignKey('team.team_id'))
    is_shared = Column(Boolean, default=False)
    tapis_pipeline_id = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('template_id', 'version', name='uix_template_version'),
    )

    owner = relationship("AppUser", back_populates="workflows")
    team = relationship("Team", back_populates="workflows")
    nodes = relationship("WfNode", back_populates="template", cascade="all, delete-orphan")


class WfNode(Base):
    __tablename__ = 'wf_node'
    
    node_id = Column(Integer, primary_key=True, index=True)
    template_version_id = Column(Integer, ForeignKey('workflow_template.template_version_id', ondelete='CASCADE'), nullable=False)
    step_type_key = Column(String, ForeignKey('step_type_registry.step_type_key'), nullable=False)
    node_label = Column(String, default='')
    default_config = Column(JSON, default={})
    position_x = Column(Float, default=0.0)
    position_y = Column(Float, default=0.0)

    template = relationship("WorkflowTemplate", back_populates="nodes")


class WfEdge(Base):
    __tablename__ = 'wf_edge'
    
    edge_id = Column(Integer, primary_key=True, index=True)
    template_version_id = Column(Integer, ForeignKey('workflow_template.template_version_id', ondelete='CASCADE'), nullable=False)
    source_node_id = Column(Integer, ForeignKey('wf_node.node_id', ondelete='CASCADE'), nullable=False)
    target_node_id = Column(Integer, ForeignKey('wf_node.node_id', ondelete='CASCADE'), nullable=False)
    source_port_id = Column(Integer, ForeignKey('step_type_port.port_id'), nullable=False)
    target_port_id = Column(Integer, ForeignKey('step_type_port.port_id'), nullable=False)
    condition_expr = Column(String, nullable=True)
    condition_desc = Column(String, default='')

    __table_args__ = (
        UniqueConstraint('template_version_id', 'source_node_id', 'target_node_id', 'source_port_id', 'target_port_id', name='uix_wf_edge'),
    )


class PipelineRun(Base):
    __tablename__ = 'pipeline_run'
    
    run_id = Column(Integer, primary_key=True, index=True)
    template_version_id = Column(Integer, ForeignKey('workflow_template.template_version_id'))
    user_id = Column(Integer, ForeignKey('app_user.user_id'), nullable=False)
    name = Column(String, default='')
    status = Column(String, default='pending')
    slurm_account = Column(String)
    tapis_pipeline_run_uuid = Column(String)
    frozen_config = Column(JSON, default={})
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("AppUser", back_populates="pipeline_runs")
    steps = relationship("RunStep", back_populates="run", cascade="all, delete-orphan")


class RunStep(Base):
    __tablename__ = 'run_step'
    
    run_step_id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey('pipeline_run.run_id', ondelete='CASCADE'), nullable=False)
    node_id = Column(Integer, ForeignKey('wf_node.node_id'))
    step_label = Column(String, default='')
    status = Column(String, default='pending')
    config = Column(JSON, default={})
    tapis_job_uuid = Column(String)
    tapis_job_status = Column(String)
    percent_complete = Column(Integer, default=0)
    cost = Column(Integer, default=0)
    error_message = Column(String)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    run = relationship("PipelineRun", back_populates="steps")


class RunEdge(Base):
    __tablename__ = 'run_edge'
    
    run_edge_id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey('pipeline_run.run_id', ondelete='CASCADE'), nullable=False)
    source_step_id = Column(Integer, ForeignKey('run_step.run_step_id', ondelete='CASCADE'), nullable=False)
    target_step_id = Column(Integer, ForeignKey('run_step.run_step_id', ondelete='CASCADE'), nullable=False)
    source_port_id = Column(Integer, ForeignKey('step_type_port.port_id'), nullable=False)
    target_port_id = Column(Integer, ForeignKey('step_type_port.port_id'), nullable=False)
    artifact_ref = Column(String)


class RunArtifact(Base):
    __tablename__ = 'run_artifact'
    
    artifact_id = Column(Integer, primary_key=True, index=True)
    run_step_id = Column(Integer, ForeignKey('run_step.run_step_id', ondelete='CASCADE'), nullable=False)
    port_id = Column(Integer, ForeignKey('step_type_port.port_id'), nullable=False)
    artifact_type = Column(String, ForeignKey('port_data_type.type_key'), nullable=False)
    uri = Column(String, nullable=False)
    tapis_file_uri = Column(String)
    tapis_system_id = Column(String)
    metadata_json = Column('metadata', JSON, default={})
    geom = Column(Geometry(geometry_type='GEOMETRY', srid=4326))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
