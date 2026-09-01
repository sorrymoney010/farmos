const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function Home() {
  return (
    <main>
      <p className="eyebrow">FARMOS</p>
      <h1>Phase 1 Operations Console</h1>
      <p className="status">Web service is running.</p>
      <p>
        API: <a href={`${apiUrl}/docs`}>{apiUrl}/docs</a>
      </p>
    </main>
  );
}
