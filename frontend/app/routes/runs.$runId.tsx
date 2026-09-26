import type { Route } from "./+types/runs.$runId";
import { AppShell, Container, Text, Group, ThemeIcon, ActionIcon, Badge, Loader, Tooltip, Button, Drawer, Stack, Code, Divider, Modal, Select, JsonInput, Alert } from "@mantine/core";
import { IconActivity, IconArrowLeft, IconRefresh, IconSettings, IconEdit, IconRepeat, IconPlayerPlay, IconInfoCircle } from "@tabler/icons-react";
import { useNavigate } from "react-router";
import { notifications } from "@mantine/notifications";
import { useState, useEffect, useCallback, useMemo } from "react";
import { ReactFlow, ReactFlowProvider, Background, Controls } from "@xyflow/react";
import CustomNode from "../components/CustomNode";
import StepSettingsModal from "../components/StepSettingsModal";
import ThemeToggle from "../components/ThemeToggle";
import { getStepPanel } from "../pages/registry";
import TopNav from "../components/TopNav";
import type { StepMeta, ConnectedInput } from "../pages/types";
import { StepLogsModal } from "./runs";
import { apiFetch } from "../lib/api";

const nodeTypes = { customNode: CustomNode };

export async function clientLoader({ params }: { params: any }) {
  const runId = params.runId;
  // Fetch run detail first (gives template_version_id + per-node status).
  const detailRes = await apiFetch(`/api/pipeline-runs/${runId}/detail`);
  if (!detailRes.ok) throw new Error("Run not found");
  const detail = await detailRes.json();

  const [stepsRes, tmplRes] = await Promise.all([
    apiFetch("/api/step-types"),
    apiFetch(`/api/workflow-templates/${detail.template_version_id}`),
  ]);
  const stepTypes = await stepsRes.json();
  const template = tmplRes.ok ? await tmplRes.json() : null;
  return { runId, detail, stepTypes, template };
}

const runColor = (s: string) => {
  const v = (s || "").toUpperCase();
  if (v === "COMPLETED") return "teal";
  if (v === "FAILED") return "red";
  if (v === "RUNNING") return "blue";
  if (v === "CANCELLED") return "orange";
  return "gray";
};

// Every field of the backend's RunOptions (main.py), in the order they should
// appear. This list is the SINGLE source of truth for both jobs it does: what
// the Configuration drawer shows, and what a re-run carries over — so a field
// added to RunOptions only has to be added here.
//
// Enumerating a subset by hand is exactly what broke re-runs: gpu_exec_system,
// gpu_exec_queue and archive_dir were added to RunOptions after the Re-run
// button was written and never added to the copy, so every re-run silently
// dropped them. Losing archive_dir is fatal rather than cosmetic — the archive
// base reverts to {work_dir}/wf_runs (see transactions.get_run_archive_context),
// and image-preprocess-studio's PRE-SUBMIT operations.json upload 403s against
// a directory the user can't write, failing the run within seconds and before
// any Tapis job exists to inspect. The drawer hid it too, since it was reading
// the same short list.
const RUN_OPTION_FIELDS: { key: string; label: string }[] = [
  { key: "slurm_account", label: "Slurm account" },
  { key: "exec_system", label: "Exec system" },
  { key: "exec_queue", label: "Exec queue" },
  { key: "gpu_exec_system", label: "GPU exec system" },
  { key: "gpu_exec_queue", label: "GPU exec queue" },
  { key: "work_dir", label: "Work dir" },
  { key: "archive_system", label: "Archive system" },
  { key: "archive_dir", label: "Archive dir" },
];

// Shown above those in the drawer, but NOT part of the re-run payload: the
// template is identified by template_version_id, and RunOptions has no `name`.
// frozen_config also carries the full nodes/edges snapshot, which we
// deliberately don't dump here — the canvas already shows the DAG shape.
const RUN_INFO_FIELDS: { key: string; label: string }[] = [
  { key: "name", label: "Template" },
];

