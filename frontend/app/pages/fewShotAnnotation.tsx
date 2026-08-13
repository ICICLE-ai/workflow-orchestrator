import { useEffect, useState } from "react";
import type { ComponentProps, ComponentType } from "react";
import {
  Stack,
  Group,
  Text,
  Title,
  Divider,
  Switch,
  NumberInput,
  TextInput,
  SimpleGrid,
  Tooltip,
  Loader,
  Code,
  Badge,
  Alert,
} from "@mantine/core";
import { IconInfoCircle } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { BACKEND_URL, fetchCurrentUser } from "../lib/api";

// Type-only import — erased at build, so this browser-only package (talks to
// Patra + the Tapis vault directly from the client) never loads during SSR.
// The runtime module is loaded lazily below, client-side only.
import type { ModelSelector as ModelSelectorComponent } from "@icicle-ai/patra-model-selector";

const studioFetch = (path: string, init?: RequestInit) =>
  fetch(`${BACKEND_URL}${path}`, { ...init, credentials: "include" });

type ModelSelectorProps = ComponentProps<typeof ModelSelectorComponent>;

interface Libs {
  ModelSelector: ComponentType<ModelSelectorProps>;
}

// What actually reaches the job. step.json's tapis_job substitutes these two
// config fields directly into CLI args — `--proposers ${proposer_model}` and
// `--embedder ${embedder_model}` — so they must hold the MODEL NAME the
// few_shot_detection app recognizes, not the Patra card's UUID. ModelSelector
// only reports a UUID back (onModelSelect: (modelId) => void), so the card's
// name is looked up from the Patra card list and mapped here.
//
// Matched as a case-insensitive substring of the card name, FIRST RULE WINS —
// so the more specific entries come first. That ordering is what keeps a card
// named e.g. "SAM3 + DINOv3 ensemble" from resolving arbitrarily.
const MODEL_NAME_RULES: ReadonlyArray<readonly [match: string, jobValue: string]> = [
  ["bioclip", "bioclip"],
  ["owl", "owlv2"],
  ["sam", "sam3"],
  ["dino", "dinov3"],
];

const JOB_MODEL_VALUES = MODEL_NAME_RULES.map(([, value]) => value);

function jobValueForCardName(cardName: string | undefined | null): string | null {
  const name = (cardName ?? "").toLowerCase();
  if (!name) return null;
  for (const [match, jobValue] of MODEL_NAME_RULES) {
    if (name.includes(match)) return jobValue;
  }
  return null;
}

// Distinguishes a legacy stored Patra UUID from a model name, so a template
// saved before this mapping existed still highlights the right card while its
// value is being corrected.
const looksLikeUuid = (v: string) => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v);

// A field label with an info icon whose hover tooltip is the config_schema's
// own `description` — one source of truth for the helper text, shown here as
// a real tooltip instead of GenericConfigForm's static under-label text.
function LabelWithTooltip({ text, tip }: { text: string; tip?: string }) {
  return (
    <Group gap={4} wrap="nowrap">
      <Text size="sm" fw={500}>{text}</Text>
      {tip && (
        <Tooltip label={tip} multiline w={280} withArrow position="right">
          <IconInfoCircle size={14} style={{ cursor: "help", opacity: 0.6, flexShrink: 0 }} />
        </Tooltip>
      )}
    </Group>
  );
}

// What this selection will actually put on the job's command line. Shown
// because the card the user clicks and the string the job receives are no
// longer the same thing — without this, a mis-mapped model is invisible until
// the job fails on an HPC queue minutes later.
function SelectedModelValue({ value, argName, warning }: { value: string; argName: string; warning: string | null }) {
  if (!value) return null;
  return (
    <Group gap={6} mt={6} wrap="nowrap" align="flex-start">
      <Text size="xs" c="dimmed" style={{ whiteSpace: "nowrap" }}>Passes:</Text>
      <Code>{`${argName} ${value}`}</Code>
      {warning && (
        <Tooltip label={warning} multiline w={300} withArrow position="top">
          <Badge size="xs" color="yellow" variant="light" style={{ cursor: "help", flexShrink: 0 }}>
            unrecognized
          </Badge>
        </Tooltip>
      )}
    </Group>
  );
}

