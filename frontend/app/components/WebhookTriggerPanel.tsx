import { useEffect, useState } from "react";
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  CopyButton,
  Divider,
  Group,
  Loader,
  Stack,
  Switch,
  Text,
  Tooltip,
} from "@mantine/core";
import {
  IconCheck,
  IconCopy,
  IconRefresh,
  IconTrash,
  IconWebhook,
} from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { apiFetch } from "../lib/api";

export interface WebhookTrigger {
  trigger_id: number;
  trigger_type: string;
  template_id: number;
  template_version_id: number;
  target_version_id: number | null;
  target_version: number | null;
  is_enabled: boolean;
  run_options: Record<string, any>;
  trigger_count: number;
  last_triggered_at: string | null;
  last_run_id: number | null;
  webhook_url: string;
  method: string;
}

// The run settings stored on a trigger come back as the backend's RunOptions
// dump, which drops nulls but keeps empty strings — so normalize both sides the
// same way before comparing, or the panel claims the settings are out of sync
// every time it loads.
function normalizeOptions(options: Record<string, any> | undefined): string {
  const entries = Object.entries(options || {})
    .filter(([, v]) => v !== null && v !== undefined)
    .sort(([a], [b]) => a.localeCompare(b));
  return JSON.stringify(entries);
}

