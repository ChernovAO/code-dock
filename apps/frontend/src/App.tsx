import { useEffect, useState } from "react";

type Status = "loading" | "ok" | "error";

const LABELS: Record<Status, string> = {
  loading: "проверяем…",
  ok: "работает",
  error: "недоступен",
};

export function App() {
  const [status, setStatus] = useState<Status>("loading");

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/health", { signal: controller.signal })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((body: { status?: string }) => setStatus(body.status === "ok" ? "ok" : "error"))
      .catch((e: unknown) => {
        if (!(e instanceof DOMException && e.name === "AbortError")) setStatus("error");
      });
    return () => controller.abort();
  }, []);

  return (
    <main>
      <h1>Code Dock</h1>
      <p>Бэкенд: {LABELS[status]}</p>
    </main>
  );
}
