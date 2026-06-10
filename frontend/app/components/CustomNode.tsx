import React, { useState } from 'react';
import { Handle, Position, useReactFlow } from '@xyflow/react';
import { ActionIcon, Group, Text, Modal, Button, Stack, NumberInput, TextInput, Switch, Box } from '@mantine/core';
import { IconSettings, IconPlayerPlay, IconTrash } from '@tabler/icons-react';

export default function CustomNode({ id, data }: any) {
  const { setNodes, setEdges } = useReactFlow();
  const [opened, setOpened] = useState(false);
  const [config, setConfig] = useState(data.config_values || {});

  // Fetch full step schema config from the registry (passed down in data)
  const fullConfig = data.fullStepConfig || {};
  const schema = fullConfig.config_schema || {};

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

  return (
    <>
      <div style={{
        background: 'white', borderRadius: '8px', border: '1px solid #e2e8f0', 
        minWidth: '250px', boxShadow: '0 4px 6px -1px rgb(0 0 0 / 0.1)'
      }}>
        {/* Node Header */}
        <Box p="xs" bg="blue.0" style={{ borderBottom: '1px solid #e2e8f0', borderRadius: '8px 8px 0 0' }}>
          <Group justify="space-between" wrap="nowrap">
            <Text fw={600} size="sm" c="blue.9">{fullConfig.display_name || data.nodeType}</Text>
            <Group gap="xs" wrap="nowrap">
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

        {/* Node Body */}
        <Box p="sm">
          <Text size="xs" c="dimmed" lineClamp={2}>{fullConfig.description || "No description"}</Text>
        </Box>

        {/* Handles */}
        {fullConfig.inputs?.map((input: any, index: number) => (
          <Handle
            key={`in-${input.name}`}
            type="target"
            position={Position.Left}
            id={input.name}
            style={{ top: `${(index + 1) * 20 + 40}px` }}
          />
        ))}
        {fullConfig.outputs?.map((output: any, index: number) => (
          <Handle
            key={`out-${output.name}`}
            type="source"
            position={Position.Right}
            id={output.name}
            style={{ top: `${(index + 1) * 20 + 40}px` }}
          />
        ))}
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
