import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

import devOrigins from "./dev-origins.cjs";
import { resolveBackendOrigin } from "./backend-origin.cjs";

const configDir = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
    turbopack: {
        root: configDir,
    },
    // Loopback and Tailscale names, plus SAIVERSE_ALLOWED_ORIGINS (LAN hostname,
    // custom domain). The list and its matching rules live in dev-origins.cjs.
    allowedDevOrigins: devOrigins.buildAllowedDevOrigins(process.env.SAIVERSE_ALLOWED_ORIGINS),
    async rewrites() {
        // fallback に置くことで、Next.js の動的 Route Handler
        // (app/api/addon/[...path]/route.ts など) が先に評価される。
        // 通常配列で返すと `afterFiles` 相当になり filesystem 静的ルートの
        // 次・動的ルートの前で評価されてしまい、動的 Route Handler が
        // 完全に無視される問題があった。
        return {
            beforeFiles: [],
            afterFiles: [],
            fallback: [
                {
                    source: '/api/:path*',
                    // 既定は本番バックエンド。隔離テスト環境 (port 18000,
                    // docs/test_environment.md) へ向けるときだけ env で差し替える
                    destination: `${resolveBackendOrigin()}/api/:path*`,
                },
            ],
        };
    },
    devIndicators: false as any,
    // Allow larger file uploads for ChatGPT export import
    // Prevent Next.js from stripping trailing slashes before rewrites.
    // Without this, /api/addon/ becomes /api/addon, FastAPI returns a 307
    // redirect to 127.0.0.1:8000/api/addon/ which leaks to the client —
    // remote clients (phone via Tailscale) can't reach 127.0.0.1:8000.
    skipTrailingSlashRedirect: true,
    experimental: {
        serverActions: {
            bodySizeLimit: '5000mb',
        },
        proxyClientMaxBodySize: '5000mb',
        // rewrites の中継が裏のバックエンドとの間に張るソケットの無活動タイムアウト。
        // 未設定だと Next.js が 30 秒にする (next/dist/server/lib/router-utils/
        // proxy-request.js の `proxyTimeout || 30000`)。ペルソナまるごとパックの
        // 出力など、返事の最初のバイトまで数分黙る API が 30 秒で切られ、画面には
        // 500 だけが出ていた (docs/issues/persona_pack_export_fails_via_ui_relay.md)。
        // 値は「バイトが一切流れない時間」の上限であって、リクエスト全体の長さでは
        // ない。本当に死んだバックエンドへの接続を手放すための安全弁として 1 時間。
        proxyTimeout: 3_600_000,
        // Next.js 16.4 は既定で agentUpgrade を 'security' にするため
        // (next/dist/server/config-shared.js)、配布後に勧告が出ると start.bat の
        // next build や start-dev.bat の next dev でコーディングエージェント向けの
        // 対話メニュー (Upgrade now / Skip) が割り込む。しかもそこで書き換えると
        // package.json / package-lock.json に差分が出て本体の自動更新が止まる。
        // フロントエンドの版は SAIVerse 側の package-lock.json (npm ci) で管理する。
        agentUpgrade: false,
    },
};

export default nextConfig;
