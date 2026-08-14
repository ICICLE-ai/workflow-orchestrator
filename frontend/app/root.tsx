import {
  isRouteErrorResponse,
  Links,
  Meta,
  Outlet,
  Scripts,
  ScrollRestoration,
} from "react-router";

import type { Route } from "./+types/root";
import "./app.css";
import "@mantine/core/styles.css";
import "@xyflow/react/dist/style.css";
import { MantineProvider, ColorSchemeScript, Button, Group, Text, Paper } from "@mantine/core";
import { useEffect, useState } from "react";
import { fetchCurrentUser, loginUrl, logout, type CurrentUser } from "./lib/api";
import { hostOwnsAuth } from "./lib/embed";
import { Notifications } from "@mantine/notifications";

export const links: Route.LinksFunction = () => [
  { rel: "preconnect", href: "https://fonts.googleapis.com" },
  {
    rel: "preconnect",
    href: "https://fonts.gstatic.com",
    crossOrigin: "anonymous",
  },
  {
    rel: "stylesheet",
    href: "https://fonts.googleapis.com/css2?family=Inter:ital,opsz,wght@0,14..32,100..900;1,14..32,100..900&display=swap",
  },
];

export function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <meta charSet="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <Meta />
        <Links />
        <ColorSchemeScript />
      </head>
      <body>
        {children}
        <ScrollRestoration />
        <Scripts />
      </body>
    </html>
  );
}

// Small fixed control showing the signed-in Tapis user, with login/logout.
// Sits above the per-route AppShell headers so it's visible everywhere.
//
// Embedded in TapisUI the host owns the session: the widget shows who you are
// but offers no login or logout, since both would act on this app's session
// while the host's X-Tapis-Token cookie keeps deciding who you actually are.
function AuthWidget() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [embedded, setEmbedded] = useState(false);

  useEffect(() => {
    // hostOwnsAuth() reads document.cookie / window.top, so it can only run
    // after hydration — not during the SSR render.
    setEmbedded(hostOwnsAuth());
    fetchCurrentUser().then((u) => {
      setUser(u);
      if (u?.auth_mode === "tapis-token") setEmbedded(true);
      setLoaded(true);
    });
  }, []);

  if (!loaded) return null;

  // Embedded and unauthenticated means the host's token was missing or rejected.
  // Nothing this app can do about it, and a "Login with Tapis" button here would
  // lead into a frame Tapis won't render — so stay out of the way.
  if (embedded && !user) return null;

  return (
    <Paper
      shadow="xs"
      p={6}
      radius="md"
      withBorder
      style={{ position: "fixed", bottom: 10, left: 12, zIndex: 1000 }}
    >
      {user ? (
        <Group gap="xs">
          <Text size="sm" fw={500}>{user.username}</Text>
          {!embedded && (
            <Button size="compact-xs" variant="light" color="gray" onClick={() => logout()}>
              Logout
            </Button>
          )}
        </Group>
      ) : (
        <Button size="compact-xs" variant="light" component="a" href={loginUrl()}>
          Login with Tapis
        </Button>
      )}
    </Paper>
  );
}

export default function App() {
  return (

    <MantineProvider defaultColorScheme="light">
      <Notifications />
        <AuthWidget />
        <Outlet />
    </MantineProvider>


  );
}

export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  let message = "Oops!";
  let details = "An unexpected error occurred.";
  let stack: string | undefined;

  if (isRouteErrorResponse(error)) {
    message = error.status === 404 ? "404" : "Error";
    details =
      error.status === 404
        ? "The requested page could not be found."
        : error.statusText || details;
  } else if (import.meta.env.DEV && error && error instanceof Error) {
    details = error.message;
    stack = error.stack;
  }

  return (
    <main className="pt-16 p-4 container mx-auto">
      <h1>{message}</h1>
      <p>{details}</p>
      {stack && (
        <pre className="w-full p-4 overflow-x-auto">
          <code>{stack}</code>
        </pre>
      )}
    </main>
  );
}
