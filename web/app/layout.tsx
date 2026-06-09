import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Health & Longevity Knowledge",
  description:
    "The Web App: review Candidates, approve them into the Body of Knowledge, and browse & edit its Claims, Protocols, and Concepts.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <nav className="nav">
          <a href="/">Review queue</a>
          <a href="/claims">Claims</a>
          <a href="/protocols">Protocols</a>
          <a href="/concepts">Concepts</a>
        </nav>
        <main>{children}</main>
      </body>
    </html>
  );
}
