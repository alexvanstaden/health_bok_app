import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Health & Longevity Knowledge",
  description:
    "The Web App: review Candidates, approve them into the Body of Knowledge, browse & edit its Claims, Protocols, and Concepts, and record the personal layer of Goals, Markers, and Decisions.",
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
          <a href="/ask">Ask</a>
          <a href="/creators">Creators</a>
          <a href="/backfill">Backfill</a>
          <a href="/claims">Claims</a>
          <a href="/protocols">Protocols</a>
          <a href="/concepts">Concepts</a>
          <a href="/goals">Goals</a>
          <a href="/markers">Markers</a>
          <a href="/decisions">Decisions</a>
        </nav>
        <main>{children}</main>
      </body>
    </html>
  );
}
