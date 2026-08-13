import { Group, Button } from "@mantine/core";
import { IconFolder, IconActivity } from "@tabler/icons-react";
import { useNavigate, useLocation } from "react-router";

// Persistent "Templates" / "Runs" navigation, shown in every page's header
// (see _index.tsx, templates.tsx, runs.tsx, runs.$runId.tsx, WorkflowCanvas.tsx)
// so switching between them never requires going back to the dashboard
// first. The active section is highlighted from the current route; both
// /templates/... (the canvas editor) and /runs/... (a run's detail page)
// count as "on" their respective section.
export default function TopNav() {
  const navigate = useNavigate();
  const location = useLocation();
  const onTemplates = location.pathname.startsWith("/templates");
  const onRuns = location.pathname.startsWith("/runs");

  return (
    <Group gap={4}>
      <Button
        size="xs"
        variant={onTemplates ? "light" : "subtle"}
        color={onTemplates ? "blue" : "gray"}
        leftSection={<IconFolder size={14} />}
        onClick={() => navigate("/templates")}
      >
        Templates
      </Button>
      <Button
        size="xs"
        variant={onRuns ? "light" : "subtle"}
        color={onRuns ? "teal" : "gray"}
        leftSection={<IconActivity size={14} />}
        onClick={() => navigate("/runs")}
      >
        Runs
      </Button>
    </Group>
  );
}
