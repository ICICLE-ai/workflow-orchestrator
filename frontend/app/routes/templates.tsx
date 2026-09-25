import type { Route } from "./+types/templates";
import { AppShell, Container, Title, Text, Button, Group, Card, SimpleGrid, ThemeIcon, ActionIcon, TextInput, Select } from "@mantine/core";
import { IconActivity, IconArrowLeft, IconPlus, IconEdit, IconSearch } from "@tabler/icons-react";
import { useNavigate, Link } from "react-router";
import { useMemo, useState } from "react";
import { apiFetch } from "../lib/api";
import TopNav from "../components/TopNav";
import ThemeToggle from "../components/ThemeToggle";

export async function clientLoader() {
  const res = await apiFetch("/api/workflow-templates");
  if (!res.ok) throw new Error("Failed to load templates");
  return res.json();
}

const SORT_OPTIONS = [
  { value: "name_asc", label: "Name (A–Z)" },
  { value: "name_desc", label: "Name (Z–A)" },
  { value: "created_desc", label: "Newest first" },
  { value: "created_asc", label: "Oldest first" },
];

export default function Templates({ loaderData }: Route.ComponentProps) {
  const navigate = useNavigate();
  const templates = loaderData;

  const [search, setSearch] = useState("");
  const [category, setCategory] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<string>("created_desc");

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const t of templates) if (t.category) set.add(t.category);
    return Array.from(set).sort();
  }, [templates]);

  const visibleTemplates = useMemo(() => {
    const q = search.trim().toLowerCase();
    let result = templates.filter((t: any) => {
      if (category && t.category !== category) return false;
      if (!q) return true;
      return (t.name || "").toLowerCase().includes(q) || (t.description || "").toLowerCase().includes(q);
    });
    result = [...result].sort((a: any, b: any) => {
      switch (sortBy) {
        case "name_asc": return (a.name || "").localeCompare(b.name || "");
        case "name_desc": return (b.name || "").localeCompare(a.name || "");
        case "created_asc": return new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime();
        case "created_desc":
        default: return new Date(b.created_at || 0).getTime() - new Date(a.created_at || 0).getTime();
      }
    });
    return result;
  }, [templates, search, category, sortBy]);

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
          <ThemeToggle />
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
            <>
              <Group mb="lg" gap="sm" wrap="wrap">
                <TextInput
                  placeholder="Search templates..."
                  leftSection={<IconSearch size={16} />}
                  value={search}
                  onChange={(e) => setSearch(e.currentTarget.value)}
                  style={{ flex: 1, minWidth: 200 }}
                />
                <Select
                  placeholder="All categories"
                  data={categories}
                  value={category}
                  onChange={setCategory}
                  clearable
                  w={180}
                />
                <Select
                  data={SORT_OPTIONS}
                  value={sortBy}
                  onChange={(v) => setSortBy(v || "created_desc")}
                  allowDeselect={false}
                  w={170}
                />
              </Group>

              {visibleTemplates.length === 0 ? (
                <Text c="dimmed" ta="center" py="xl">No templates match your filters.</Text>
              ) : (
                <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }} spacing="lg">
                  {visibleTemplates.map((t: any) => (
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
            </>
          )}
        </Container>
      </AppShell.Main>
    </AppShell>
  );
}
