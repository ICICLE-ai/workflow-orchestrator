import React, { useState, useCallback, useRef, useEffect } from 'react';
import { ReactFlow, ReactFlowProvider, addEdge, useNodesState, useEdgesState, Background, Controls } from '@xyflow/react';
import { AppShell, Group, Button, Text, ActionIcon, Stack, Title, Drawer, TextInput, Textarea, Select, Notification, Loader, Alert, List, Accordion, Divider, Modal, Badge } from '@mantine/core';
import { IconArrowLeft, IconDeviceFloppy, IconX, IconPlayerPlay, IconAlertTriangle } from '@tabler/icons-react';
import { useNavigate, useParams, useLoaderData } from 'react-router';
import CustomNode from '../components/CustomNode';
import { apiFetch, fetchCurrentUser } from '../lib/api';
import { TAPIS_SYSTEMS, defaultWorkDir } from '../lib/tapis';
import TopNav from '../components/TopNav';

const nodeTypes = { customNode: CustomNode };

export async function clientLoader({ params }: { params: any }) {
  // Fetch step types and port data types from backend
  const [stepsRes, typesRes] = await Promise.all([
    apiFetch("/api/step-types"),
    apiFetch("/api/port-data-types"),
  ]);
  const stepTypes = await stepsRes.json();
  const portDataTypes = await typesRes.json();

  let templateData = null;
  if (params.id) {
    const tRes = await apiFetch(`/api/workflow-templates/${params.id}`);
    if (tRes.ok) templateData = await tRes.json();
  }

  return { stepTypes, portDataTypes, templateData, id: params.id };
}

// Build a type compatibility checker from the port_data_type hierarchy
function buildTypeChecker(portDataTypes: any[]) {
  const typeMap = new Map<string, { parent_type: string | null; coerce_from: string[] }>();
  for (const t of portDataTypes) {
    typeMap.set(t.type_key, { parent_type: t.parent_type, coerce_from: t.coerce_from || [] });
  }

  // Get the full ancestor chain for a type (including itself)
  function getAncestors(typeKey: string): Set<string> {
    const ancestors = new Set<string>();
    let current: string | null = typeKey;
    while (current) {
      ancestors.add(current);
      const entry = typeMap.get(current);
      current = entry?.parent_type || null;
    }
    return ancestors;
  }

  // Check if sourceType is compatible with targetType
  // Rules from the schema design:
  // 1. Exact match: sourceType === targetType
  // 2. Parent match: sourceType is a child of targetType (e.g., image_dir -> file_collection)
  // 3. Coercion: targetType.coerce_from includes sourceType
  return function isCompatible(sourceType: string, targetType: string): boolean {
    // Rule 1: Exact match
    if (sourceType === targetType) return true;

    // Rule 2: Source is a subtype of target (source's ancestors include target)
    const sourceAncestors = getAncestors(sourceType);
    if (sourceAncestors.has(targetType)) return true;

    // Rule 3: Target accepts coercion from source
    const targetEntry = typeMap.get(targetType);
    if (targetEntry && targetEntry.coerce_from.includes(sourceType)) return true;

    return false;
  };
}

// Pipeline stages shown in the palette, in execution order. A step is placed in
// a stage when its `category` (from step.json) matches one of these names.
const STAGES = [
  'Data Collection',
  'Data Creation',
  'Data Pre-processing',
  'Data Harmonization',
  'Training',
  'Inference',
  'Visualization',
  'Post-processing',
];

// Canonical string form of the graph, for detecting unsaved changes. Covers
// exactly what a saved version persists — each node's type and config, and each
// edge's endpoints and ports — and deliberately NOT node positions: nudging a
// box on the canvas changes nothing about what would run, and treating it as a
// modification would make the "unsaved changes" prompt fire constantly.
//
// Nodes and edges are sorted because React Flow reorders them freely (selecting
// a node moves it to the end so it renders on top), which would otherwise read
// as a change.
function graphSnapshot(nodes: any[], edges: any[]): string {
  const n = nodes
    .map((x: any) => JSON.stringify([x.id, x.data?.nodeType, x.data?.config_values ?? {}]))
    .sort();
  const e = edges
    .map((x: any) => JSON.stringify([x.source, x.sourceHandle, x.target, x.targetHandle]))
    .sort();
  return JSON.stringify({ n, e });
}