export default function FewShotAnnotationPanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  const schema = step.config_schema || {};
  const tip = (key: string) => schema[key]?.description;

  const field = (key: string) => config[key] !== undefined ? config[key] : schema[key]?.default;
  const setField = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  // annotation_file / ground_truth / dataset are wired input ports (see
  // step.json) — shown read-only here from the connected upstream node's
  // config, not editable directly. Every source-like step in this app (source
  // image dir/json file, smart_labeler) exposes its single value under "path".
  const wiredPath = (portName: string) => String(connectedInputs[portName]?.config?.path ?? "");

  const useSahi = Boolean(field("use_sahi"));

  // Client-only load of the Patra model selector (talks to Patra/Tapis
  // directly from the browser; see docs/adding-a-step-custom-ui.md §5).
  const [libs, setLibs] = useState<Libs | null>(null);
  useEffect(() => {
    let cancelled = false;
    import("@icicle-ai/patra-model-selector").then((mod) => {
      if (!cancelled) setLibs({ ModelSelector: mod.ModelSelector });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // ModelSelector needs a raw Tapis token + username (gated-model / HF-token
  // vault flow) — same "logged in but no Tapis session shouldn't bounce the
  // whole app to login" reasoning as smartLabeler.tsx's tapis-file-explorer use.
  const [tapisToken, setTapisToken] = useState<string | undefined>(undefined);
  const [tapisUsername, setTapisUsername] = useState<string | undefined>(undefined);
  useEffect(() => {
    studioFetch("/api/tapis/token")
      .then((res) => (res.ok ? res.json() : null))
      .then((d) => d && setTapisToken(d.token))
      .catch(() => {});
    fetchCurrentUser().then((u) => setTapisUsername(u?.username));
  }, []);

  // The Patra card list, purely so a selected UUID can be resolved to its
  // model NAME (see MODEL_NAME_RULES). ModelSelector fetches this list itself
  // for rendering, but doesn't expose it or the selected card's name — hence
  // the second call. Failure is non-fatal: the selector still works, selections
  // just can't be mapped, which the warning below makes visible rather than
  // silently writing an unusable value into the job.
  const [cards, setCards] = useState<Array<{ uuid: string; name: string }> | null>(null);
  const [cardsError, setCardsError] = useState<string | null>(null);
  useEffect(() => {
    if (!tapisToken) return;
    let cancelled = false;
    import("@icicle-ai/patra-model-selector")
      .then((mod) => mod.listPatraModels(tapisToken))
      .then((list) => {
        if (!cancelled) setCards((list ?? []).map((c) => ({ uuid: c.uuid, name: c.name })));
      })
      .catch((e: any) => {
        if (!cancelled) setCardsError(e?.message || "Could not load Patra model cards");
      });
    return () => {
      cancelled = true;
    };
  }, [tapisToken]);

  const jobValueForUuid = (uuid: string) => jobValueForCardName(cards?.find((c) => c.uuid === uuid)?.name);
  const cardNameForUuid = (uuid: string) => cards?.find((c) => c.uuid === uuid)?.name;
  // Reverse direction, for keeping the chosen card highlighted: several cards
  // can share a job value (the same model published twice), so the first match
  // in list order wins — deterministic, and any of them denotes the same model.
  const uuidForJobValue = (value: string) => cards?.find((c) => jobValueForCardName(c.name) === value)?.uuid;

  const selectModel = (key: "proposer_model" | "embedder_model") => (uuid: string) => {
    // Falls back through: mapped name -> the card's own name (lowercased, so a
    // model outside the four known rules still reaches the job as something
    // legible) -> the raw UUID, only when the card list never loaded. The last
    // two are flagged by modelWarning below.
    const value = jobValueForUuid(uuid) ?? cardNameForUuid(uuid)?.trim().toLowerCase() ?? uuid;
    setField(key, value);
  };

  // One-time correction of templates saved before this mapping existed, whose
  // stored value is a Patra UUID the job can't use. Only rewrites a value that
  // resolves to a KNOWN card with a mappable name — anything else is left alone
  // and surfaced as a warning instead of being guessed at.
  //
  // Deliberately keyed on `cards` alone: `config`/`onChange` change identity on
  // every render, and including them would re-run this in a loop.
  useEffect(() => {
    if (!cards) return;
    const next = { ...config };
    let changed = false;
    for (const key of ["proposer_model", "embedder_model"] as const) {
      const stored = String(config[key] ?? "");
      if (!stored || !looksLikeUuid(stored)) continue;
      const mapped = jobValueForUuid(stored);
      if (mapped) {
        next[key] = mapped;
        changed = true;
      }
    }
    if (changed) onChange(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cards]);

  // A stored value the job won't recognize — either a leftover UUID that
  // couldn't be resolved, or a card whose name matches none of the rules.
  const modelWarning = (value: string) => {
    if (!value || JOB_MODEL_VALUES.includes(value)) return null;
    if (looksLikeUuid(value)) {
      return cards
        ? "This is a Patra card ID, not a model name — the job needs a name. Re-select the model to fix it."
        : "Stored as a Patra card ID. The card list hasn't loaded, so it can't be mapped to a model name yet.";
    }
    return `"${value}" isn't one of the names this step maps to (${JOB_MODEL_VALUES.join(", ")}) — the job may reject it.`;
  };

  const selectedIdsFor = (value: string) => {
    if (!value) return [];
    const uuid = uuidForJobValue(value);
    if (uuid) return [uuid];
    // Legacy config still holding a UUID: highlight it directly so the panel
    // reflects what's saved while the value itself is being corrected.
    return looksLikeUuid(value) ? [value] : [];
  };

  return (
    <Stack gap="lg">
      <div>
        <Title order={6} mb="xs">Inputs</Title>
        <Stack gap="xs">
          <TextInput
            label={<LabelWithTooltip text="Annotation file" tip={tip("annotation_file")} />}
            value={wiredPath("annotation_file")}
            placeholder="Connect an annotation_file (JSON) input"
            readOnly
            variant="filled"
          />
          <TextInput
            label={<LabelWithTooltip text="Ground truth" tip={tip("ground_truth")} />}
            value={wiredPath("ground_truth")}
            placeholder="Connect a ground_truth (image directory) input"
            readOnly
            variant="filled"
          />
          <TextInput
            label={<LabelWithTooltip text="Dataset" tip={tip("dataset")} />}
            value={wiredPath("dataset")}
            placeholder="Connect a dataset (image directory) input"
            readOnly
            variant="filled"
          />
        </Stack>
      </div>

      <Divider />

      <div>
        <Title order={6} mb="xs">Model cards</Title>
        {cardsError && (
          <Alert variant="light" color="yellow" mb="xs" py={6}>
            <Text size="xs">
              Couldn't load the Patra model list ({cardsError}), so a selected card can't be resolved to the
              model name the job expects. Picking a model will still work, but check the value shown under each
              selector before running.
            </Text>
          </Alert>
        )}
        {!libs || tapisToken === undefined ? (
          <Group justify="center" p="md"><Loader size="sm" /></Group>
        ) : (
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
            <div>
              <LabelWithTooltip text="Proposer" tip={tip("proposer_model")} />
              <libs.ModelSelector
                title="Select a proposer model"
                maxHeight={380}
                multiSelect={false}
                filterList={["04ac0992-d0ab-4a50-8a4f-92a5da89d848", "1eef29f7-cb6e-430a-a47f-b0075df2cd48"]}
                selectedModelIds={selectedIdsFor(String(config.proposer_model ?? ""))}
                onModelSelect={selectModel("proposer_model")}
                onModelDeselect={(id) => {
                  // The stored value is a model name now, so compare against the
                  // deselected card's OWN resolved value rather than the id.
                  if (selectedIdsFor(String(config.proposer_model ?? "")).includes(id)) setField("proposer_model", "");
                }}
                tapisToken={tapisToken}
                tapisUsername={tapisUsername}
              />
              <SelectedModelValue value={String(config.proposer_model ?? "")} argName="--proposers" warning={modelWarning(String(config.proposer_model ?? ""))} />
            </div>
            <div>
              <LabelWithTooltip text="Embedder" tip={tip("embedder_model")} />
              <libs.ModelSelector
                title="Select an embedder model"
                maxHeight={380}
                multiSelect={false}
                filterList={["8c517ed0-c9c0-4f57-bb9d-f066ab4ec34e", "04ac0992-d0ab-4a50-8a4f-92a5da89d848", "affa8339-13a8-41d0-95ed-475147e7900a"]}
                selectedModelIds={selectedIdsFor(String(config.embedder_model ?? ""))}
                onModelSelect={selectModel("embedder_model")}
                onModelDeselect={(id) => {
                  if (selectedIdsFor(String(config.embedder_model ?? "")).includes(id)) setField("embedder_model", "");
                }}
                tapisToken={tapisToken}
                tapisUsername={tapisUsername}
              />
              <SelectedModelValue value={String(config.embedder_model ?? "")} argName="--embedder" warning={modelWarning(String(config.embedder_model ?? ""))} />
            </div>
          </SimpleGrid>
        )}
      </div>

      <Divider />

      <div>
        <Title order={6} mb="xs">SAHI (Slicing Aided Hyper Inference)</Title>
        <Stack gap="xs">
          <Switch
            label={<LabelWithTooltip text="Enable SAHI" tip={tip("use_sahi")} />}
            checked={useSahi}
            onChange={(e) => setField("use_sahi", e.currentTarget.checked)}
          />
          {useSahi && (
            <Group grow>
              <NumberInput
                label={<LabelWithTooltip text="Tile size" tip={tip("tile_size")} />}
                value={Number(field("tile_size") ?? 640)}
                onChange={(v) => setField("tile_size", v)}
                min={32}
                step={32}
              />
              <NumberInput
                label={<LabelWithTooltip text="Overlap ratio" tip={tip("overlap_ratio")} />}
                value={Number(field("overlap_ratio") ?? 0.2)}
                onChange={(v) => setField("overlap_ratio", v)}
                min={0}
                max={1}
                step={0.05}
                decimalScale={2}
              />
            </Group>
          )}
        </Stack>
      </div>

      <Divider />

      <div>
        <Title order={6} mb="xs">Batch size</Title>
        <NumberInput
          label={<LabelWithTooltip text="Batch size" tip={tip("batch_size")} />}
          value={Number(field("batch_size") ?? 8)}
          onChange={(v) => setField("batch_size", v)}
          min={1}
          step={1}
        />
      </div>

      <Divider />

      <div>
        <Title order={6} mb="xs">Thresholds</Title>
        <Group grow>
          <NumberInput
            label={<LabelWithTooltip text="Confidence" tip={tip("confidence_threshold")} />}
            value={Number(field("confidence_threshold") ?? 0.3)}
            onChange={(v) => setField("confidence_threshold", v)}
            min={0}
            max={1}
            step={0.05}
            decimalScale={2}
          />
          <NumberInput
            label={<LabelWithTooltip text="Similarity" tip={tip("similarity_threshold")} />}
            value={Number(field("similarity_threshold") ?? 0.7)}
            onChange={(v) => setField("similarity_threshold", v)}
            min={0}
            max={1}
            step={0.05}
            decimalScale={2}
          />
          <NumberInput
            label={<LabelWithTooltip text="NMS IoU" tip={tip("nms_iou_threshold")} />}
            value={Number(field("nms_iou_threshold") ?? 0.5)}
            onChange={(v) => setField("nms_iou_threshold", v)}
            min={0}
            max={1}
            step={0.05}
            decimalScale={2}
          />
        </Group>
      </div>
    </Stack>
  );
}

// A wider centered modal — two side-by-side model card grids need more room
// than the default "lg" (see StepSettingsModal, which honors this).
(FewShotAnnotationPanel as any).modalSize = "1100px";
