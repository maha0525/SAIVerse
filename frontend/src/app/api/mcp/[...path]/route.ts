/**
 * Next.js Route Handler: pass-through proxy for /api/mcp/*.
 *
 * MCP API は JSON のみで、/api/addon/[...path] のような Range/Media
 * 特殊対応は不要。シンプルな透過プロキシ。
 */
import type { NextRequest } from "next/server";

const BACKEND = process.env.SAIVERSE_BACKEND_URL ?? "http://127.0.0.1:8000";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const STRIP_REQUEST_HEADERS = new Set([
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "accept-encoding",
]);

const STRIP_RESPONSE_HEADERS = new Set([
    "content-encoding",
    "transfer-encoding",
    "connection",
]);

function filterHeaders(src: Headers, strip: Set<string>): Headers {
    const out = new Headers();
    src.forEach((value, key) => {
        if (!strip.has(key.toLowerCase())) {
            out.set(key, value);
        }
    });
    return out;
}

async function proxy(
    req: NextRequest,
    context: { params: Promise<{ path: string[] }> },
): Promise<Response> {
    const { path } = await context.params;
    const upstream = new URL(
        `/api/mcp/${path.map(encodeURIComponent).join("/")}`,
        BACKEND,
    );
    upstream.search = new URL(req.url).search;

    const method = req.method.toUpperCase();
    const headers = filterHeaders(req.headers, STRIP_REQUEST_HEADERS);
    const hasBody = method !== "GET" && method !== "HEAD";

    try {
        const init: RequestInit & { duplex?: "half" } = {
            method,
            headers,
            signal: req.signal,
            redirect: "manual",
        };
        if (hasBody) {
            init.body = req.body as BodyInit | null;
            init.duplex = "half";
        }
        const upstreamResp = await fetch(upstream, init);
        const respHeaders = filterHeaders(upstreamResp.headers, STRIP_RESPONSE_HEADERS);
        respHeaders.delete("connection");
        respHeaders.delete("keep-alive");
        return new Response(upstreamResp.body, {
            status: upstreamResp.status,
            statusText: upstreamResp.statusText,
            headers: respHeaders,
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error(
            "[mcp-proxy] upstream fetch failed:",
            method,
            upstream.pathname,
            msg,
        );
        return new Response(`upstream fetch failed: ${msg}`, { status: 502 });
    }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
export const PATCH = proxy;
export const HEAD = proxy;
export const OPTIONS = proxy;