// A draggable palette card for a step type. `variant` switches the accent color:
// data sources (green, dashed), pipeline steps (blue), data sinks (amber, dashed).
function StepCard({ step, variant }: { step: any; variant: 'source' | 'processing' | 'sink' }) {
  const styles = {
    source: { border: '1px dashed #10b981', color: '#059669' },
    processing: { border: '1px solid #3b82f6', color: '#1d4ed8' },
    sink: { border: '1px dashed #f59e0b', color: '#b45309' },
  }[variant];
  return (
    <div
      onDragStart={(e) => {
        e.dataTransfer.setData('application/reactflow', step.step_type_key);
        e.dataTransfer.effectAllowed = 'move';
      }}
      draggable
      style={{
        width: '100%',
        boxSizing: 'border-box',
        padding: '10px', background: 'white',
        border: styles.border,
        borderRadius: '6px', cursor: 'grab',
        boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
      }}
    >
      <div style={{
        fontSize: '13px', fontWeight: 500, color: styles.color,
        overflowWrap: 'break-word', wordBreak: 'break-word',
      }}>
        {step.display_name}
      </div>
      {step.description && (
        <div style={{
          fontSize: '11px', fontWeight: 400, color: '#64748b', marginTop: 4,
          whiteSpace: 'normal', overflowWrap: 'break-word', wordBreak: 'break-word',
        }}>
          {step.description}
        </div>
      )}
    </div>
  );
}