// Which steps a resume would re-execute. Mirrors resume_pipeline_run in
// main.py: every step that never completed, plus — when a checkpoint is chosen
// — that step and everything downstream of it, whose inputs redoing the
// checkpoint invalidates. Recomputed here rather than asked of the server so
// the modal can show what a resume will do BEFORE the user commits to it; the
// backend stays the authority and recomputes the same set on the request.
function rerunSet(steps: any[], edges: any[], fromNode: string | null): Set<string> {
  const rerun = new Set<string>(
    steps.filter((s: any) => s.status !== "completed").map((s: any) => String(s.node_id))
  );
  if (fromNode) {
    const adj: Record<string, string[]> = {};
    edges.forEach((e: any) => { (adj[String(e.source)] ||= []).push(String(e.target)); });
    // `seen` guards the walk rather than trusting the graph to be acyclic —
    // nothing validates that at save time, and a cycle would hang the browser.
    const seen = new Set<string>();
    const stack = [fromNode];
    while (stack.length) {
      const node = stack.pop()!;
      if (seen.has(node)) continue;
      seen.add(node);
      rerun.add(node);
      (adj[node] || []).forEach((t) => stack.push(t));
    }
  }
  const known = new Set(steps.map((s: any) => String(s.node_id)));
  return new Set([...rerun].filter((k) => known.has(k)));
}

// Sentinel for the Select's "don't redo anything that succeeded" option. Maps to
// omitting from_node_id entirely, which is a meaningfully different request from
// naming a step — not just a different step id — so it needs its own value.
const CONTINUE = "__continue__";

