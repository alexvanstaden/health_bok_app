import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Health & Longevity Knowledge",
  description:
    "The Web App: review Candidates, approve them into the Body of Knowledge, and see their Claims.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <main>{children}</main>
      </body>
    </html>
  );
}
