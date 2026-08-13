import { useEffect, useState } from "react";
import {
  Stack, Group, Text, Title, NumberInput, TextInput, Select, ScrollArea, Badge, Alert,
} from "@mantine/core";
import { IconAlertTriangle } from "@tabler/icons-react";
import type { StepPanelProps } from "./types";
import { apiFetch } from "../lib/api";
import { splitTapisUri } from "../lib/tapis";
import ParamSection from "../components/ParamSection";

// Settings panel for the 'mission_export' step (backend/steps/mission_export/step.json)
// — converts a platform-agnostic flight_plan.json into drone/GCS-specific
// mission files (one per charge cycle), in up to four formats.
//
// The one thing this panel does beyond a plain form: most parameters only
// apply to SOME of the four output formats (sprayer PWM is ArduPilot+PX4
// only, hover/cruise speed is PX4 only, the enum/tag fields are DJI only).
// Each section greys itself out — via ParamSection's `disabled` — when the
// currently selected --format can't use it, rather than showing a flat list
// where half the fields silently do nothing.
//
// Registered in registry.ts under the key "mission_export".
export default function MissionExportPanel({ config, onChange, step, connectedInputs }: StepPanelProps) {
  const val = (key: string) => config[key] !== undefined ? config[key] : step.config_schema[key]?.default;
  const set = (key: string, value: unknown) => onChange({ ...config, [key]: value });

  const format = String(val("format") ?? "all");
  const usesFormat = (...formats: string[]) => format === "all" || formats.includes(format);

  const sprayerServoChannel = Number(val("sprayer_servo_channel") ?? 9);
  const sprayerPwmOn = Number(val("sprayer_pwm_on") ?? 1900);
  const sprayerPwmOff = Number(val("sprayer_pwm_off") ?? 1100);
  const hoverSpeedMps = Number(val("hover_speed_mps") ?? 5.0);
  const cruiseSpeedRaw = config["cruise_speed_mps"];
  const cruiseSpeedMps = cruiseSpeedRaw === undefined || cruiseSpeedRaw === "" ? null : Number(cruiseSpeedRaw);

  // Best-effort preview of the wired flight_plan.json — how many charge
  // cycles / total waypoints it has. Purely informational: fetched directly
  // via the generic Tapis-files proxy (system+path, same as the old
  // shapefile viewer used), silently absent if there's no run yet, the
  // upstream step hasn't produced a value, or the fetch fails.
  const flightPlanPort = step.inputs.find((p) => p.data_type === "json_results")?.port_name;
  const wiredUri = flightPlanPort ? String(connectedInputs[flightPlanPort]?.config?.path || "") : "";
  const [planSummary, setPlanSummary] = useState<{ cycles: number; waypoints: number } | null>(null);
  useEffect(() => {
    setPlanSummary(null);
    const parts = splitTapisUri(wiredUri);
    if (!parts) return;
    let cancelled = false;
    apiFetch(`/api/tapis-files/content?system=${encodeURIComponent(parts.system)}&path=${encodeURIComponent(parts.path)}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled || !data || !Array.isArray(data.cycles)) return;
        const waypoints = data.cycles.reduce((sum: number, c: any) => sum + (Array.isArray(c) ? c.length : 0), 0);
        setPlanSummary({ cycles: data.cycles.length, waypoints });
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [wiredUri]);

  return (
    <ScrollArea style={{ height: "100%" }}>
      <Stack gap="lg" p="lg" maw={980} mx="auto">
        <div>
          <Title order={3}>📤 Mission Export Adapter</Title>
          <Text size="sm" c="dimmed" mt={4}>
            Converts flight_plan.json into one mission file per charge cycle, per format. Sections below grey
            out when they don't apply to the currently selected format.
          </Text>
        </div>

        <ParamSection
          title="Format & output"
          explainer="Which platform format(s) to generate. Each format writes one file per charge cycle into its own subfolder under the output directory."
          diagram={<FormatBadges format={format} />}
        >
          <Select
            label="Format"
            data={[
              { value: "all", label: "All formats" },
              { value: "ardupilot", label: "ArduPilot (.waypoints)" },
              { value: "px4", label: "PX4 (.plan)" },
              { value: "generic_csv", label: "Generic CSV" },
              { value: "dji", label: "DJI (.kmz)" },
            ]}
            value={format}
            onChange={(v) => set("format", v ?? "all")}
            allowDeselect={false}
          />
          {planSummary && (
            <Text size="xs" c="dimmed">
              Wired flight plan: <b>{planSummary.cycles}</b> charge cycle{planSummary.cycles === 1 ? "" : "s"},{" "}
              <b>{planSummary.waypoints}</b> waypoints total.
            </Text>
          )}
        </ParamSection>

        <ParamSection
          title="Sprayer control"
          explainer="Both ArduPilot and PX4 fire the sprayer via MAV_CMD_DO_SET_SERVO — toggling this channel between the two PWM values gets embedded directly into those mission files. Values must match how your sprayer relay is actually wired."
          diagram={<SprayerPwmDiagram channel={sprayerServoChannel} pwmOn={sprayerPwmOn} pwmOff={sprayerPwmOff} />}
          disabled={!usesFormat("ardupilot", "px4")}
          disabledHint="Only used by: ArduPilot, PX4"
        >
          <NumberInput label="Servo/relay channel" min={1} value={sprayerServoChannel}
            onChange={(v) => set("sprayer_servo_channel", Number(v) || 0)} />
          <Group grow>
            <NumberInput label="PWM on (µs)" min={1000} max={2000} value={sprayerPwmOn}
              onChange={(v) => set("sprayer_pwm_on", Number(v) || 0)} />
            <NumberInput label="PWM off (µs)" min={1000} max={2000} value={sprayerPwmOff}
              onChange={(v) => set("sprayer_pwm_off", Number(v) || 0)} />
          </Group>
        </ParamSection>

        <ParamSection
          title="Flight speed"
          explainer="PX4-specific: hover speed drives vertical/hovering maneuvers, cruise speed drives horizontal flight. Leave cruise speed blank to derive it automatically from the flight plan's own meta.speed_mph instead of setting it explicitly."
          diagram={<SpeedDiagram hover={hoverSpeedMps} cruise={cruiseSpeedMps} />}
          disabled={!usesFormat("px4")}
          disabledHint="Only used by: PX4"
        >
          <NumberInput label="Hover speed (m/s)" min={0.1} decimalScale={1} value={hoverSpeedMps}
            onChange={(v) => set("hover_speed_mps", Number(v) || 0)} />
          <NumberInput label="Cruise speed (m/s) — optional" min={0.1} decimalScale={1}
            placeholder="auto from meta.speed_mph"
            value={cruiseSpeedMps ?? ""}
            onChange={(v) => set("cruise_speed_mps", v === "" ? "" : Number(v) || 0)} />
        </ParamSection>

        <ParamSection
          title="DJI (.kmz) — unverified placeholders"
          explainer="DJI's public docs cover camera/gimbal actions, not Agras spray control, so these values are guesses: DJI Pilot 2 will likely reject the import until the enum values and tag names match your actual aircraft's real WPML export."
          diagram={null}
          disabled={!usesFormat("dji")}
          disabledHint="Only used by: DJI"
        >
          <Alert icon={<IconAlertTriangle size={14} />} color="yellow" py={6}>
            <Text size="xs">
              Only confirmed example found: DJI M30 = enum 67 (from DJI's own docs). Agras values aren't publicly
              documented — confirm against a real DJI Pilot 2 / Agras export before relying on these.
            </Text>
          </Alert>
          <Group grow>
            <NumberInput label="Drone enum value" min={0} value={Number(val("drone_enum_value") ?? 0)}
              onChange={(v) => set("drone_enum_value", Number(v) || 0)} />
            <NumberInput label="Drone sub-enum value" min={0} value={Number(val("drone_sub_enum_value") ?? 0)}
              onChange={(v) => set("drone_sub_enum_value", Number(v) || 0)} />
            <NumberInput label="Payload enum value" min={0} value={Number(val("payload_enum_value") ?? 0)}
              onChange={(v) => set("payload_enum_value", Number(v) || 0)} />
          </Group>
          <Group grow>
            <TextInput label="Spray action tag" value={String(val("spray_action_tag") ?? "")}
              onChange={(e) => set("spray_action_tag", e.currentTarget.value)} />
            <TextInput label="Spray param tag" value={String(val("spray_param_tag") ?? "")}
              onChange={(e) => set("spray_param_tag", e.currentTarget.value)} />
          </Group>
        </ParamSection>
      </Stack>
    </ScrollArea>
  );
}

// Full-height scrollable layout, matching flightPlan.tsx (see StepSettingsModal,
// which honors this static flag).
(MissionExportPanel as any).fullScreen = true;

// --- helpers -------------------------------------------------------------

const ALL_FORMATS = [
  { key: "ardupilot", label: "ArduPilot", ext: ".waypoints" },
  { key: "px4", label: "PX4", ext: ".plan" },
  { key: "generic_csv", label: "CSV", ext: ".csv" },
  { key: "dji", label: "DJI", ext: ".kmz" },
];

function FormatBadges({ format }: { format: string }) {
  const active = format === "all" ? ALL_FORMATS.map((f) => f.key) : [format];
  return (
    <Stack gap={6}>
      {ALL_FORMATS.map((f) => (
        <Badge key={f.key} variant={active.includes(f.key) ? "filled" : "outline"}
          color={active.includes(f.key) ? "cyan" : "gray"} size="sm">
          {f.label} {f.ext}
        </Badge>
      ))}
    </Stack>
  );
}

// PWM range bar (1000-2000µs) with the configured on/off values marked, and a
// small marker animating back and forth between them to suggest the valve
// toggling — proportioned from the real configured values, not fabricated.
function SprayerPwmDiagram({ channel, pwmOn, pwmOff }: { channel: number; pwmOn: number; pwmOff: number }) {
  const W = 200;
  const clamp = (v: number) => Math.min(2000, Math.max(1000, v));
  const toX = (v: number) => ((clamp(v) - 1000) / 1000) * W;
  const onX = toX(pwmOn);
  const offX = toX(pwmOff);

  return (
    <Stack gap={4} align="center">
      <svg width={W + 20} height={50}>
        <line x1={10} y1={25} x2={W + 10} y2={25} stroke="#cbd5e1" strokeWidth={4} strokeLinecap="round" />
        <circle cx={10 + offX} cy={25} r={5} fill="#94a3b8" />
        <circle cx={10 + onX} cy={25} r={5} fill="#16a34a" />
        <circle r={5} fill="#0891b2">
          <animate attributeName="cx" values={`${10 + offX};${10 + onX};${10 + offX}`} dur="2.4s" repeatCount="indefinite" />
          <animate attributeName="cy" values="10;10;10" dur="2.4s" repeatCount="indefinite" />
        </circle>
      </svg>
      <Text size="xs" c="dimmed">channel {channel} · {pwmOff}µs off → {pwmOn}µs on</Text>
    </Stack>
  );
}

// Two relative-fill bars (hover vs cruise speed), scaled against a fixed
// illustrative max — cruise shown greyed/"auto" when left blank.
function SpeedDiagram({ hover, cruise }: { hover: number; cruise: number | null }) {
  const illustrativeMaxMps = 15;
  const hoverPct = Math.min(100, Math.max(2, (hover / illustrativeMaxMps) * 100));
  const cruisePct = cruise != null ? Math.min(100, Math.max(2, (cruise / illustrativeMaxMps) * 100)) : 0;

  return (
    <Stack gap={8} w={180}>
      <div>
        <Text size="xs" c="dimmed" mb={2}>Hover</Text>
        <div style={{ height: 8, background: "#e2e8f0", borderRadius: 4, overflow: "hidden" }}>
          <div style={{ width: `${hoverPct}%`, height: "100%", background: "#0891b2", transition: "width 200ms ease" }} />
        </div>
      </div>
      <div>
        <Text size="xs" c="dimmed" mb={2}>Cruise {cruise == null && "(auto)"}</Text>
        <div style={{ height: 8, background: "#e2e8f0", borderRadius: 4, overflow: "hidden" }}>
          <div style={{
            width: `${cruisePct}%`, height: "100%",
            background: cruise == null ? "#cbd5e1" : "#7c3aed", transition: "width 200ms ease",
          }} />
        </div>
      </div>
    </Stack>
  );
}