function formatFired(iso: string | null): string {
  if (!iso) return "never fired";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "never fired";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "fired just now";
  if (mins < 60) return `fired ${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `fired ${hours}h ago`;
  return `fired ${Math.round(hours / 24)}d ago`;
}

/**
 * The webhook trigger for one workflow, shown inside Run Settings.
 *
 * A trigger is how something OUTSIDE the Studio starts a run: this panel hands
 * the user a secret URL that launches the workflow when POSTed to, with the run
 * settings they have open captured onto it. The run belongs to them — their
 * Tapis token, their allocation — however the URL is called, so the URL is the
 * only credential involved and is treated as a secret (Regenerate revokes every
 * copy of it).
 *
 * The trigger is created on first open rather than behind a button: the point of
 * the feature is that opening Run Settings *gives* you a webhook. Creation is
 * idempotent per (workflow, user) on the backend, so re-opening the drawer shows
 * the same URL instead of minting a second one and orphaning whatever the user
 * already pasted into their cron job.
 */
export default function WebhookTriggerPanel({
  opened,
  templateVersionId,
  runOptions,
}: {
  // Drives the load: the panel fetches (and if needed creates) the trigger when
  // the drawer holding it opens, not on mount — this lives inside a Drawer whose
  // children stay mounted while it's closed.
  opened: boolean;
  templateVersionId: number | null | undefined;
  // The Run Settings currently on screen. Captured onto the trigger at creation,
  // and re-captured when the user presses "Use current run settings".
  runOptions: Record<string, any>;
}) {
  const [trigger, setTrigger] = useState<WebhookTrigger | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const request = async (path: string, init?: RequestInit) => {
    const res = await apiFetch(path, init);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    return res.json();
  };

  useEffect(() => {
    if (!opened || !templateVersionId) return;
    let cancelled = false;

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        // List first, create only if there is nothing to show. The create
        // endpoint is idempotent, so this ordering is about not sending a POST
        // on every open rather than about avoiding duplicates.
        const existing: WebhookTrigger[] = await request(
          `/api/workflow-templates/${templateVersionId}/triggers`
        );
        if (cancelled) return;
        const webhook = existing.find((t) => t.trigger_type === "webhook");
        if (webhook) {
          setTrigger(webhook);
          return;
        }
        const created: WebhookTrigger = await request(
          `/api/workflow-templates/${templateVersionId}/triggers`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ trigger_type: "webhook", run_options: runOptions }),
          }
        );
        if (!cancelled) setTrigger(created);
      } catch (e: any) {
        if (!cancelled) setError(e?.message || "Could not load the webhook trigger");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    load();
    return () => {
      cancelled = true;
    };
    // runOptions is deliberately not a dependency: it changes as the user edits
    // the fields above, and re-running this on every keystroke would re-POST.
    // "Use current run settings" is how those edits reach an existing trigger.
  }, [opened, templateVersionId]);

  const act = async (label: string, fn: () => Promise<WebhookTrigger | null>) => {
    setBusy(true);
    try {
      setTrigger(await fn());
    } catch (e: any) {
      notifications.show({
        color: "red",
        title: `Could not ${label}`,
        message: e?.message || "Unknown error",
      });
    } finally {
      setBusy(false);
    }
  };

  const setEnabled = (enabled: boolean) =>
    act(enabled ? "enable the webhook" : "disable the webhook", () =>
      request(`/api/triggers/${trigger!.trigger_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_enabled: enabled }),
      })
    );

  const syncRunOptions = () =>
    act("update the webhook's run settings", async () => {
      const updated = await request(`/api/triggers/${trigger!.trigger_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_options: runOptions }),
      });
      notifications.show({ color: "green", message: "Webhook now uses these run settings." });
      return updated;
    });

  const rotate = () =>
    act("regenerate the webhook", async () => {
      const updated = await request(`/api/triggers/${trigger!.trigger_id}/rotate`, {
        method: "POST",
      });
      notifications.show({
        color: "yellow",
        message: "New URL generated — the previous one no longer works.",
      });
      return updated;
    });

  const remove = () =>
    act("delete the webhook", async () => {
      // POST, not DELETE — same reason as SecretsMenu: DELETE always triggers a
      // CORS preflight, which some tunnels between frontend and backend mangle.
      await request(`/api/triggers/${trigger!.trigger_id}/delete`, { method: "POST" });
      notifications.show({ message: "Webhook deleted." });
      return null;
    });

  const recreate = () =>
    act("create the webhook", () =>
      request(`/api/workflow-templates/${templateVersionId}/triggers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trigger_type: "webhook", run_options: runOptions }),
      })
    );

  const optionsDiffer =
    !!trigger && normalizeOptions(trigger.run_options) !== normalizeOptions(runOptions);
  const curl = trigger ? `curl -X POST "${trigger.webhook_url}"` : "";

  return (
    <>
      <Divider label="Trigger" labelPosition="left" />

      <Group gap="xs" align="center">
        <IconWebhook size={16} />
        <Text size="sm" fw={600} style={{ flex: 1 }}>
          Webhook
        </Text>
        {trigger && (
          <Switch
            size="xs"
            checked={trigger.is_enabled}
            disabled={busy}
            onChange={(e) => setEnabled(e.currentTarget.checked)}
            label={trigger.is_enabled ? "Enabled" : "Disabled"}
          />
        )}
      </Group>

      <Text size="xs" c="dimmed">
        POST to this URL to start a run without opening the Studio — from a sensor, a cron job, or
        another pipeline. The run is yours: your Tapis token, your allocation, the run settings
        above. Anyone holding the URL can start it, so treat it like a password.
      </Text>

      {loading && (
        <Group gap="xs">
          <Loader size={14} />
          <Text size="xs" c="dimmed">
            Generating webhook…
          </Text>
        </Group>
      )}

      {error && (
        <Alert color="red" variant="light" title="Webhook unavailable">
          <Text size="xs">{error}</Text>
        </Alert>
      )}

      {!loading && !error && !trigger && (
        <Button size="xs" variant="light" loading={busy} onClick={recreate}>
          Generate a webhook URL
        </Button>
      )}

      {trigger && (
        <Stack gap={6}>
          <Code
            block
            style={{ fontSize: 11, wordBreak: "break-all", whiteSpace: "pre-wrap" }}
          >
            {`POST ${trigger.webhook_url}`}
          </Code>

          <Group gap="xs">
            <CopyButton value={trigger.webhook_url} timeout={1500}>
              {({ copied, copy }) => (
                <Button
                  size="xs"
                  variant="light"
                  color={copied ? "green" : undefined}
                  leftSection={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
                  onClick={copy}
                >
                  {copied ? "Copied" : "Copy URL"}
                </Button>
              )}
            </CopyButton>
            <CopyButton value={curl} timeout={1500}>
              {({ copied, copy }) => (
                <Button
                  size="xs"
                  variant="subtle"
                  color={copied ? "green" : "gray"}
                  leftSection={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
                  onClick={copy}
                >
                  {copied ? "Copied" : "Copy curl"}
                </Button>
              )}
            </CopyButton>
            <Tooltip label="Generate a new URL and invalidate this one" withArrow>
              <ActionIcon variant="subtle" color="gray" disabled={busy} onClick={rotate}>
                <IconRefresh size={16} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="Delete this webhook" withArrow>
              <ActionIcon variant="subtle" color="red" disabled={busy} onClick={remove}>
                <IconTrash size={16} />
              </ActionIcon>
            </Tooltip>
          </Group>

          <Group gap={6}>
            <Badge size="xs" variant="light" color="gray">
              {trigger.trigger_count} {trigger.trigger_count === 1 ? "run" : "runs"} triggered
            </Badge>
            <Badge size="xs" variant="light" color="gray">
              {formatFired(trigger.last_triggered_at)}
            </Badge>
            {trigger.last_run_id && (
              <Anchor size="xs" href={`/runs/${trigger.last_run_id}`}>
                last run #{trigger.last_run_id}
              </Anchor>
            )}
          </Group>

          {/* A webhook follows the workflow, not the version it was made on, so
              say which version a call would actually execute — the answer
              changes the moment a new version is saved. */}
          {trigger.target_version != null && (
            <Text size="xs" c="dimmed">
              Runs the latest saved version (v{trigger.target_version}).
            </Text>
          )}

          {optionsDiffer && (
            <Alert color="yellow" variant="light" p="xs">
              <Stack gap={6}>
                <Text size="xs">
                  This webhook launches with the run settings captured when it was last updated,
                  which differ from the ones above.
                </Text>
                <Button size="xs" variant="light" color="yellow" loading={busy} onClick={syncRunOptions}>
                  Use current run settings
                </Button>
              </Stack>
            </Alert>
          )}
        </Stack>
      )}
    </>
  );
}
