import React, { useEffect, useRef, useState } from "react";
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Stack,
  Table,
  Tabs,
  Text,
  Tooltip,
  Transition,
} from "@mantine/core";
import {
  IconBrandDocker,
  IconBox,
  IconCheck,
  IconCopy,
  IconDownload,
  IconFileZip,
  IconInfoCircle,
  IconRefresh,
} from "@tabler/icons-react";
import { apiFetch } from "../lib/api";
import SafeCopyButton from "./SafeCopyButton";
import classes from "./DeployDialog.module.css";

// Mirrors GET /api/workflow-templates/{id}/deployment (backend/engine/deployment.py).
interface RunnerTarget {
  image: string;
  get: string;
  // Run from inside the unzipped deployment zip.
  run: string;
  note: string;
}

interface StepImage {
  sif: string;
  steps: string[];
  app_id: string;
  app_version: string;
  runtime: string | null;
  container_image: string | null;
  docker_image: string | null;
  pull_uri: string | null;
  download_url: string | null;
  staged: boolean;
  note: string;
  pull_command: string | null;
}

export interface DeploymentInfo {
  runner: {
    version: string;
    docker: RunnerTarget;
    apptainer: RunnerTarget;
    // The zip's run.sh: the node's own python3 + apptainer, nothing nested.
    // Optional: absent from a backend older than this dialog.
    host?: { run: string; note: string };
  };
  images: StepImage[];
  // Each source node, whether a step reads it on the node, and what it feeds.
  sources: {
    label: string;
    platform_path: string;
    used: boolean;
    // A "My node" path: already where the data is, whatever the data root.
    node_local?: boolean;
    used_by: { step: string; port: string | null }[];
  }[];
  steps: number;
  unsupported: string[];
  secrets_required: string[];
  warnings: string[];
}

// POST /api/workflow-templates/{id}/deployment-link
interface DownloadLink {
  url: string;
  bundle_url: string;
  filename: string;
  folder: string;
  data_root: string;
  workdir: string;
  expires_at: string;
}

// The paths a previous set of links was minted with, when they've since changed.
interface PathChange {
  from: { data_root: string; workdir: string };
  to: { data_root: string; workdir: string };
}

