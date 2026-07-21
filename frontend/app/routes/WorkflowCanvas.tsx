import React, { useState, useCallback, useRef, useEffect } from 'react';
import { ReactFlow, ReactFlowProvider, addEdge, useNodesState, useEdgesState, Background, Controls } from '@xyflow/react';
import { AppShell, Group, Button, Text, ActionIcon, Stack, Title, Drawer, TextInput, Textarea, Notification, Code, Badge, Loader, Alert, List } from '@mantine/core';
import { IconArrowLeft, IconDeviceFloppy, IconX, IconPlayerPlay, IconAlertTriangle } from '@tabler/icons-react';
import { useNavigate, useParams, useLoaderData } from 'react-router';
import CustomNode from '../components/CustomNode';
import { apiFetch } from '../lib/api';

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
  'Data Pre-processing',
  'Data Harmonization',
  'Training',
  'Inference',
  'Visualization',
  'Post-processing',
];

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
      title={step.description || step.display_name}
      style={{
        padding: '10px', background: 'white',
        border: styles.border,
        borderRadius: '6px', cursor: 'grab', fontSize: '13px', fontWeight: 500,
        color: styles.color, boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
      }}
    >
      {step.display_name}
    </div>
  );
}

function Flow() {
  const { stepTypes, portDataTypes, templateData, id } = useLoaderData() as any;
  const navigate = useNavigate();
  const reactFlowWrapper = useRef<HTMLDivElement>(null);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [reactFlowInstance, setReactFlowInstance] = useState<any>(null);
  
  const [drawerOpened, setDrawerOpened] = useState(false);
  const [formData, setFormData] = useState({ name: '', description: '', category: 'Custom', allocation_account: 'uot260' });
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string[]>([]);
  const [outputWarnings, setOutputWarnings] = useState<string[]>([]);

  // Workflow run state (DBOS execution)
  const [running, setRunning] = useState(false);
  const [runState, setRunState] = useState<string | null>(null); // overall DBOS state
  const [runSteps, setRunSteps] = useState<any[]>([]);
  const [progressGraph, setProgressGraph] = useState<string>('');
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Run settings — where/how the workflow's Tapis jobs execute. Defaults target
  // OSC Pitzer (the working exec system); the user can edit before launching.
  const [runSettingsOpened, setRunSettingsOpened] = useState(false);
  const [runOptions, setRunOptions] = useState({
    exec_system: 'pitzer-tapis',
    exec_queue: 'gpu',
    work_dir: '/fs/ess/PAS2699/shyama',
    archive_system: 'pitzer-tapis',
    slurm_account: 'PAS2699',
  });

  // Stop polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

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

  // Kick off durable execution of the saved template via the DBOS engine with
  // the chosen run options (exec system / queue / paths), then poll status.
  const handleRun = async () => {
    if (!templateData) return;
    setRunSettingsOpened(false);
    setRunning(true);
    setRunState('PENDING');
    setRunSteps([]);
    setProgressGraph('');

    try {
      const res = await apiFetch(
        `/api/pipeline-runs/${templateData.template_version_id}/execute`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(runOptions),
        }
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Failed to start workflow');
      }
      const { dbos_workflow_id } = await res.json();

      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = setInterval(async () => {
        try {
          const sRes = await apiFetch(`/api/pipeline-runs/status/${dbos_workflow_id}`);
          if (!sRes.ok) return;
          const status = await sRes.json();
          const record = status.database_record || {};
          setRunState(record.run_status || status.workflow_state);
          setRunSteps(record.steps || []);
          setProgressGraph(status.progress_graph || '');

          if (record.run_status === 'COMPLETED' || record.run_status === 'FAILED') {
            if (pollRef.current) clearInterval(pollRef.current);
            setRunning(false);
          }
        } catch {
          /* transient; keep polling */
        }
      }, 2000);
    } catch (e: any) {
      setRunState('FAILED');
      setRunning(false);
      setConnectionError(e.message || 'Failed to start workflow');
      setTimeout(() => setConnectionError(null), 4000);
    }
  };

  const stepColor = (s: string) =>
    s === 'completed' ? 'teal' : s === 'running' ? 'blue' : s === 'failed' ? 'red' : 'gray';

  return (
    <AppShell header={{ height: 60 }} aside={{ width: 250, breakpoint: 'sm' }} padding="0">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group>
            <ActionIcon variant="subtle" color="gray" onClick={() => navigate('/templates')}>
              <IconArrowLeft size={20} />
            </ActionIcon>
            <Title order={4}>{templateData ? `${templateData.name} v${templateData.version}` : 'New Template'}</Title>
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
            <Button leftSection={<IconDeviceFloppy size={16} />} onClick={() => setDrawerOpened(true)}>
              {templateData ? 'Save New Version' : 'Save Template'}
            </Button>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Aside p="md" style={{ borderLeft: '1px solid #e2e8f0', background: '#f8fafc', overflowY: 'auto' }}>
        {/* Data Sources — inputs, kept distinct from the pipeline stages */}
        <Text fw={600} mb="xs">Data Sources</Text>
        <Stack gap="xs" mb="xl">
          {stepTypes.filter((s: any) => s.category === 'source').map((step: any) => (
            <StepCard key={step.step_type_key} step={step} variant="source" />
          ))}
        </Stack>

        {/* Pipeline stages, in execution order. Each stage groups its sub-steps
            by the step's `category` (set in step.json). Empty stages still show
            so the structure is visible and ready for future steps. */}
        {STAGES.map((stage) => {
          const stageSteps = stepTypes.filter((s: any) => s.category === stage);
          return (
            <div key={stage} style={{ marginBottom: 18 }}>
              <Text fw={600} mb="xs">{stage}</Text>
              <Stack gap="xs">
                {stageSteps.length > 0 ? (
                  stageSteps.map((step: any) => (
                    <StepCard key={step.step_type_key} step={step} variant="processing" />
                  ))
                ) : (
                  <Text size="xs" c="dimmed" fs="italic">No steps yet</Text>
                )}
              </Stack>
            </div>
          );
        })}

        {/* Data Sinks — outputs, the write-side complement of Data Sources */}
        {stepTypes.some((s: any) => s.category === 'sink') && (
          <div style={{ marginBottom: 18 }}>
            <Text fw={600} mb="xs">Data Sinks</Text>
            <Stack gap="xs">
              {stepTypes.filter((s: any) => s.category === 'sink').map((step: any) => (
                <StepCard key={step.step_type_key} step={step} variant="sink" />
              ))}
            </Stack>
          </div>
        )}
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
          {/* Run status panel */}
          {runState && (
            <div style={{
              position: 'absolute', top: 16, right: 16, zIndex: 1000,
              width: 320, background: 'white', border: '1px solid #e2e8f0',
              borderRadius: 10, boxShadow: '0 8px 24px rgba(0,0,0,0.12)', padding: 14,
            }}>
              <Group justify="space-between" mb="xs">
                <Text fw={700} size="sm">Workflow Run</Text>
                <Badge color={runState === 'COMPLETED' ? 'teal' : runState === 'FAILED' ? 'red' : 'blue'} variant="light">
                  {runState}
                </Badge>
              </Group>
              <Stack gap={6}>
                {runSteps.map((s: any) => (
                  <Group key={s.node_id} justify="space-between" wrap="nowrap">
                    <Text size="xs" c="dark.6" style={{ whiteSpace: 'nowrap' }}>{s.node_id}</Text>
                    <Badge size="xs" color={stepColor(s.status)} variant="light">{s.status}</Badge>
                  </Group>
                ))}
              </Stack>
              {progressGraph && (
                <Code block mt="sm" style={{ fontSize: 10, lineHeight: 1.3 }}>{progressGraph}</Code>
              )}
              {!running && (
                <Button size="xs" variant="subtle" mt="xs" fullWidth onClick={() => setRunState(null)}>
                  Dismiss
                </Button>
              )}
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
            Where this workflow's jobs run on Tapis. Defaults target OSC Pitzer.
          </Text>
          <TextInput
            label="Exec system"
            description="Tapis compute system (e.g. pitzer-tapis, ascend-tapis)"
            value={runOptions.exec_system}
            onChange={(e) => setRunOptions({ ...runOptions, exec_system: e.currentTarget.value })}
          />
          <TextInput
            label="Queue"
            description="Scheduler queue (OSC: cpu / gpu / nextgen)"
            value={runOptions.exec_queue}
            onChange={(e) => setRunOptions({ ...runOptions, exec_queue: e.currentTarget.value })}
          />
          <TextInput
            label="Slurm account"
            description="Allocation to charge (e.g. PAS2699)"
            value={runOptions.slurm_account}
            onChange={(e) => setRunOptions({ ...runOptions, slurm_account: e.currentTarget.value })}
          />
          <TextInput
            label="Work dir"
            description="Absolute scratch/project base path for job working dirs"
            value={runOptions.work_dir}
            onChange={(e) => setRunOptions({ ...runOptions, work_dir: e.currentTarget.value })}
          />
          <TextInput
            label="Archive system"
            description="Where step outputs are archived (unless a sink node overrides it)"
            value={runOptions.archive_system}
            onChange={(e) => setRunOptions({ ...runOptions, archive_system: e.currentTarget.value })}
          />
          <Button color="green" fullWidth mt="md" leftSection={<IconPlayerPlay size={16} />} onClick={handleRun}>
            Launch Run
          </Button>
        </Stack>
      </Drawer>
    </AppShell>
  );
}

export default function WorkflowCanvasWrapper() {
  return (
    <ReactFlowProvider>
      <Flow />
    </ReactFlowProvider>
  );
}
