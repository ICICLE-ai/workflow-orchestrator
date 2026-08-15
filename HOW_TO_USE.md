# Harvest Workflow Orchestrator

A browser-based workflow builder for the full ML lifecycle: wire up a pipeline in
a drag-and-drop canvas — data sources, pre-processing, annotation, training,
inference, visualization, sinks — then execute it as a graph of real Tapis jobs
on HPC, and watch every step's status, config and logs live on the same canvas.

**Tags:** CI4AI, Digital-Agriculture, Software

### License

[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<!-- Add any other licenses you want to include. -->

## References

- [Tapis Jobs API](https://tapis-project.github.io/live-docs/?service=Jobs) — the HPC job submission service each step is submitted to.
- [README.md](README.md) — install and run the frontend + backend locally.
- [backend/INTEGRATION.md](backend/INTEGRATION.md) — Tapis credentials, app/system provisioning, and what the engine needs before a job can run.
- [docs/adding-a-step-form.md](docs/adding-a-step-form.md) — add a new step whose settings UI is auto-generated from its schema.
- [docs/adding-a-step-custom-ui.md](docs/adding-a-step-custom-ui.md) — add a step with a custom interactive panel (map, labeler, live preview).
- [frontend/app/pages/README.md](frontend/app/pages/README.md) — the step settings page contract (`StepPanelProps`).

## Acknowledgements

<!-- Please include other funding sources above this line. -->

*National Science Foundation (NSF) funded AI institute for Intelligent Cyberinfrastructure with Computational Learning in the Environment (ICICLE) (OAC 2112606)*

## Issue reporting

Please report issues via [GitHub Issues](https://github.com/ICICLE-ai/workflow-orchestrator/issues).

---

# Tutorials

## Build a workflow (canvas)

Open the app (locally, `http://localhost:5173` — see [README.md](README.md)).
The dashboard is the entry point: **Manage Templates** and **Past Runs**, with a
persistent *Templates* / *Runs* nav in the header and a ⚙ **Secrets** menu on
the right.

![Dashboard](<img width="1917" height="1024" alt="Screenshot 2026-08-14 201354" src="https://github.com/user-attachments/assets/9ad74ca7-89ba-48ba-9886-e400dfc4d01e" />

)

Sign in first — the widget in the bottom-left corner shows your Tapis username,
or a **Login with Tapis** button if you aren't signed in yet. Nothing that
touches files or submits jobs works until you are (see
[Sign in to Tapis](#sign-in-to-tapis)).

1. **Create a template** — go to **Templates → Create New Template**. You land on
   an empty canvas with the step palette down the right-hand side.

   ![Templates list](<img width="1919" height="1017" alt="Screenshot 2026-08-14 175330" src="https://github.com/user-attachments/assets/901b332b-0242-415a-a5a2-f1b81aeafddd" />
<img width="1919" height="1021" alt="Screenshot 2026-08-14 175115" src="https://github.com/user-attachments/assets/ea1479ae-051e-442a-bc27-c37ea50bd292" />


)

2. **Add steps** — the palette groups every registered step into collapsible
   sections: **Data Sources** (green, dashed), the eight pipeline stages —
   *Data Collection, Data Creation, Data Pre-processing, Data Harmonization,
   Training, Inference, Visualization, Post-processing* (blue), and **Data
   Sinks** (amber, dashed). Drag a card onto the canvas to add it as a node.

   ![Step palette](<img width="1919" height="1027" alt="Screenshot 2026-08-14 175711" src="https://github.com/user-attachments/assets/bf2abb3a-43f8-45dc-b91c-d5b201b5da6d" />


)

3. **Understand a node** — each node shows its display name, its **inputs** on
   the left (teal handles) and **outputs** on the right (violet handles), with a
   badge naming each port's data type. The header carries three actions:

   | Icon | Action |
   | --- | --- |
   | ⚙ (blue) | **Settings** — the step's own configuration (see step 5). |
   | 🖥 (purple) | **Run Configuration** — the compute this step requests. Hidden for design-time-only steps. |
   | 🗑 (red) | **Delete** the node and every edge attached to it. |

   ![Node anatomy](<img width="1919" height="1028" alt="Screenshot 2026-08-14 175537" src="https://github.com/user-attachments/assets/ebea11ba-d984-4866-b054-aa6a8763f534" />

)

4. **Connect steps** — drag from an output handle to an input handle. Connections
   are **type-checked**: a link is only allowed when the source port's data type
   matches the target's, is a subtype of it, or the target declares a coercion
   from it. An incompatible drop is refused with a red *Invalid Connection*
   toast naming both ports and their types.

   ![Connecting nodes]( <img width="1919" height="1027" alt="Screenshot 2026-08-14 175711" src="https://github.com/user-attachments/assets/2e26d346-fb13-4d59-b51d-6efed5dc4abe" />

   )

6. **Configure each step** — click ⚙ on a node. Most steps get a form generated
   straight from their schema (numbers, switches, dropdowns, text, a Tapis path
   picker, a secret selector). Steps with a richer UI — Smart Labeler, Zero-Shot
   Annotation, the Geospatial Map viewer, Flight Plan, Training, Visualization,
   PATRA publishing, the image pre-processing studio — open their own interactive
   panel instead, some of them full-screen. Press **Save Configuration** (or
   **Save & Close**) to write the values back onto the node.

   ![Step settings](<img width="1919" height="1022" alt="Screenshot 2026-08-14 175807" src="https://github.com/user-attachments/assets/44895f1e-24f7-414c-a669-036aa73fef3b" />

)

7. **Set the compute (optional)** — click 🖥 on any step that submits a job to set
   node count, cores per node, memory, max runtime and GPUs. Leave *Execution
   system* empty to inherit the run's target; the badge in the corner tells you
   whether the step declares itself a **GPU step** or a **CPU step**. If you do
   pin a system, the queue dropdown loads that system's real queues and warns
   when a request exceeds the queue's limits.

   ![Run configuration](<img width="1903" height="1021" alt="Screenshot 2026-08-14 175952" src="https://github.com/user-attachments/assets/a40ef753-e9f9-426c-b2bc-16e84a8f879e" />

)

8. **Save the template** — click **Save Template**, give it a name, description
   and allocation account, then **Confirm Save**.

   - Saving is **blocked** while any required input port has no incoming edge;
     the drawer lists exactly which node is missing which input.
   - Saving **warns** (but lets you proceed with *Save Anyway*) when a node's
     output isn't wired to a sink — those results won't be written anywhere.

   ![Saving a template](<img width="1918" height="1026" alt="Screenshot 2026-08-14 200222" src="https://github.com/user-attachments/assets/ac2a3415-4d87-4b43-93e1-0f76409648cf" />
   )


Reopening a template from **Templates → Edit Template** puts you back on the
canvas with its title showing `name vN`. Saving again creates a **new version**
rather than overwriting the old one, so earlier runs keep pointing at exactly the
graph they executed.

## Run the workflow on Tapis

### Sign in

Standalone, click **Login with Tapis** in the bottom-left widget: the backend
redirects you to your Tapis tenant, and back to the app with a session cookie.
Embedded inside TapisUI, the host already holds your token — you're signed in
automatically and the widget shows your username with no login/logout controls.

### Configure the run

With a saved template open, click **Run Workflow** (green, top right). The **Run
Settings** drawer is where the run's Tapis targets are chosen.

![Run settings](<img width="1907" height="1020" alt="Screenshot 2026-08-14 200409" src="https://github.com/user-attachments/assets/abba229d-68ff-4533-bb39-db70dbd9b496" />

)

The run declares **two** execution targets, not one. Each step's definition says
whether it needs a GPU, and the engine routes it to the matching pair — so
Zero-Shot Annotation and Training can land on a GPU queue while Flight Plan and
Geospatial run on a CPU queue **in the same run**. The GPU target's description
names the GPU steps actually present on your canvas. Field-by-field detail is in
[Run settings reference](#run-settings-reference).

Click **Launch Run**. If the canvas differs from the last saved version (an
*Unsaved changes* badge is shown next to the buttons), you're asked first whether
to keep those edits as a new version or run them without recording one — either
way **what runs is what's on screen**, never a stale saved graph.

### Watch it run

Launching takes you straight to `/runs/{id}`: the same graph, read-only, with
each node tinted by its live status.

![Live run](<img width="1919" height="1015" alt="Screenshot 2026-08-14 200544" src="https://github.com/user-attachments/assets/ba14988f-5699-41c8-9733-169e40e17f42" />

)

| Node state | Meaning |
| --- | --- |
| **Pending** (grey, faded) | Not started. Shows *Waiting on: …* while an upstream step is unfinished. |
| **Running** (blue) | The step's Tapis job is in flight. |
| **Completed** (green) | Finished; its outgoing edges start animating. |
| **Failed** (red) | The step errored — open its logs. |
| **Blocked** / **Cancelled** | Upstream failure, or the run was stopped. |

A second, smaller badge carries the raw **Tapis job status** — the full
vocabulary (`STAGING_INPUTS`, `QUEUED`, `RUNNING`, `ARCHIVING`, `FINISHED`, …) —
so a step that looks stuck can be told apart from one merely sitting in a queue.
The page polls every 2.5 s while the run is `RUNNING`; ⟳ in the header refreshes
on demand.

**Click any node** to open its logs: the step's resolved configuration, Tapis'
own outcome message, the orchestrator's error (if any), and the tail of the
container's `tapisjob.out`. Nodes that never submit a job (Smart Labeler, the
map viewer, …) open their own panel read-only against the run's resolved values
instead, so you can inspect what they produced.

![Step logs](<img width="1919" height="1025" alt="Screenshot 2026-08-14 200742" src="https://github.com/user-attachments/assets/e74402c1-d15e-43d7-82e7-694df2a15d1e" />

)

The ⚙ in the header shows the run's frozen launch configuration; **Edit Template**
jumps back to the canvas; a `FAILED` or `CANCELLED` run gets a **Re-run** button
that relaunches it with the same settings.

**Past Runs** (`/runs`) lists every run with its status. Expand a finished run
for its per-step breakdown and logs, hit **View Live Graph** on an active one, or
**Stop** it — which cancels the workflow and any in-flight Tapis job.

![Past runs](<img width="1919" height="1019" alt="Screenshot 2026-08-14 200941" src="https://github.com/user-attachments/assets/faf7c864-693f-4d05-9289-4fadf44886f8" />

)

---

# How-To Guides

## Sign in to Tapis

| Situation | What happens |
| --- | --- |
| Standalone app | **Login with Tapis** → OAuth2 redirect → session cookie. **Logout** clears it. |
| Embedded in TapisUI | The host's Tapis token is picked up automatically; no login/logout is offered. |
| Local dev, no Tapis client | Set `TAPIS_USE_MOCK=true` in `backend/.env` and `/login` gives you a mock session so the app stays runnable. |

Your token is what the engine submits jobs *as*, so runs are owned by, and
charged to, the person who launched them.

## Point a step at files on Tapis

Any path field rendered by the generic form (schema type `tapis_path`) gives you
a **system** dropdown plus a path box with a **Browse** button that opens the
Tapis file explorer scoped to the selected system. Pick a file or a directory
depending on what the field wants; the chosen system is stored alongside the
path, and downstream steps receive the full `tapis://system/path` URI rather than
a bare path.

Available systems: `pitzer-tapis`, `cardinal-tapis`, `ascend-tapis`,
`expanse-tapis`, `expanse-tapis-static`.

## Store an API token as a secret

Some steps need a credential (Weights & Biases, Hugging Face, …). Never type it
into a config field — put it in the vault instead:

1. Click the ⚙ **Secrets** icon on the dashboard header.
2. Enter a **key** (e.g. `WANDB_API_KEY`), the **value**, and an optional
   description, then **Add secret**.

![Secrets menu](<img width="1919" height="1025" alt="Screenshot 2026-08-14 201113" src="https://github.com/user-attachments/assets/5652e8d3-ee19-4e1d-8d9b-88988258d85a" />

)

Secrets are shared across your team and **write-only** — the list never returns a
value again. A step field of type `secret` then offers a dropdown of keys, and
only the *key* is stored on the node, in the saved template, and in the run's
frozen config. The real value is resolved server-side and substituted into the
job spec at submission time only.

Steps that always need one specific secret can reference it directly in their job
template as `${secrets.KEY}`, with no per-node field at all — see
[docs/adding-a-step-form.md](docs/adding-a-step-form.md).

## Send GPU and CPU steps to different systems

1. In **Run Settings**, set the **CPU target** (exec system + queue) — every step
   with no GPU requirement follows it.
2. Set the **GPU target** — every step whose definition declares
   `"resources": {"gpu": true}` follows this one instead. The field's description
   lists which steps on your canvas that currently applies to.
3. To take one specific node off both, open its 🖥 **Run Configuration** and pick
   an **Execution system** there. That pins the step to its own system and queue,
   ignoring the run's targets. Clearing it returns the step to inheriting.

Archiving stays run-level regardless: a run whose GPU steps are on Expanse and
CPU steps on OSC still writes all its artifacts to one place.

## Run settings reference

| Field | Meaning |
| --- | --- |
| **CPU target — Exec system** | Where steps with no GPU requirement run. Also becomes the default archive system. |
| **CPU target — Queue** | Loaded live from the system's own batch queues; the description shows that queue's node/core/runtime limits. |
| **GPU target — Exec system** | Where steps declaring a GPU requirement run. |
| **GPU target — Queue** | As above, for the GPU system. |
| **Slurm account** | The allocation to charge (e.g. `PAS2699`). Prefilled from the template's allocation account. |
| **Work dir** | Derived automatically from the archive system, charge account and your Tapis username — read-only. |
| **Archive system** | Where step outputs are archived, unless a sink node overrides it. |
| **Archive dir** | Optional base directory on the archive system. Blank derives it from *Work dir*. |

Whatever you choose, each step archives under
`.../{run_id}/{step_type_key}/{node_id}`, so one run's artifacts never collide
with another's.

## Run configuration reference (per step)

| Field | Default | Meaning |
| --- | --- | --- |
| **Execution system** | *inherit* | Pin this step to its own system. Empty = follow the run's CPU or GPU target. |
| **Queue** | *system default* | Only selectable once a system is pinned. |
| **Node count** | 1 | Nodes requested. |
| **Cores per node** | 8 | Cores per node requested. |
| **Memory (MB)** | 64800 | Memory requested. |
| **Max runtime (minutes)** | 210 | Wall-clock limit. |
| **GPUs** | 0 | Becomes a `-G <n>` scheduler request. `0` on a GPU step removes it. |

These override whatever the step type's own job template specifies. When a queue
is pinned, over-requesting against its published limits is flagged here rather
than by a Tapis rejection minutes into the run.

## Fix a workflow that won't save

| Message | Fix |
| --- | --- |
| *"X" is missing required input "y"* | Connect that input to an upstream output or a data source. Optional inputs are never flagged. |
| *"X" output "y" is not saved to a sink* | Add a sink node (e.g. **💾 Write Results (JSON)**) and wire the output into it — or press **Save Anyway** if leaving it unconsumed is deliberate. |
| *Cannot connect: … is incompatible with …* | The two ports' data types don't match. Insert an adapter step, or use a port of the right type. |

## Stop, re-run, and recover

- **Stop** a running run from the *Past Runs* list. This cancels the durable
  workflow, its child step workflows, and any in-flight Tapis job. It can't be
  undone.
- **Re-run** appears on a `FAILED` or `CANCELLED` run's page and relaunches the
  same template version with the same Tapis options — no re-entering settings.
- To change something first, use **Edit Template** on the run page: it opens the
  exact version that ran.

## Add a new step type

Steps are registered from `backend/steps/<key>/step.json` — no frontend code is
needed for a step whose settings are ordinary fields.

- Form-rendered settings: [docs/adding-a-step-form.md](docs/adding-a-step-form.md)
- Custom interactive panel: [docs/adding-a-step-custom-ui.md](docs/adding-a-step-custom-ui.md)

---

# Explanation

## Templates, versions, and runs

A **template** is a named graph: nodes (a step type + its configuration) and
edges (output port → input port). Saving an existing template writes a **new
version** instead of mutating it, and a run always points at one specific
version. Launching with unsaved edits still records the exact graph behind the
scenes — as a version if you asked for one, otherwise as a hidden draft — so
every run stays reproducible and none of them silently changes meaning when you
edit the template later.

A **run** additionally freezes its launch configuration (exec systems, queues,
account, directories, and the resolved node/edge snapshot). That frozen config is
what the engine reads when rendering each step's job, and what the run page's ⚙
drawer shows you afterwards.

## Ports and type compatibility

Every port has a data type — `image_dir`, `pytorch_model`, `json_results`,
`csv_data`, `shapefile`, `geopackage`, `heatmap_image`, and others. An edge is
allowed when:

1. the types are identical, or
2. the source type is a **subtype** of the target type (e.g. `image_dir` into a
   port accepting `file_collection`), or
3. the target type declares a **coercion** from the source type.

This is checked in the browser as you draw the connection, and again on the
server when the template is saved. It is what makes a graph structurally valid
before a single job is submitted.

## The step catalogue

| Stage | Steps |
| --- | --- |
| **Data Sources** | Image Directory, YOLO Labels Directory, CSV Dataset, JSON File, Pretrained Model, Shapefile, GeoPackage |
| **Data Collection** | Extract Frames |
| **Data Pre-processing** | Preprocessing, Image Crop, Image pre-processing studio |
| **Training** | Model Training, Publish to PATRA |
| **Inference** | Model Inference, YOLO Inference, Object Detection, Image Classifier, Zero-Shot Annotation, Few-Shot Annotation, Smart Labeler |
| **Visualization** | Visualization, Heatmap Generation, Class Histogram, Geospatial Map Viewer |
| **Post-processing** | Geospatial (GeoPackage), Custom Shapefile, Flight Plan Generator, Mission Export Adapter, Annotation Format Adapter |
| **Data Sinks** | Write Image Directory, Write Model, Write Results (JSON), Write CSV, Write Shapefile, Write Heatmap |

Sources have outputs but no inputs; sinks have inputs but no outputs. Empty
stages still appear in the palette so the pipeline's shape stays visible.

## Design-time steps vs job steps

Most steps submit a Tapis job. A few — Smart Labeler, the Geospatial Map viewer,
the Annotation Format Adapter's editor — are **design-time only**: they run
entirely in the browser against Tapis files, produce their output there and then,
and never queue anything. Those nodes have no 🖥 Run Configuration icon (there's
no compute to request), and on a run page clicking one opens its panel rather
than a log view.

## From node to Tapis job

When a run executes, the engine walks the DAG in dependency order. For each node
it resolves the node's inputs from its incoming edges (binding the upstream
step's output URIs to this step's input ports), merges the node's frozen
configuration, resolves any secret references, renders the step type's Tapis job
template by substituting `${…}` placeholders, and submits it to
`POST /v3/jobs/submit`. It then polls the job's status until it finishes,
recording each transition against the step — which is exactly what the canvas is
showing you live. Per-step state lives in `pipeline_run` / `run_step`; if no
Tapis credential is configured the engine falls back to a mock client so the
system still runs end-to-end locally.

Full detail, including credentials and the provisioning a real run requires, is
in [backend/INTEGRATION.md](backend/INTEGRATION.md).
