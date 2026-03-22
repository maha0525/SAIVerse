import type { Metadata, Viewport } from "next";
import "./globals.css";

export const viewport: Viewport = {
    viewportFit: "cover",
    width: "device-width",
    initialScale: 1,
    maximumScale: 1,
    userScalable: false,
    themeColor: [
        { media: "(prefers-color-scheme: light)", color: "#f5f5f5" },
        { media: "(prefers-color-scheme: dark)", color: "#1a1a2e" },
    ],
};

export const metadata: Metadata = {
    title: "SAIVerse City Interface",
    description: "Next Generation UI for SAIVerse",
    icons: {
        icon: [
            { url: "/icon.jpg", type: "image/jpeg" },
            { url: "/icon.jpg", sizes: "any", type: "image/jpeg" },
        ],
    },
    formatDetection: {
        telephone: false,
    },
};

export default function RootLayout({
    children,
}: Readonly<{
    children: React.ReactNode;
}>) {
    return (
        <html lang="ja" suppressHydrationWarning>
            <head>
                <script dangerouslySetInnerHTML={{ __html: `
                    (function() {
                        function applyTheme() {
                            var theme = localStorage.getItem('saiverse-theme') || 'system';
                            var resolved = theme;
                            if (theme === 'system') {
                                resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
                            }
                            document.documentElement.dataset.theme = resolved;
                        }
                        applyTheme();
                        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
                        window.addEventListener('theme-change', applyTheme);
                    })();
                `}} />
            </head>
            <body suppressHydrationWarning>{children}</body>
        </html>
    );
}
