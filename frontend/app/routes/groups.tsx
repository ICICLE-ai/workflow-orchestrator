import type { Route } from "./+types/groups";
import {
  ActionIcon,
  Alert,
  AppShell,
  Badge,
  Button,
  Card,
  Container,
  Group,
  Menu,
  Modal,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Textarea,
  ThemeIcon,
  Title,
} from "@mantine/core";
import {
  IconActivity,
  IconArrowLeft,
  IconDots,
  IconLogout,
  IconPlus,
  IconShieldCheck,
  IconTrash,
  IconUsersGroup,
} from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useRevalidator } from "react-router";
import { apiFetch, fetchCurrentUser } from "../lib/api";
import TopNav from "../components/TopNav";
import ThemeToggle from "../components/ThemeToggle";
import UserPicker from "../components/UserPicker";

// User groups: named sets of people a workflow can be shared with in one step
// (see ShareDialog and backend/groups.py). Lists the groups the caller is in;
// selecting one shows its members, which admins can manage.

type GroupSummary = {
  group_id: number;
  name: string;
  description: string;
  my_role: "admin" | "member";
  member_count: number;
};

type Member = { user_id: number; username: string; email?: string | null; role: "admin" | "member" };
type GroupDetail = GroupSummary & { members: Member[] };

export async function clientLoader() {
  const [res, me] = await Promise.all([apiFetch("/api/groups"), fetchCurrentUser()]);
  if (!res.ok) throw new Error("Failed to load groups");
  return { groups: (await res.json()) as GroupSummary[], me };
}

