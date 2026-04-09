import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Polymarket Agent",
  description: "Portfolio dashboard (reads API on your VPS)",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="de">
      <body style={{ margin: 0, fontFamily: "system-ui, sans-serif", background: "#0f1419", color: "#e7ecf3" }}>
        {children}
      </body>
    </html>
  );
}
