import { useEffect, useRef, useState, type ReactNode, type SyntheticEvent } from "react";

// Drop-in replacement for Mantine's <CopyButton> (same render-prop shape).
//
// Mantine's copy is navigator.clipboard only, and fails SILENTLY when that's
// unavailable — which is the normal case here, not an edge case:
//   * embedded in TapisUI, the iframe has no `clipboard-write` permission, so
//     writeText rejects;
//   * served over plain http on anything but localhost (a LAN IP, say),
//     navigator.clipboard is undefined outright.
// So copy with execCommand("copy") on a temporary textarea FIRST — it runs
// synchronously inside the click, which iframes allow and which keeps the
// user activation Safari insists on (awaiting a rejected clipboard promise
// first can spend it). Then navigator.clipboard, and as a last resort a
// prompt() the user can copy from by hand. The button never just does nothing.
function execCommandCopy(text: string, anchor?: Element | null): boolean {
  // The textarea goes next to the clicked button rather than on <body>: a
  // Mantine Drawer/Modal traps focus, and a textarea outside the trap can't be
  // focused/selected, so execCommand would copy nothing.
  const host = anchor?.parentElement ?? document.body;
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  Object.assign(ta.style, { position: "fixed", top: "0", left: "0", opacity: "0" });
  host.appendChild(ta);
  try {
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    ta.remove();
    if (anchor instanceof HTMLElement) anchor.focus();
  }
}

export async function copyText(text: string, anchor?: Element | null): Promise<boolean> {
  if (execCommandCopy(text, anchor)) return true;
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permission-policy rejection — fall through to the manual prompt.
  }
  window.prompt("Copy to clipboard: Ctrl+C / Cmd+C, then Enter", text);
  return false;
}

export default function SafeCopyButton({
  value,
  timeout = 1000,
  children,
}: {
  value: string;
  timeout?: number;
  children: (payload: { copied: boolean; copy: (e?: SyntheticEvent) => void }) => ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = (e?: SyntheticEvent) => {
    const anchor = e?.currentTarget instanceof Element ? e.currentTarget : null;
    copyText(value, anchor).then((ok) => {
      if (!ok) return;
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), timeout);
    });
  };

  return <>{children({ copied, copy })}</>;
}
