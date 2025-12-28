import type { NextConfig } from "next";

const nextConfig: NextConfig = {
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
            bodySizeLimit: '100mb',
        },
        proxyClientMaxBodySize: '100mb',
    },
};

export default nextConfig;
