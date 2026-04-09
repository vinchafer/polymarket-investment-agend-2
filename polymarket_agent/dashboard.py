from __future__ import annotations

import json
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.cors import CORSMiddleware

from .db import Database
from .settings import Settings


def _summary_payload(db: Database, settings: Settings) -> dict[str, Any]:
    from datetime import datetime, timezone

    day_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    starting = db.get_starting_capital() or settings.starting_capital_usdc
    cash = db.get_portfolio_cash()
    deployed = db.capital_at_risk()
    return {
        "starting_capital_usdc": starting,
        "free_cash_usdc": cash,
        "deployed_usdc": deployed,
        "nav_usdc": cash + deployed,
        "open_positions": db.open_positions_count(),
        "daily_realized_pnl_usdc": db.daily_realized_pnl(day_prefix),
        "halt_until": db.get_risk_state("halt_until", ""),
        "dry_run": settings.dry_run,
        "execution_mode": settings.execution_mode,
        "positions": db.list_open_positions(30),
        "recent_events": db.list_recent_events(25),
        "recent_executions": db.list_recent_executions(15),
        "recent_audit": db.list_recent_audit(20),
        "tracked_wallets": db.list_tracked_wallets(),
        "attribution": db.list_attribution_signals(40),
        "leaderboard": {
            "time_period": settings.leaderboard_time_period,
            "category": settings.leaderboard_category,
            "order_by": settings.leaderboard_order_by,
            "curator_interval_hours": settings.wallet_curator_interval_hours,
        },
    }


