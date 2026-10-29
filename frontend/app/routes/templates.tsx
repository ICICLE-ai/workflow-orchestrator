import type { Route } from "./+types/templates";
import { AppShell, Container, Title, Text, Button, Group, Card, SimpleGrid, ThemeIcon, ActionIcon, TextInput, Select, Badge, Tooltip, SegmentedControl } from "@mantine/core";
import { IconActivity, IconArrowLeft, IconPlus, IconEdit, IconSearch, IconShare, IconWorld, IconUsers, IconLock } from "@tabler/icons-react";
import { useNavigate, useRevalidator } from "react-router";
import { useMemo, useState } from "react";
import { apiFetch } from "../lib/api";
import TopNav from "../components/TopNav";
import ThemeToggle from "../components/ThemeToggle";
import ShareDialog from "../components/ShareDialog";

export async function clientLoader() {
  const res = await apiFetch("/api/workflow-templates");
  if (!res.ok) throw new Error("Failed to load templates");
  return res.json();
}

// Which templates to show, by why the caller can see them (the API's `access`).
const ACCESS_FILTERS = [
  { value: "all", label: "All" },
  { value: "mine", label: "Mine" },
  { value: "shared", label: "Shared with me" },
  { value: "public", label: "Public" },
];

// The card's visibility badge. For the owner it says who can see it; for
// everyone else it says why THEY can.
function AccessBadge({ t }: { t: any }) {
  if (t.is_owner) {
    if (t.is_public) return <Badge size="xs" color="teal" variant="light" leftSection={<IconWorld size={10} />}>Public</Badge>;
    if (t.has_shares) return <Badge size="xs" color="indigo" variant="light" leftSection={<IconUsers size={10} />}>Shared</Badge>;
    return <Badge size="xs" color="gray" variant="light" leftSection={<IconLock size={10} />}>Private</Badge>;
  }
  const by = t.owner_username ? ` by ${t.owner_username}` : "";
  if (t.access === "shared") {
    return (
      <Tooltip label={`Shared with you${by}`}>
        <Badge size="xs" color="indigo" variant="light" leftSection={<IconUsers size={10} />}>Shared with you</Badge>
      </Tooltip>
    );
  }
  return (
    <Tooltip label={`Published${by}`}>
      <Badge size="xs" color="teal" variant="light" leftSection={<IconWorld size={10} />}>Public</Badge>
    </Tooltip>
  );
}

const SORT_OPTIONS = [
  { value: "name_asc", label: "Name (A–Z)" },
  { value: "name_desc", label: "Name (Z–A)" },
  { value: "created_desc", label: "Newest first" },
  { value: "created_asc", label: "Oldest first" },
];

export default function Templates({ loaderData }: Route.ComponentProps) {
  const navigate = useNavigate();
  const revalidator = useRevalidator();
  const templates = loaderData;
  const [sharing, setSharing] = useState<any | null>(null);
  const [access, setAccess] = useState("all");

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
      if (access === "mine" && !t.is_owner) return false;
      if (access === "shared" && t.access !== "shared") return false;
      if (access === "public" && !t.is_public) return false;
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
  }, [templates, search, category, sortBy, access]);

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
                <SegmentedControl data={ACCESS_FILTERS} value={access} onChange={setAccess} />
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
                  <Group justify="space-between" mb="xs" wrap="nowrap">
                    <Text fw={500} truncate>{t.name}</Text>
                    <Group gap={6} wrap="nowrap">
                      <AccessBadge t={t} />
                      <Text size="xs" c="dimmed">v{t.version}</Text>
                    </Group>
                  </Group>
                  {!t.is_owner && t.owner_username && (
                    <Text size="xs" c="dimmed" mb={4}>by {t.owner_username}</Text>
                  )}
                  <Text size="sm" c="dimmed" lineClamp={2} mb="md">
                    {t.description || "No description provided."}
                  </Text>
                  <Group mt="md" gap="xs" wrap="nowrap">
                    <Button
                      variant="light"
                      color="blue"
                      radius="md"
                      style={{ flex: 1 }}
                      leftSection={<IconEdit size={16} />}
                      onClick={() => navigate(`/templates/${t.template_version_id}/edit`)}
                    >
                      {t.is_owner ? "Edit Template" : "Open Template"}
                    </Button>
                    {t.is_owner && (
                      <Tooltip label="Share">
                        <ActionIcon variant="light" color="indigo" size="lg" radius="md" aria-label="Share" onClick={() => setSharing(t)}>
                          <IconShare size={16} />
                        </ActionIcon>
                      </Tooltip>
                    )}
                  </Group>
                </Card>
                  ))}
                </SimpleGrid>
              )}
            </>
          )}
        </Container>
      </AppShell.Main>

      {sharing && (
        <ShareDialog
          opened
          onClose={() => setSharing(null)}
          templateId={sharing.template_id}
          templateName={sharing.name}
          onSaved={() => revalidator.revalidate()}
        />
      )}
    </AppShell>
  );
}
