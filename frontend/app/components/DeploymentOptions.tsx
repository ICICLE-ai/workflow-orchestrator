import { useEffect, useState } from "react";
import {
  Alert,
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
} from "@mantine/core";
import { IconBrandDocker, IconBox, IconCheck, IconCopy, IconFileZip, IconInfoCircle } from "@tabler/icons-react";
import { apiFetch, BACKEND_URL } from "../lib/api";
import { hostOwnsAuth } from "../lib/embed";
import SafeCopyButton from "./SafeCopyButton";

// Mirrors GET /api/workflow-templates/{id}/deployment (backend/engine/deployment.py).
interface RunnerTarget {
  image: string;
  published: boolean;
  get: string;
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

interface DeploymentInfo {
  runner: { version: string; docker: RunnerTarget; apptainer: RunnerTarget; python: { run: string } };
  images: StepImage[];
  secrets_required: string[];
  warnings: string[];
}

function Snippet({ code }: { code: string }) {
  return (
    <div style={{ position: "relative" }}>
      <Code block style={{ whiteSpace: "pre-wrap", wordBreak: "break-all", paddingRight: 36 }}>
        {code}
      </Code>
      <SafeCopyButton value={code}>
        {({ copied, copy }) => (
          <Tooltip label={copied ? "Copied" : "Copy"} withArrow>
            <Button
              size="compact-xs"
              variant="subtle"
              color={copied ? "teal" : "gray"}
              onClick={copy}
              style={{ position: "absolute", top: 4, right: 4 }}
            >
              {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
            </Button>
          </Tooltip>
        )}
      </SafeCopyButton>
    </div>
  );
}

function RunnerTab({ target, kind, secrets }: { target: RunnerTarget; kind: "docker" | "apptainer"; secrets: string[] }) {
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Text size="sm">Runner image:</Text>
        <Code>{target.image}</Code>
        {!target.published && (
          <Tooltip label="No published image is configured on the server (WF_RUNNER_DOCKER_IMAGE), so build it from runner/." withArrow>
            <Badge size="sm" variant="light" color="gray">build locally</Badge>
          </Tooltip>
        )}
      </Group>
      <Text size="xs" fw={600}>1. Get the image</Text>
      <Snippet code={target.get} />
      {secrets.length > 0 && (
        <>
          <Text size="xs" fw={600}>2. Export credentials (never stored in the bundle)</Text>
          <Snippet code={secrets.map((s) => `export ${s}=...`).join("\n")} />
        </>
      )}
      <Text size="xs" fw={600}>{secrets.length > 0 ? "3" : "2"}. Run the downloaded bundle</Text>
      <Snippet code={target.run} />
      <Text size="xs" c="dimmed">
        {target.note} Step images go in the images dir — see the table below, or use the offline zip.
        {kind === "docker" && " Add --dry-run to print each step's command without running it."}
      </Text>
    </Stack>
  );
}

export default function DeploymentOptions({
  templateVersionId,
  query,
  fileStem,
}: {
  templateVersionId: number;
  // Same query string the bundle download uses, so commands match the file.
  query: string;
  fileStem: string;
}) {
  const [info, setInfo] = useState<DeploymentInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [zipping, setZipping] = useState(false);

  // Debounced: the path fields above re-render this on every keystroke.
  useEffect(() => {
    let cancelled = false;
    const t = setTimeout(async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await apiFetch(`/api/workflow-templates/${templateVersionId}/deployment?${query}`);
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail || `HTTP ${res.status}`);
        }
        const data = await res.json();
        if (!cancelled) setInfo(data);
      } catch (e: any) {
        if (!cancelled) setError(e.message || "Could not load deployment options");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 400);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [templateVersionId, query]);

  const zipPath = `/api/workflow-templates/${templateVersionId}/deployment.zip?${query}`;

