import { useEffect, useState } from "react";
import { Modal, Stack, Group, Button, Loader, Text, ScrollArea, UnstyledButton, Alert } from "@mantine/core";
import { IconFolder, IconPhoto, IconArrowUp } from "@tabler/icons-react";
import { apiFetch } from "../lib/api";
import type { RemoteFile } from "@icicle-ai/opencv-image-playground";

// Reusable modal that browses a Tapis storage directory (via the backend's
// /api/tapis-files proxy) and lets the user pick an image. Any step panel can use
// it. Navigation is JAILED to `rootPath`: only that directory, its files, and its
// descendant directories are reachable — never a parent.
//
// A 401/403 (no Tapis token) is surfaced inline; we do NOT redirect to login
// (the user is app-authenticated, just lacks a Tapis token), so we use a plain
// credentialed fetch rather than apiFetch.

export type { RemoteFile };

const IMAGE_EXT = /\.(jpe?g|png|bmp|tiff?|webp|gif)$/i;
const qs = (system: string, path: string) =>
  `system=${encodeURIComponent(system)}&path=${encodeURIComponent(path)}`;

const norm = (p: string) => "/" + (p || "").replace(/^\/+|\/+$/g, "");
export const parentOf = (p: string) =>
  p.replace(/\/+$/, "").split("/").slice(0, -1).join("/") || "/";

// apiFetch, not a bare fetch: it carries whichever credential this deployment
// uses — the session cookie standalone, the host's X-Tapis-Token when embedded.
const studioFetch = (path: string, init?: RequestInit) => apiFetch(path, init);

export default function TapisDirectoryBrowser({ opened, system, rootPath, onPick, onCancel }: {
  opened: boolean;
  system: string;
  rootPath: string;
  onPick: (f: RemoteFile) => void;
  onCancel: () => void;
}) {
  const root = norm(rootPath || "/");
  const [path, setPath] = useState(root);
  const [entries, setEntries] = useState<RemoteFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset to the (jailed) root each time the browser is opened.
  useEffect(() => {
    if (opened) setPath(root);
  }, [opened, root]);

  useEffect(() => {
    if (!opened) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await studioFetch(`/api/tapis-files/list?${qs(system, path)}`);
        if (!res.ok) {
          const e = await res.json().catch(() => ({}));
          if (!cancelled) {
            setEntries([]);
            setError(
              res.status === 401 || res.status === 403
                ? "Log in with a real Tapis account to browse files."
                : e.detail || `Could not list directory (HTTP ${res.status}).`
            );
          }
        } else {
          const data = await res.json();
          if (!cancelled) setEntries(data.result || []);
        }
      } catch {
        if (!cancelled) setError("Could not reach the backend.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [opened, system, path]);

  const atRoot = norm(path) === root;
  const parent = parentOf(path);
  const dirs = entries.filter((e) => e.type === "dir");
  const images = entries.filter(
    (e) => e.type === "file" && (IMAGE_EXT.test(e.name) || (e.mimeType || "").startsWith("image/"))
  );

  return (
    <Modal opened={opened} onClose={onCancel} title="Browse input directory" size="lg" zIndex={2000}>
      <Stack gap="xs">
        <Group gap="xs" wrap="nowrap">
          <Button
            size="xs"
            variant="light"
            leftSection={<IconArrowUp size={14} />}
            disabled={atRoot}  /* jailed: cannot go above the supplied root */
            onClick={() => setPath(parent)}
          >
            Up
          </Button>
          <Text size="sm" c="dimmed" style={{ wordBreak: "break-all" }}>
            {system || "(no system)"}:{path}
          </Text>
        </Group>

        {error && <Alert color="red" variant="light">{error}</Alert>}

        {loading ? (
          <Group justify="center" p="md"><Loader size="sm" /></Group>
        ) : (
          <ScrollArea.Autosize mah={360}>
            <Stack gap={2}>
              {dirs.map((d) => (
                <UnstyledButton key={d.path} onClick={() => setPath(d.path)} style={{ padding: 6, borderRadius: 6 }}>
                  <Group gap={8}><IconFolder size={16} /><Text size="sm">{d.name}</Text></Group>
                </UnstyledButton>
              ))}
              {images.map((f) => (
                <UnstyledButton key={f.path} onClick={() => onPick(f)} style={{ padding: 6, borderRadius: 6 }}>
                  <Group gap={8}><IconPhoto size={16} color="#7c3aed" /><Text size="sm">{f.name}</Text></Group>
                </UnstyledButton>
              ))}
              {!error && dirs.length === 0 && images.length === 0 && (
                <Text size="sm" c="dimmed">No folders or images here.</Text>
              )}
            </Stack>
          </ScrollArea.Autosize>
        )}
      </Stack>
    </Modal>
  );
}
