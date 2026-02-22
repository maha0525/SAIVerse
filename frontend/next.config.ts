import path from "node:path";
import { fileURLToPath } from "node:url";

import type { NextConfig } from "next";

const configDir = path.dirname(fileURLToPath(import.meta.url));

const nextConfig: NextConfig = {
    turbopack: {
        root: configDir,
    },
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
    experimental: {
        serverActions: {
            bodySizeLimit: '5000mb',
        },
        proxyClientMaxBodySize: '5000mb',
    },
};

export default nextConfig;
