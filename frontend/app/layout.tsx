import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cadence — practice interviews that answer back",
  description:
    "Run a technical practice interview with an interviewer that follows up on what you actually said, then read an evidence-based scorecard.",
};

export const viewport: Viewport = {
  themeColor: "#16181c",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        {/*
          Fonts are loaded from the CDN rather than next/font so the project
          builds in an offline or network-restricted environment. In production
          behind a stable network, next/font/google self-hosts these and removes
          the third-party request plus the layout shift — a one-file change.
        */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wdth,wght@12..96,75..100,400..800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