function ResumeModal({ opened, onClose, runId, detail, template, stepTypes, onResumed }: any) {
  const steps: any[] = detail?.steps || [];
  const edges: any[] = template?.edges || [];
  const incomplete = steps.filter((s) => s.status !== "completed");

  // Label a node the way the canvas does, so the checkpoint list reads as the
  // same graph the user is looking at behind the modal.
  const labelFor = useCallback((nodeId: string) => {
    const node = (template?.nodes || []).find((n: any) => String(n.id) === nodeId);
    const type = node?.data?.nodeType;
    const meta = stepTypes.find((s: any) => s.step_type_key === type);
    return meta?.display_name || type || `Step ${nodeId}`;
  }, [template, stepTypes]);

  // A failed run opens on its failed step rather than on "continue": the two
  // resume identically (a failed step never completed, so it re-runs either
  // way), but naming it is what reveals the config editor — and a run that
  // broke on a bad parameter is exactly the case for changing one.
  const failedNode = steps.find((s) => s.status === "failed");
  const initialFrom = failedNode ? String(failedNode.node_id)
    : incomplete.length ? CONTINUE
    : String(steps[0]?.node_id ?? CONTINUE);

  const [from, setFrom] = useState<string>(initialFrom);
  const [configText, setConfigText] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);

  // The node's DESIGN-TIME config, not run_step.config: a step's config row is
  // rewritten with fully resolved inputs as it runs, so the stored value is the
  // last attempt's resolved paths. The backend resets to this same frozen
  // config, so this is what the redo will actually start from.
  const frozenConfigFor = useCallback((nodeId: string) => {
    const node = (detail?.frozen_config?.nodes || []).find((n: any) => String(n.id) === nodeId);
    return node?.inputs || {};
  }, [detail]);

  // Reset the form whenever the modal reopens — otherwise it reopens holding the
  // previous attempt's checkpoint and an edited config for a different step.
  useEffect(() => {
    if (!opened) return;
    setFrom(initialFrom);
    setConfigText(
      initialFrom === CONTINUE ? "" : JSON.stringify(frozenConfigFor(initialFrom), null, 2)
    );
  // initialFrom is derived from `detail`, which polls; depending on it would
  // reset the user's selection mid-edit on every refresh.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened]);

  const fromNode = from === CONTINUE ? null : from;
  const rerun = useMemo(() => rerunSet(steps, edges, fromNode), [steps, edges, fromNode]);
  const preserved = steps.filter((s) => !rerun.has(String(s.node_id)));

  const configError = useMemo(() => {
    if (!fromNode || !configText.trim()) return null;
    try {
      const parsed = JSON.parse(configText);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return "Configuration must be a JSON object.";
      }
      return null;
    } catch {
      return "Not valid JSON.";
    }
  }, [fromNode, configText]);

  const onPickStep = (value: string | null) => {
    const next = value || CONTINUE;
    setFrom(next);
    setConfigText(next === CONTINUE ? "" : JSON.stringify(frozenConfigFor(next), null, 2));
  };

  const submit = async () => {
    if (configError) return;
    setSubmitting(true);
    try {
      const body: any = {};
      if (fromNode) body.from_node_id = Number(fromNode);
      // Only send a config override when it actually differs from the frozen
      // config — an unchanged round-trip would still be a legitimate override,
      // but sending it makes the run's history claim a change that wasn't one.
      if (fromNode && configText.trim()) {
        const parsed = JSON.parse(configText);
        if (JSON.stringify(parsed) !== JSON.stringify(frozenConfigFor(fromNode))) {
          body.step_config = { [fromNode]: parsed };
        }
      }
      const res = await apiFetch(`/api/pipeline-runs/${runId}/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Could not resume the run (HTTP ${res.status}).`);
      }
      const result = await res.json();
      notifications.show({
        color: "blue",
        title: "Run resumed",
        message: `${result.steps_rerunning.length} step(s) re-running, `
          + `${result.steps_preserved.length} preserved.`,
      });
      onClose();
      onResumed();
    } catch (e: any) {
      notifications.show({
        color: "red",
        title: "Could not resume",
        message: e?.message || "Unknown error",
      });
    }
    setSubmitting(false);
  };

  const options = [
    ...(incomplete.length
      ? [{ value: CONTINUE, label: "Continue where it stopped — redo nothing that succeeded" }]
      : []),
    ...steps.map((s: any) => ({
      value: String(s.node_id),
      label: `Redo ${labelFor(String(s.node_id))} (${s.status}) and everything after it`,
    })),
  ];

  return (
    <Modal opened={opened} onClose={onClose} size="lg" title={`Resume run #${runId}`}>
      <Stack gap="md">
        <Select
          label="Resume from"
          description="Steps before this point keep the output they already produced."
          data={options}
          value={from}
          onChange={onPickStep}
          allowDeselect={false}
        />

        <div>
          <Text size="sm" fw={600} mb={6}>
            Will re-run ({rerun.size})
          </Text>
          <Group gap={6}>
            {steps.filter((s: any) => rerun.has(String(s.node_id))).map((s: any) => (
              <Badge key={s.node_id} color="blue" variant="light">
                {labelFor(String(s.node_id))}
              </Badge>
            ))}
            {rerun.size === 0 && <Text size="sm" c="dimmed">Nothing — this run is already complete.</Text>}
          </Group>
        </div>

        <div>
          <Text size="sm" fw={600} mb={6}>
            Preserved ({preserved.length})
          </Text>
          <Group gap={6}>
            {preserved.map((s: any) => (
              <Badge key={s.node_id} color="teal" variant="light">
                {labelFor(String(s.node_id))}
              </Badge>
            ))}
            {preserved.length === 0 && <Text size="sm" c="dimmed">None — every step re-runs.</Text>}
          </Group>
        </div>

        {fromNode && (
          <JsonInput
            label={`Configuration for ${labelFor(fromNode)}`}
            description="Applies to this resume only — the saved template is unchanged."
            value={configText}
            onChange={setConfigText}
            error={configError}
            autosize
            minRows={4}
            maxRows={14}
            formatOnBlur
          />
        )}

        <Alert icon={<IconInfoCircle size={16} />} color="gray" variant="light" p="xs">
          <Text size="xs">
            Resuming continues this same run. To start over instead, close this and use
            Start over — that launches a separate run and leaves this one's record intact.
          </Text>
        </Alert>

        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button
            onClick={submit}
            disabled={submitting || rerun.size === 0 || !!configError}
            leftSection={submitting ? <Loader size={12} /> : <IconPlayerPlay size={14} />}
          >
            {submitting ? "Resuming…" : "Resume"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function Flow({ runId, detail, stepTypes, template }: any) {
  // node_id -> run status (as strings the CustomNode understands)
  const statusByNode: Record<string, string> = {};
  (detail?.steps || []).forEach((s: any) => { statusByNode[String(s.node_id)] = s.status; });

  // node_id -> raw Tapis job status (the full Tapis vocabulary: PENDING,
  // STAGING_INPUTS, STAGING_JOB, SUBMITTING_JOB, QUEUED, RUNNING, ARCHIVING,
  // FINISHED, FAILED, CANCELLED, ...), shown alongside the coarse run status.
  const tapisByNode: Record<string, string> = {};
  (detail?.steps || []).forEach((s: any) => {
    if (s.tapis_job_status) tapisByNode[String(s.node_id)] = s.tapis_job_status;
  });

  // node_id -> resolved config, for the per-step "view configuration" panel.
  const configByNode: Record<string, any> = {};
  (detail?.steps || []).forEach((s: any) => { configByNode[String(s.node_id)] = s.config; });

  // node_id -> display label, for "waiting on" hints.
  const labelByNode: Record<string, string> = {};
  (template?.nodes || []).forEach((n: any) => {
    const sc = stepTypes.find((s: any) => s.step_type_key === n.data.nodeType);
    labelByNode[String(n.id)] = sc?.display_name || n.data.nodeType;
  });

  // target node_id -> [source node_id, ...], from the template's edges.
  const incomingByTarget: Record<string, string[]> = {};
  (template?.edges || []).forEach((e: any) => {
    (incomingByTarget[String(e.target)] ||= []).push(String(e.source));
  });

  // Hydrate template nodes with step config + this run's per-node status.
  const nodes = (template?.nodes || []).map((n: any) => {
    const stepConfig = stepTypes.find((s: any) => s.step_type_key === n.data.nodeType);
    const st = statusByNode[String(n.id)] || 'pending';
    // A pending node is either genuinely "waiting" on an unfinished upstream
    // step, or (once every upstream step is done) about to be picked up by
    // the orchestrator — only show the hint in the former case.
    let waitingOn: string | undefined;
    if (st === 'pending') {
      const unfinished = (incomingByTarget[String(n.id)] || [])
        .filter((srcId) => statusByNode[srcId] !== 'completed');
      waitingOn = unfinished.length ? unfinished.map((id) => labelByNode[id] || id).join(', ') : undefined;
    }
    return {
      ...n,
      // read-only: no dragging/connecting on the run view
      draggable: false,
      connectable: false,
      data: {
        ...n.data,
        fullStepConfig: stepConfig,
        runStatus: st,
        tapisStatus: tapisByNode[String(n.id)],
        waitingOn,
      },
    };
  });
  const edges = (template?.edges || []).map((e: any) => ({
    ...e,
    animated: statusByNode[String(e.source)] === 'completed',
  }));

  // node_id -> step_type, for the logs modal title
  const typeByNode: Record<string, string> = {};
  (detail?.steps || []).forEach((s: any) => { typeByNode[String(s.node_id)] = s.step_type; });

  const [logNode, setLogNode] = useState<number | null>(null);
  // Design-time-only steps (submits_job: false — smart_labeler, geospatial_map,
  // ...) never ran a Tapis job, so the logs modal has nothing useful to show.
  // For those, open the step's own custom panel (registry.ts) against this
  // run's resolved config instead, so the visualization/labels are viewable
  // from the run page too, not just at design time.
  const [panelNode, setPanelNode] = useState<any | null>(null);

  const onNodeClick = (_evt: any, node: any) => {
    const stepConfig = node.data?.fullStepConfig;
    const hasRunPanel = stepConfig && stepConfig.submits_job === false && !!getStepPanel(stepConfig.step_type_key);
    if (hasRunPanel) {
      setPanelNode(node);
    } else {
      setLogNode(Number(node.id));
    }
  };

  // Build the panel's StepMeta + synthetic connectedInputs from this run's
  // resolved config (node_config defaults + own config + resolved edge
  // values, all flattened by port name — see workflows._resolve_inputs).
  // Panels only ever read connectedInputs[port].config.path, so pointing that
  // at the resolved value is enough without re-deriving the source node.
  let panelStep: StepMeta | null = null;
  let panelConnectedInputs: Record<string, ConnectedInput> = {};
  let panelConfig: Record<string, any> = {};
  if (panelNode) {
    const fullConfig = panelNode.data?.fullStepConfig || {};
    const inputs = (fullConfig.inputs || []).map((p: any) => ({
      port_name: p.port_name || p.name,
      data_type: p.data_type || p.type || 'any',
    }));
    const outputs = (fullConfig.outputs || []).map((p: any) => ({
      port_name: p.port_name || p.name,
      data_type: p.data_type || p.type || 'any',
    }));
    panelStep = {
      step_type_key: panelNode.data.nodeType,
      display_name: fullConfig.display_name || panelNode.data.nodeType,
      category: fullConfig.category,
      config_schema: fullConfig.config_schema || {},
      inputs,
      outputs,
      submits_job: fullConfig.submits_job,
    };
    panelConfig = configByNode[String(panelNode.id)] || {};
    for (const port of inputs) {
      if (panelConfig[port.port_name] === undefined) continue;
      panelConnectedInputs[port.port_name] = {
        sourceNodeId: '',
        sourceType: '',
        sourcePort: port.port_name,
        config: { path: String(panelConfig[port.port_name] ?? '') },
      };
    }
  }

  return (
    <div style={{ width: '100%', height: 'calc(100vh - 60px)' }}>
      <StepLogsModal
        runId={Number(runId)}
        nodeId={logNode}
        stepType={logNode != null ? typeByNode[String(logNode)] : undefined}
        config={logNode != null ? configByNode[String(logNode)] : undefined}
        opened={logNode != null}
        onClose={() => setLogNode(null)}
      />
      {panelNode && panelStep && (
        <StepSettingsModal
          opened={true}
          onClose={() => setPanelNode(null)}
          nodeId={String(panelNode.id)}
          step={panelStep}
          initialConfig={panelConfig}
          templateVersionId={detail?.template_version_id}
          connectedInputs={panelConnectedInputs}
          runId={Number(runId)}
          onSave={() => setPanelNode(null)}
          viewOnly
        />
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={onNodeClick}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        fitView
      >
        <Background color="#ccc" gap={16} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

export default function RunView({ loaderData }: Route.ComponentProps) {
  const navigate = useNavigate();
  const { runId, detail: initialDetail, stepTypes, template } = loaderData as any;

  const [detail, setDetail] = useState<any>(initialDetail);
  const [refreshing, setRefreshing] = useState(false);
  const [configOpened, setConfigOpened] = useState(false);
  const [rerunning, setRerunning] = useState(false);
  const [resumeOpened, setResumeOpened] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await apiFetch(`/api/pipeline-runs/${runId}/detail`);
      if (r.ok) setDetail(await r.json());
    } catch { /* ignore */ }
  }, [runId]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  }, [load]);

  // Poll run detail while the run is active so node statuses update live;
  // the header's Refresh button covers on-demand checks any other time.
  useEffect(() => {
    const active = (detail?.status || "").toUpperCase() === "RUNNING";
    if (!active) return;
    const id = setInterval(load, 2500);
    return () => clearInterval(id);
  }, [detail, load]);

  // Re-run this template with the same run-level Tapis options, for
  // recovering from a failed/cancelled run without re-entering settings.
  //
  // Every option the original run carried is passed through (RUN_OPTION_FIELDS
  // — see the note there). Values are copied verbatim, and only absent ones are
  // omitted, so the backend applies a default only where the original had none
  // either: a re-run must reproduce the original's configuration, not re-derive
  // it.
  const handleRerun = useCallback(async () => {
    setRerunning(true);
    try {
      const fc = detail.frozen_config || {};
      const options = Object.fromEntries(
        RUN_OPTION_FIELDS
          .map(({ key }) => [key, fc[key]])
          .filter(([, value]) => value !== undefined && value !== null)
      );
      const res = await apiFetch(`/api/pipeline-runs/${detail.template_version_id}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(options),
      });
      if (res.ok) {
        const { run_id } = await res.json();
        navigate(`/runs/${run_id}`);
        return;
      }
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Could not start the run (HTTP ${res.status}).`);
    } catch (e: any) {
      // Previously swallowed: the button just stopped spinning and the failure
      // was indistinguishable from nothing having happened.
      notifications.show({
        color: "red",
        title: "Could not re-run",
        message: e?.message || "Unknown error",
      });
    }
    setRerunning(false);
  }, [detail, navigate]);

  const status = (detail?.status || "").toUpperCase();
  const canRerun = status === "FAILED" || status === "CANCELLED";
  // Resuming is offered for any run that isn't in flight — including a COMPLETED
  // one, where it means "redo this step and everything after it" rather than
  // "carry on". Only a RUNNING run has nothing to resume: it has to be stopped
  // first, which the backend also enforces.
  const canResume = !!status && status !== "RUNNING";

  return (
    <AppShell header={{ height: 60 }} padding="0">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group>
            <ActionIcon variant="subtle" color="gray" onClick={() => navigate('/runs')}>
              <IconArrowLeft size={20} />
            </ActionIcon>
            <ThemeIcon variant="gradient" gradient={{ from: 'indigo', to: 'cyan' }} size="md" radius="md">
              <IconActivity size={16} />
            </ThemeIcon>
            <Text fw={700}>Run #{runId}</Text>
            <TopNav />
            {detail.template_version_id && (
              <Button size="xs" variant="subtle" color="gray" leftSection={<IconEdit size={14} />}
                onClick={() => navigate(`/templates/${detail.template_version_id}/edit`)}>
                Edit Template
              </Button>
            )}
          </Group>
          <Group gap="sm">
            {canResume && (
              <Button size="xs" color="blue"
                leftSection={<IconPlayerPlay size={14} />}
                onClick={() => setResumeOpened(true)}>
                Resume
              </Button>
            )}
            {canRerun && (
              <Tooltip label="Launch this template again as a new run, from the first step">
                <Button size="xs" color="blue" variant="light"
                  leftSection={rerunning ? <Loader size={12} /> : <IconRepeat size={14} />}
                  disabled={rerunning} onClick={handleRerun}>
                  {rerunning ? 'Starting…' : 'Start over'}
                </Button>
              </Tooltip>
            )}
            <Tooltip label="View configuration">
              <ActionIcon variant="light" color="gray" onClick={() => setConfigOpened(true)}>
                <IconSettings size={18} />
              </ActionIcon>
            </Tooltip>
            <ThemeToggle />
            <Tooltip label="Refresh status">
              <ActionIcon variant="light" color="gray" onClick={handleRefresh} disabled={refreshing}>
                {refreshing ? <Loader size={16} /> : <IconRefresh size={18} />}
              </ActionIcon>
            </Tooltip>
            <Badge color={runColor(detail.status)} variant="light" size="lg">{detail.status}</Badge>
          </Group>
        </Group>
      </AppShell.Header>

      <ResumeModal
        opened={resumeOpened}
        onClose={() => setResumeOpened(false)}
        runId={runId}
        detail={detail}
        template={template}
        stepTypes={stepTypes}
        onResumed={load}
      />

      <Drawer opened={configOpened} onClose={() => setConfigOpened(false)} title="Run configuration" position="right">
        <Stack gap="sm">
          {[...RUN_INFO_FIELDS, ...RUN_OPTION_FIELDS].map(({ key, label }) => {
            const value = detail.frozen_config?.[key];
            if (!value) return null;
            return (
              <div key={key}>
                <Text size="xs" c="dimmed">{label}</Text>
                <Text size="sm">{String(value)}</Text>
              </div>
            );
          })}
          <Divider label="Full launch config" labelPosition="left" mt="sm" />
          <Code block style={{ whiteSpace: 'pre-wrap', fontSize: 11 }}>
            {JSON.stringify(detail.frozen_config || {}, null, 2)}
          </Code>
        </Stack>
      </Drawer>

      <AppShell.Main>
        {template ? (
          <ReactFlowProvider>
            <Flow runId={runId} detail={detail} stepTypes={stepTypes} template={template} />
          </ReactFlowProvider>
        ) : (
          <Container py="xl">
            <Text c="dimmed">The template for this run is no longer available.</Text>
          </Container>
        )}
      </AppShell.Main>
    </AppShell>
  );
}
