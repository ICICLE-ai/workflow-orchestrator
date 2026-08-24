from sqlalchemy import Column, Integer, String, Boolean, Float, ForeignKey, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

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

    # Per-user Tapis OAuth2 tokens. Populated by the /oauth2/callback exchange and
    # refreshed on demand (see engine/tapis_auth.py). The DBOS engine resolves the
    # run owner's token from here so jobs are submitted as the user who launched
    # them, not a shared service account.
    tapis_access_token = Column(String)
    tapis_refresh_token = Column(String)
    tapis_token_expires_at = Column(DateTime(timezone=True))

    team = relationship("Team", back_populates="users")
    workflows = relationship("WorkflowTemplate", back_populates="owner")
    pipeline_runs = relationship("PipelineRun", back_populates="user")


class Secret(Base):
    __tablename__ = 'secret'

    secret_id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey('team.team_id'), nullable=False)
    # Reference name a step's config_schema (type: "secret") stores and looks
    # this row up by — e.g. "WANDB_API_KEY". Never the value itself.
    key = Column(String, nullable=False)
    description = Column(String, default='')
    # Fernet-encrypted at rest (see engine/secrets.py). Decrypted only inside
    # that module, at job-submission time — never returned by the API or
    # persisted anywhere else (run_step/pipeline_run store only the key).
    encrypted_value = Column(String, nullable=False)
    created_by_id = Column(Integer, ForeignKey('app_user.user_id'))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('team_id', 'key', name='uix_team_secret_key'),
    )


class StepTypeRegistry(Base):
    __tablename__ = 'step_type_registry'
    
    step_type_key = Column(String, primary_key=True)
    tapis_app_id = Column(String)
    display_name = Column(String, nullable=False)
    description = Column(String, default='')
    category = Column(String, default='general')
    icon = Column(String, default='default')
    config_schema = Column(JSON, nullable=False, default={})
    # Full Tapis job-spec template (from step.json), with ${...} placeholders the
    # engine substitutes at run time. Null for steps that aren't executable.
    tapis_job = Column(JSON, default=None)
    # Whether this step type submits a Tapis job at all. False for design-time-only
    # steps (e.g. smart_labeler, geospatial_map) that just produce/view artifacts
    # in the UI — no compute resources to configure, so the canvas hides the Run
    # Configuration control for them. Defaults from step.json's `tapis_job` when
    # the step.json doesn't set it explicitly (see main.sync_step_registry).
    submits_job = Column(Boolean, default=True)
    # What this step NEEDS from an exec system, not where it runs — currently
    # just {"gpu": bool}. Mirrored from step.json's "resources" each sync. The
    # run supplies a CPU target and a GPU target (RunOptions), and
    # get_run_archive_context routes each node to the matching pair, so a
    # GPU step (zero_shot_annotation, training) and a CPU step (flight_plan,
    # geospatial) in the SAME run land on different systems/queues without
    # either one hardcoding a site the way several step.json files used to.
    resources = Column(JSON, default=dict)
    # Hide this step from the canvas PALETTE without removing it. Distinct from
    # is_active, which the registry sync owns (a step.json that disappears is
    # deactivated, one that reappears is reactivated) and so can't express a
    # deliberate "keep it registered but don't offer it yet". Mirrored from
    # step.json's "hidden" each sync.
    #
    # Hidden steps are still RETURNED by /api/step-types: a saved template
    # resolves each of its nodes against that list (WorkflowCanvas.tsx), so
    # dropping one would leave every existing template containing it with
    # unconfigurable, port-less nodes. Only the palette filters on it.
    hidden = Column(Boolean, default=False)
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
    # For OUTPUT ports: the artifact's subpath within the step's job output dir
    # (e.g. 'predictions.json' or 'annotated'). Lets a step expose multiple
    # distinct outputs, each routable to its own downstream node / sink.
    output_path = Column(String, default=None)
    # For OUTPUT ports whose output_path is a DIRECTORY containing a single
    # dynamically-named file (e.g. a script that stamps its own output
    # filename with a timestamp) rather than the file itself: an fnmatch
    # pattern (matched against bare filenames, e.g. 'annotations_*.json') used
    # to resolve the actual file inside that directory once the job completes
    # — see engine.tapis.resolve_latest_file and _derive_outputs in
    # engine/workflows.py. None for every other port (output_path already
    # names the exact artifact, or the port IS meant to be a directory, e.g.
    # an image_dir output).
    file_glob = Column(String, default=None)

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
    # Published to every authenticated user, regardless of team.
    #
    # Deliberately separate from is_shared/team_id rather than expressed through
    # them: every user is auto-attached to `default_team` (see auth._upsert_user),
    # so a team-scoped flag would mean "everyone" today and silently narrow to
    # "my team" the moment real teams exist. This one means the same thing
    # whatever the team topology turns into.
    #
    # Grants READ + RUN + CLONE, never write: only the owner can add a version
    # (see owned_template_or_404 in main.py). Set on every row sharing a
    # template_id, so publishing covers the whole version lineage.
    is_public = Column(Boolean, default=False, nullable=False, server_default='false')
    tapis_pipeline_id = Column(String)
    # Allocation/charge account (e.g. 'uot260') set at template creation; used as
    # the default slurm_account when running this template.
    allocation_account = Column(String)
    # A version created implicitly by "run these changes without saving" rather
    # than by an explicit Save.
    #
    # The engine cannot run an unsaved canvas: it reads a node's job template,
    # config schema, ports and — critically — its EDGES from wf_node/wf_edge
    # rows keyed by template_version_id (see engine/transactions.py's
    # get_incoming_edges), not from the run's frozen_config. So "don't save a
    # version" still has to persist one; this flag is what keeps it out of the
    # template list and out of the user's version numbering in spirit, while
    # leaving the run fully reproducible and traceable.
    is_draft = Column(Boolean, default=False)
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
    # Bridge to the DBOS durable-execution engine: the id of the DBOS workflow
    # orchestrating this run. Set when execution is kicked off; used to poll
    # run status and correlate run_step rows with DBOS child workflows.
    dbos_workflow_id = Column(String, unique=True, index=True)
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
    config = Column(JSON, default={})  # resolved step inputs (merged defaults + overrides)
    outputs = Column(JSON, default={})  # outputs produced by this step, consumed by downstream steps
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


# Nothing reads or writes this table yet — a step's outputs are tracked on
# run_step. It previously carried a PostGIS `geom` column, which forced the
# whole database to have the postgis extension for a column no code ever
# selected; on a stock Postgres that made schema creation fail outright. The
# geospatial features read geometry from GeoPackage/shapefiles pulled off Tapis
# (see geospatial.py), never from the database, so the column is gone and
# postgis is no longer a deployment requirement.
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
    created_at = Column(DateTime(timezone=True), server_default=func.now())
