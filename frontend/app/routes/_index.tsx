import type { Route } from "./+types/_index";
import { AppShell, Container, Title, Text, Button, Group, Card, SimpleGrid, ThemeIcon } from "@mantine/core";
import { IconLayoutDashboard, IconActivity, IconFolder } from "@tabler/icons-react";
import { useNavigate } from "react-router";
import SecretsMenu from "../components/SecretsMenu";
import TopNav from "../components/TopNav";

export function meta({}: Route.MetaArgs) {
  return [
    { title: "No-Code Workflow Studio" },
    { name: "description", content: "Build dynamic AI pipelines" },
  ];
}

export default function Dashboard() {
  const navigate = useNavigate();

  return (
    <AppShell
      header={{ height: 60 }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          {/* nowrap: the product name is long enough to wrap onto a second
              line on a narrow viewport, which a fixed 60px header would clip. */}
          <Group wrap="nowrap">
            <ThemeIcon variant="gradient" gradient={{ from: 'indigo', to: 'cyan' }} size="lg" radius="md">
              <IconActivity size={20} />
            </ThemeIcon>
            <Text fw={700} size="lg" style={{ whiteSpace: 'nowrap' }}>No-Code Workflow Studio</Text>
            <TopNav />
          </Group>
          <SecretsMenu />
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Container size="lg" py="xl">
          <div style={{ textAlign: 'center', marginBottom: '40px' }}>
            <Title order={1} size="h1" fw={900} variant="gradient" gradient={{ from: 'indigo', to: 'cyan' }}>
              Workflow Orchestrator
            </Title>
            <Text c="dimmed" mt="md" size="lg" maw={600} mx="auto">
              Design, configure, and execute dynamic AI pipelines using our drag-and-drop React Flow canvas.
            </Text>
          </div>

          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="lg">
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <ThemeIcon variant="light" color="blue" size={50} radius="md" mb="md">
                <IconFolder size={30} />
              </ThemeIcon>
              <Text fw={500} size="lg" mt="md">Manage Templates</Text>
              <Text mt="sm" c="dimmed" size="sm">
                Create new pipeline templates or edit existing workflow configurations.
              </Text>
              <Button variant="light" color="blue" fullWidth mt="md" radius="md" onClick={() => navigate('/templates')}>
                View Templates
              </Button>
            </Card>

            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <ThemeIcon variant="light" color="teal" size={50} radius="md" mb="md">
                <IconActivity size={30} />
              </ThemeIcon>
              <Text fw={500} size="lg" mt="md">Past Runs</Text>
              <Text mt="sm" c="dimmed" size="sm">
                View execution history, logs, and outputs for all pipeline runs.
              </Text>
              <Button variant="light" color="teal" fullWidth mt="md" radius="md" onClick={() => navigate('/runs')}>
                View History
              </Button>
            </Card>
          </SimpleGrid>
        </Container>
      </AppShell.Main>
    </AppShell>
  );
}
