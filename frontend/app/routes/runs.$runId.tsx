import type { Route } from "./+types/runs.$runId";
import { AppShell, Container, Title, Text, Group, ThemeIcon, ActionIcon, Badge, Loader, Box } from "@mantine/core";
import { IconActivity, IconArrowLeft } from "@tabler/icons-react";
import { useNavigate } from "react-router";
import { useState, useEffect, useCallback } from "react";
import { ReactFlow, ReactFlowProvider, Background, Controls } from "@xyflow/react";
import CustomNode from "../components/CustomNode";
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
  return "gray";
};

function Flow({ runId, initialDetail, stepTypes, template }: any) {
  const [detail, setDetail] = useState<any>(initialDetail);

  // Poll run detail while the run is active so node statuses update live.
  const load = useCallback(async () => {
    try {
      const r = await apiFetch(`/api/pipeline-runs/${runId}/detail`);
      if (r.ok) setDetail(await r.json());
    } catch { /* ignore */ }
  }, [runId]);

  useEffect(() => {
    const active = (detail?.status || "").toUpperCase() === "RUNNING";
    if (!active) return;
    const id = setInterval(load, 2500);
    return () => clearInterval(id);
  }, [detail, load]);

  // node_id -> run status (as strings the CustomNode understands)
  const statusByNode: Record<string, string> = {};
  (detail?.steps || []).forEach((s: any) => { statusByNode[String(s.node_id)] = s.status; });

  // Hydrate template nodes with step config + this run's per-node status.
  const nodes = (template?.nodes || []).map((n: any) => {
    const stepConfig = stepTypes.find((s: any) => s.step_type_key === n.data.nodeType);
    return {
      ...n,
      // read-only: no dragging/connecting on the run view
      draggable: false,
      connectable: false,
      data: {
        ...n.data,
        fullStepConfig: stepConfig,
        runStatus: statusByNode[String(n.id)] || 'pending',
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

  const onNodeClick = (_evt: any, node: any) => {
    // Open logs for any node that ran a job or failed.
    const st = statusByNode[String(node.id)];
    if (st === 'failed' || st === 'completed' || st === 'running' || st === 'cancelled') {
      setLogNode(Number(node.id));
    }
  };

  return (
    <div style={{ width: '100%', height: 'calc(100vh - 60px)' }}>
      <StepLogsModal
        runId={Number(runId)}
        nodeId={logNode}
        stepType={logNode != null ? typeByNode[String(logNode)] : undefined}
        opened={logNode != null}
        onClose={() => setLogNode(null)}
      />
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
  const { runId, detail, stepTypes, template } = loaderData as any;

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
          </Group>
          <Badge color={runColor(detail.status)} variant="light" size="lg">{detail.status}</Badge>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        {template ? (
          <ReactFlowProvider>
            <Flow runId={runId} initialDetail={detail} stepTypes={stepTypes} template={template} />
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
