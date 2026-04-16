/**
 * Next.js Route Handler that streams SSE from the FastAPI backend.
 *
 * next.config.ts の `rewrites` は通常の HTTP リクエストには機能するが、
 * text/event-stream の長命チャンク転送はバッファされて流れてこないケースが
 * ある(特に Turbopack)。ここでは明示的に fetch → ReadableStream をそのまま
 * 返すことで、EventSource の初回接続後にサーバー側から届く chunk が確実に
 * ブラウザに届くようにする。
 *
 * /api/addon/events の URL を維持することで、useAddonEvents フック側は無改修。
 */
import type { NextRequest } from "next/server";

const BACKEND = process.env.SAIVERSE_BACKEND_URL ?? "http://127.0.0.1:8000";

// このルートは常に動的。Next.js の静的最適化を避ける。
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
// 長命コネクションを許す(デフォルトのタイムアウトを伸ばす)
export const maxDuration = 3600;

export async function GET(req: NextRequest): Promise<Response> {
    const upstream = new URL("/api/addon/events", BACKEND);

    let upstreamResp: Response;
    try {
        upstreamResp = await fetch(upstream, {
            method: "GET",
            headers: {
                accept: "text/event-stream",
                "cache-control": "no-cache",
                "x-forwarded-for": req.headers.get("x-forwarded-for") ?? "",
            },
            signal: req.signal,
            // duplex は GET（body 無し）では不要。削除するとデフォルト ReadableStream
            // 挙動が保たれ、Next.js ランタイムでの互換性が高い。
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error("[addon-events proxy] upstream fetch failed:", msg);
        return new Response(`upstream fetch failed: ${msg}`, { status: 502 });
    }

    if (!upstreamResp.ok || !upstreamResp.body) {
        const bodyText = await upstreamResp.text().catch(() => "");
        console.error(
            "[addon-events proxy] upstream returned non-ok:",
            upstreamResp.status,
            bodyText.slice(0, 200),
        );
        return new Response(
            `upstream error: ${upstreamResp.status} ${bodyText.slice(0, 200)}`,
            { status: 502 },
        );
    }

    return new Response(upstreamResp.body, {
        status: 200,
        headers: {
            "content-type": "text/event-stream; charset=utf-8",
            "cache-control": "no-cache, no-transform",
            connection: "keep-alive",
            "x-accel-buffering": "no",
        },
    });
}
