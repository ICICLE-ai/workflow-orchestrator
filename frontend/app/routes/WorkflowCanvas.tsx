import React, { useState, useCallback, useRef, useEffect } from 'react';
import { ReactFlow, ReactFlowProvider, addEdge, useNodesState, useEdgesState, Background, Controls } from '@xyflow/react';
import { AppShell, Group, Button, Text, ActionIcon, Stack, Title, Drawer, TextInput, Textarea } from '@mantine/core';
import { IconArrowLeft, IconDeviceFloppy } from '@tabler/icons-react';
import { useNavigate, useParams, useLoaderData } from 'react-router';
import CustomNode from '../components/CustomNode';

const nodeTypes = { customNode: CustomNode };

export async function clientLoader({ params }: { params: any }) {
  // Fetch step types from backend
  const res = await fetch("http://localhost:8002/api/step-types");
  const stepTypes = await res.json();

  let templateData = null;
  if (params.id) {
    const tRes = await fetch(`http://localhost:8002/api/workflow-templates/${params.id}`);
    if (tRes.ok) templateData = await tRes.json();
  }

  return { stepTypes, templateData, id: params.id };
}

function Flow() {
  const { stepTypes, templateData, id } = useLoaderData() as any;
  const navigate = useNavigate();
  const reactFlowWrapper = useRef<HTMLDivElement>(null);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [reactFlowInstance, setReactFlowInstance] = useState<any>(null);
  
  const [drawerOpened, setDrawerOpened] = useState(false);
  const [formData, setFormData] = useState({ name: '', description: '', category: 'Custom' });

  useEffect(() => {
    if (templateData) {
      setFormData({ name: templateData.name, description: templateData.description, category: templateData.category });
      
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

  const onConnect = useCallback((params: any) => setEdges((eds) => addEdge(params, eds)), [setEdges]);

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

  const handleSave = async () => {
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
      ? `http://localhost:8002/api/workflow-templates/${templateData.template_id}/versions` 
      : 'http://localhost:8002/api/workflow-templates';

    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      alert(data.message);
      navigate('/templates');
    } catch (err) {
      alert('Failed to save template');
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
          </Group>
          <Button leftSection={<IconDeviceFloppy size={16} />} onClick={() => setDrawerOpened(true)}>
            {templateData ? 'Save New Version' : 'Save Template'}
          </Button>
        </Group>
      </AppShell.Header>

      <AppShell.Aside p="md" style={{ borderLeft: '1px solid #e2e8f0', background: '#f8fafc' }}>
        <Text fw={600} mb="md">Available Steps</Text>
        <Stack gap="sm">
          {stepTypes.map((step: any) => (
            <div
              key={step.step_type_key}
              onDragStart={(e) => {
                e.dataTransfer.setData('application/reactflow', step.step_type_key);
                e.dataTransfer.effectAllowed = 'move';
              }}
              draggable
              style={{
                padding: '10px', background: 'white', border: '1px solid #e2e8f0', 
                borderRadius: '6px', cursor: 'grab', fontSize: '14px', fontWeight: 500,
                boxShadow: '0 1px 2px rgba(0,0,0,0.05)'
              }}
            >
              {step.display_name}
            </div>
          ))}
        </Stack>
      </AppShell.Aside>

      <AppShell.Main>
        <div style={{ width: '100%', height: 'calc(100vh - 60px)' }} ref={reactFlowWrapper}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
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

      <Drawer opened={drawerOpened} onClose={() => setDrawerOpened(false)} title="Save Workflow Template" position="right">
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
          <Button fullWidth onClick={handleSave} mt="md">Confirm Save</Button>
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
