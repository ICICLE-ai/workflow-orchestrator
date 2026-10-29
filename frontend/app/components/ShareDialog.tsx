import { useEffect, useState } from "react";
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Divider,
  Group,
  Loader,
  Modal,
  MultiSelect,
  SegmentedControl,
  Stack,
  Text,
} from "@mantine/core";
import { IconAlertTriangle, IconLock, IconUsers, IconWorld, IconX } from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { Link } from "react-router";
import { apiFetch } from "../lib/api";
import UserPicker from "./UserPicker";

// Owner-only sharing settings for a template lineage (every version, including
// ones saved later). Backed by GET/PUT /api/workflow-templates/{template_id}/sharing.
//
// Three levels, all READ access (open, run, clone — never save over):
//   Private  — only the owner.
//   Shared   — specific users and/or whole groups the owner belongs to.
//   Public   — every signed-in user. The user/group list is kept while public,
//              so switching back to Shared restores it rather than starting over.

type Visibility = "private" | "shared" | "public";

type SharingState = {
  template_id: number;
  name: string;
  is_public: boolean;
  visibility: Visibility;
  users: { user_id: number; username: string; email?: string | null }[];
  groups: { group_id: number; name: string }[];
};

type MyGroup = { group_id: number; name: string; member_count: number };

export default function ShareDialog({
  opened,
  onClose,
  templateId,
  templateName,
  onSaved,
}: {
  opened: boolean;
  onClose: () => void;
  templateId: number;
  templateName?: string;
  onSaved?: (state: SharingState) => void;
}) {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visibility, setVisibility] = useState<Visibility>("private");
  const [usernames, setUsernames] = useState<string[]>([]);
  const [groupIds, setGroupIds] = useState<string[]>([]);
  const [myGroups, setMyGroups] = useState<MyGroup[]>([]);

  useEffect(() => {
    if (!opened) return;
    setLoading(true);
    setError(null);
    Promise.all([
      apiFetch(`/api/workflow-templates/${templateId}/sharing`),
      apiFetch("/api/groups"),
    ])
      .then(async ([sRes, gRes]) => {
        if (!sRes.ok) throw new Error((await sRes.json().catch(() => ({}))).detail || "Could not load sharing settings.");
        const s: SharingState = await sRes.json();
        const g: MyGroup[] = gRes.ok ? await gRes.json() : [];
        setVisibility(s.visibility);
        setUsernames(s.users.map((u) => u.username));
        setGroupIds(s.groups.map((x) => String(x.group_id)));
        // A group shared earlier that the owner has since left still has to
        // show up, or saving would silently drop it.
        const known = new Set(g.map((x) => x.group_id));
        setMyGroups([...g, ...s.groups.filter((x) => !known.has(x.group_id)).map((x) => ({ ...x, member_count: 0 }))]);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [opened, templateId]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Private clears the grantee list; Public keeps it (see header comment).
      const keep = visibility !== "private";
      const res = await apiFetch(`/api/workflow-templates/${templateId}/sharing`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          is_public: visibility === "public",
          usernames: keep ? usernames : [],
          group_ids: keep ? groupIds.map(Number) : [],
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "Could not save sharing settings.");
      notifications.show({
        color: "green",
        title: "Sharing updated",
        message:
          body.visibility === "public"
            ? "Every signed-in user can now open and run this workflow."
            : body.visibility === "shared"
              ? "The people and groups you chose can now open and run this workflow."
              : "This workflow is now private to you.",
      });
      onSaved?.(body);
      onClose();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const nothingChosen = visibility === "shared" && usernames.length === 0 && groupIds.length === 0;

  return (
    <Modal opened={opened} onClose={onClose} title={`Share “${templateName ?? "workflow"}”`} size="lg" centered>
      {loading ? (
        <Group justify="center" py="xl"><Loader /></Group>
      ) : (
        <Stack gap="md">
          <SegmentedControl
            fullWidth
            value={visibility}
            onChange={(v) => setVisibility(v as Visibility)}
            data={[
              { value: "private", label: <Group gap={6} justify="center" wrap="nowrap"><IconLock size={14} />Private</Group> },
              { value: "shared", label: <Group gap={6} justify="center" wrap="nowrap"><IconUsers size={14} />Specific people</Group> },
              { value: "public", label: <Group gap={6} justify="center" wrap="nowrap"><IconWorld size={14} />Public</Group> },
            ]}
          />

          <Text size="sm" c="dimmed">
            {visibility === "private" && "Only you can see this workflow."}
            {visibility === "shared" && "Only the people and groups below can see this workflow."}
            {visibility === "public" && "Every signed-in user can see this workflow."}{" "}
            {visibility !== "private" &&
              "They can open, run and copy it, but only you can save new versions. Every version is covered, including future ones."}
          </Text>

          {visibility !== "private" && (
            <>
              {visibility === "public" && (usernames.length > 0 || groupIds.length > 0) && (
                <Text size="xs" c="dimmed">
                  The people and groups below are kept, so switching back from Public restores them.
                </Text>
              )}
              <Divider label="Groups" labelPosition="left" />
              {myGroups.length === 0 ? (
                <Text size="sm" c="dimmed">
                  You aren&apos;t in any groups yet.{" "}
                  <Anchor component={Link} to="/groups" size="sm">Create a group</Anchor> to share with a whole team at once.
                </Text>
              ) : (
                <MultiSelect
                  placeholder={groupIds.length ? undefined : "Choose groups you belong to"}
                  data={myGroups.map((g) => ({
                    value: String(g.group_id),
                    label: g.member_count ? `${g.name} (${g.member_count} members)` : g.name,
                  }))}
                  value={groupIds}
                  onChange={setGroupIds}
                  searchable
                  clearable
                />
              )}

              <Divider label="People" labelPosition="left" />
              <UserPicker
                exclude={usernames}
                onPick={(name) => setUsernames((prev) => (prev.includes(name) ? prev : [...prev, name]))}
              />
              {usernames.length > 0 && (
                <Group gap={6}>
                  {usernames.map((name) => (
                    <Badge
                      key={name}
                      variant="light"
                      size="lg"
                      rightSection={
                        <ActionIcon
                          size="xs"
                          variant="transparent"
                          aria-label={`Remove ${name}`}
                          onClick={() => setUsernames((prev) => prev.filter((n) => n !== name))}
                        >
                          <IconX size={12} />
                        </ActionIcon>
                      }
                      style={{ textTransform: "none" }}
                    >
                      {name}
                    </Badge>
                  ))}
                </Group>
              )}
            </>
          )}

          {error && (
            <Alert color="red" icon={<IconAlertTriangle size={16} />}>{error}</Alert>
          )}

          <Group justify="flex-end" mt="sm">
            <Button variant="default" onClick={onClose}>Cancel</Button>
            <Button onClick={save} loading={saving} disabled={nothingChosen}>
              Save
            </Button>
          </Group>
          {nothingChosen && (
            <Text size="xs" c="dimmed" ta="right">Add at least one person or group, or choose Private.</Text>
          )}
        </Stack>
      )}
    </Modal>
  );
}