function Flow() {
  const { stepTypes, portDataTypes, templateData, id } = useLoaderData() as any;
  const navigate = useNavigate();

  // Steps offered in the palette. `hidden` (step.json -> StepTypeRegistry.hidden)
  // takes a step out of the drag-and-drop list without unregistering it, so a
  // step that's registered but not ready to be offered stops appearing.
  //
  // Only the PALETTE filters. The full `stepTypes` list is still what saved
  // templates resolve their nodes against below — filtering there would leave
  // every existing template that uses a hidden step with port-less,
  // unconfigurable nodes.
  const paletteSteps = (stepTypes as any[]).filter((s: any) => !s.hidden);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [reactFlowInstance, setReactFlowInstance] = useState<any>(null);
  
  const [drawerOpened, setDrawerOpened] = useState(false);
  const [formData, setFormData] = useState({ name: '', description: '', category: 'Custom', allocation_account: 'uot260' });
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string[]>([]);
  const [outputWarnings, setOutputWarnings] = useState<string[]>([]);

  // Workflow run kickoff (DBOS execution) — the run's live status is shown on
  // /runs/:runId, which we navigate to as soon as launch succeeds.
  const [running, setRunning] = useState(false);

  // The graph as of the last load/save (see graphSnapshot). `null` until a
  // template is loaded, and for a brand-new unsaved canvas.
  const [savedSnapshot, setSavedSnapshot] = useState<string | null>(null);
  // Prompt shown when Run is pressed with unsaved edits — see handleRun.
  const [unsavedPromptOpen, setUnsavedPromptOpen] = useState(false);
  const isDirty = savedSnapshot !== null && graphSnapshot(nodes, edges) !== savedSnapshot;

  // Run settings — where/how the workflow's Tapis jobs execute. Defaults target
  // OSC Pitzer (the working exec system); the user can edit before launching.
  const [runSettingsOpened, setRunSettingsOpened] = useState(false);
  const [runOptions, setRunOptions] = useState({
    // The run declares TWO exec targets, not one: each step's step.json says
    // whether it needs a GPU ("resources": {"gpu": true}), and the engine routes
    // it to the matching pair (engine/transactions.py resolve_node_exec_target).
    // That's what lets zero_shot_annotation land on a GPU queue while
    // flight_plan/geospatial go to a CPU queue in the SAME run — previously
    // impossible, since every step either followed one run-level pair or
    // hardcoded its own site in step.json.
    exec_system: 'pitzer-tapis',
    exec_queue: 'cpu',
    // Blank inherits the CPU pair above, preserving single-target behaviour for
    // runs that don't care to split.
    gpu_exec_system: 'pitzer-tapis',
    gpu_exec_queue: 'gpu',
    work_dir: defaultWorkDir('pitzer-tapis', { slurmAccount: 'PAS2699' }),
    archive_system: 'pitzer-tapis',
    // Optional override for the base archive directory (run_id/step_type_key/
    // node_id is still appended beneath it). Empty means "derive from work_dir",
    // same as before this field existed — see get_run_archive_context.
    archive_dir: '',
    slurm_account: 'PAS2699',
  });

  // Tapis username — needed for expanse-tapis's per-user scratch path.
  const [tapisUsername, setTapisUsername] = useState('');
  useEffect(() => {
    fetchCurrentUser().then((u) => setTapisUsername(u?.username || ''));
  }, []);

  // work_dir is the base every step ARCHIVES under, and archiving stays
  // run-level even now that exec target is per-node — so it follows the
  // ARCHIVE system's scratch/project layout, not any one step's exec system.
  // (A run whose GPU steps are on Expanse and CPU steps on OSC has no single
  // exec system to inherit a path from; its artifacts still have one home.)
  useEffect(() => {
    setRunOptions((prev) => ({
      ...prev,
      work_dir: defaultWorkDir(prev.archive_system, { slurmAccount: prev.slurm_account, username: tapisUsername }),
    }));
  }, [runOptions.archive_system, runOptions.slurm_account, tapisUsername]);

  // Queue choices come from the exec system itself (Tapis' batchLogicalQueues),
  // not a hardcoded list. Fetched once per system, for each of the two targets.
  const cpuQueues = useSystemQueues(runOptions.exec_system);
  const gpuQueues = useSystemQueues(runOptions.gpu_exec_system);

  // Keep each queue valid for its own system: when a system changes, a queue
  // name it doesn't offer is replaced by that system's default.
  useEffect(() => {
    const names = cpuQueues.queues.map((q: any) => q.name);
    if (!names.length) return;
    setRunOptions((prev) =>
      names.includes(prev.exec_queue)
        ? prev
        : { ...prev, exec_queue: cpuQueues.defaultQueue || names[0] || prev.exec_queue }
    );
  }, [cpuQueues.queues, cpuQueues.defaultQueue]);
  useEffect(() => {
    const names = gpuQueues.queues.map((q: any) => q.name);
    if (!names.length) return;
    setRunOptions((prev) =>
      names.includes(prev.gpu_exec_queue)
        ? prev
        : { ...prev, gpu_exec_queue: gpuQueues.defaultQueue || names[0] || prev.gpu_exec_queue }
    );
  }, [gpuQueues.queues, gpuQueues.defaultQueue]);

  const queues = cpuQueues.queues;
  const queuesLoading = cpuQueues.loading;

  // Which steps actually on this canvas will follow the GPU target — makes the
  // routing concrete instead of asking the user to remember which step types
  // declare "resources": {"gpu": true}.
  const gpuStepNames = Array.from(new Set(
    nodes
      .filter((n: any) => n.data?.fullStepConfig?.resources?.gpu)
      .map((n: any) => n.data?.fullStepConfig?.display_name || n.data?.nodeType)
      .filter(Boolean)
  )) as string[];

  // Human-readable limits for a selected queue, shown as the Select's
  // description once queues have loaded.
  const queueDetail = (qs: any[], name: string) => {
    const q = qs.find((qq: any) => qq.name === name);
    if (!q) return null;
    const parts = [
      q.maxNodeCount != null && `max ${q.maxNodeCount} node(s)`,
      q.maxCoresPerNode != null && `${q.maxCoresPerNode} cores/node`,
      q.maxMinutes != null && `${q.maxMinutes} min max`,
    ].filter(Boolean);
    return parts.length ? parts.join(" · ") : null;
  };
  const selectedQueueDetail = queueDetail(queues, runOptions.exec_queue);
  const gpuQueueDetail = queueDetail(gpuQueues.queues, runOptions.gpu_exec_queue);

  // Build the type checker once
  const isTypeCompatible = useCallback(
    buildTypeChecker(portDataTypes || []),
    [portDataTypes]
  );

  useEffect(() => {
    if (templateData) {
      setFormData({
        name: templateData.name,
        description: templateData.description,
        category: templateData.category,
        allocation_account: templateData.allocation_account || 'uot260',
      });
      // Prefill the run's charge account from the template's allocation account.
      if (templateData.allocation_account) {
        setRunOptions((prev) => ({ ...prev, slurm_account: templateData.allocation_account }));
      }

      const hydratedNodes = templateData.nodes.map((n: any) => {
        const stepConfig = stepTypes.find((s: any) => s.step_type_key === n.data.nodeType);
        return {
          ...n,
          data: { ...n.data, fullStepConfig: stepConfig, template_version_id: templateData.template_version_id }
        };
      });
      setNodes(hydratedNodes);
      setEdges(templateData.edges || []);
      // Baseline for the unsaved-changes check below: what the loaded version
      // actually contains. Anything the user does from here diverges from it.
      setSavedSnapshot(graphSnapshot(hydratedNodes, templateData.edges || []));
    }
  }, [templateData, stepTypes]);

  // Validate connection before allowing it
  const isValidConnection = useCallback((connection: any) => {
    const sourceNode = nodes.find((n) => n.id === connection.source);
    const targetNode = nodes.find((n) => n.id === connection.target);
    if (!sourceNode || !targetNode) return false;

    const sourceConfig = sourceNode.data?.fullStepConfig;
    const targetConfig = targetNode.data?.fullStepConfig;
    if (!sourceConfig || !targetConfig) return false;

    // Find the output port on the source node
    const sourceOutputs = (sourceConfig.outputs || []).map((p: any) => ({
      port_name: p.port_name || p.name,
      data_type: p.data_type || p.type || 'any',
    }));
    const sourcePort = sourceOutputs.find((p: any) => p.port_name === connection.sourceHandle);

    // Find the input port on the target node
    const targetInputs = (targetConfig.inputs || []).map((p: any) => ({
      port_name: p.port_name || p.name,
      data_type: p.data_type || p.type || 'any',
    }));
    const targetPort = targetInputs.find((p: any) => p.port_name === connection.targetHandle);

    if (!sourcePort || !targetPort) return false;

    return isTypeCompatible(sourcePort.data_type, targetPort.data_type);
  }, [nodes, isTypeCompatible]);

  const onConnect = useCallback((params: any) => {
    // Double-check type compatibility and show error if invalid
    const sourceNode = nodes.find((n) => n.id === params.source);
    const targetNode = nodes.find((n) => n.id === params.target);

    if (sourceNode && targetNode) {
      const sourceConfig = sourceNode.data?.fullStepConfig;
      const targetConfig = targetNode.data?.fullStepConfig;

      const sourceOutputs = (sourceConfig?.outputs || []).map((p: any) => ({
        port_name: p.port_name || p.name,
        data_type: p.data_type || p.type || 'any',
      }));
      const targetInputs = (targetConfig?.inputs || []).map((p: any) => ({
        port_name: p.port_name || p.name,
        data_type: p.data_type || p.type || 'any',
      }));

      const srcPort = sourceOutputs.find((p: any) => p.port_name === params.sourceHandle);
      const tgtPort = targetInputs.find((p: any) => p.port_name === params.targetHandle);

      if (srcPort && tgtPort && !isTypeCompatible(srcPort.data_type, tgtPort.data_type)) {
        setConnectionError(
          `Cannot connect: "${srcPort.port_name}" (${srcPort.data_type}) is incompatible with "${tgtPort.port_name}" (${tgtPort.data_type})`
        );
        setTimeout(() => setConnectionError(null), 4000);
        return;
      }
    }

    setEdges((eds) => addEdge(params, eds));
  }, [setEdges, nodes, isTypeCompatible]);

  const onDragOver = useCallback((event: any) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback((event: any) => {
    event.preventDefault();
    const type = event.dataTransfer.getData('application/reactflow');
    if (!type || !reactFlowInstance) return;

    const stepConfig = stepTypes.find((s: any) => s.step_type_key === type);
    const position = reactFlowInstance.screenToFlowPosition({ x: event.clientX, y: event.clientY });
    
    const newNode = {
      id: `${type}_${new Date().getTime()}`,
      type: 'customNode',
      position,
      data: { nodeType: type, config_values: {}, fullStepConfig: stepConfig }
    };
    setNodes((nds) => nds.concat(newNode));
  }, [reactFlowInstance, stepTypes, setNodes]);

  // Find required input ports that aren't fed by an incoming edge. Returns a
  // list of human-readable problems; empty means the workflow is complete.
  const findHangingInputs = (): string[] => {
    // node id -> set of target ports that have an incoming edge
    const satisfied = new Map<string, Set<string>>();
    for (const e of edges) {
      if (!e.targetHandle) continue;
      if (!satisfied.has(e.target)) satisfied.set(e.target, new Set());
      satisfied.get(e.target)!.add(e.targetHandle);
    }
    const problems: string[] = [];
    for (const n of nodes) {
      const cfg = stepTypes.find((s: any) => s.step_type_key === n.data.nodeType);
      const inputs = (cfg?.inputs || []);
      for (const p of inputs) {
        const name = p.port_name || p.name;
        const required = p.is_required !== false; // default required
        if (!required) continue;
        if (!satisfied.get(n.id)?.has(name)) {
          const label = cfg?.display_name || n.data.nodeType;
          problems.push(`"${label}" is missing required input "${name}"`);
        }
      }
    }
    return problems;
  };

  // Find output ports with no outgoing edge — the user may have forgotten to
  // attach a sink (e.g. inference's predictions). This is a SOFT warning, not a
  // hard block: intermediate outputs are often intentionally left unconsumed.
  // Source and sink nodes are excluded (sources always dangle by design; sinks
  // have no outputs).
  const findHangingOutputs = (): string[] => {
    const used = new Map<string, Set<string>>(); // node id -> source ports that feed an edge
    for (const e of edges) {
      if (!e.sourceHandle) continue;
      if (!used.has(e.source)) used.set(e.source, new Set());
      used.get(e.source)!.add(e.sourceHandle);
    }
    const warnings: string[] = [];
    for (const n of nodes) {
      const cfg = stepTypes.find((s: any) => s.step_type_key === n.data.nodeType);
      if (!cfg || cfg.category === 'source' || cfg.category === 'sink') continue;
      for (const p of (cfg.outputs || [])) {
        const name = p.port_name || p.name;
        if (!used.get(n.id)?.has(name)) {
          warnings.push(`"${cfg.display_name || n.data.nodeType}" output "${name}" is not saved to a sink`);
        }
      }
    }
    return warnings;
  };

  const handleSave = async (confirmedHangingOutputs = false) => {
    // Hard constraint: block incomplete workflows (unsatisfied required inputs).
    const hanging = findHangingInputs();
    if (hanging.length > 0) {
      setSaveError(hanging);
      setOutputWarnings([]);
      return;
    }
    setSaveError([]);

    // Soft constraint: warn about unconsumed outputs, but let the user proceed.
    if (!confirmedHangingOutputs) {
      const outWarn = findHangingOutputs();
      if (outWarn.length > 0) {
        setOutputWarnings(outWarn);
        return;
      }
    }
    setOutputWarnings([]);

    const payload = {
      ...formData,
      nodes: nodes.map(n => ({
        id: n.id,
        type: n.data.nodeType,
        position: n.position,
        data: { config_values: n.data.config_values }
      })),
      edges: edges.map(e => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.sourceHandle, targetHandle: e.targetHandle }))
    };

    const url = id && templateData
      ? `/api/workflow-templates/${templateData.template_id}/versions`
      : '/api/workflow-templates';

    try {
      const res = await apiFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        // Surface the backend's validation message (e.g. hanging inputs it caught).
        const err = await res.json().catch(() => ({}));
        setSaveError([err.detail || 'Failed to save template']);
        return;
      }
      const data = await res.json();
      alert(data.message);
      navigate('/templates');
    } catch (err) {
      setSaveError(['Failed to save template (network error).']);
    }
  };

  // Persist the current canvas as a new version and return its
  // template_version_id. `draft` marks it as a "run without saving" snapshot —
  // same rows, but hidden from the template list (see WorkflowTemplate.is_draft).
  // Unlike handleSave this doesn't navigate away, because the run flow needs to
  // keep going with the id it returns.
  const persistVersion = async (draft: boolean): Promise<number> => {
    const payload = {
      ...formData,
      nodes: nodes.map((n: any) => ({
        id: n.id,
        type: n.data.nodeType,
        position: n.position,
        data: { config_values: n.data.config_values },
      })),
      edges: edges.map((e: any) => ({
        id: e.id, source: e.source, target: e.target,
        sourceHandle: e.sourceHandle, targetHandle: e.targetHandle,
      })),
    };
    const res = await apiFetch(
      `/api/workflow-templates/${templateData.template_id}/versions${draft ? '?draft=true' : ''}`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }
    );
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || 'Could not save the workflow');
    }
    const data = await res.json();
    return data.template_version_id;
  };

  // Kick off durable execution via the DBOS engine with the chosen run options
  // (exec system / queue / paths), then jump straight to the run's live-status
  // page — the execute endpoint creates the run synchronously so run_id is
  // available immediately, no polling needed here.
  //
  // `versionId` is which version to execute. It is ALWAYS a version that
  // reflects what's on screen: with unsaved edits, handleRun persists one first
  // (as a real version or a draft, the user's choice) rather than running the
  // last-saved graph. Running a stale version silently produced results for a
  // workflow the user was no longer looking at.
  const executeVersion = async (versionId: number) => {
    setRunSettingsOpened(false);
    setUnsavedPromptOpen(false);
    setRunning(true);
    try {
      const res = await apiFetch(`/api/pipeline-runs/${versionId}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(runOptions),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Failed to start workflow');
      }
      const { run_id } = await res.json();
      navigate(`/runs/${run_id}`);
    } catch (e: any) {
      setRunning(false);
      setConnectionError(e.message || 'Failed to start workflow');
      setTimeout(() => setConnectionError(null), 4000);
    }
  };

  const handleRun = async () => {
    if (!templateData) return;
    if (isDirty) {
      // Don't launch anything yet — ask how the changes should be handled.
      setRunSettingsOpened(false);
      setUnsavedPromptOpen(true);
      return;
    }
    executeVersion(templateData.template_version_id);
  };

  // Both answers to that prompt: capture the canvas, then run what was captured.
  const runWithUnsavedChanges = async (saveAsVersion: boolean) => {
    setRunning(true);
    try {
      const versionId = await persistVersion(!saveAsVersion);
      // The canvas now matches what's stored, so Run stops prompting until the
      // next edit — whichever option was taken.
      setSavedSnapshot(graphSnapshot(nodes, edges));
      await executeVersion(versionId);
    } catch (e: any) {
      setRunning(false);
      setUnsavedPromptOpen(false);
      setConnectionError(e.message || 'Could not save the workflow');
      setTimeout(() => setConnectionError(null), 6000);
    }
  };

  return (
    <AppShell header={{ height: 60 }} aside={{ width: 250, breakpoint: 'sm' }} padding="0">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group>
            <ActionIcon variant="subtle" color="gray" onClick={() => navigate('/templates')}>
              <IconArrowLeft size={20} />
            </ActionIcon>
            <Title order={4}>{templateData ? `${templateData.name} v${templateData.version}` : 'New Template'}</Title>
            <TopNav />
          </Group>
          <Group gap="sm">
            {templateData && (
              <Button
                color="green"
                leftSection={running ? <Loader size={14} color="white" /> : <IconPlayerPlay size={16} />}
                onClick={() => setRunSettingsOpened(true)}
                disabled={running}
                title="Configure and run this workflow"
              >
                {running ? 'Running…' : 'Run Workflow'}
              </Button>
            )}
            {/* Standing indicator, so the state that changes what Run does is
                visible before Run is pressed rather than only in the prompt. */}
            {isDirty && (
              <Badge color="yellow" variant="light" title="The canvas differs from the last saved version">
                Unsaved changes
              </Badge>
            )}
            <Button leftSection={<IconDeviceFloppy size={16} />} onClick={() => setDrawerOpened(true)}>
              {templateData ? 'Save New Version' : 'Save Template'}
            </Button>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Aside p="md" style={{ borderLeft: '1px solid #e2e8f0', background: '#f8fafc', overflowY: 'auto' }}>
        {/* Categories (Data Sources, each pipeline stage, Data Sinks) are each
            collapsible — `multiple` lets more than one stay open at once, and
            the default value opens every section so this looks the same as
            before until a section is deliberately collapsed. Kept in local
            state (not persisted) — a fresh page load always starts fully open. */}
        <Accordion
          multiple
          defaultValue={['__sources__', ...STAGES, '__sinks__']}
          variant="separated"
          chevronPosition="left"
          styles={{ control: { padding: '8px 4px' }, panel: { paddingInline: 4 }, label: { padding: 0 } }}
        >
          {/* Data Sources — inputs, kept distinct from the pipeline stages */}
          <Accordion.Item value="__sources__">
            <Accordion.Control><Text fw={600}>Data Sources</Text></Accordion.Control>
            <Accordion.Panel>
              <Stack gap="xs">
                {paletteSteps.filter((s: any) => s.category === 'source').map((step: any) => (
                  <StepCard key={step.step_type_key} step={step} variant="source" />
                ))}
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>

          {/* Pipeline stages, in execution order. Each stage groups its sub-steps
              by the step's `category` (set in step.json). Empty stages still show
              so the structure is visible and ready for future steps. */}
          {STAGES.map((stage) => {
            const stageSteps = paletteSteps.filter((s: any) => s.category === stage);
            return (
              <Accordion.Item key={stage} value={stage}>
                <Accordion.Control><Text fw={600}>{stage}</Text></Accordion.Control>
                <Accordion.Panel>
                  <Stack gap="xs">
                    {stageSteps.length > 0 ? (
                      stageSteps.map((step: any) => (
                        <StepCard key={step.step_type_key} step={step} variant="processing" />
                      ))
                    ) : (
                      <Text size="xs" c="dimmed" fs="italic">No steps yet</Text>
                    )}
                  </Stack>
                </Accordion.Panel>
              </Accordion.Item>
            );
          })}

          {/* Data Sinks — outputs, the write-side complement of Data Sources */}
          {paletteSteps.some((s: any) => s.category === 'sink') && (
            <Accordion.Item value="__sinks__">
              <Accordion.Control><Text fw={600}>Data Sinks</Text></Accordion.Control>
              <Accordion.Panel>
                <Stack gap="xs">
                  {paletteSteps.filter((s: any) => s.category === 'sink').map((step: any) => (
                    <StepCard key={step.step_type_key} step={step} variant="sink" />
                  ))}
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
          )}
        </Accordion>
      </AppShell.Aside>

      <AppShell.Main>
        <div style={{ width: '100%', height: 'calc(100vh - 60px)', position: 'relative' }} ref={reactFlowWrapper}>
          {/* Connection error toast */}
          {connectionError && (
            <div style={{
              position: 'absolute', top: 16, left: '50%', transform: 'translateX(-50%)',
              zIndex: 1000, minWidth: 400,
            }}>
              <Notification
                icon={<IconX size={18} />}
                color="red"
                title="Invalid Connection"
                onClose={() => setConnectionError(null)}
                withBorder
                style={{ boxShadow: '0 8px 24px rgba(0,0,0,0.15)' }}
              >
                {connectionError}
              </Notification>
            </div>
          )}
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            isValidConnection={isValidConnection}
            onInit={setReactFlowInstance}
            onDrop={onDrop}
            onDragOver={onDragOver}
            nodeTypes={nodeTypes}
            fitView
          >
            <Background color="#ccc" gap={16} />
            <Controls />
          </ReactFlow>
        </div>
      </AppShell.Main>

      <Drawer opened={drawerOpened} onClose={() => { setDrawerOpened(false); setSaveError([]); setOutputWarnings([]); }} title="Save Workflow Template" position="right">
        <Stack>
          <TextInput
            label="Template Name"
            value={formData.name}
            onChange={(e) => setFormData({ ...formData, name: e.currentTarget.value })}
            required
          />
          <Textarea
            label="Description"
            value={formData.description}
            onChange={(e) => setFormData({ ...formData, description: e.currentTarget.value })}
          />
          <TextInput
            label="Allocation account"
            description="Charge code for this template's runs (e.g. uot260)."
            value={formData.allocation_account}
            onChange={(e) => setFormData({ ...formData, allocation_account: e.currentTarget.value })}
          />
          {saveError.length > 0 && (
            <Alert color="red" icon={<IconAlertTriangle size={18} />} title="Workflow incomplete — cannot save">
              <Text size="sm" mb={saveError.length > 1 ? 'xs' : 0}>
                Every required input must be connected to an upstream output or a data source.
              </Text>
              {saveError.length > 1 ? (
                <List size="sm" spacing={2}>
                  {saveError.map((p, i) => <List.Item key={i}>{p}</List.Item>)}
                </List>
              ) : (
                <Text size="sm">{saveError[0]}</Text>
              )}
            </Alert>
          )}
          {outputWarnings.length > 0 && (
            <Alert color="yellow" icon={<IconAlertTriangle size={18} />} title="Some outputs aren't saved">
              <Text size="sm" mb="xs">
                These node outputs aren't connected to a sink, so their results won't be
                written anywhere. If that's intentional you can proceed; otherwise add a
                sink (e.g. "Write Results") to keep them.
              </Text>
              <List size="sm" spacing={2}>
                {outputWarnings.map((p, i) => <List.Item key={i}>{p}</List.Item>)}
              </List>
            </Alert>
          )}
          {outputWarnings.length > 0 ? (
            <Group grow mt="md">
              <Button variant="default" onClick={() => setOutputWarnings([])}>Go Back</Button>
              <Button color="yellow" onClick={() => handleSave(true)}>Save Anyway</Button>
            </Group>
          ) : (
            <Button fullWidth onClick={() => handleSave()} mt="md">Confirm Save</Button>
          )}
        </Stack>
      </Drawer>

      {/* Run Settings — where/how the workflow's Tapis jobs execute. Prefilled
          with OSC Pitzer defaults; user reviews/edits, then launches. */}
      <Drawer opened={runSettingsOpened} onClose={() => setRunSettingsOpened(false)} title="Run Settings" position="right">
        <Stack>
          <Text size="sm" c="dimmed">
            Where this workflow's jobs run on Tapis. Each step declares whether it needs a GPU, and is routed
            to the matching target below — so GPU steps (zero-shot, training) and CPU steps (flight plan,
            geospatial) can run on different systems and queues within one run. A step can still pin its own
            system via its Run Configuration.
          </Text>

          <Divider label="CPU target" labelPosition="left" />
          <Select
            label="Exec system"
            description="Where steps with no GPU requirement run"
            data={TAPIS_SYSTEMS}
            value={runOptions.exec_system}
            allowDeselect={false}
            onChange={(v) => setRunOptions((prev) => ({
              ...prev,
              exec_system: v ?? prev.exec_system,
              // Default the archive system to match; the field below still
              // lets it be pointed elsewhere afterward.
              archive_system: v ?? prev.archive_system,
            }))}
          />
          <Select
            label="Queue"
            description={
              queuesLoading
                ? "Loading queues from the exec system…"
                : selectedQueueDetail || "Scheduler queue offered by the exec system"
            }
            data={queues.map((q: any) => q.name).filter(Boolean)}
            value={runOptions.exec_queue}
            onChange={(v) => setRunOptions((prev) => ({ ...prev, exec_queue: v ?? prev.exec_queue }))}
            disabled={queuesLoading}
            placeholder={queuesLoading ? "Loading…" : "Select a queue"}
            allowDeselect={false}
          />

          <Divider label="GPU target" labelPosition="left" />
          <Select
            label="Exec system"
            description={`Where steps declaring a GPU requirement run${gpuStepNames.length ? ` — ${gpuStepNames.join(', ')} on this canvas` : ' (none on this canvas yet)'}`}
            data={TAPIS_SYSTEMS}
            value={runOptions.gpu_exec_system}
            allowDeselect={false}
            onChange={(v) => setRunOptions((prev) => ({ ...prev, gpu_exec_system: v ?? prev.gpu_exec_system }))}
          />
          <Select
            label="Queue"
            description={
              gpuQueues.loading
                ? "Loading queues from the GPU exec system…"
                : gpuQueueDetail || "Scheduler queue offered by the GPU exec system"
            }
            data={gpuQueues.queues.map((q: any) => q.name).filter(Boolean)}
            value={runOptions.gpu_exec_queue}
            onChange={(v) => setRunOptions((prev) => ({ ...prev, gpu_exec_queue: v ?? prev.gpu_exec_queue }))}
            disabled={gpuQueues.loading}
            placeholder={gpuQueues.loading ? "Loading…" : "Select a queue"}
            allowDeselect={false}
          />

          <Divider label="Shared" labelPosition="left" />
          <TextInput
            label="Slurm account"
            description="Allocation to charge (e.g. PAS2699)"
            value={runOptions.slurm_account}
            onChange={(e) => setRunOptions({ ...runOptions, slurm_account: e.currentTarget.value })}
          />
          <TextInput
            label="Work dir"
            description="Derived automatically from the exec system (and charge account / Tapis username)"
            value={runOptions.work_dir}
            disabled
          />
          <Select
            label="Archive system"
            description="Where step outputs are archived (unless a sink node overrides it)"
            data={TAPIS_SYSTEMS}
            value={runOptions.archive_system}
            allowDeselect={false}
            onChange={(v) => setRunOptions((prev) => ({ ...prev, archive_system: v ?? prev.archive_system }))}
          />
          <TextInput
            label="Archive dir (optional)"
            description="Base archive directory on the archive system. Leave blank to derive it from Work dir instead — either way, each step still archives under .../{run_id}/{step_type_key}/{node_id}."
            placeholder={runOptions.work_dir ? `${runOptions.work_dir.replace(/\/$/, '')}/wf_runs` : 'wf_runs'}
            value={runOptions.archive_dir}
            onChange={(e) => setRunOptions({ ...runOptions, archive_dir: e.currentTarget.value })}
          />
          <Button color="green" fullWidth mt="md" leftSection={<IconPlayerPlay size={16} />} onClick={handleRun}>
            Launch Run
          </Button>
        </Stack>
      </Drawer>

      {/* Unsaved changes at launch time. The run has NOT started at this point —
          whichever option is taken, what executes is the graph currently on the
          canvas, never the last-saved one. */}
      <Modal
        opened={unsavedPromptOpen}
        onClose={() => setUnsavedPromptOpen(false)}
        title="This workflow has unsaved changes"
        centered
      >
        <Stack gap="md">
          <Text size="sm">
            The canvas differs from the last saved version. Your changes will be what runs either way — choose
            whether to keep them in this workflow's version history.
          </Text>
          <Button
            fullWidth
            loading={running}
            leftSection={<IconDeviceFloppy size={16} />}
            onClick={() => runWithUnsavedChanges(true)}
          >
            Save as a new version, then run
          </Button>
          <Button
            fullWidth
            variant="default"
            loading={running}
            leftSection={<IconPlayerPlay size={16} />}
            onClick={() => runWithUnsavedChanges(false)}
          >
            Run these changes without saving a version
          </Button>
          <Text size="xs" c="dimmed">
            "Without saving" still records the exact graph behind the scenes so the run stays reproducible — it
            just won't appear as a version of this workflow, and won't change what opens next time.
          </Text>
          <Button variant="subtle" color="gray" onClick={() => setUnsavedPromptOpen(false)} disabled={running}>
            Cancel
          </Button>
        </Stack>
      </Modal>
    </AppShell>
  );
}

// Fetch a Tapis exec system's batch logical queues. Used once per exec target
// (the run declares a CPU one and a GPU one), so it's a hook rather than an
// inline effect — two copies of the fetch/cancel/failure handling would
// otherwise sit side by side in Flow.
function useSystemQueues(system: string) {
  const [queues, setQueues] = useState<any[]>([]);
  const [defaultQueue, setDefaultQueue] = useState<string>('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    if (!system) {
      setQueues([]);
      setDefaultQueue('');
      return;
    }
    let cancelled = false;
    setLoading(true);
    apiFetch(`/api/tapis-systems/${system}/queues`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (cancelled || !d) return;
        setQueues(d.queues || []);
        setDefaultQueue(d.default_queue || '');
      })
      .catch(() => { if (!cancelled) setQueues([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [system]);
  return { queues, defaultQueue, loading };
}

export default function WorkflowCanvasWrapper() {
  return (
    <ReactFlowProvider>
      <Flow />
    </ReactFlowProvider>
  );
}
