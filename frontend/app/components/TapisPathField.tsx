import { useState } from "react";
import type { ComponentType } from "react";
import { Group, Select, TextInput, Button, Text, Stack } from "@mantine/core";
import { IconFolderOpen } from "@tabler/icons-react";
import { TAPIS_SYSTEMS } from "../lib/tapis";
import { getTapisToken } from "../lib/api";

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
  path,
  selectType = "file",
  onSystemChange,
  onPathChange,
}: {
  label: string;
  description?: string;
  system: string;
  path: string;
  selectType?: "file" | "dir";
  onSystemChange: (system: string) => void;
  onPathChange: (path: string) => void;
}) {
  const [browserOpen, setBrowserOpen] = useState(false);
  const [Modal, setModal] = useState<ComponentType<FileSelectModalWrapperProps> | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      if (tfeMod) setModal(() => tfeMod.FileSelectModalWrapper);
      if (!freshToken) {
        throw new Error("No valid Tapis token — log in with a real Tapis account.");
      }
      setToken(freshToken);
      setBrowserOpen(true);
    } catch (err: any) {
      setError(err?.message || "Could not open the Tapis file browser");
    } finally {
      setOpening(false);
    }
  };

  // Start the browser at the current path's own directory (file mode) or the
  // path itself (dir mode); "/" when nothing's set yet.
  const startPath = (() => {
    if (!path) return "/";
    if (selectType === "dir") return path;
    const parts = path.split("/").filter(Boolean);
    parts.pop();
    return parts.length ? "/" + parts.join("/") : "/";
  })();

  return (
    <Stack gap={4}>
      <Text size="sm" fw={500}>{label}</Text>
      {description && <Text size="xs" c="dimmed">{description}</Text>}
      <Group gap="xs" wrap="nowrap" align="center">
        <Select
          size="sm"
          placeholder="System"
          data={TAPIS_SYSTEMS}
          value={system || null}
          onChange={(v) => onSystemChange(v ?? "")}
          allowDeselect={false}
          w={170}
        />
        <TextInput
          size="sm"
          style={{ flex: 1 }}
          placeholder={selectType === "dir" ? "/path/to/directory" : "/path/to/file"}
          value={path}
          onChange={(e) => onPathChange(e.currentTarget.value)}
        />
        <Button
          size="sm"
          variant="default"
          onClick={openBrowser}
          loading={opening}
          leftSection={<IconFolderOpen size={14} />}
        >
          Browse
        </Button>
      </Group>
      {error && <Text size="xs" c="red">{error}</Text>}
      {browserOpen && Modal && token && (
        <Modal
          token={token}
          systemId={system}
          path={startPath}
          selectMode={{ mode: "single", types: [selectType] }}
          toggle={() => setBrowserOpen(false)}
          onSelect={(_systemId: string | null, files: TapisFileEntry[]) => {
            if (files[0]) onPathChange(files[0].path);
            setBrowserOpen(false);
          }}
        />
      )}
    </Stack>
  );
}
