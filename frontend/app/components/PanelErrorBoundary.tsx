import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { Alert, Button, Code, Group, ScrollArea, Stack, Text } from "@mantine/core";
import { IconAlertTriangle, IconArrowBackUp, IconRefresh } from "@tabler/icons-react";

// Containment boundary for a step's settings panel (registry.ts). Panels are
// where this app integrates the most third-party surface — the @icicle-ai
// canvas/annotation/file-explorer components, Patra, the OpenCV playground —
// and any of them can throw on data shaped differently than it expects. Without
// a boundary, one such throw unmounts the ENTIRE React tree: the modal, the
// canvas underneath, and the router with it, leaving the bare "Oops!" screen
// and no way back except a reload (which loses unsaved canvas edits).
//
// Scoped here rather than at the route so the blast radius is exactly one
// panel: the canvas keeps rendering behind the modal, and "Back to canvas"
// closes the modal and returns to it with the workflow intact.
//
// Caveat worth knowing when a panel misbehaves and this DOESN'T appear: React
// error boundaries only catch errors thrown during render, in lifecycle
// methods, and in constructors below them. An error inside an event handler, a
// setTimeout, or an un-awaited promise never reaches here — those still need
// their own try/catch in the panel (which is why the panels' fetch helpers
// catch and surface failures themselves).
export default class PanelErrorBoundary extends Component<
  {
    children: ReactNode;
    // Shown in the fallback, so a user reporting the problem can say which
    // panel broke without reading a stack trace.
    stepName: string;
    // Dismiss the host modal. This is the "get me out" action, so it must not
    // depend on any of the crashed panel's own state.
    onClose: () => void;
    // Changing this clears a captured error and retries the panel. The host
    // passes the node identity, so opening a DIFFERENT step after one crashed
    // starts clean instead of inheriting the previous failure.
    resetKey?: unknown;
  },
  { error: Error | null; componentStack: string | null }
> {
  state: { error: Error | null; componentStack: string | null } = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The fallback below shows the message; the component stack is the part
    // that actually locates the fault inside a bundled dependency, so keep it
    // in the console where a developer can expand it.
    console.error(`[step panel] "${this.props.stepName}" crashed:`, error, info.componentStack);
    this.setState({ componentStack: info.componentStack ?? null });
  }

  componentDidUpdate(prev: { resetKey?: unknown }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null, componentStack: null });
    }
  }

  private retry = () => this.setState({ error: null, componentStack: null });

  render() {
    const { error, componentStack } = this.state;
    if (!error) return this.props.children;

    return (
      <Stack gap="md" p="lg" maw={800} mx="auto">
        <Alert
          variant="light"
          color="red"
          icon={<IconAlertTriangle size={18} />}
          title={`The "${this.props.stepName}" panel stopped responding`}
        >
          <Stack gap="xs">
            <Text size="sm">
              Something in this step's interface failed while rendering. Your workflow is unaffected — nothing
              was saved or lost, and the canvas is still open behind this dialog.
            </Text>
            <Code block style={{ whiteSpace: "pre-wrap" }}>
              {error.message || String(error)}
            </Code>
            <Text size="xs" c="dimmed">
              If this step loads a file, the most likely cause is data shaped differently than the panel
              expects. Retrying with a different file or input often works; the browser console has the full
              stack trace.
            </Text>
          </Stack>
        </Alert>

        {componentStack && (
          <ScrollArea.Autosize mah={200}>
            <Code block style={{ fontSize: 11, whiteSpace: "pre-wrap" }}>
              {componentStack.trim()}
            </Code>
          </ScrollArea.Autosize>
        )}

        <Group>
          <Button leftSection={<IconArrowBackUp size={16} />} onClick={this.props.onClose}>
            Back to canvas
          </Button>
          {/* Worth offering: a crash driven by transient state (a half-loaded
              image, a race on first mount) frequently doesn't recur, and a
              retry is cheaper than reopening the step. A deterministic one just
              lands back here. */}
          <Button variant="default" leftSection={<IconRefresh size={16} />} onClick={this.retry}>
            Try again
          </Button>
        </Group>
      </Stack>
    );
  }
}
