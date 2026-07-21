import React, { useState } from 'react';
import { Handle, Position, useReactFlow } from '@xyflow/react';
import { ActionIcon, Group, Text, Box, Badge } from '@mantine/core';
import { IconSettings, IconPlayerPlay, IconTrash } from '@tabler/icons-react';
import { apiFetch } from '../lib/api';
import StepSettingsModal from './StepSettingsModal';
import type { StepMeta, ConnectedInput } from '../pages/types';

export default function CustomNode({ id, data }: any) {
  const { setNodes, setEdges, getEdges, getNode } = useReactFlow();
  const [opened, setOpened] = useState(false);
  const [config, setConfig] = useState(data.config_values || {});

  // Fetch full step schema config from the registry (passed down in data)
  const fullConfig = data.fullStepConfig || {};
  const schema = fullConfig.config_schema || {};

  // Normalize port data: API returns port_name, step.json uses name
  const inputs: { port_name: string; data_type: string }[] = (fullConfig.inputs || []).map((p: any) => ({
    port_name: p.port_name || p.name,
    data_type: p.data_type || p.type || 'any',
  }));
  const outputs: { port_name: string; data_type: string }[] = (fullConfig.outputs || []).map((p: any) => ({
    port_name: p.port_name || p.name,
    data_type: p.data_type || p.type || 'any',
  }));

  const handleDelete = () => {
    setNodes((nds) => nds.filter((n) => n.id !== id));
    setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id));
  };

  // Persist a saved config back onto this node, then close the settings modal.
  const handleSaveConfig = (nextConfig: Record<string, any>) => {
    setConfig(nextConfig);
    setNodes((nds) =>
      nds.map((n) => (n.id === id ? { ...n, data: { ...n.data, config_values: nextConfig } } : n))
    );
    setOpened(false);
  };

  // Step metadata handed to the settings page (registry-resolved or generic).
  const step: StepMeta = {
    step_type_key: data.nodeType,
    display_name: fullConfig.display_name || data.nodeType,
    category: fullConfig.category,
    config_schema: schema,
    inputs,
    outputs,
  };

  // Resolve what's wired into each input port (computed when the settings modal
  // is open). For each input, find the incoming edge and expose the upstream
  // node's type + config_values so a panel can read the connected value.
  const connectedInputs: Record<string, ConnectedInput> = {};
  if (opened) {
    const edges = getEdges();
    for (const port of inputs) {
      const edge = edges.find((e) => e.target === id && e.targetHandle === port.port_name);
      if (!edge) continue;
      const src = getNode(edge.source);
      if (!src) continue;
      connectedInputs[port.port_name] = {
        sourceNodeId: edge.source,
        sourceType: (src.data as any)?.nodeType,
        sourcePort: edge.sourceHandle ?? '',
        config: (src.data as any)?.config_values ?? {},
      };
    }
  }

  const handleRun = async () => {
    try {
      const res = await apiFetch("/api/pipeline-runs/execute-node", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          template_version_id: data.template_version_id || 0,
          node_id: id,
          config_values: config
        })
      });
      const result = await res.json();
      alert(result.message || "Executed!");
    } catch (e) {
      alert("Failed to execute node.");
    }
  };

  const maxPorts = Math.max(inputs.length, outputs.length, 1);
  const portRowHeight = 28;

  const isSource = fullConfig.category === 'source';

  // Read-only run-status mode: when data.runStatus is present, the node is being
  // shown on the live run page. Tint its border/glow by status and hide the
  // interactive Run/Settings/Delete controls.
  const runStatus: string | undefined = data.runStatus;
  const STATUS_STYLE: Record<string, { color: string; label: string; glow: string }> = {
    completed: { color: '#10b981', label: 'Completed', glow: '0 0 0 3px rgba(16,185,129,0.25)' },
    running:   { color: '#3b82f6', label: 'Running',   glow: '0 0 0 3px rgba(59,130,246,0.35)' },
    failed:    { color: '#ef4444', label: 'Failed',    glow: '0 0 0 3px rgba(239,68,68,0.30)' },
    cancelled: { color: '#f97316', label: 'Cancelled', glow: 'none' },
    pending:   { color: '#94a3b8', label: 'Pending',   glow: 'none' },
  };
  const statusStyle = runStatus ? (STATUS_STYLE[runStatus] || STATUS_STYLE.pending) : null;

  const headerBg = isSource
    ? 'linear-gradient(135deg, #d1fae5, #a7f3d0)' // Emerald theme for sources
    : 'linear-gradient(135deg, #e0f2fe, #dbeafe)'; // Blue theme for processing

  const headerBorder = isSource ? '#6ee7b7' : '#bfdbfe';
  const textColor = isSource ? 'teal.9' : 'blue.8';
  const nodeBorder = statusStyle
    ? `2px solid ${statusStyle.color}`
    : (isSource ? '1px solid #10b981' : '1px solid #d0d7de');

  return (
    <>
      <div style={{
        background: 'white', borderRadius: '10px', border: nodeBorder,
        minWidth: '280px',
        boxShadow: statusStyle && statusStyle.glow !== 'none'
          ? `0 4px 12px rgba(0,0,0,0.08), ${statusStyle.glow}`
          : '0 4px 12px rgba(0,0,0,0.08)',
        opacity: runStatus === 'pending' ? 0.6 : 1,
        position: 'relative',
      }}>
        {/* Node Header */}
        <Box px="sm" py={8} style={{
          background: headerBg,
          borderBottom: `1px solid ${headerBorder}`,
          borderRadius: '10px 10px 0 0',
        }}>
          <Group justify="space-between" wrap="nowrap">
            <Text fw={700} size="sm" c={textColor}>{fullConfig.display_name || data.nodeType}</Text>
            {statusStyle ? (
              <Badge size="sm" variant="filled" style={{ backgroundColor: statusStyle.color }}>
                {runStatus === 'running' ? '● ' : ''}{statusStyle.label}
              </Badge>
            ) : (
              <Group gap={4} wrap="nowrap">
                <ActionIcon variant="light" color="green" size="sm" onClick={handleRun} title="Run Step">
                  <IconPlayerPlay size={14} />
                </ActionIcon>
                <ActionIcon variant="light" color="blue" size="sm" onClick={() => setOpened(true)} title="Settings">
                  <IconSettings size={14} />
                </ActionIcon>
                <ActionIcon variant="light" color="red" size="sm" onClick={handleDelete} title="Delete">
                  <IconTrash size={14} />
                </ActionIcon>
              </Group>
            )}
          </Group>
        </Box>

        {/* Port Rows */}
        <Box py={6}>
          {Array.from({ length: maxPorts }).map((_, i) => {
            const inp = inputs[i];
            const out = outputs[i];
            return (
              <div key={i} style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                height: `${portRowHeight}px`,
                padding: '0 12px',
                position: 'relative',
              }}>
                {/* Input label (left side) */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', minWidth: 0 }}>
                  {inp && (
                    <>
                      <Text size="xs" fw={500} c="dark.6" style={{ whiteSpace: 'nowrap' }}>
                        {inp.port_name}
                      </Text>
                      <Badge size="xs" variant="light" color="teal" radius="sm" style={{ textTransform: 'lowercase', flexShrink: 0 }}>
                        {inp.data_type}
                      </Badge>
                    </>
                  )}
                </div>

                {/* Output label (right side) */}
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', justifyContent: 'flex-end', minWidth: 0 }}>
                  {out && (
                    <>
                      <Badge size="xs" variant="light" color="violet" radius="sm" style={{ textTransform: 'lowercase', flexShrink: 0 }}>
                        {out.data_type}
                      </Badge>
                      <Text size="xs" fw={500} c="dark.6" style={{ whiteSpace: 'nowrap' }}>
                        {out.port_name}
                      </Text>
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </Box>

        {/* Description footer */}
        {fullConfig.description && (
          <Box px="sm" pb={8} pt={2} style={{ borderTop: '1px solid #f1f5f9' }}>
            <Text size="xs" c="dimmed" lineClamp={2}>{fullConfig.description}</Text>
          </Box>
        )}

        {/* Handles — positioned to align with port rows */}
        {inputs.map((inp, index) => {
          const headerHeight = 40; // header box height
          const paddingTop = 6;   // py={6} on port container
          const handleTop = headerHeight + paddingTop + (index * portRowHeight) + (portRowHeight / 2);
          return (
            <Handle
              key={`in-${inp.port_name}`}
              type="target"
              position={Position.Left}
              id={inp.port_name}
              style={{
                top: `${handleTop}px`,
                width: '10px',
                height: '10px',
                background: '#14b8a6',
                border: '2px solid white',
                boxShadow: '0 0 0 1px #14b8a6',
              }}
            />
          );
        })}
        {outputs.map((out, index) => {
          const headerHeight = 40;
          const paddingTop = 6;
          const handleTop = headerHeight + paddingTop + (index * portRowHeight) + (portRowHeight / 2);
          return (
            <Handle
              key={`out-${out.port_name}`}
              type="source"
              position={Position.Right}
              id={out.port_name}
              style={{
                top: `${handleTop}px`,
                width: '10px',
                height: '10px',
                background: '#8b5cf6',
                border: '2px solid white',
                boxShadow: '0 0 0 1px #8b5cf6',
              }}
            />
          );
        })}
      </div>

      <StepSettingsModal
        opened={opened}
        onClose={() => setOpened(false)}
        nodeId={id}
        step={step}
        initialConfig={config}
        templateVersionId={data.template_version_id}
        connectedInputs={connectedInputs}
        onSave={handleSaveConfig}
      />
    </>
  );
}
