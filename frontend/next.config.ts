import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

const configDir = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
    turbopack: {
        root: configDir,
    },
    allowedDevOrigins: (() => {
        // Base: always allow loopback and all Tailscale domains (*.ts.net covers any tailnet)
        const origins: string[] = ["localhost", "127.0.0.1", "*.ts.net"];
        // Optional: comma-separated extra origins via env var (e.g. LAN hostname, custom domain)
        const extra = process.env.SAIVERSE_ALLOWED_ORIGINS;
        if (extra) origins.push(...extra.split(",").map((s) => s.trim()).filter(Boolean));
        return origins;
    })(),
    async rewrites() {
        return [
            {
                source: '/api/:path*',
                destination: 'http://127.0.0.1:8000/api/:path*',
            },
        ];
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
    },
};

export default nextConfig;