  // The zip can be gigabytes, so standalone it's a plain navigation the browser
  // streams to disk (the session cookie authenticates it). Embedded in TapisUI
  // the credential is a header a link can't carry, so it falls back to a blob —
  // held in memory, which is fine for the image sizes seen so far.
  const downloadZip = async () => {
    if (!hostOwnsAuth()) {
      window.location.href = `${BACKEND_URL}${zipPath}`;
      return;
    }
    setZipping(true);
    setError(null);
    try {
      const res = await apiFetch(zipPath);
      if (!res.ok) throw new Error(`Zip failed (HTTP ${res.status})`);
      const url = URL.createObjectURL(await res.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `${fileStem}-deploy.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (e: any) {
      setError(e.message || "Could not download the zip");
    } finally {
      setZipping(false);
    }
  };

  if (!info) {
    return loading ? (
      <Group gap="xs"><Loader size="xs" /><Text size="sm" c="dimmed">Loading run instructions…</Text></Group>
    ) : error ? (
      <Text size="sm" c="red">{error}</Text>
    ) : null;
  }

  const fetchable = info.images.filter((i) => !i.staged && (i.download_url || i.pull_uri));
  const zippable = info.images.filter((i) => !i.staged && i.download_url);

  return (
    <Stack gap="sm">
      <Tabs defaultValue="docker" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="docker" leftSection={<IconBrandDocker size={14} />}>Docker</Tabs.Tab>
          <Tabs.Tab value="apptainer" leftSection={<IconBox size={14} />}>Apptainer</Tabs.Tab>
          <Tabs.Tab value="zip" leftSection={<IconFileZip size={14} />}>Offline zip</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="docker" pt="xs">
          <RunnerTab target={info.runner.docker} kind="docker" secrets={info.secrets_required} />
        </Tabs.Panel>

        <Tabs.Panel value="apptainer" pt="xs">
          <RunnerTab target={info.runner.apptainer} kind="apptainer" secrets={info.secrets_required} />
        </Tabs.Panel>

        <Tabs.Panel value="zip" pt="xs">
          <Stack gap="xs">
            <Text size="sm">
              One file with everything for a node without network access: <Code>bundle.json</Code>, the step
              images in <Code>images/</Code> ({zippable.length} of {info.images.filter((i) => !i.staged).length}{" "}
              available to the server), the runner, <Code>run.sh</Code> / <Code>run-docker.sh</Code>, and a
              README saying where each piece goes.
            </Text>
            <Snippet
              code={`unzip ${fileStem}-deploy.zip && cd ${fileStem}\n./run.sh --data-root /path/to/data --workdir /path/to/workdir      # host: python3 + apptainer\n./run-docker.sh --data-root /path/to/data --workdir /path/to/workdir   # or inside Docker`}
            />
            <Text size="xs" c="dimmed">
              To share images across workflows, move <Code>images/*.sif</Code> into one directory and pass{" "}
              <Code>--images-dir</Code>. Downloading redeems each image link once (they're use-limited), so keep
              the zip rather than downloading it again.
            </Text>
            <Group>
              <Button leftSection={<IconFileZip size={16} />} onClick={downloadZip} loading={zipping}>
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
            label="Read from each step's live Tapis app definition (containerImage + runtime). Put each .sif in your images dir under the name shown."
          >
            <IconInfoCircle size={14} />
          </Tooltip>
          {loading && <Loader size="xs" />}
        </Group>
        <Table withTableBorder fz="xs" verticalSpacing={4}>
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
                    {img.staged && <Text size="xs" c="dimmed">Staged as an input — the runner downloads it.</Text>}
                    {!img.staged && !img.docker_image && !img.pull_uri && img.download_url && (
                      <Text size="xs">Download link{img.runtime ? ` (${img.runtime})` : ""}</Text>
                    )}
                    {img.note && <Text size="xs" c="dimmed">{img.note}</Text>}
                  </Stack>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
        {fetchable.length > 0 && (
          <>
            <Text size="xs" fw={600} mt={4}>Fetch them into the images dir</Text>
            <Snippet code={fetchable.map((i) => i.pull_command).filter(Boolean).join("\n")} />
          </>
        )}
      </Stack>

      {error && <Alert color="red" icon={<IconInfoCircle size={16} />}>{error}</Alert>}
    </Stack>
  );
}
