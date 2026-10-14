import { useEffect, useRef, useState, type SyntheticEvent } from "react";
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Divider,
  Group,
  Loader,
  Stack,
  Switch,
  Text,
  Tooltip,
} from "@mantine/core";
import {
  IconAlertTriangle,
  IconCheck,
  IconDeviceFloppy,
  IconCopy,
  IconRefresh,
  IconTrash,
  IconWebhook,
} from "@tabler/icons-react";
import { notifications } from "@mantine/notifications";
import { apiFetch } from "../lib/api";
import SafeCopyButton from "./SafeCopyButton";

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
  unsaved = false,
  onSaveRequest,
  createIfMissing,
}: {
  // Drives the load: the panel fetches (and if needed creates) the trigger when
  // the drawer holding it opens, not on mount — this lives inside a Drawer whose
  // children stay mounted while it's closed.
  opened: boolean;
  templateVersionId: number | null | undefined;
  // The Run Settings currently on screen. Captured onto the trigger at creation,
  // and re-captured when the user presses "Use current run settings".
  runOptions: Record<string, any>;
  // The canvas has edits that aren't saved. A fire always runs the LATEST SAVED
  // version (see _trigger_target_version), so those edits won't be in it —
  // warn before the user hands the URL to anything.
  unsaved?: boolean;
  // Opens the canvas's save flow. Omitted when the user can't save (a template
  // they don't own), which hides the button.
  onSaveRequest?: () => void;
  // Whether a trigger may be CREATED now. `opened` alone only loads an existing
  // one (and keeps its run settings in sync); RunSettingsDrawer sets this once
  // the webhook section is expanded, so merely opening Run doesn't mint a URL.
  // Defaults to `opened`.
  createIfMissing?: boolean;
}) {
  const mayCreate = createIfMissing ?? opened;
  const [trigger, setTrigger] = useState<WebhookTrigger | null>(null);

  const unsavedMessage =
    "This template has unsaved changes. The webhook runs the last saved version" +
    (trigger?.target_version ? ` (v${trigger.target_version})` : "") +
    ", not what's on the canvas — save the template if your changes should be included.";

  // Copy still happens; the red toast just makes sure the user knows WHAT the
  // copied URL will run before they paste it somewhere.
  const copyWithWarning = (copy: (e?: SyntheticEvent) => void, e: SyntheticEvent) => {
    copy(e);
    if (unsaved) {
      notifications.show({ color: "red", title: "Template not saved", message: unsavedMessage, autoClose: 8000 });
    }
  };
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
        if (!mayCreate) return;
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
    // The auto-sync below is how those edits reach an existing trigger.
  }, [opened, templateVersionId, mayCreate]);

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

  // The webhook fires with the run settings STORED on the trigger. Those used to
  // be a snapshot updated only by a "Use current run settings" button, so a
  // webhook kept launching with whatever was on screen when it was created —
  // e.g. an old charge account, whose derived scratch work_dir/archive dir the
  // user could no longer write to (FILES_CLIENT_SSH_PERM_DENIED at mkdir). Now
  // the settings shown in Run Settings ARE the webhook's: any difference is
  // saved back (debounced, since queue lists settle a moment after opening).
  const [syncing, setSyncing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const optionsKey = normalizeOptions(runOptions);

  const saveRunOptions = async () => {
    if (!trigger) return;
    setSyncing(true);
    setSyncError(null);
    try {
      const updated: WebhookTrigger = await request(`/api/triggers/${trigger.trigger_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_options: runOptions }),
      });
      setTrigger(updated);
    } catch (e: any) {
      // Kept on screen, not only as a toast: a failed sync means the webhook
      // fires with settings that differ from what this drawer shows.
      setSyncError(e?.message || "Unknown error");
    } finally {
      setSyncing(false);
    }
  };

  // Sync straight away when the trigger first loads out of date, then debounce
  // later edits (queue lists settle a moment after the drawer opens). An error
  // stops the auto-retry until the settings change or "Save to webhook now".
  const syncedOnceRef = useRef<number | null>(null);
  useEffect(() => {
    if (!opened || !trigger || !optionsDiffer || syncing) return;
    const first = syncedOnceRef.current !== trigger.trigger_id;
    if (!first && syncError) return;
    syncedOnceRef.current = trigger.trigger_id;
    const timer = setTimeout(saveRunOptions, first ? 0 : 600);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opened, trigger?.trigger_id, optionsDiffer, optionsKey]);
  // A change to the settings is a fresh attempt.
  useEffect(() => setSyncError(null), [optionsKey]);

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

      {trigger && unsaved && (
        <Alert color="red" variant="light" icon={<IconAlertTriangle size={16} />} title="Template not saved">
          <Stack gap={6}>
            <Text size="xs">{unsavedMessage}</Text>
            {onSaveRequest && (
              <Button size="xs" color="red" variant="light" leftSection={<IconDeviceFloppy size={14} />} onClick={onSaveRequest}>
                Save template
              </Button>
            )}
          </Stack>
        </Alert>
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
            <SafeCopyButton value={trigger.webhook_url} timeout={1500}>
              {({ copied, copy }) => (
                <Button
                  size="xs"
                  variant="light"
                  color={copied ? "green" : undefined}
                  leftSection={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
                  onClick={(e) => copyWithWarning(copy, e)}
                >
                  {copied ? "Copied" : "Copy URL"}
                </Button>
              )}
            </SafeCopyButton>
            <SafeCopyButton value={curl} timeout={1500}>
              {({ copied, copy }) => (
                <Button
                  size="xs"
                  variant="subtle"
                  color={copied ? "green" : "gray"}
                  leftSection={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
                  onClick={(e) => copyWithWarning(copy, e)}
                >
                  {copied ? "Copied" : "Copy curl"}
                </Button>
              )}
            </SafeCopyButton>
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

          {syncError ? (
            <Alert color="red" variant="light" p="xs" title="Webhook settings not updated">
              <Stack gap={6}>
                <Text size="xs">
                  This webhook still fires with its OLD run settings (charged to{" "}
                  {trigger.run_options?.slurm_account || "—"}, work dir {trigger.run_options?.work_dir || "—"}).
                </Text>
                <Text size="xs" ff="monospace" style={{ wordBreak: "break-word" }}>{syncError}</Text>
                <Button size="xs" color="red" variant="light" loading={syncing} onClick={saveRunOptions}>
                  Save to webhook now
                </Button>
              </Stack>
            </Alert>
          ) : syncing || optionsDiffer ? (
            <Group gap="xs" wrap="nowrap">
              <Loader size={12} />
              <Text size="xs" c="yellow.7" style={{ flex: 1 }}>Saving the run settings above to this webhook…</Text>
              {!syncing && (
                <Button size="compact-xs" variant="subtle" onClick={saveRunOptions}>Save now</Button>
              )}
            </Group>
          ) : (
            <Text size="xs" c="dimmed">
              Fires with the run settings above — charged to {trigger.run_options?.slurm_account || "—"},
              work dir {trigger.run_options?.work_dir || "—"}, outputs on {trigger.run_options?.archive_system || "—"}.
              Kept in sync automatically.
            </Text>
          )}
        </Stack>
      )}
    </>
  );
}
