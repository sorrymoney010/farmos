import "./globals.css";

export const metadata = {
  title: "FARMOS Console",
  description: "FARMOS Phase 1 operations console",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
