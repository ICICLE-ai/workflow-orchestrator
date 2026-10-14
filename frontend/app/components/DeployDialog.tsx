import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Code,
  Collapse,
  Group,
  Loader,
  Modal,
  Select,
  Spoiler,
  Stack,
  Switch,
  Text,
  TextInput,
  Tooltip,
  Transition,
} from "@mantine/core";
import {
  IconAlertTriangle,
  IconArrowRight,
  IconCheck,
  IconCircleCheck,
  IconDownload,
  IconInfoCircle,
  IconServerBolt,
} from "@tabler/icons-react";
import { apiFetch } from "../lib/api";
import { DEFAULT_TAPIS_SYSTEM, TAPIS_SYSTEMS } from "../lib/tapis";
import TapisPathField from "./TapisPathField";
import DeploymentOptions, { type DeploymentInfo } from "./DeploymentOptions";
import classes from "./DeployDialog.module.css";

// "Deploy to my node": export a saved template for hardware the user owns
// (backend/engine/local_bundle.py + engine/deployment.py). Unlike Launch Run,
// nothing executes here — this produces files and commands the user carries
// to their node.

// Select value for "a machine that isn't a Tapis system".
const OTHER = "__other__";

// Settings are saved per workflow on the server (GET/PUT .../deploy-settings),
// so they follow the user across browsers and later versions. This browser
// key only remembers the LAST settings used anywhere, as the starting point
// for a workflow that has none saved yet.
const PREFS_KEY = "wf-deploy-prefs";

interface Prefs {
  node: string;
  nodeName: string;
  // Off (the default): every input is read at exactly the path the platform
  // used, i.e. data root "/". On: inputs are re-rooted under `data_root`, for
  // data copied elsewhere in the same layout.
  remap: boolean;
  data_root: string;
  workdir: string;
}

const DEFAULT_PREFS: Prefs = {
  node: DEFAULT_TAPIS_SYSTEM,
  nodeName: "",
  remap: false,
  data_root: "/data",
  workdir: "/scratch/wf-local",
};

function loadPrefs(): Prefs {
  try {
    const { images_dir: _unused, ...saved } = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    return { ...DEFAULT_PREFS, ...saved };
  } catch {
    return DEFAULT_PREFS;
  }
}

// Where the runner will read a source, for a given data root. Mirrors
// runner.resolve_already_rooted: a platform path already under the data root
// is used as-is; anything else is re-rooted beneath it.
function resolveSource(dataRoot: string, platformPath: string): { path: string; asIs: boolean } {
  const root = dataRoot.replace(/\/+$/, "") || "/";
  if (root === "/") return { path: platformPath, asIs: true };
  if (platformPath === root || platformPath.startsWith(root + "/")) return { path: platformPath, asIs: true };
  return { path: `${root}${platformPath}`, asIs: false };
}

function Section({ n, title, right, delay, children }: {
  n: number;
  title: string;
  right?: React.ReactNode;
  delay: number;
  children: React.ReactNode;
}) {
  return (
    <section className={classes.section} style={{ animationDelay: `${delay}ms` }}>
      <div className={classes.sectionHead}>
        <span className={classes.num}>{n}</span>
        <Text fw={600} style={{ flex: 1 }}>{title}</Text>
        {right}
      </div>
      {children}
    </section>
  );
}

function Stat({ label, value, color, delay, hint }: {
  label: string;
  value: React.ReactNode;
  color?: string;
  delay: number;
  hint?: string;
}) {
  const tile = (
    <div className={classes.stat} style={{ animationDelay: `${delay}ms` }}>
      <div className={classes.statValue} style={color ? { color: `var(--mantine-color-${color}-filled)` } : undefined}>
        {value}
      </div>
      <Text size="xs" c="dimmed">{label}</Text>
    </div>
  );
  return hint ? <Tooltip label={hint} withArrow multiline w={240}>{tile}</Tooltip> : tile;
}