async function call(path: string, init?: RequestInit) {
  const res = await apiFetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (HTTP ${res.status}).`);
  return body;
}

function fail(e: any) {
  notifications.show({ color: "red", title: "Something went wrong", message: e?.message || "Unknown error" });
}

export default function Groups({ loaderData }: Route.ComponentProps) {
  const navigate = useNavigate();
  const revalidator = useRevalidator();
  const { groups, me } = loaderData;

  const [selectedId, setSelectedId] = useState<number | null>(groups[0]?.group_id ?? null);
  const [detail, setDetail] = useState<GroupDetail | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [form, setForm] = useState({ name: "", description: "" });
  const [busy, setBusy] = useState(false);

  const loadDetail = useCallback(async (id: number | null) => {
    if (id == null) return setDetail(null);
    try {
      setDetail(await call(`/api/groups/${id}`));
    } catch (e) {
      fail(e);
      setDetail(null);
    }
  }, []);

  useEffect(() => {
    // First group to join (or the first load after having none). Creating,
    // leaving and deleting pick their own next selection, because the list
    // they'd be checked against hasn't revalidated yet at that point.
    // Keyed on `groups` alone so it only runs once that revalidation lands.
    if (selectedId == null && groups.length) setSelectedId(groups[0].group_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [groups]);

  const selectOtherThan = (groupId: number) =>
    setSelectedId(groups.find((g) => g.group_id !== groupId)?.group_id ?? null);

  useEffect(() => {
    loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  // Apply a group response from a mutation, and refresh the list's counts.
  const applied = (g: GroupDetail) => {
    setDetail(g);
    revalidator.revalidate();
  };

  const isAdmin = detail?.my_role === "admin";

  const createGroup = async () => {
    setBusy(true);
    try {
      const g = await call("/api/groups", { method: "POST", body: JSON.stringify(form) });
      setCreateOpen(false);
      setForm({ name: "", description: "" });
      setSelectedId(g.group_id);
      applied(g);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const updateGroup = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      applied(await call(`/api/groups/${detail.group_id}`, { method: "PATCH", body: JSON.stringify(form) }));
      setEditOpen(false);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const deleteGroup = async () => {
    if (!detail) return;
    if (!window.confirm(`Delete “${detail.name}”? Workflows shared with it will no longer be visible to its members.`)) return;
    try {
      await call(`/api/groups/${detail.group_id}/delete`, { method: "POST" });
      notifications.show({ color: "green", title: "Group deleted", message: detail.name });
      selectOtherThan(detail.group_id);
      revalidator.revalidate();
    } catch (e) {
      fail(e);
    }
  };

  const addMember = async (username: string) => {
    if (!detail) return;
    try {
      applied(await call(`/api/groups/${detail.group_id}/members`, { method: "POST", body: JSON.stringify({ username }) }));
    } catch (e) {
      fail(e);
    }
  };

  const setRole = async (m: Member, role: Member["role"]) => {
    if (!detail) return;
    try {
      applied(await call(`/api/groups/${detail.group_id}/members/${m.user_id}`, { method: "PATCH", body: JSON.stringify({ role }) }));
    } catch (e) {
      fail(e);
    }
  };

  const removeMember = async (m: Member) => {
    if (!detail) return;
    const leaving = m.user_id === me?.user_id;
    const prompt = leaving
      ? `Leave “${detail.name}”? You'll lose access to workflows shared only with this group.`
      : `Remove ${m.username} from “${detail.name}”?`;
    if (!window.confirm(prompt)) return;
    try {
      const body = await call(`/api/groups/${detail.group_id}/members/${m.user_id}/delete`, { method: "POST" });
      if (body.left) {
        selectOtherThan(detail.group_id);
        revalidator.revalidate();
      } else {
        applied(body);
      }
    } catch (e) {
      fail(e);
    }
  };

  return (
    <AppShell header={{ height: 60 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group wrap="nowrap">
            <ActionIcon variant="subtle" color="gray" onClick={() => navigate("/")}>
              <IconArrowLeft size={20} />
            </ActionIcon>
            <ThemeIcon variant="gradient" gradient={{ from: "indigo", to: "cyan" }} size="md" radius="md">
              <IconActivity size={16} />
            </ThemeIcon>
            <Text fw={700} style={{ whiteSpace: "nowrap" }}>No-Code Workflow Studio</Text>
            <TopNav />
          </Group>
          <ThemeToggle />
        </Group>
      </AppShell.Header>

      <AppShell.Main>
        <Container size="lg" py="xl">
          <Group justify="space-between" mb="xs">
            <Title order={2}>Groups</Title>
            <Button leftSection={<IconPlus size={16} />} onClick={() => { setForm({ name: "", description: "" }); setCreateOpen(true); }}>
              New Group
            </Button>
          </Group>
          <Text c="dimmed" size="sm" mb="xl">
            Share a workflow with a group to give all of its members access at once. Members can open, run
            and copy what&apos;s shared with the group; only a workflow&apos;s owner can save new versions.
          </Text>

          {groups.length === 0 ? (
            <Stack align="center" py="xl" gap="xs">
              <IconUsersGroup size={40} stroke={1.4} color="var(--mantine-color-dimmed)" />
              <Text c="dimmed">You aren&apos;t in any groups yet. Create one to start sharing with your team.</Text>
            </Stack>
          ) : (
            <SimpleGrid cols={{ base: 1, md: 3 }} spacing="lg">
              <Stack gap="xs">
                {groups.map((g) => (
                  <Card
                    key={g.group_id}
                    withBorder
                    padding="sm"
                    radius="md"
                    onClick={() => setSelectedId(g.group_id)}
                    style={{
                      cursor: "pointer",
                      borderColor: g.group_id === selectedId ? "var(--mantine-color-blue-filled)" : undefined,
                    }}
                  >
                    <Group justify="space-between" wrap="nowrap">
                      <Text fw={500} truncate>{g.name}</Text>
                      {g.my_role === "admin" && <Badge size="xs" variant="light">Admin</Badge>}
                    </Group>
                    <Text size="xs" c="dimmed">{g.member_count} member{g.member_count === 1 ? "" : "s"}</Text>
                  </Card>
                ))}
              </Stack>

              <Card withBorder radius="md" padding="lg" style={{ gridColumn: "span 2" }}>
                {!detail ? (
                  <Text c="dimmed">Select a group.</Text>
                ) : (
                  <Stack gap="md">
                    <Group justify="space-between" align="flex-start" wrap="nowrap">
                      <div style={{ minWidth: 0 }}>
                        <Title order={3}>{detail.name}</Title>
                        {detail.description && <Text size="sm" c="dimmed">{detail.description}</Text>}
                      </div>
                      {isAdmin && (
                        <Group gap="xs" wrap="nowrap">
                          <Button size="xs" variant="default" onClick={() => { setForm({ name: detail.name, description: detail.description }); setEditOpen(true); }}>
                            Edit
                          </Button>
                          <Button size="xs" color="red" variant="light" leftSection={<IconTrash size={14} />} onClick={deleteGroup}>
                            Delete
                          </Button>
                        </Group>
                      )}
                    </Group>

                    {isAdmin ? (
                      <UserPicker label="Add member" exclude={detail.members.map((m) => m.username)} onPick={addMember} />
                    ) : (
                      <Alert variant="light" color="gray">Only group admins can add or remove members.</Alert>
                    )}

                    <Table verticalSpacing="xs" highlightOnHover>
                      <Table.Thead>
                        <Table.Tr>
                          <Table.Th>Member</Table.Th>
                          <Table.Th>Role</Table.Th>
                          <Table.Th w={40} />
                        </Table.Tr>
                      </Table.Thead>
                      <Table.Tbody>
                        {detail.members.map((m) => {
                          const isMe = m.user_id === me?.user_id;
                          return (
                            <Table.Tr key={m.user_id}>
                              <Table.Td>
                                <Text size="sm" fw={500}>{m.username}{isMe && <Text span c="dimmed" size="sm"> (you)</Text>}</Text>
                                {m.email && <Text size="xs" c="dimmed">{m.email}</Text>}
                              </Table.Td>
                              <Table.Td>
                                <Badge variant={m.role === "admin" ? "filled" : "light"} color={m.role === "admin" ? "indigo" : "gray"}>
                                  {m.role}
                                </Badge>
                              </Table.Td>
                              <Table.Td>
                                {(isAdmin || isMe) && (
                                  <Menu position="bottom-end" withinPortal>
                                    <Menu.Target>
                                      <ActionIcon variant="subtle" color="gray" aria-label={`Actions for ${m.username}`}>
                                        <IconDots size={16} />
                                      </ActionIcon>
                                    </Menu.Target>
                                    <Menu.Dropdown>
                                      {isAdmin && m.role === "member" && (
                                        <Menu.Item leftSection={<IconShieldCheck size={14} />} onClick={() => setRole(m, "admin")}>
                                          Make admin
                                        </Menu.Item>
                                      )}
                                      {isAdmin && m.role === "admin" && (
                                        <Menu.Item leftSection={<IconShieldCheck size={14} />} onClick={() => setRole(m, "member")}>
                                          Remove admin role
                                        </Menu.Item>
                                      )}
                                      <Menu.Item
                                        color="red"
                                        leftSection={isMe ? <IconLogout size={14} /> : <IconTrash size={14} />}
                                        onClick={() => removeMember(m)}
                                      >
                                        {isMe ? "Leave group" : "Remove from group"}
                                      </Menu.Item>
                                    </Menu.Dropdown>
                                  </Menu>
                                )}
                              </Table.Td>
                            </Table.Tr>
                          );
                        })}
                      </Table.Tbody>
                    </Table>
                  </Stack>
                )}
              </Card>
            </SimpleGrid>
          )}
        </Container>
      </AppShell.Main>

      <Modal
        opened={createOpen || editOpen}
        onClose={() => { setCreateOpen(false); setEditOpen(false); }}
        title={createOpen ? "New group" : "Edit group"}
        centered
      >
        <Stack>
          <TextInput
            label="Name"
            required
            data-autofocus
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.currentTarget.value })}
          />
          <Textarea
            label="Description"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.currentTarget.value })}
          />
          <Button onClick={createOpen ? createGroup : updateGroup} loading={busy} disabled={!form.name.trim()}>
            {createOpen ? "Create group" : "Save"}
          </Button>
        </Stack>
      </Modal>
    </AppShell>
  );
}
