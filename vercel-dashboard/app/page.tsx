"use client";

import { useCallback, useEffect, useState } from "react";

type Summary = Record<string, unknown>;

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE || "").replace(/\/$/, "");
const TOKEN = process.env.NEXT_PUBLIC_DASHBOARD_TOKEN || "";

export default function Page() {
  const [data, setData] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!API_BASE) {
      setErr("Set NEXT_PUBLIC_API_BASE in Vercel (your VPS /api/summary URL without path).");
      return;
    }
    try {
      const headers: Record<string, string> = { Accept: "application/json" };
      if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
      const res = await fetch(`${API_BASE}/api/summary`, { headers, cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData((await res.json()) as Summary);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    }
  }, []);

  useEffect(() => {
    void load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [load]);

  if (err && !data) {
    return (
      <main style={{ padding: 24, maxWidth: 720 }}>
        <h1 style={{ fontSize: "1.1rem" }}>Polymarket Agent</h1>
        <p style={{ color: "#8b9bb4" }}>{err}</p>
        <p style={{ color: "#8b9bb4", fontSize: "0.9rem" }}>
          Beispiel: NEXT_PUBLIC_API_BASE=https://agent.deine-domain.tld:8765 und optional NEXT_PUBLIC_DASHBOARD_TOKEN.
        </p>
      </main>
    );
  }

  const fmt = (n: unknown) => (typeof n === "number" && !Number.isNaN(n) ? n.toFixed(2) : "—");

  return (
    <main style={{ padding: 16, maxWidth: 1100, margin: "0 auto" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div>
          <h1 style={{ fontSize: "1.15rem", margin: 0 }}>Polymarket Agent</h1>
          <p style={{ color: "#8b9bb4", fontSize: "0.85rem", margin: "6px 0 0" }}>Live · 30s refresh</p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          style={{
            background: "#3d8bfd",
            color: "#fff",
            border: "none",
            borderRadius: 8,
            padding: "8px 14px",
            fontWeight: 600,
          }}
        >
          Aktualisieren
        </button>
      </header>

      {data && (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))",
              gap: 10,
              marginBottom: 16,
            }}
          >
            {[
              ["NAV (Paper)", fmt(data.nav_usdc)],
              ["Frei", fmt(data.free_cash_usdc)],
              ["Deployed", fmt(data.deployed_usdc)],
              ["Offen", String(data.open_positions ?? "—")],
              ["PnL heute", fmt(data.daily_realized_pnl_usdc)],
            ].map(([k, v]) => (
              <div key={k as string} style={{ background: "#1a2332", border: "1px solid #243044", borderRadius: 10, padding: "10px 12px" }}>
                <div style={{ color: "#8b9bb4", fontSize: "0.72rem", textTransform: "uppercase" }}>{k}</div>
                <div style={{ fontSize: "1.25rem", fontWeight: 600 }}>{v}</div>
              </div>
            ))}
          </div>

          <Section title="Top Wallets (Scout)" rows={data.tracked_wallets as object[] | undefined} cols={["address", "user_name", "pnl", "vol"]} />
          <Section title="Attribution" rows={data.attribution as object[] | undefined} cols={["market_id", "source_wallet", "slippage", "stake_usdc", "status"]} />
        </>
      )}
    </main>
  );
}

function Section({ title, rows, cols }: { title: string; rows?: object[]; cols: string[] }) {
  const list = Array.isArray(rows) ? rows : [];
  return (
    <section style={{ marginTop: 20 }}>
      <h2 style={{ fontSize: "0.95rem", color: "#8b9bb4", margin: "0 0 8px" }}>{title}</h2>
      <div style={{ overflowX: "auto", border: "1px solid #243044", borderRadius: 8 }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.82rem" }}>
          <thead>
            <tr style={{ color: "#8b9bb4" }}>
              {cols.map((c) => (
                <th key={c} style={{ textAlign: "left", padding: "6px 8px", borderBottom: "1px solid #243044" }}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {list.length === 0 ? (
              <tr>
                <td colSpan={cols.length} style={{ padding: 12, color: "#8b9bb4" }}>
                  Keine Daten
                </td>
              </tr>
            ) : (
              list.slice(0, 40).map((r, i) => (
                <tr key={i}>
                  {cols.map((c) => (
                    <td key={c} style={{ padding: "6px 8px", borderBottom: "1px solid #243044", wordBreak: "break-all" }}>
                      {String((r as Record<string, unknown>)[c] ?? "")}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
