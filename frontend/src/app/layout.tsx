import type { Metadata, Viewport } from "next";
import "./globals.css";

export const viewport: Viewport = {
    viewportFit: "cover",
};

export const metadata: Metadata = {
    title: "SAIVerse City Interface",
    description: "Next Generation UI for SAIVerse",
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
            <body>{children}</body>
        </html>
    );
}