def _cors_origins(settings: Settings) -> list[str]:
    raw = (settings.dashboard_cors_origins or "*").strip()
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def build_app() -> FastAPI:
    settings = Settings()
    db = Database(settings.database_path)

    app = FastAPI(title="Polymarket Agent Dashboard", version="0.2.0")

    origins = _cors_origins(settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=bool(origins != ["*"]),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def guard_dashboard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "OPTIONS":
            return await call_next(request)
        if request.url.path in ("/healthz", "/docs", "/openapi.json", "/redoc"):
            return await call_next(request)
        if not settings.dashboard_read_token:
            return await call_next(request)
        auth = request.headers.get("authorization", "") or ""
        bearer = ""
        if auth.lower().startswith("bearer "):
            bearer = auth[7:].strip()
        qtok = request.query_params.get("token") or ""
        if (bearer or qtok) == settings.dashboard_read_token:
            return await call_next(request)
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/summary")
    def api_summary() -> JSONResponse:
        return JSONResponse(_summary_payload(db, settings))

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        token_js = json.dumps(settings.dashboard_read_token or "")
        return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Polymarket Agent — Portfolio</title>
  <style>
    :root {{ --bg:#0f1419; --card:#1a2332; --text:#e7ecf3; --muted:#8b9bb4; --accent:#3d8bfd; }}
    body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      background:var(--bg); color:var(--text); line-height:1.45; }}
    header {{ padding:1.25rem 1.5rem; border-bottom:1px solid #243044;
      display:flex; flex-wrap:wrap; gap:0.75rem; align-items:center; justify-content:space-between; }}
    h1 {{ font-size:1.1rem; margin:0; font-weight:600; }}
    .sub {{ color:var(--muted); font-size:0.85rem; }}
    main {{ padding:1.25rem 1.5rem 2rem; max-width:1200px; margin:0 auto; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:0.75rem; margin-bottom:1.25rem; }}
    .tile {{ background:var(--card); border-radius:10px; padding:0.85rem 1rem; border:1px solid #243044; }}
    .tile .label {{ color:var(--muted); font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; }}
    .tile .value {{ font-size:1.35rem; font-weight:600; margin-top:0.25rem; }}
    section {{ margin-top:1.5rem; }}
    h2 {{ font-size:0.95rem; margin:0 0 0.5rem; color:var(--muted); font-weight:600; }}
    table {{ width:100%; border-collapse:collapse; font-size:0.85rem; }}
    th, td {{ text-align:left; padding:0.45rem 0.35rem; border-bottom:1px solid #243044; vertical-align:top; }}
    th {{ color:var(--muted); font-weight:500; }}
    .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:0.8rem; word-break:break-all; }}
    .pill {{ display:inline-block; padding:0.15rem 0.45rem; border-radius:999px; font-size:0.72rem; background:#243044; }}
    button {{ background:var(--accent); color:#fff; border:none; border-radius:8px; padding:0.45rem 0.85rem; font-weight:600; cursor:pointer; }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Polymarket Investment Agent</h1>
      <div class="sub">Auto-Refresh 30s · <span class="mono">/api/summary</span></div>
    </div>
    <button type="button" id="btn">Aktualisieren</button>
  </header>
  <main id="root"><p class="sub">Lade…</p></main>
  <script>
    const API_TOKEN = {token_js};
    function hdrs() {{
      const h = {{}};
      if (API_TOKEN) h['Authorization'] = 'Bearer ' + API_TOKEN;
      return h;
    }}
    function fmt(n) {{
      if (typeof n !== 'number' || Number.isNaN(n)) return '—';
      return n.toFixed(2);
    }}
    function esc(s) {{
      return String(s ?? '').replace(/[&<>"']/g, c =>
        ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function render(data) {{
      const halt = data.halt_until
        ? '<span class="pill">Halt bis ' + esc(data.halt_until) + '</span>'
        : '<span class="pill">kein Halt</span>';
      const lb = data.leaderboard || {{}};
      const posRows = (data.positions || []).length
        ? (data.positions || []).map(p => '<tr><td class="mono">' + esc(p.market_id) + '</td><td>' + esc(p.market_question) +
            '</td><td>' + esc(p.side) + '</td><td>' + fmt(p.stake_usdc) + '</td><td>' + fmt(p.entry_price) + '</td></tr>').join('')
        : '<tr><td colspan="5" class="sub">Keine offenen Positionen</td></tr>';
      const evRows = (data.recent_events || []).map(e =>
        '<tr><td class="mono">' + esc(e.timestamp) + '</td><td>' + esc(e.event_type) + '</td><td>' + esc(e.agent_source) +
        '</td><td>' + esc((e.market_question || '').slice(0, 140)) + '</td></tr>').join('');
      const exRows = (data.recent_executions || []).map(x =>
        '<tr><td class="mono">' + esc(x.timestamp) + '</td><td>' + esc(x.status) + '</td><td>' + fmt(x.stake_usdc) +
        '</td><td>' + fmt(x.simulated_fill_price) + '</td></tr>').join('');
      const auRows = (data.recent_audit || []).map(a =>
        '<tr><td class="mono">' + esc(a.timestamp) + '</td><td>' + esc(a.level) + '</td><td>' + esc(a.component) +
        '</td><td>' + esc(a.action) + '</td></tr>').join('');
      const twRows = (data.tracked_wallets || []).map(w =>
        '<tr><td class="mono">' + esc(w.address) + '</td><td>' + esc(w.user_name) + '</td><td>' + fmt(w.pnl) +
        '</td><td>' + fmt(w.vol) + '</td></tr>').join('');
      const atRows = (data.attribution || []).map(a =>
        '<tr><td class="mono">' + esc(a.market_id) + '</td><td class="mono">' + esc(a.source_wallet) +
        '</td><td>' + fmt(a.entry_signal_price) + '</td><td>' + fmt(a.fill_price) + '</td><td>' + fmt(a.slippage) +
        '</td><td>' + fmt(a.stake_usdc) + '</td><td>' + esc(a.status) + '</td><td>' + fmt(a.resolution_eta_hours) + '</td></tr>').join('');
      document.getElementById('root').innerHTML =
        '<div class="grid">' +
        '<div class="tile"><div class="label">Startkapital</div><div class="value">' + fmt(data.starting_capital_usdc) + ' USDC</div></div>' +
        '<div class="tile"><div class="label">Freies Cash</div><div class="value">' + fmt(data.free_cash_usdc) + ' USDC</div></div>' +
        '<div class="tile"><div class="label">Deployed</div><div class="value">' + fmt(data.deployed_usdc) + ' USDC</div></div>' +
        '<div class="tile"><div class="label">NAV (Paper)</div><div class="value">' + fmt(data.nav_usdc) + ' USDC</div></div>' +
        '<div class="tile"><div class="label">Offene Positionen</div><div class="value">' + esc(String(data.open_positions)) + '</div></div>' +
        '<div class="tile"><div class="label">Realized PnL heute</div><div class="value">' + fmt(data.daily_realized_pnl_usdc) + ' USDC</div></div>' +
        '</div>' +
        '<div class="sub">Modus: <span class="pill">' + esc(data.execution_mode) + '</span> · dry_run: <span class="pill">' +
        esc(String(data.dry_run)) + '</span> · ' + halt + '</div>' +
        '<div class="sub">Leaderboard: ' + esc(lb.time_period || '') + ' / ' + esc(lb.category || '') +
        ' · Curator alle ' + esc(String(lb.curator_interval_hours || '')) + 'h</div>' +
        '<section><h2>Top Wallets (Scout)</h2><table><thead><tr><th>Address</th><th>User</th><th>PNL</th><th>Vol</th></tr></thead><tbody>' +
        (twRows || '<tr><td colspan="4" class="sub">Keine Daten — warte auf ersten Curator-Run</td></tr>') + '</tbody></table></section>' +
        '<section><h2>Attribution (Paper)</h2><table><thead><tr><th>Market</th><th>Wallet</th><th>Signal</th><th>Fill</th><th>Slip</th><th>Stake</th><th>Status</th><th>Res. h</th></tr></thead><tbody>' +
        (atRows || '<tr><td colspan="8" class="sub">Noch keine Trades</td></tr>') + '</tbody></table></section>' +
        '<section><h2>Offene Positionen</h2><table><thead><tr><th>Market</th><th>Frage</th><th>Side</th><th>Stake</th><th>Entry</th></tr></thead><tbody>' +
        posRows + '</tbody></table></section>' +
        '<section><h2>Letzte Events</h2><table><thead><tr><th>Zeit</th><th>Typ</th><th>Agent</th><th>Markt</th></tr></thead><tbody>' +
        evRows + '</tbody></table></section>' +
        '<section><h2>Executions (Paper)</h2><table><thead><tr><th>Zeit</th><th>Status</th><th>Stake</th><th>Fill</th></tr></thead><tbody>' +
        exRows + '</tbody></table></section>' +
        '<section><h2>Audit</h2><table><thead><tr><th>Zeit</th><th>Level</th><th>Component</th><th>Action</th></tr></thead><tbody>' +
        auRows + '</tbody></table></section>';
    }}
    async function load() {{
      try {{
        const res = await fetch('/api/summary', {{ headers: hdrs() }});
        if (!res.ok) throw new Error('HTTP ' + res.status);
        render(await res.json());
      }} catch (e) {{
        document.getElementById('root').innerHTML = '<p class="sub">Laden fehlgeschlagen</p>';
      }}
    }}
    document.getElementById('btn').addEventListener('click', load);
    load();
    setInterval(load, 30000);
  </script>
</body>
</html>"""

    return app


app = build_app()


def run() -> None:
    settings = Settings()
    uvicorn.run(
        app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
    )
