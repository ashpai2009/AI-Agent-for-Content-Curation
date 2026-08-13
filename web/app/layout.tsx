import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Curation Council",
  description:
    "Upload an OATutor workbook, let the council audit and repair it, download the result.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