// A command block with a copy button. Lights up briefly whenever its text
// changes (not on first render), so an updated command can't be missed.
function Snippet({ code }: { code: string }) {
  const prev = useRef(code);
  const [flashKey, setFlashKey] = useState(0);
  useEffect(() => {
    if (prev.current !== code) {
      prev.current = code;
      setFlashKey((k) => k + 1);
    }
  }, [code]);
  return (
    <div className={classes.snippet}>
      <div key={flashKey} className={flashKey ? classes.flash : undefined}>
        <Code block style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", paddingRight: 36 }}>
          {code}
        </Code>
      </div>
      <SafeCopyButton value={code}>
        {({ copied, copy }) => (
          <Tooltip label={copied ? "Copied" : "Copy"} withArrow>
            <Button
              size="compact-xs"
              variant="subtle"
              color={copied ? "teal" : "gray"}
              onClick={copy}
              className={classes.copyBtn}
            >
              <span key={String(copied)} className={copied ? classes.copied : undefined} style={{ display: "inline-flex" }}>
                {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
              </span>
            </Button>
          </Tooltip>
        )}
      </SafeCopyButton>
    </div>
  );
}

// An image's download link (from backend/image_sources.json): openable and
// copyable, so whoever deploys a step always has its link at hand.
function LinkCell({ url }: { url: string }) {
  return (
    <Group gap={4} wrap="nowrap">
      <Badge size="xs" variant="light" color="teal">link</Badge>
      <Anchor href={url} target="_blank" rel="noreferrer" size="xs" truncate="end" maw={260} title={url}>
        {url.replace(/^https?:\/\//, "")}
      </Anchor>
      <SafeCopyButton value={url}>
        {({ copied, copy }) => (
          <Tooltip label={copied ? "Copied" : "Copy link"} withArrow>
            <Button size="compact-xs" variant="subtle" color={copied ? "teal" : "gray"} onClick={copy}>
              {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
            </Button>
          </Tooltip>
        )}
      </SafeCopyButton>
    </Group>
  );
}

// One entry on the numbered timeline. `i` staggers the entrance animation.
function Step({ n, title, i, children }: { n: number; title: React.ReactNode; i: number; children: React.ReactNode }) {
  return (
    <div className={classes.step} style={{ animationDelay: `${i * 70}ms` }}>
      <div className={classes.stepDot}>{n}</div>
      <div className={classes.stepTitle}>{title}</div>
      <Stack gap={6}>{children}</Stack>
    </div>
  );
}

// Signed links work for a plain navigation in every auth mode (cookie or
// TapisUI header), and the browser streams the file straight to disk.
const openLink = (url?: string) => {
  if (url) window.location.href = url;
};

// Both runtimes start the same way: fetch the deployment zip (bundle.json,
// plus every step image in containers/ when they ride in the zip) onto the
// node with curl via a signed link, unzip, cd in. Every run command after that
// is relative to that folder.
function RunSteps({
  mode,
  runner,
  secrets,
  fileStem,
  nodeName,
  link,
  linkLoading,
  changed,
  imagesDir,
  imagesInZip,
}: {
  mode: "docker" | "apptainer";
  runner: DeploymentInfo["runner"];
  secrets: string[];
  fileStem: string;
  nodeName: string;
  link: DownloadLink | null;
  linkLoading: boolean;
  changed: PathChange | null;
  imagesDir: string;
  imagesInZip: boolean;
}) {
  const filename = link?.filename || `${fileStem}-deploy.zip`;
  const folder = link?.folder || fileStem;
  const on = nodeName ? <> on <Code>{nodeName}</Code></> : null;
  const exportStep = (n: number) =>
    secrets.length > 0 && (
      <Step n={n} i={n} title="Export your credentials">
        <Snippet code={secrets.map((s) => `export ${s}=...`).join("\n")} />
        <Text size="xs" c="dimmed">Never stored in the zip; the runner reads them from your shell.</Text>
      </Step>
    );
  const after = secrets.length > 0 ? 1 : 0;
  let runStep = 0;
  return (
    <Stack gap="md">
      <Transition mounted={!!(changed && link)} transition="slide-down" duration={250}>
        {(style) => (
          <Alert style={style} color="teal" icon={<IconRefresh size={16} />} title="Updated for your new paths">
            <Text size="xs">
              bundle.json, the download links and the run commands now use data root{" "}
              <Code>{changed?.to.data_root}</Code> and work dir <Code>{changed?.to.workdir}</Code> (were{" "}
              <Code>{changed?.from.data_root}</Code> and <Code>{changed?.from.workdir}</Code>). Links copied
              before this still carry the old paths.
            </Text>
          </Alert>
        )}
      </Transition>

      <div className={classes.timeline}>
        <Step n={1} i={1} title={<>Download the deployment zip{on}</>}>
          <Text size="xs" c="dimmed">
            {imagesInZip ? (
              <>Holds <Code>bundle.json</Code>, every step image in <Code>containers/</Code> and the runner.</>
            ) : (
              <>Holds <Code>bundle.json</Code> and the runner, a small download.</>
            )}{" "}
            Paste this on the node, or download it here and copy it over yourself.
          </Text>
          {link ? (
            <Snippet code={`curl -fL -o ${filename} '${link.url}'`} />
          ) : (
            <Group gap="xs"><Loader size="xs" /><Text size="xs" c="dimmed">Preparing a download link…</Text></Group>
          )}
          <Group gap="xs">
            <Button
              size="xs"
              variant="light"
              leftSection={<IconFileZip size={14} />}
              disabled={!link || linkLoading}
              onClick={() => openLink(link?.url)}
            >
              Download zip in browser
            </Button>
            {link && (
              <Text size="xs" c="dimmed">
                Link valid until {new Date(link.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}{" "}
                · anyone with it can download, so don't share it
              </Text>
            )}
          </Group>
        </Step>

        <Step n={2} i={2} title="Unzip it and go into the folder">
          <Snippet code={`unzip ${filename} && cd ${folder}`} />
          <Text size="xs" c="dimmed">Every command below runs from inside this folder.</Text>
          {!imagesInZip && (
            <Text size="xs" c="dimmed">
              The first run downloads each step's image into{" "}
              {imagesDir ? <Code>{imagesDir}</Code> : <><Code>containers/</Code> in this folder</>} and keeps it
              there, so later runs start straight away.
              {imagesDir && " Any workflow deployed with the same images folder reuses them."}
            </Text>
          )}
        </Step>

        {mode === "docker" ? (
          <>
            <Step n={3} i={3} title="Pull the runner image">
              <Snippet code={runner.docker.get} />
              <Text size="xs" c="dimmed">Public image, so no login or build needed.</Text>
            </Step>
            {exportStep(4)}
            <Step n={(runStep = 4 + after)} i={runStep} title="Run the workflow">
              <Snippet code={runner.docker.run} />
              <Text size="xs" c="dimmed">
                Your work dir and the folders the workflow reads are mounted at the same paths inside the
                container. {runner.docker.note} Add <Code>--dry-run</Code> to preview each step without running it.
              </Text>
            </Step>
          </>
        ) : (
          <>
            {exportStep(3)}
            {runner.host ? (
            <Step
              n={(runStep = 3 + after)}
              i={runStep}
              title={<>Run the workflow <Badge size="xs" variant="light" color="teal" ml={4}>recommended on HPC</Badge></>}
            >
              <Snippet code={runner.host.run} />
              <Text size="xs" c="dimmed">
                Runs the runner with the node's own python3 and starts each step with the node's own apptainer,
                so nothing is nested. {runner.host.note} Add <Code>--dry-run</Code> to preview each step first.
              </Text>
            </Step>
            ) : (
              <Alert color="yellow" icon={<IconInfoCircle size={16} />}>
                <Text size="xs">The server is running an older version. Restart the backend to see these steps.</Text>
              </Alert>
            )}
            <Step n={runStep + 1} i={runStep + 1} title="Or: run the runner itself inside Apptainer">
              <Snippet code={`${runner.apptainer.get}\n${runner.apptainer.run}`} />
              <Text size="xs" c="dimmed">{runner.apptainer.note}</Text>
            </Step>
          </>
        )}
      </div>

      <Alert variant="light" color="gray" icon={<IconInfoCircle size={16} />} title="Changed the paths after unzipping?">
        <Stack gap={6}>
          <Text size="xs">
            No need to download the images again. From inside the unzipped folder, replace just bundle.json, then
            rerun step {runStep}.
          </Text>
          {link && <Snippet code={`curl -fL -o bundle.json '${link.bundle_url}'`} />}
          <Group>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconDownload size={14} />}
              disabled={!link || linkLoading}
              onClick={() => openLink(link?.bundle_url)}
            >
              Download bundle.json
            </Button>
          </Group>
        </Stack>
      </Alert>
    </Stack>
  );
}

export default function DeploymentOptions({
  templateVersionId,
  query,
  fileStem,
  nodeName,
  info,
  loading,
  imagesDir,
  imagesInZip,
  onIncludeImages,
}: {
  templateVersionId: number;
  // Same query string the bundle download uses, so commands match the file.
  query: string;
  fileStem: string;
  // Shown in step titles; empty when the user hasn't named their node.
  nodeName: string;
  info: DeploymentInfo | null;
  loading: boolean;
  // The images folder on the node; "" is containers/ in the unzipped folder.
  imagesDir: string;
  imagesInZip: boolean;
  // Switches the dialog to putting the images in the zip.
  onIncludeImages: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [link, setLink] = useState<DownloadLink | null>(null);
  const [linkLoading, setLinkLoading] = useState(false);
  const [changed, setChanged] = useState<PathChange | null>(null);
  const lastLink = useRef<DownloadLink | null>(null);

  // Links encode the paths they were minted with, so they're re-minted (on
  // the same debounce as the instructions) whenever the paths change — the
  // curl commands shown always fetch a bundle.json matching the fields above.
  // Minting only signs a token; nothing is built until a link is used.
  useEffect(() => {
    let cancelled = false;
    setLinkLoading(true);
    const t = setTimeout(async () => {
      try {
        const res = await apiFetch(`/api/workflow-templates/${templateVersionId}/deployment-link?${query}`, {
          method: "POST",
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const next: DownloadLink = await res.json();
        if (cancelled) return;
        const prev = lastLink.current;
        if (prev && (prev.data_root !== next.data_root || prev.workdir !== next.workdir)) {
          setChanged({
            from: { data_root: prev.data_root, workdir: prev.workdir },
            to: { data_root: next.data_root, workdir: next.workdir },
          });
        }
        lastLink.current = next;
        setLink(next);
        setError(null);
      } catch (e: any) {
        if (!cancelled) setError(e.message || "Could not create a download link");
      } finally {
        if (!cancelled) setLinkLoading(false);
      }
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [templateVersionId, query]);

  if (!info) {
    return loading ? (
      <Group gap="xs"><Loader size="xs" /><Text size="sm" c="dimmed">Loading run instructions…</Text></Group>
    ) : null;
  }

  const fetchable = info.images.filter((i) => !i.staged && (i.download_url || i.pull_uri));
  const zippable = info.images.filter((i) => !i.staged && i.download_url);

  return (
    <Stack gap="md">
      <Tabs defaultValue="docker" variant="pills" radius="xl" keepMounted={false}>
        <Tabs.List grow>
          <Tabs.Tab value="docker" leftSection={<IconBrandDocker size={16} />}>
            Docker <Badge size="xs" variant="light" ml={4}>recommended</Badge>
          </Tabs.Tab>
          <Tabs.Tab value="apptainer" leftSection={<IconBox size={16} />}>Apptainer</Tabs.Tab>
          <Tabs.Tab value="zip" leftSection={<IconFileZip size={16} />}>Offline zip</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="docker" pt="md" className={classes.tabPanel}>
          <RunSteps
            mode="docker"
            runner={info.runner}
            secrets={info.secrets_required}
            fileStem={fileStem}
            nodeName={nodeName}
            link={link}
            linkLoading={linkLoading}
            changed={changed}
            imagesDir={imagesDir}
            imagesInZip={imagesInZip}
          />
        </Tabs.Panel>

        <Tabs.Panel value="apptainer" pt="md" className={classes.tabPanel}>
          <RunSteps
            mode="apptainer"
            runner={info.runner}
            secrets={info.secrets_required}
            fileStem={fileStem}
            nodeName={nodeName}
            link={link}
            linkLoading={linkLoading}
            changed={changed}
            imagesDir={imagesDir}
            imagesInZip={imagesInZip}
          />
        </Tabs.Panel>

        <Tabs.Panel value="zip" pt="md" className={classes.tabPanel}>
          <Stack gap="xs">
            {!imagesInZip && (
              <Alert color="yellow" icon={<IconInfoCircle size={16} />} p="xs">
                <Group justify="space-between" gap="xs" wrap="nowrap">
                  <Text size="xs">
                    This zip has no images in it, so the node will need network access to download them. For a
                    node without it, include them in the zip.
                  </Text>
                  <Button size="compact-xs" variant="light" onClick={onIncludeImages}>
                    Include images
                  </Button>
                </Group>
              </Alert>
            )}
            <Text size="sm">
              One file for a node without network access: <Code>bundle.json</Code>, the step images in{" "}
              <Code>containers/</Code> ({zippable.length} of {info.images.filter((i) => !i.staged).length} available
              to the server), the runner, <Code>run.sh</Code> / <Code>run-docker.sh</Code>, and a README saying
              where each piece goes.
            </Text>
            <Snippet
              code={`unzip ${fileStem}-deploy.zip && cd ${fileStem}\n./run.sh --data-root /path/to/data --workdir /path/to/workdir      # host: python3 + apptainer\n./run-docker.sh --data-root /path/to/data --workdir /path/to/workdir   # or inside Docker`}
            />
            <Text size="xs" c="dimmed">
              To share images across workflows, move <Code>containers/*.sif</Code> into one directory and pass{" "}
              <Code>--images-dir</Code>. Downloading uses up one go of each image's link (they're use-limited), so
              keep the zip rather than downloading it again.
            </Text>
            <Group>
              <Button leftSection={<IconFileZip size={16} />} disabled={!link || linkLoading} onClick={() => openLink(link?.url)}>
                Download zip
              </Button>
            </Group>
          </Stack>
        </Tabs.Panel>
      </Tabs>

      <Stack gap={4}>
        <Group gap={6}>
          <Text size="sm" fw={600}>Step container images</Text>
          <Tooltip
            multiline
            w={320}
            withArrow
            label={
              imagesInZip
                ? "Read from each step's live Tapis app definition (containerImage + runtime). The zip already includes these; the table is for fetching them yourself."
                : "Read from each step's live Tapis app definition (containerImage + runtime). The runner downloads the ones with a link on first run; the table is for fetching them yourself."
            }
          >
            <IconInfoCircle size={14} />
          </Tooltip>
          {loading && <Loader size="xs" />}
        </Group>
        <Table withTableBorder highlightOnHover fz="xs" verticalSpacing={4}>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>File</Table.Th>
              <Table.Th>Steps</Table.Th>
              <Table.Th>Image</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {info.images.map((img) => (
              <Table.Tr key={img.sif + img.steps.join()}>
                <Table.Td><Code>{img.sif}</Code></Table.Td>
                <Table.Td>{img.steps.join(", ")}</Table.Td>
                <Table.Td>
                  <Stack gap={2}>
                    {img.docker_image && (
                      <Group gap={4}><Badge size="xs" variant="light">docker</Badge><Code>{img.docker_image}</Code></Group>
                    )}
                    {!img.docker_image && img.pull_uri && <Code>{img.pull_uri}</Code>}
                    {img.staged && <Text size="xs" c="dimmed">Staged as an input; the runner downloads it.</Text>}
                    {img.download_url?.startsWith("http") && <LinkCell url={img.download_url} />}
                    {img.note && <Text size="xs" c="dimmed">{img.note}</Text>}
                  </Stack>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
        {fetchable.length > 0 && (
          <>
            <Text size="xs" fw={600} mt={4}>
              Fetch them into {imagesDir ? <Code>{imagesDir}</Code> : "containers/"} yourself
            </Text>
            <Snippet code={fetchable.map((i) => i.pull_command).filter(Boolean).join("\n")} />
          </>
        )}
      </Stack>

      {error && <Alert color="red" icon={<IconInfoCircle size={16} />}>{error}</Alert>}
    </Stack>
  );
}
