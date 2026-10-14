import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import { Group, Select, TextInput, Button, Text, Stack, Tooltip, Kbd } from "@mantine/core";
import { IconFolderOpen } from "@tabler/icons-react";
import { NODE_LOCAL_SYSTEM, PATH_SYSTEM_OPTIONS } from "../lib/tapis";
import { getTapisToken } from "../lib/api";

// Keyboard: Enter in the path field opens Browse, and Enter inside the browser
// selects (for a folder field: the folder currently open). The browser is the
// external @icicle-ai/tapis-file-explorer modal, which has no keyboard option
// of its own, so Enter presses its own Select button (data-testid="modalSelect")
// — the package's selection logic stays the single source of truth.

// Elements in the browser that already do something on Enter (a folder row,
// a breadcrumb, Up/Back) keep it when reached by KEYBOARD (:focus-visible), so
// Tab + Enter still navigates. A mouse click doesn't leave that state, and the
// close button the modal auto-focuses on open isn't one of these — so in the
// common case Enter means Select.
const KEEPS_ENTER = ".MuiListItemButton-root, .MuiLink-root, .MuiIconButton-root, input, textarea, select";

function selectOnEnter(e: KeyboardEvent) {
  if (e.key !== "Enter" || e.isComposing || e.repeat || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  const target = e.target as HTMLElement | null;
  if (target?.closest(KEEPS_ENTER) && target.matches(":focus-visible, input, textarea, select")) return;
  // The last match: the browser is the most recently opened modal.
  const buttons = document.querySelectorAll<HTMLButtonElement>('[data-testid="modalSelect"]');
  const select = buttons[buttons.length - 1];
  if (!select || select.disabled) return;
  e.preventDefault();
  e.stopPropagation();
  select.click();
}

const normalize = (p: string) => "/" + p.replace(/^\/+|\/+$/g, "");

// Classic edit distance, for "did you mean" on a mistyped folder name.
function editDistance(a: string, b: string): number {
  const prev = Array.from({ length: b.length + 1 }, (_, j) => j);
  for (let i = 1; i <= a.length; i++) {
    let diag = prev[0];
    prev[0] = i;
    for (let j = 1; j <= b.length; j++) {
      const up = prev[j];
      prev[j] = Math.min(prev[j] + 1, prev[j - 1] + 1, diag + (a[i - 1] === b[j - 1] ? 0 : 1));
      diag = up;
    }
  }
  return prev[b.length];
}

type Listing = { dirs: { name: string }[]; files: { name: string }[] };

// The deepest existing folder on the way to `wanted`, plus the segments
// below it that don't exist and that folder's listing. Walks up one segment
// per failed listing; null when nothing can be listed (no access, offline),
// in which case the browser just opens `wanted` and shows its own error.
async function nearestExisting(
  list: (path: string, system: string, token: string) => Promise<Listing>,
  system: string,
  token: string,
  wanted: string,
): Promise<{ path: string; missing: string[]; listing: Listing } | null> {
  const segs = normalize(wanted).split("/").filter(Boolean);
  for (let n = segs.length; n >= 0; n--) {
    const path = "/" + segs.slice(0, n).join("/");
    try {
      return { path, missing: segs.slice(n), listing: await list(path, system, token) };
    } catch {
      /* missing or unreadable: try the parent */
    }
  }
  return null;
}

// Type-only import — erased at build, so this MUI-based package never loads
// during SSR (same reasoning as smartLabeler.tsx's use of the same package).
// The runtime module is loaded lazily, on first "Browse" click, below.
import type { FileSelectModalWrapperProps, TapisFileEntry } from "@icicle-ai/tapis-file-explorer";

// A "system" dropdown (TAPIS_SYSTEMS) + a path text field with a "Browse"
// button that opens @icicle-ai/tapis-file-explorer's Tapis directory/file
// picker, scoped to whichever system is currently selected — replaces
// hand-typing a bare path and remembering which Tapis system it's on.
//
// Used by GenericConfigForm for any config_schema field with
// `"type": "tapis_path"` (see StepConfigField.selectType for file-vs-dir), and
// directly by any custom step panel that wants the same widget standalone.
export default function TapisPathField({
  label,
  description,
  system,
  path: rawPath,
  selectType = "file",
  showSystemSelect = true,
  mapPickedPath = (p) => p,
  toSystemPath = (p) => p,
  browseDisabledReason: browseDisabledProp,
  onSystemChange,
  onPathChange,
}: {
  label: string;
  description?: string;
  system: string;
  path: string;
  selectType?: "file" | "dir";
  // Off when one system selector outside the field serves several fields.
  showSystemSelect?: boolean;
  // Turns the explorer's system-relative path into what the field stores
  // (e.g. prefixing the system's rootDir to get a real path on the node).
  mapPickedPath?: (path: string) => string;
  // The inverse, for where Browse opens: the field's value as a path the
  // explorer understands, or "/" when it can't be expressed as one.
  toSystemPath?: (path: string) => string;
  // When set, Browse is disabled and this explains why (on hover).
  browseDisabledReason?: string;
  onSystemChange?: (system: string) => void;
  onPathChange: (path: string) => void;
}) {
  const isNodeLocal = system === NODE_LOCAL_SYSTEM;
  // "My node" isn't on Tapis, so there's nothing to browse.
  const browseDisabledReason =
    browseDisabledProp ||
    (isNodeLocal ? "Your node isn't on Tapis, so it can't be browsed from here. Type the path as it is on your node." : undefined);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [Modal, setModal] = useState<ComponentType<FileSelectModalWrapperProps> | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Where the browser actually opens, after checking the typed path exists.
  const [browseFrom, setBrowseFrom] = useState("/");
  // Shown when the typed path doesn't exist: where Browse opened instead, and
  // the closest real name for the first missing folder, if one is close.
  const [notice, setNotice] = useState<{ text: string; suggestion?: string } | null>(null);
  useEffect(() => setNotice(null), [rawPath, system]);

  // Capture phase, so this runs before the focused element's own Enter handling
  // (which selectOnEnter defers to when appropriate).
  useEffect(() => {
    if (!browserOpen) return;
    document.addEventListener("keydown", selectOnEnter, true);
    return () => document.removeEventListener("keydown", selectOnEnter, true);
  }, [browserOpen]);

  // Where Browse should open: the path itself for a folder field; for a file
  // field the file's folder — unless the path ends in "/", which says the
  // user typed a folder, so open that rather than its parent. "/" when empty.
  const startPath = (() => {
    const path = toSystemPath(rawPath);
    if (!path) return "/";
    if (selectType === "dir" || path.endsWith("/")) return normalize(path);
    const parts = path.split("/").filter(Boolean);
    parts.pop();
    return parts.length ? "/" + parts.join("/") : "/";
  })();

  const openBrowser = async () => {
    if (!system) {
      setError("Select a Tapis system first.");
      return;
    }
    setError(null);
    setOpening(true);
    try {
      // Resolved on every open, not cached on this component: the explorer
      // calls Tapis directly, so a token held from a previous open could be one
      // the Tapis session has since replaced. getTapisToken serves from a shared
      // cache while it's demonstrably still valid, so this is normally free.
      const [tfeMod, freshToken] = await Promise.all([
        Modal ? Promise.resolve(null) : import("@icicle-ai/tapis-file-explorer"),
        getTapisToken(),
      ]);
      const mod = tfeMod ?? (await import("@icicle-ai/tapis-file-explorer"));
      if (tfeMod) setModal(() => tfeMod.FileSelectModalWrapper);
      if (!freshToken) {
        throw new Error("No valid Tapis token — log in with a real Tapis account.");
      }

      // A mistyped or not-yet-created folder would open on a bare listing
      // error. Open the nearest folder that exists instead, and say so.
      const found = await nearestExisting(mod.getTapisDirListing, system, freshToken, startPath);
      if (found && found.missing.length) {
        const [first] = found.missing;
        const names = [...found.listing.dirs, ...found.listing.files].map((e) => e.name);
        const close = names
          .map((name) => ({ name, d: editDistance(first.toLowerCase(), name.toLowerCase()) }))
          .filter((c) => c.d <= Math.max(1, Math.floor(first.length / 4)))
          .sort((x, y) => x.d - y.d)[0];
        const base = found.path === "/" ? "" : found.path;
        setNotice({
          text: `"${first}" doesn't exist in ${found.path} on ${system}, so Browse opened there.`,
          suggestion: close ? [base, close.name, ...found.missing.slice(1)].join("/") : undefined,
        });
      } else {
        setNotice(null);
      }
      setBrowseFrom(found ? found.path : startPath);
      setToken(freshToken);
      setBrowserOpen(true);
    } catch (err: any) {
      setError(err?.message || "Could not open the Tapis file browser");
    } finally {
      setOpening(false);
    }
  };


  return (
    <Stack gap={4}>
      <Text size="sm" fw={500}>{label}</Text>
      {description && <Text size="xs" c="dimmed">{description}</Text>}
      <Group gap="xs" wrap="nowrap" align="center">
        {showSystemSelect && (
          <Select
            size="sm"
            placeholder="System"
            data={PATH_SYSTEM_OPTIONS}
            value={system || null}
            onChange={(v) => onSystemChange?.(v ?? "")}
            allowDeselect={false}
            w={200}
          />
        )}
        <TextInput
          size="sm"
          style={{ flex: 1 }}
          placeholder={
            isNodeLocal
              ? "/path/on/your/node"
              : selectType === "dir" ? "/path/to/directory" : "/path/to/file"
          }
          value={rawPath}
          onChange={(e) => onPathChange(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing && !browseDisabledReason) {
              e.preventDefault();
              openBrowser();
            }
          }}
          rightSection={
            browseDisabledReason ? undefined : (
              <Tooltip label="Press Enter to browse" withArrow>
                <Kbd size="xs">↵</Kbd>
              </Tooltip>
            )
          }
        />
        <Tooltip
          label={
            browseDisabledReason ||
            `Browse (Enter in the path field). In the browser, Enter selects ${
              selectType === "dir" ? "the open folder" : "the highlighted file"
            }.`
          }
          withArrow
          multiline
          w={240}
        >
          {/* Wrapped so the tooltip still fires: a disabled button gets no pointer events. */}
          <span style={{ display: "inline-flex" }}>
            <Button
              size="sm"
              variant="default"
              onClick={openBrowser}
              loading={opening}
              disabled={!!browseDisabledReason}
              leftSection={<IconFolderOpen size={14} />}
            >
              Browse
            </Button>
          </span>
        </Tooltip>
      </Group>
      {error && <Text size="xs" c="red">{error}</Text>}
      {isNodeLocal && showSystemSelect && (
        <Text size="xs" c="dimmed">
          A path on your own machine: this workflow then runs only through <b>Deploy to my node</b>, not on the
          platform.
        </Text>
      )}
      {notice && (
        <Group gap={6} wrap="wrap">
          <Text size="xs" c="orange">{notice.text}</Text>
          {notice.suggestion && (
            <Button
              size="compact-xs"
              variant="light"
              color="orange"
              onClick={() => onPathChange(mapPickedPath(notice.suggestion!))}
            >
              Did you mean {notice.suggestion}?
            </Button>
          )}
        </Group>
      )}
      {browserOpen && Modal && token && (
        <Modal
          token={token}
          systemId={system}
          path={browseFrom}
          selectMode={{ mode: "single", types: [selectType] }}
          toggle={() => setBrowserOpen(false)}
          onSelect={(_systemId: string | null, files: TapisFileEntry[]) => {
            if (files[0]) onPathChange(mapPickedPath(files[0].path));
            setBrowserOpen(false);
          }}
        />
      )}
    </Stack>
  );
}
