import { useState } from "react";
import {
  Stack,
  Group,
  TextInput,
  Textarea,
  NumberInput,
  Switch,
  PasswordInput,
  Text,
  Badge,
  Divider,
  Paper,
  Tabs,
  Alert,
  Button,
  Select,
} from "@mantine/core";
import { IconDatabase, IconKey, IconInfoCircle, IconCheck, IconX, IconDownload } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";

const PATRA_BACKEND = "https://patrabackend.pods.icicleai.tapis.io";

const CATEGORIES = [
  { value: "classification", label: "Image Classification" },
  { value: "object detection", label: "Object Detection" },
  { value: "segmentation", label: "Segmentation" },
  { value: "feature-extraction", label: "Feature Extraction" },
  { value: "NLP", label: "NLP" },
  { value: "regression", label: "Regression" },
];

// ─────────────────────────────────────────────────────────────
// PatraPanel — custom settings UI for the patra_upload step.
// Registered in registry.ts under "patra_upload".
// ─────────────────────────────────────────────────────────────
export default function PatraPanel({ config, onChange, step }: StepPanelProps) {
  const [publishing, setPublishing] = useState(false);
  const [publishResult, setPublishResult] = useState<{ success: boolean; message: string } | null>(null);

  const val = (key: string, fallback?: unknown) => {
    const v = config[key];
    return v !== undefined ? v : (step.config_schema?.[key]?.default ?? fallback);
  };
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  const handlePublish = async () => {
    const token = val("tapis_token", "") as string;
    if (!token) {
      setPublishResult({ success: false, message: "Please provide a Tapis token in the API Keys tab." });
      return;
    }
    if (!val("name", "")) {
      setPublishResult({ success: false, message: "Model name is required." });
      return;
    }
    if (!val("short_description", "")) {
      setPublishResult({ success: false, message: "Short description is required." });
      return;
    }
    if (!val("category", "")) {
      setPublishResult({ success: false, message: "Category is required." });
      return;
    }
    if (!val("input_type", "")) {
      setPublishResult({ success: false, message: "Input type is required." });
      return;
    }

    setPublishing(true);
    setPublishResult(null);

    try {
      const payload: Record<string, unknown> = {
        name: val("name"),
        version: val("version", "1.0"),
        short_description: val("short_description"),
        full_description: val("full_description"),
        keywords: val("keywords"),
        category: val("category"),
        input_type: val("input_type"),
        author: "anagha27",
        is_private: val("is_private", false),
      };

      // Add ai_model if we have model details
      const modelLocation = val("model_location", "") as string;
      const testAccuracy = val("test_accuracy", 0) as number;
      const framework = val("framework", "") as string;
      const modelType = val("model_type", "") as string;

      if (modelLocation || testAccuracy || framework || modelType) {
        payload.ai_model = {
          name: val("name"),
          version: val("version", "1.0"),
          framework: framework,
          model_type: modelType,
          owner: "anagha27",
          location: modelLocation,
          license: val("license", "Apache 2.0"),
          test_accuracy: testAccuracy,
        };
      }

      const resp = await fetch(`${PATRA_BACKEND}/v1/assets/model-cards`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Tapis-Token": token,
        },
        body: JSON.stringify(payload),
      });

      const data = await resp.json();

      if (resp.ok && data.created) {
        setPublishResult({
          success: true,
          message: `Published! Asset ID: ${data.asset_id} — UUID: ${data.asset_uuid}`,
        });
        set("patra_uuid", data.asset_uuid);
      } else if (data.duplicate) {
        setPublishResult({ success: false, message: "Model card already exists with this name/version/author." });
      } else {
        setPublishResult({ success: false, message: `Failed: ${JSON.stringify(data)}` });
      }
    } catch (e) {
      setPublishResult({ success: false, message: `Error: ${e}` });
    } finally {
      setPublishing(false);
    }
  };

  return (
    <Tabs defaultValue="model">
      <Tabs.List mb="md">
        <Tabs.Tab value="model"   leftSection={<IconDatabase size={14} />}>Model Card</Tabs.Tab>
        <Tabs.Tab value="secrets" leftSection={<IconKey size={14} />}>API Keys</Tabs.Tab>
      </Tabs.List>

      {/* ── Tab 1: Model Card ─────────────────────────────── */}
      <Tabs.Panel value="model">
        <Stack gap="md">
          <Alert icon={<IconInfoCircle size={14} />} color="blue" variant="light">
            <Text size="xs">
              Publish your trained model to the PATRA knowledge base so other researchers can discover and use it.
            </Text>
          </Alert>

          <Group grow>
            <TextInput
              label="Model Name"
              description="Human-readable name people will search for"
              placeholder="beans-disease-classifier"
              required
              value={String(val("name", ""))}
              onChange={(e) => set("name", e.currentTarget.value)}
            />
            <TextInput
              label="Version"
              placeholder="1.0"
              value={String(val("version", "1.0"))}
              onChange={(e) => set("version", e.currentTarget.value)}
            />
          </Group>

          <Select
            label="Category"
            description="Task or domain this model belongs to"
            data={CATEGORIES}
            value={String(val("category", "classification"))}
            onChange={(v) => set("category", v ?? "classification")}
            allowDeselect={false}
            required
          />

          <TextInput
            label="Input Type"
            placeholder="image"
            value={String(val("input_type", "image"))}
            onChange={(e) => set("input_type", e.currentTarget.value)}
            required
          />

          <Textarea
            label="Short Description"
            description="One or two sentence summary shown on cards and search results"
            placeholder="Bean leaf disease classifier trained on OSC..."
            rows={2}
            value={String(val("short_description", ""))}
            onChange={(e) => set("short_description", e.currentTarget.value)}
            required
          />

          <Textarea
            label="Full Description"
            description="Full write-up shown on the model detail page"
            rows={4}
            value={String(val("full_description", ""))}
            onChange={(e) => set("full_description", e.currentTarget.value)}
          />

          <TextInput
            label="Keywords"
            description="Comma-separated terms e.g. classification, plant disease, beans"
            placeholder="classification, plant disease, beans"
            value={String(val("keywords", ""))}
            onChange={(e) => set("keywords", e.currentTarget.value)}
          />

          <Divider label="AI Model Details" labelPosition="left" />

          <Group grow>
            <TextInput
              label="Framework"
              placeholder="PyTorch / HuggingFace Transformers"
              value={String(val("framework", ""))}
              onChange={(e) => set("framework", e.currentTarget.value)}
            />
            <TextInput
              label="Model Architecture"
              placeholder="Vision Transformer (ViT)"
              value={String(val("model_type", ""))}
              onChange={(e) => set("model_type", e.currentTarget.value)}
            />
          </Group>

          <TextInput
            label="Model Weights URL"
            description="Link to download the trained model weights e.g. HuggingFace Hub URL"
            placeholder="https://huggingface.co/username/model-name"
            value={String(val("model_location", ""))}
            onChange={(e) => set("model_location", e.currentTarget.value)}
          />

          {/* Load from metrics button */}
          {config.metrics && (
            <Button
              variant="light"
              size="xs"
              leftSection={<IconDownload size={14} />}
              onClick={async () => {
                try {
                  const metricsPath = String(config.metrics);
                  const res = await fetch(
                    `http://localhost:8002/api/tapis-files/content?system=pitzer-tapis&path=${encodeURIComponent(metricsPath + "/metrics.json")}`,
                    { credentials: "include" }
                  );
                  const data = await res.json();
                  if (data.metrics?.eval_accuracy) {
                    set("test_accuracy", data.metrics.eval_accuracy);
                    set("short_description", 
                      `${String(val("name", "Model"))} trained on ${data.metadata?.dataset?.split("/").pop() || "dataset"}. Achieves ${(data.metrics.eval_accuracy * 100).toFixed(1)}% accuracy.`
                    );
                  }
                } catch (e) {
                  console.error("Failed to load metrics:", e);
                }
              }}
            >
              Load from Training Results
            </Button>
          )}

          <Group grow>
            <NumberInput
              label="Test Accuracy"
              description="Value between 0 and 1"
              min={0}
              max={1}
              decimalScale={4}
              value={Number(val("test_accuracy", 0))}
              onChange={(v) => set("test_accuracy", v)}
            />
            <TextInput
              label="License"
              placeholder="Apache 2.0"
              value={String(val("license", "Apache 2.0"))}
              onChange={(e) => set("license", e.currentTarget.value)}
            />
          </Group>

          <Switch
            label="Private"
            description="Private records are restricted to your organization"
            checked={Boolean(val("is_private", false))}
            onChange={(e) => set("is_private", e.currentTarget.checked)}
          />

          <Divider />

          {/* Publish button */}
          <Button
            onClick={handlePublish}
            loading={publishing}
            color="blue"
            fullWidth
          >
            Publish to PATRA
          </Button>

          {/* Result */}
          {publishResult && (
            <Paper withBorder p="sm" radius="md" bg={publishResult.success ? "green.0" : "red.0"}>
              <Group gap="xs">
                {publishResult.success
                  ? <IconCheck size={16} color="green" />
                  : <IconX size={16} color="red" />
                }
                <Text size="xs" c={publishResult.success ? "green.7" : "red.7"}>
                  {publishResult.message}
                </Text>
              </Group>
              {publishResult.success && (
                <Badge
                  mt={6}
                  size="xs"
                  variant="light"
                  color="blue"
                  component="a"
                  href="https://patra.pods.icicleai.tapis.io/modelcards"
                  target="_blank"
                  style={{ cursor: "pointer" }}
                >
                  View on PATRA →
                </Badge>
              )}
            </Paper>
          )}
        </Stack>
      </Tabs.Panel>

      {/* ── Tab 2: API Keys ──────────────────────────────── */}
      <Tabs.Panel value="secrets">
        <Stack gap="md">
          <Alert icon={<IconKey size={14} />} color="yellow" variant="light">
            <Text size="xs">
              Your Tapis token is used to authenticate with PATRA. Get a fresh token from icicleai.tapis.io.
            </Text>
          </Alert>

          <PasswordInput
            label="Tapis Token"
            description="Your JWT token from icicleai.tapis.io"
            placeholder="eyJ..."
            value={String(val("tapis_token", ""))}
            onChange={(e) => set("tapis_token", e.currentTarget.value)}
          />
        </Stack>
      </Tabs.Panel>
    </Tabs>
  );
}