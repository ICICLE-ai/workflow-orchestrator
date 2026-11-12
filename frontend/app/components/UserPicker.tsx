import { useEffect, useRef, useState } from "react";
import { Autocomplete, Button, Group } from "@mantine/core";
import { IconUserPlus } from "@tabler/icons-react";
import { apiFetch } from "../lib/api";

export type UserBrief = { user_id: number; username: string; email?: string | null };

// Username field with as-you-type suggestions from /api/users/search, used by
// the group member list and the share dialog. Only users who have signed in at
// least once exist server-side, so suggestions are also the set that can be
// added — but a typed username is still accepted and validated on submit.
export default function UserPicker({
  onPick,
  exclude = [],
  label = "Add",
  placeholder = "Search by username or email",
  loading = false,
}: {
  onPick: (username: string) => void | Promise<void>;
  exclude?: string[];
  label?: string;
  placeholder?: string;
  loading?: boolean;
}) {
  const [value, setValue] = useState("");
  const [options, setOptions] = useState<UserBrief[]>([]);
  // Enter on a highlighted suggestion fires both onOptionSubmit and our own
  // onKeyDown; this keeps that from adding the same person twice.
  const submitting = useRef(false);

  useEffect(() => {
    const q = value.trim();
    if (q.length < 2) {
      setOptions([]);
      return;
    }
    // Debounced, and ignores responses for a query the user has typed past.
    let stale = false;
    const timer = setTimeout(async () => {
      const res = await apiFetch(`/api/users/search?q=${encodeURIComponent(q)}`);
      if (!stale && res.ok) setOptions(await res.json());
    }, 200);
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [value]);

  const submit = async (username: string) => {
    const name = username.trim();
    if (!name || submitting.current) return;
    submitting.current = true;
    try {
      await onPick(name);
      setValue("");
    } finally {
      submitting.current = false;
    }
  };

  return (
    <Group gap="xs" wrap="nowrap" align="flex-end">
      <Autocomplete
        style={{ flex: 1 }}
        placeholder={placeholder}
        value={value}
        onChange={setValue}
        // Plain usernames: Mantine writes the picked option's label back into
        // the input, and that text is what gets submitted.
        data={options.map((u) => u.username).filter((name) => !exclude.includes(name))}
        onOptionSubmit={(v) => submit(v)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && value.trim()) {
            e.preventDefault();
            submit(value);
          }
        }}
      />
      <Button
        variant="light"
        leftSection={<IconUserPlus size={16} />}
        disabled={!value.trim()}
        loading={loading}
        onClick={() => submit(value)}
      >
        {label}
      </Button>
    </Group>
  );
}