export default function DeployDialog({
  opened,
  onClose,
  templateData,
  isDirty,
}: {
  opened: boolean;
  onClose: () => void;
  templateData: { template_version_id: number; name: string; version: number } | null;
  isDirty: boolean;
}) {
  const [prefs, setPrefs] = useState<Prefs>(DEFAULT_PREFS);
  const [loaded, setLoaded] = useState(false);
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  // Set by an edit, so loading saved settings doesn't immediately save them back.
  const edited = useRef(false);
  const [info, setInfo] = useState<DeploymentInfo | null>(null);
  const [infoLoading, setInfoLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rootDir, setRootDir] = useState<string | null>(null);

  const tvid = templateData?.template_version_id;

  // On open: this workflow's saved settings, else the last ones used anywhere
  // in this browser, else defaults. localStorage is read here rather than at
  // module load because it doesn't exist during SSR.
  useEffect(() => {
    if (!opened || !tvid) return;
    let cancelled = false;
    setLoaded(false);
    setSaveState("idle");
    edited.current = false;
    const fallback = loadPrefs();
    apiFetch(`/api/workflow-templates/${tvid}/deploy-settings`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!cancelled) setPrefs(d?.settings ? { ...fallback, ...d.settings } : fallback);
      })
      .catch(() => {
        if (!cancelled) setPrefs(fallback);
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [opened, tvid]);

  const update = (patch: Partial<Prefs>) => {
    edited.current = true;
    setPrefs((p) => {
      const next = { ...p, ...patch };
      try {
        localStorage.setItem(PREFS_KEY, JSON.stringify(next));
      } catch {
        /* private mode / blocked storage: the server copy still saves */
      }
      return next;
    });
  };

  // Debounced save to the server after an edit.
  useEffect(() => {
    if (!loaded || !tvid || !edited.current) return;
    setSaveState("saving");
    const t = setTimeout(async () => {
      try {
        const res = await apiFetch(`/api/workflow-templates/${tvid}/deploy-settings`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(prefs),
        });
        setSaveState(res.ok ? "saved" : "error");
      } catch {
        setSaveState("error");
      }
    }, 700);
    return () => clearTimeout(t);
  }, [prefs, loaded, tvid]);

  const isOther = prefs.node === OTHER;
  const system = isOther ? "" : prefs.node;
  const nodeName = isOther ? prefs.nodeName.trim() : system;

  // Tapis paths are relative to the system's rootDir, fetched to turn a
  // browsed folder into the real path on the node (and back, for where Browse
  // opens). Unknown (null) is treated as "/".
  useEffect(() => {
    if (!opened || !system) {
      setRootDir(null);
      return;
    }
    let cancelled = false;
    setRootDir(null);
    apiFetch(`/api/tapis-systems/${encodeURIComponent(system)}/queues`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (!cancelled) setRootDir(d?.root_dir || null); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [opened, system]);
  // An unresolved Tapis macro (e.g. ${apiUserId}) can't be prefixed reliably.
  const rootUsable = !!rootDir && !rootDir.includes("${");
  const root = rootUsable ? rootDir!.replace(/\/+$/, "") : "";
  const toNodePath = (p: string) => `${root}/${p.replace(/^\/+/, "")}`.replace(/\/+$/, "") || "/";
  const toSystemPath = (p: string) => {
    if (!p.startsWith("/")) return "/";
    if (!root) return p;
    return p === root ? "/" : p.startsWith(root + "/") ? p.slice(root.length) : "/";
  };
  const browseDisabledReason = isOther
    ? "Browsing works only for Tapis systems. Type the path as it appears on your node."
    : undefined;

  // Shared by every request below so the instructions, the links and the
  // downloaded files all describe the same paths.
  const dataRoot = prefs.remap ? prefs.data_root : "/";
  const query = new URLSearchParams({
    data_root: dataRoot,
    workdir: prefs.workdir,
    // Every route runs from the unzipped deployment zip, whose step images
    // are in containers/.
    images_dir: "./containers",
  }).toString();
  const fileStem = templateData
    ? `${(templateData.name || "workflow").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "workflow"}-v${templateData.version}`
    : "workflow";

  // Debounced: the path fields re-render this on every keystroke.
  useEffect(() => {
    if (!opened || !templateData || !loaded) return;
    let cancelled = false;
    const t = setTimeout(async () => {
      setInfoLoading(true);
      try {
        const res = await apiFetch(`/api/workflow-templates/${templateData.template_version_id}/deployment?${query}`);
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const data = await res.json();
        if (!cancelled) {
          setInfo(data);
          setError(null);
        }
      } catch (e: any) {
        if (!cancelled) setError(e.message || "Could not check this workflow");
      } finally {
        if (!cancelled) setInfoLoading(false);
      }
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [opened, templateData?.template_version_id, query, loaded]);

  const resolved = useMemo(
    () =>
      (info?.sources || []).map((s) => ({
        ...s,
        // A backend from before source analysis sends neither field: treat
        // every source as read, with nothing known about what it feeds.
        used: s.used ?? true,
        used_by: s.used_by ?? [],
        // A "My node" path is read exactly as typed; the runner never re-roots it.
        ...(s.node_local ? { path: s.platform_path, asIs: true } : resolveSource(dataRoot, s.platform_path)),
      })),
    [info, dataRoot],
  );
  const readSources = resolved.filter((s) => s.used);
  const unusedSources = resolved.filter((s) => !s.used);
  // A data root that IS one of the workflow's own platform folders (or holds
  // one) almost always means the data is already where the platform has it,
  // and "same paths" is what's wanted.
  const rootIsPlatformFolder =
    prefs.remap &&
    prefs.data_root.startsWith("/") &&
    prefs.data_root.replace(/\/+$/, "") !== "" &&
    readSources.some((s) => {
      const r = prefs.data_root.replace(/\/+$/, "");
      return s.platform_path === r || s.platform_path.startsWith(r + "/");
    });

  // Small JSON, so a blob download through apiFetch (which carries the
  // credential in every auth mode) is fine here.
  const downloadBundle = async () => {
    if (!templateData) return;
    try {
      const res = await apiFetch(`/api/workflow-templates/${templateData.template_version_id}/local-bundle?${query}`);
      if (!res.ok) throw new Error(`Export failed (HTTP ${res.status})`);
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download = `${fileStem}-bundle.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      setError(e.message || "Could not download the bundle");
    }
  };

  const warnings = info?.warnings || [];
  const realWarnings = warnings.filter((w) => !w.startsWith("note"));
  const unsupported = info?.unsupported || [];
  const ready = !!info && unsupported.length === 0 && realWarnings.length === 0;
  const nonStaged = (info?.images || []).filter((i) => !i.staged);
  const inZip = nonStaged.filter((i) => i.download_url).length;

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={<Text fw={700}>Deploy to my node</Text>}
      size="xl"
      radius="md"
      centered
      transitionProps={{ transition: "pop", duration: 220 }}
      overlayProps={{ blur: 3 }}
    >
      <Stack gap="md">
        <div className={classes.hero}>
          <div className={classes.heroIcon}><IconServerBolt size={24} /></div>
          <div>
            <Text fw={600}>Run this workflow on a machine you choose</Text>
            <Text size="sm" c="dimmed">
              Tell us where it will run, check that it's ready, then copy the commands. No Tapis or scheduler is
              needed on your node.
            </Text>
            <div className={classes.flow}>
              <span className={classes.flowChip}>This workflow (last saved version)</span>
              <IconArrowRight size={12} />
              <span className={classes.flowChip}>bundle.json + containers</span>
              <IconArrowRight size={12} />
              <span className={classes.flowChip}>{nodeName || "your node"}</span>
            </div>
          </div>
        </div>

        {isDirty && (
          <Alert color="yellow" icon={<IconAlertTriangle size={16} />}>
            The canvas has unsaved changes. What you deploy is the last saved version, so save first if you want
            those changes included.
          </Alert>
        )}

        <Section
          n={1}
          title="Where will it run?"
          delay={60}
          right={
            <Transition mounted={saveState !== "idle"} transition="fade" duration={200}>
              {(style) => (
                <Text size="xs" c={saveState === "error" ? "red" : "dimmed"} style={style} className={classes.saveState}>
                  {saveState === "saving" && <><Loader size={10} /> Saving…</>}
                  {saveState === "saved" && <><IconCheck size={12} /> Saved for this workflow</>}
                  {saveState === "error" && "Couldn't save these settings"}
                </Text>
              )}
            </Transition>
          }
        >
          <Stack gap="sm">
            <Select
              label="Node"
              description="A Tapis system lets you browse its folders below. Pick Other for your own machine or any node that isn't on Tapis."
              data={[
                { group: "Tapis systems", items: TAPIS_SYSTEMS },
                { group: "Somewhere else", items: [{ value: OTHER, label: "Other (my own machine)" }] },
              ]}
              value={prefs.node}
              onChange={(v) => update({ node: v ?? DEFAULT_TAPIS_SYSTEM })}
              allowDeselect={false}
            />
            <Collapse expanded={isOther}>
              <TextInput
                label="Node name"
                description="Anything that helps you recognise it, e.g. lab-gpu-01. Only used in the instructions below."
                placeholder="lab-gpu-01"
                value={prefs.nodeName}
                onChange={(e) => update({ nodeName: e.currentTarget.value })}
              />
            </Collapse>
            {!isOther && rootDir && !rootUsable && (
              <Text size="xs" c="yellow" className={classes.reveal}>
                This system's root ({rootDir}) isn't a plain path, so browsed folders are shown relative to it.
                Check the path matches what you see on the node.
              </Text>
            )}

            <Switch
              checked={!prefs.remap}
              onChange={(e) => update({ remap: !e.currentTarget.checked })}
              label="My data is at the same paths as on the platform"
              description="Recommended when the node can see the same folders the workflow uses (e.g. a node on the same cluster). Each input is read from exactly the path the platform uses; nothing is added in front."
            />
            <Collapse expanded={prefs.remap}>
              <TapisPathField
                label="Data root"
                description="The folder your copy of the data sits under, laid out the same way as on the platform. The platform's path is added beneath it, e.g. /data + /fs/ess/project/images → /data/fs/ess/project/images."
                system={system}
                path={prefs.data_root}
                selectType="dir"
                showSystemSelect={false}
                mapPickedPath={toNodePath}
                toSystemPath={toSystemPath}
                browseDisabledReason={browseDisabledReason}
                onPathChange={(p) => update({ data_root: p })}
              />
            </Collapse>

            <Transition mounted={rootIsPlatformFolder} transition="slide-down" duration={200}>
              {(style) => (
                <Alert style={style} color="blue" icon={<IconInfoCircle size={16} />} p="xs">
                  <Group justify="space-between" gap="xs" wrap="nowrap">
                    <Text size="xs">
                      This folder is one of the workflow's own platform folders, so your data is already where
                      the platform has it. Use same paths instead.
                    </Text>
                    <Button size="compact-xs" variant="light" onClick={() => update({ remap: false })}>
                      Use same paths
                    </Button>
                  </Group>
                </Alert>
              )}
            </Transition>

            {resolved.length > 0 && (
              <div className={classes.pathPreview}>
                <Text size="xs" fw={600} mb={6}>On your node, the steps will read</Text>
                {readSources.length === 0 && (
                  <Text size="xs" c="dimmed">No step reads data from a folder on the node.</Text>
                )}
                {readSources.map((s) => (
                  <div key={s.label + s.platform_path} className={classes.sourceCard}>
                    <Group gap={6} justify="space-between" wrap="nowrap">
                      <Text size="xs" fw={500} truncate>
                        {s.used_by.map((u) => `${u.step} · ${u.port}`).join(", ") || s.label}
                      </Text>
                      <Badge size="xs" variant="light" color={s.node_local ? "grape" : s.asIs ? "teal" : "blue"}>
                        {s.node_local ? "on your node" : s.asIs ? "same path" : "copy under data root"}
                      </Badge>
                    </Group>
                    <Code className={classes.sourcePath}>{s.path}</Code>
                    {!s.asIs && (
                      <Text size="xs" c="dimmed">
                        platform path <Code>{s.platform_path}</Code>, placed under your data root
                      </Text>
                    )}
                  </div>
                ))}
                {unusedSources.length > 0 && (
                  <Text size="xs" c="dimmed" mt={6}>
                    Not needed on the node:{" "}
                    {unusedSources.map((s, i) => (
                      <span key={s.platform_path}>
                        {i > 0 && ", "}
                        <Code>{s.platform_path}</Code>
                        {s.used_by.length > 0 && ` (${s.used_by.map((u) => u.port).join(", ")})`}
                      </span>
                    ))}
                    . On the platform that's where results are archived; on your node every step writes under
                    the work dir instead, so it isn't mounted.
                  </Text>
                )}
              </div>
            )}

            <TapisPathField
              label="Work dir"
              description="Where each step's working files and outputs are written. Created if it doesn't exist."
              system={system}
              path={prefs.workdir}
              selectType="dir"
              showSystemSelect={false}
              mapPickedPath={toNodePath}
              toSystemPath={toSystemPath}
              browseDisabledReason={browseDisabledReason}
              onPathChange={(p) => update({ workdir: p })}
            />

          </Stack>
        </Section>

        <Section
          n={2}
          title="Check the workflow"
          delay={140}
          right={
            info ? (
              ready ? (
                <Badge color="teal" variant="light" leftSection={<IconCircleCheck size={12} />}>Ready to run</Badge>
              ) : (
                <Badge color="yellow" variant="light" leftSection={<IconAlertTriangle size={12} />}>Needs attention</Badge>
              )
            ) : infoLoading ? (
              <Loader size="xs" />
            ) : null
          }
        >
          {info ? (
            <Stack gap="sm">
              <div className={classes.stats}>
                <Stat label="Steps to run" value={info.steps} delay={0} />
                <Stat
                  label="Container images"
                  value={nonStaged.length}
                  delay={60}
                  hint={`${inZip} of ${nonStaged.length} can be included in the zip; the table below shows the rest.`}
                />
                <Stat
                  label="Credentials needed"
                  value={info.secrets_required.length}
                  color={info.secrets_required.length ? "blue" : undefined}
                  delay={120}
                  hint={info.secrets_required.length ? info.secrets_required.join(", ") : "None"}
                />
                <Stat
                  label="Warnings"
                  value={realWarnings.length}
                  color={realWarnings.length ? "yellow" : "teal"}
                  delay={180}
                />
              </div>
              {unsupported.length > 0 && (
                <Alert color="red" icon={<IconAlertTriangle size={16} />} title="Can't run off-platform" className={classes.reveal}>
                  <Text size="xs">
                    {unsupported.join(", ")} run inside the platform, not in a container. Run them on the platform,
                    or remove them from the workflow before deploying.
                  </Text>
                </Alert>
              )}
              {warnings.length > 0 && (
                <Spoiler maxHeight={70} showLabel="Show all" hideLabel="Show less">
                  <Stack gap={4}>
                    {warnings.map((w, i) => (
                      <Text key={i} size="xs" c={w.startsWith("note") ? "dimmed" : undefined}>
                        • {w}
                      </Text>
                    ))}
                  </Stack>
                </Spoiler>
              )}
              <Group>
                <Button size="xs" variant="default" leftSection={<IconDownload size={14} />} onClick={downloadBundle}>
                  Download bundle.json only
                </Button>
              </Group>
            </Stack>
          ) : (
            <Group gap="xs">
              {infoLoading && <Loader size="xs" />}
              <Text size="sm" c={error ? "red" : "dimmed"}>{error || "Checking…"}</Text>
            </Group>
          )}
        </Section>

        <Section n={3} title="Run it" delay={220}>
          {opened && templateData && (
            <DeploymentOptions
              templateVersionId={templateData.template_version_id}
              query={query}
              fileStem={fileStem}
              nodeName={nodeName}
              info={info}
              loading={infoLoading}
            />
          )}
        </Section>

        {error && info && <Text size="sm" c="red">{error}</Text>}
        <Text size="xs" c="dimmed">The full walkthrough is in docs/local-deployment.md.</Text>
      </Stack>
    </Modal>
  );
}
