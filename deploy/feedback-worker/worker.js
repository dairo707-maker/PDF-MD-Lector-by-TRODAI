// Cloudflare Worker template for PDF/MD Lector anonymous support & bug reports.
// Deploy with: `npx wrangler secret put GITHUB_TOKEN` (never hardcode tokens here).

const MAX_BODY_BYTES = 32768;
const ALLOWED_KINDS = new Set(["bug", "suggestion", "crash", "error", "feedback"]);

const UNREDACTED_PATTERNS = [
  /(?:[A-Za-z]:[\\/]+|\\)Users[\\/]+(?!<usuario>)/i,
  /\/(?:Users|home)\/(?!<usuario>)[^\s/]+/i,
  /\\\\\?\\UNC[\\/](?!<equipo>)[A-Za-z0-9_.-]+/i,
  /(?<![A-Za-z0-9:/\\])(?:\\\\|\/\/)(?![?.<])[A-Za-z0-9_.-]+[\\/](?!<dato_privado>)[^\s\\/:*?"<>|]+/,
  /OneDrive[ \t]*-[ \t]*(?![ \t]*<organizacion>)/i,
  /[\w.+-]+@[\w-]+\.[\w.-]+/,
  /(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?::\d{1,5})?(?![\d.])/,
  /(?<![A-Za-z0-9:])\[?(?:(?:[0-9a-f]{1,4}:){7}[0-9a-f]{1,4}|(?:[0-9a-f]{1,4}:){1,7}:|(?:[0-9a-f]{1,4}:){1,6}:[0-9a-f]{1,4}|(?:[0-9a-f]{1,4}:){1,5}(?::[0-9a-f]{1,4}){1,2}|(?:[0-9a-f]{1,4}:){1,4}(?::[0-9a-f]{1,4}){1,3}|(?:[0-9a-f]{1,4}:){1,3}(?::[0-9a-f]{1,4}){1,4}|(?:[0-9a-f]{1,4}:){1,2}(?::[0-9a-f]{1,4}){1,5}|[0-9a-f]{1,4}:(?::[0-9a-f]{1,4}){1,6}|:(?::[0-9a-f]{1,4}){1,7}|::(?:ffff(?::0{1,4})?:)?(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}|(?:[0-9a-f]{1,4}:){1,4}:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})(?:%[A-Za-z0-9_.-]+)?\]?(?::\d{1,5})?(?![A-Za-z0-9:])/i,
  /(?:[A-Za-z]:[\\/]|\/)[^\s"'<>]+\.(?:pdf|md|markdown)\b/i,
  /(?<![\w.-])(?!(?<=<)documento\.(?:pdf|md|markdown)>)[\w.-]+\.(?:pdf|md|markdown)\b/i,
];

function corsHeaders(env) {
  return {
    "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json; charset=utf-8",
  };
}

function jsonResponse(env, status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: corsHeaders(env),
  });
}

function hasUnredactedPii(text) {
  for (const re of UNREDACTED_PATTERNS) {
    if (re.test(text)) {
      return true;
    }
  }
  return false;
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(env) });
    }

    if (request.method !== "POST") {
      return jsonResponse(env, 405, { error: "method_not_allowed" });
    }

    const contentLengthHeader = request.headers.get("Content-Length");
    if (contentLengthHeader && Number(contentLengthHeader) > MAX_BODY_BYTES) {
      return jsonResponse(env, 413, { error: "payload_too_large" });
    }

    const rawText = await request.text();
    const byteLength = new TextEncoder().encode(rawText).byteLength;
    if (byteLength > MAX_BODY_BYTES) {
      return jsonResponse(env, 413, { error: "payload_too_large" });
    }

    let payload;
    try {
      payload = JSON.parse(rawText);
    } catch {
      return jsonResponse(env, 400, { error: "invalid_json" });
    }

    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return jsonResponse(env, 400, { error: "invalid_schema" });
    }

    const title = typeof payload.title === "string" ? payload.title.trim() : "";
    const body = typeof payload.body === "string" ? payload.body.trim() : "";
    const kind = typeof payload.kind === "string" ? payload.kind.trim().toLowerCase() : "feedback";
    const version = typeof payload.version === "string" ? payload.version.trim() : "";

    if (!title || title.length > 200 || !body || body.length > 24000) {
      return jsonResponse(env, 400, { error: "invalid_fields" });
    }
    if (!ALLOWED_KINDS.has(kind) || version.length > 40) {
      return jsonResponse(env, 400, { error: "invalid_metadata" });
    }

    if (hasUnredactedPii(title) || hasUnredactedPii(body)) {
      return jsonResponse(env, 422, { error: "unredacted_pii_detected" });
    }

    if (!env.GITHUB_TOKEN) {
      return jsonResponse(env, 503, { error: "server_not_configured" });
    }

    const repo = env.GITHUB_REPO || "dairo707-maker/PDF-MD-Lector-by-TRODAI";
    const ghResponse = await fetch(`https://api.github.com/repos/${repo}/issues`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "PDF-MD-Lector-Feedback-Worker",
      },
      body: JSON.stringify({
        title: `[${kind}] ${title}`,
        body,
        labels: ["feedback", kind],
      }),
    });

    if (!ghResponse.ok) {
      return jsonResponse(env, 502, { error: "upstream_rejected", status: ghResponse.status });
    }

    const created = await ghResponse.json();
    return jsonResponse(env, 201, {
      ok: true,
      issue_number: created.number,
      html_url: created.html_url,
    });
  },
};
