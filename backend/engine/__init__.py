"""DBOS durable-execution engine for running workflow templates.

Ported from the `dbos-example` branch and adapted to run against the main
Harvest schema (models.py): runs are tracked in pipeline_run/run_step, the DAG
is read from wf_node/wf_edge, and each step's Tapis app comes from
step_type_registry.tapis_app_id. The engine's own models.py / step_types table
from dbos-example are intentionally dropped — this package owns no tables of its
own; DBOS keeps its internal durable state in its own schema in the same DB.
"""
