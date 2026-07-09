import { type RouteConfig, index, route } from "@react-router/dev/routes";

export default [
  index("routes/_index.tsx"),
  route("templates", "routes/templates.tsx"),
  route("templates/new", "routes/WorkflowCanvas.tsx", { id: "template-new" }),
  route("templates/:id/edit", "routes/WorkflowCanvas.tsx", { id: "template-edit" }),
  route("runs", "routes/runs.tsx"),
  route("runs/:runId", "routes/runs.$runId.tsx"),
] satisfies RouteConfig;
