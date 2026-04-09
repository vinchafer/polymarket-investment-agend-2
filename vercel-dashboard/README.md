# Vercel Dashboard (read-only)

This Next.js app calls your **VPS FastAPI** (`polymarket-dashboard` Python service) at `GET /api/summary`.

## Vercel setup

1. Import this repo in Vercel and set the **root directory** to `vercel-dashboard`.
2. Environment variables:
   - `NEXT_PUBLIC_API_BASE` — e.g. `https://agent.example.com:8765` (must be reachable from the public internet).
   - `NEXT_PUBLIC_DASHBOARD_TOKEN` — optional; must match `DASHBOARD_READ_TOKEN` on the server.

3. On the VPS, allow CORS from your Vercel origin, e.g.:

`DASHBOARD_CORS_ORIGINS=https://your-app.vercel.app`

4. Prefer HTTPS on the VPS (Caddy / nginx reverse proxy) and firewall-limit port exposure.
