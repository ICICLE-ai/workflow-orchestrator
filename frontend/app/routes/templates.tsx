import type { Route } from "./+types/templates";
import { AppShell, Container, Title, Text, Button, Group, Card, SimpleGrid, ThemeIcon, ActionIcon } from "@mantine/core";
import { IconActivity, IconArrowLeft, IconPlus, IconEdit } from "@tabler/icons-react";
import { useNavigate, Link } from "react-router";
import { apiFetch } from "../lib/api";
import TopNav from "../components/TopNav";

export async function clientLoader() {
  const res = await apiFetch("/api/workflow-templates");
  if (!res.ok) throw new Error("Failed to load templates");
  return res.json();
}

export default function Templates({ loaderData }: Route.ComponentProps) {
  const navigate = useNavigate();
  const templates = loaderData;

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          {/* nowrap: the product name is long enough to wrap onto a second
              line on a narrow viewport, which a fixed 60px header would clip. */}
          <Group wrap="nowrap">
            <ActionIcon variant="subtle" color="gray" onClick={() => navigate('/')}>
              <IconArrowLeft size={20} />
            </ActionIcon>
            <ThemeIcon variant="gradient" gradient={{ from: 'indigo', to: 'cyan' }} size="md" radius="md">
              <IconActivity size={16} />
            </ThemeIcon>
            <Text fw={700} style={{ whiteSpace: 'nowrap' }}>No-Code Workflow Studio</Text>
            <TopNav />
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Container size="lg" py="xl">
          <Group justify="space-between" mb="xl">
            <Title order={2}>Workflow Templates</Title>
            <Button leftSection={<IconPlus size={16} />} onClick={() => navigate('/templates/new')}>
              Create New Template
            </Button>
          </Group>

          {templates.length === 0 ? (
            <Text c="dimmed" ta="center" py="xl">No templates found. Create one to get started!</Text>
          ) : (
            <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="lg">
              {templates.map((t: any) => (
                <Card key={t.template_id} shadow="sm" padding="lg" radius="md" withBorder>
                  <Group justify="space-between" mb="xs">
                    <Text fw={500}>{t.name}</Text>
                    <Text size="xs" c="dimmed">v{t.version}</Text>
                  </Group>
                  <Text size="sm" c="dimmed" lineClamp={2} mb="md">
                    {t.description || "No description provided."}
                  </Text>
                  <Button 
                    variant="light" 
                    color="blue" 
                    fullWidth 
                    mt="md" 
                    radius="md" 
                    leftSection={<IconEdit size={16} />}
                    onClick={() => navigate(`/templates/${t.template_version_id}/edit`)}
                  >
                    Edit Template
                  </Button>
                </Card>
              ))}
            </SimpleGrid>
          )}
        </Container>
      </AppShell.Main>
    </AppShell>
  );
}
