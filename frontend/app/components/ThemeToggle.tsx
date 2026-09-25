import { ActionIcon, Tooltip, useMantineColorScheme, useComputedColorScheme } from "@mantine/core";
import { IconSun, IconMoon } from "@tabler/icons-react";

// Light/dark switch, shown in every page's header next to the settings
// icon (SecretsMenu on the dashboard) so the two controls read as a pair.
// Reads/writes via Mantine's color scheme manager, which persists the
// choice to localStorage.
export default function ThemeToggle() {
  const { setColorScheme } = useMantineColorScheme();
  const computedColorScheme = useComputedColorScheme("light");

  return (
    <Tooltip label={computedColorScheme === "dark" ? "Switch to light mode" : "Switch to dark mode"}>
      <ActionIcon
        onClick={() => setColorScheme(computedColorScheme === "dark" ? "light" : "dark")}
        variant="light"
        size="lg"
        radius="md"
        aria-label="Toggle color scheme"
      >
        {computedColorScheme === "dark" ? <IconSun size={20} /> : <IconMoon size={20} />}
      </ActionIcon>
    </Tooltip>
  );
}
