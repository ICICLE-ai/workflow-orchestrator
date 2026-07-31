// Direct-to-Tapis file operations, called from the browser instead of proxied
// through the backend's /api/tapis-files/* endpoints. Those endpoints remain
// the default for panels that only hold an app session (no raw Tapis token
// client-side) — but a panel like smart_labeler already fetches a raw token
// (see /api/tapis/token) to drive tapis-file-explorer's direct image reads, so
// there's no added exposure in also writing directly. This is a home for that
// kind of call, starting with upload; more backend-independent Tapis calls can
// move here over time as the frontend takes on more of them directly.

// Base URL of the Tapis v3 API, called directly. Matches the backend's own
// TAPIS_BASE_URL default (see backend/engine/tapis_auth.py). Set
// VITE_TAPIS_BASE_URL in frontend/.env to override; restart Vite after changing.
export const TAPIS_BASE_URL: string = (
  import.meta.env.VITE_TAPIS_BASE_URL ?? "https://icicleai.tapis.io"
).replace(/\/+$/, "");

// Tapis file paths may contain slashes; each segment must be encoded on its
// own so the slashes survive as path separators. Unlike
// @icicle-ai/tapis-file-explorer's own encodeTapisPath (which strips the
// leading slash before encoding), we normalize to exactly ONE leading slash
// and keep it — Tapis paths are meant to be absolute, and dropping it here
// was producing a path Tapis couldn't resolve.
function encodeTapisPath(path: string): string {
  const withLeadingSlash = "/" + path.replace(/^\/+/, "");
  return withLeadingSlash
    .split("/")
    .map(encodeURIComponent)
    .join("/");
}

export interface UploadTapisFileParams {
  system: string;
  path: string;
  content: string | Blob;
  token: string;
  contentType?: string;
}

// Write `content` to `path` on `system`, as the token's owner — the direct-
// to-Tapis equivalent of the backend's POST /api/tapis-files/upload. A
// multipart upload against Tapis' own Files API
// (POST /v3/files/ops/{system}/{path}).
export async function uploadTapisFile({
  system,
  path,
  content,
  token,
  contentType = "application/json",
}: UploadTapisFileParams): Promise<void> {
  const filename = path.replace(/\/+$/, "").split("/").pop() || "file";
  // encodeTapisPath already returns a leading "/", so no extra separator here.
  const url = `${TAPIS_BASE_URL}/v3/files/ops/${system}${encodeTapisPath(path)}`;
  const blob = typeof content === "string" ? new Blob([content], { type: contentType }) : content;
  const form = new FormData();
  form.append("file", blob, filename);

  const res = await fetch(url, {
    method: "POST",
    headers: { "X-Tapis-Token": token },
    body: form,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Tapis upload failed (HTTP ${res.status}): ${text.slice(0, 300)}`);
  }
}
