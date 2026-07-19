import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "Antra Player",
    template: "%s · Antra Player",
  },
  description:
    "Приватный музыкальный плеер для библиотеки, сохранённой через Telegram.",
  applicationName: "Antra Player",
  manifest: "/manifest.webmanifest",
  icons: {
    icon: "/icon-192.png",
    apple: "/icon-192.png",
  },
  openGraph: {
    title: "Antra Player",
    description:
      "Приватный музыкальный плеер для библиотеки, сохранённой через Telegram.",
    images: [{ url: "/og.png", width: 1731, height: 909 }],
    type: "website",
  },
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Antra",
  },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#081412",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
