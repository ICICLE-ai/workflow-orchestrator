import React, { useState } from 'react';
import { Handle, Position, useReactFlow } from '@xyflow/react';
import { ActionIcon, Group, Text, Modal, Button, Stack, NumberInput, TextInput, Switch, Box, Badge } from '@mantine/core';
import { IconSettings, IconPlayerPlay, IconTrash } from '@tabler/icons-react';

export default function CustomNode({ id, data }: any) {
  const { setNodes, setEdges } = useReactFlow();
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

  const handleSaveConfig = () => {
    setNodes((nds) =>
      nds.map((n) => {
        if (n.id === id) {
          return { ...n, data: { ...n.data, config_values: config } };
        }
        return n;
      })
    );
    setOpened(false);
  };

  const handleRun = async () => {
    try {
      const res = await fetch("http://localhost:8002/api/pipeline-runs/execute-node", {
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

  // Dynamically render inputs based on config_schema
  const renderInputs = () => {
    return Object.entries(schema).map(([key, field]: [string, any]) => {
      const value = config[key] !== undefined ? config[key] : field.default;

      if (field.type === 'int' || field.type === 'float') {
        return (
          <NumberInput
            key={key}
            label={key}
            description={field.description}
            value={value}
            onChange={(val) => setConfig({ ...config, [key]: val })}
          />
        );
      }
      if (field.type === 'boolean') {
        return (
          <Switch
            key={key}
            label={key}
            description={field.description}
            checked={value}
            onChange={(e) => setConfig({ ...config, [key]: e.currentTarget.checked })}
          />
        );
      }
      return (
        <TextInput
          key={key}
          label={key}
          description={field.description}
          value={value}
          onChange={(e) => setConfig({ ...config, [key]: e.currentTarget.value })}
        />
      );
    });
  };

  const maxPorts = Math.max(inputs.length, outputs.length, 1);
  const portRowHeight = 28;

  const isSource = fullConfig.category === 'source';
  
  const headerBg = isSource 
    ? 'linear-gradient(135deg, #d1fae5, #a7f3d0)' // Emerald theme for sources
    : 'linear-gradient(135deg, #e0f2fe, #dbeafe)'; // Blue theme for processing
    
  const headerBorder = isSource ? '#6ee7b7' : '#bfdbfe';
  const textColor = isSource ? 'teal.9' : 'blue.8';
  const nodeBorder = isSource ? '1px solid #10b981' : '1px solid #d0d7de';

  return (
    <>
      <div style={{
        background: 'white', borderRadius: '10px', border: nodeBorder,
        minWidth: '280px', boxShadow: '0 4px 12px rgba(0,0,0,0.08)',
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

      <Modal opened={opened} onClose={() => setOpened(false)} title={`${fullConfig.display_name} Configuration`}>
        <Stack>
          {Object.keys(schema).length > 0 ? renderInputs() : <Text c="dimmed">No configuration available for this step.</Text>}
          <Button onClick={handleSaveConfig} fullWidth mt="md">Save Configuration</Button>
        </Stack>
      </Modal>
    </>
  );
}
