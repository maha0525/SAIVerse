import type { Metadata } from "next";
import "./globals.css";

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
        <html lang="ja">
            <body>{children}</body>
        </html>
    );
}
