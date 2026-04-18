/**
 * Next.js Route Handler: generic pass-through proxy for /api/addon/*.
 *
 * Why this exists — a more specific replacement for the global `/api/:path*`
 * rewrite in `next.config.ts`:
 *
 *   - `<audio>` 要素は長めの音声ファイルを HTTP Range リクエスト
 *     (`Range: bytes=X-`) で分割ロードする。
 *   - しかし Next.js の `rewrites` 経由だと Range ヘッダや 206 Partial Content
 *     レスポンスが正しく中継されず、バッファが尽きた時点で再生が途中で停止する
 *     (特に Turbopack 下、2〜3 分程度の音声でも再現)。
 *   - ここで fetch → ReadableStream の素通しプロキシに置き換えることで、
 *     Range / 206 / Content-Range / Accept-Ranges をすべて維持する。
 *
 * 既存の `/api/addon/events/route.ts` (SSE 専用) は、より特定のパスなので
 * このキャッチオール Route Handler より優先される。
 */
import type { NextRequest } from "next/server";

const BACKEND = process.env.SAIVERSE_BACKEND_URL ?? "http://127.0.0.1:8000";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const maxDuration = 3600;

// リクエスト転送時に落とすヘッダ（hop-by-hop or fetch が自動付与するもの）。
const STRIP_REQUEST_HEADERS = new Set([
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "accept-encoding", // 自動再エンコードで Range 計算が狂うのを防ぐ
]);

// レスポンス側で取り除くヘッダ。content-length / content-encoding は Node の
// 自動再エンコードでズレるため明示排除。
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
        `/api/addon/${path.map(encodeURIComponent).join("/")}`,
        BACKEND,
    );
    // クエリ文字列もそのまま引き継ぐ
    const reqUrl = new URL(req.url);
    upstream.search = reqUrl.search;

    const method = req.method.toUpperCase();
    const headers = filterHeaders(req.headers, STRIP_REQUEST_HEADERS);

    const hasBody = method !== "GET" && method !== "HEAD";

    let upstreamResp: Response;
    try {
        const init: RequestInit & { duplex?: "half" } = {
            method,
            headers,
            signal: req.signal,
            // Next.js/Node の fetch は redirect を自動で追うので明示的に manual
            // (FastAPI が 307 を返すケースでクライアントに見せたい)。
            redirect: "manual",
        };
        if (hasBody) {
            init.body = req.body as BodyInit | null;
            // Node 18+ の fetch で ReadableStream を送るには duplex 必須。
            init.duplex = "half";
        }
        upstreamResp = await fetch(upstream, init);
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("[addon-proxy] upstream fetch failed:", method, upstream.pathname, msg);
        return new Response(`upstream fetch failed: ${msg}`, { status: 502 });
    }

    const respHeaders = filterHeaders(upstreamResp.headers, STRIP_RESPONSE_HEADERS);
    return new Response(
        method === "HEAD" ? null : upstreamResp.body,
        {
            status: upstreamResp.status,
            statusText: upstreamResp.statusText,
            headers: respHeaders,
        },
    );
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
export const PATCH = proxy;
export const HEAD = proxy;
export const OPTIONS = proxy;
