import type { Route } from "./+types/runs.$runId";
import { AppShell, Container, Text, Group, ThemeIcon, ActionIcon, Badge, Loader, Tooltip, Button, Drawer, Stack, Code, Divider } from "@mantine/core";
import { IconActivity, IconArrowLeft, IconRefresh, IconSettings, IconEdit, IconRepeat } from "@tabler/icons-react";
import { useNavigate } from "react-router";
import { useState, useEffect, useCallback } from "react";
import { ReactFlow, ReactFlowProvider, Background, Controls } from "@xyflow/react";
import CustomNode from "../components/CustomNode";
import StepSettingsModal from "../components/StepSettingsModal";
import { getStepPanel } from "../pages/registry";
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

// Run-level Tapis options worth surfacing in the Configuration drawer, in the
// order they should appear. frozen_config also carries the full nodes/edges
// snapshot, which we deliberately don't dump here — the canvas already shows
// the DAG shape.
const RUN_OPTION_FIELDS: { key: string; label: string }[] = [
  { key: "name", label: "Template" },
  { key: "slurm_account", label: "Slurm account" },
  { key: "exec_system", label: "Exec system" },
  { key: "exec_queue", label: "Exec queue" },
  { key: "work_dir", label: "Work dir" },
  { key: "archive_system", label: "Archive system" },
];

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
  const handleRerun = useCallback(async () => {
    setRerunning(true);
    try {
      const fc = detail.frozen_config || {};
      const options = {
        slurm_account: fc.slurm_account,
        exec_system: fc.exec_system,
        exec_queue: fc.exec_queue,
        work_dir: fc.work_dir,
        archive_system: fc.archive_system,
      };
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
    } catch { /* ignore */ }
    setRerunning(false);
  }, [detail, navigate]);

  const status = (detail?.status || "").toUpperCase();
  const canRerun = status === "FAILED" || status === "CANCELLED";

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
            {detail.template_version_id && (
              <Button size="xs" variant="subtle" color="gray" leftSection={<IconEdit size={14} />}
                onClick={() => navigate(`/templates/${detail.template_version_id}/edit`)}>
                Edit Template
              </Button>
            )}
          </Group>
          <Group gap="sm">
            {canRerun && (
              <Button size="xs" color="blue" variant="light"
                leftSection={rerunning ? <Loader size={12} /> : <IconRepeat size={14} />}
                disabled={rerunning} onClick={handleRerun}>
                {rerunning ? 'Starting…' : 'Re-run'}
              </Button>
            )}
            <Tooltip label="View configuration">
              <ActionIcon variant="light" color="gray" onClick={() => setConfigOpened(true)}>
                <IconSettings size={18} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="Refresh status">
              <ActionIcon variant="light" color="gray" onClick={handleRefresh} disabled={refreshing}>
                {refreshing ? <Loader size={16} /> : <IconRefresh size={18} />}
              </ActionIcon>
            </Tooltip>
            <Badge color={runColor(detail.status)} variant="light" size="lg">{detail.status}</Badge>
          </Group>
        </Group>
      </AppShell.Header>

      <Drawer opened={configOpened} onClose={() => setConfigOpened(false)} title="Run configuration" position="right">
        <Stack gap="sm">
          {RUN_OPTION_FIELDS.map(({ key, label }) => {
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
