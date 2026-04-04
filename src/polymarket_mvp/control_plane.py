from __future__ import annotations

import argparse

from flask import Flask, jsonify, render_template_string, request

from .common import utc_now_iso
from .tg_approver import control_system, system_control_status


CONTROL_PLANE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>System Control</title>
  <style>
    :root {
      --bg: #11161f;
      --panel: #1a2230;
      --border: #2f3b52;
      --text: #eef3fb;
      --muted: #97a5bd;
      --good: #22a06b;
      --warn: #d8a031;
      --bad: #d84e64;
      --accent: #6eb6ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: radial-gradient(circle at top, #1b2737 0%, var(--bg) 56%);
      color: var(--text);
      font: 14px/1.5 "SF Mono", "IBM Plex Mono", Menlo, Monaco, Consolas, monospace;
    }
    .page { max-width: 980px; margin: 0 auto; padding: 24px; }
    .hero, .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 20px 44px rgba(0, 0, 0, 0.22);
    }
    .hero { padding: 22px; margin-bottom: 16px; }
    .hero h1 { margin: 0 0 8px; font-size: 30px; }
    .hero p { margin: 0; color: var(--muted); }
    .hero-meta { margin-top: 12px; color: var(--muted); }
    .panel { padding: 16px; margin-bottom: 16px; }
    .panel h2 { margin: 0 0 12px; font-size: 16px; }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 12px;
    }
    button {
      appearance: none;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #243146;
      color: var(--text);
      font: inherit;
      padding: 10px 14px;
      cursor: pointer;
    }
    button:hover { border-color: var(--accent); }
    button.warn { color: #ffd480; }
    button.bad { color: #ff8d9d; }
    .log {
      min-height: 44px;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.02);
      color: var(--muted);
    }
    .services {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
    }
    .card {
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.02);
    }
    .name { font-weight: 700; margin-bottom: 6px; }
    .badge {
      display: inline-block;
      border: 1px solid currentColor;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      margin-bottom: 8px;
    }
    .green { color: var(--good); }
    .yellow { color: var(--warn); }
    .red { color: var(--bad); }
    .muted { color: var(--muted); }
    .line { margin: 4px 0; overflow-wrap: anywhere; }
    a { color: var(--accent); }
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <h1>System Control</h1>
      <p>This control plane stays outside the trading system. You can start the system even when the main dashboard on port 8787 is down.</p>
      <div class="hero-meta" id="snapshot-meta">Loading...</div>
    </section>

    <section class="panel">
      <h2>Actions</h2>
      <div class="actions">
        <button data-action="start_system">Start System</button>
        <button data-action="restart_system" class="warn">Restart System</button>
        <button data-action="stop_system" class="bad">Stop System</button>
        <button data-action="refresh_status">Refresh Status</button>
      </div>
      <div id="action-log" class="log">Control plane online.</div>
    </section>

    <section class="panel">
      <h2>Service Status</h2>
      <div id="services" class="services"></div>
    </section>

    <section class="panel">
      <h2>Links</h2>
      <div class="line"><a href="http://127.0.0.1:8787/ops" target="_blank" rel="noreferrer">Open main dashboard</a></div>
      <div class="line"><a href="/api/system/status" target="_blank" rel="noreferrer">Open control-plane status JSON</a></div>
    </section>
  </div>

  <script>
    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function statusClass(service) {
      if (service.manager === "launchd") return service.loaded ? "green" : "red";
      return service.running ? "green" : "red";
    }

    function statusLabel(service) {
      if (service.manager === "launchd") return service.loaded ? "loaded" : "not loaded";
      return service.running ? "running" : "stopped";
    }

    function renderService(service) {
      return `
        <div class="card">
          <div class="name">${escapeHtml(service.name)} <span class="muted">(${escapeHtml(service.manager)})</span></div>
          <div class="badge ${statusClass(service)}">${escapeHtml(statusLabel(service))}</div>
          ${service.pids ? `<div class="line muted">pids: ${escapeHtml(service.pids.join(", "))}</div>` : ""}
          ${service.port ? `<div class="line muted">port: ${escapeHtml(service.port)}</div>` : ""}
          ${service.label ? `<div class="line muted">label: ${escapeHtml(service.label)}</div>` : ""}
          ${service.log ? `<div class="line muted">log: ${escapeHtml(service.log)}</div>` : ""}
        </div>
      `;
    }

    async function refreshStatus() {
      const response = await fetch("/api/system/status", { headers: { "Accept": "application/json" } });
      if (!response.ok) throw new Error(`status ${response.status}`);
      const payload = await response.json();
      document.getElementById("snapshot-meta").textContent = `updated ${new Date(payload.timestamp).toLocaleString()}`;
      document.getElementById("services").innerHTML = (payload.services || []).map(renderService).join("");
    }

    async function control(action) {
      const log = document.getElementById("action-log");
      if (action === "refresh_status") {
        await refreshStatus();
        log.textContent = "Status refreshed.";
        return;
      }
      log.textContent = `sending ${action}...`;
      const response = await fetch("/api/system/control", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ action }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || `status ${response.status}`);
      }
      log.textContent = payload.message || `${action} accepted`;
      window.setTimeout(refreshStatus, 1200);
      window.setTimeout(refreshStatus, 3000);
    }

    document.querySelectorAll("[data-action]").forEach(button => {
      button.addEventListener("click", async () => {
        const action = button.getAttribute("data-action");
        try {
          await control(action);
        } catch (error) {
          document.getElementById("action-log").textContent = `control failed: ${error}`;
        }
      });
    });

    refreshStatus().catch(error => {
      document.getElementById("action-log").textContent = `status load failed: ${error}`;
    });
    window.setInterval(() => refreshStatus().catch(() => {}), 5000);
  </script>
</body>
</html>
"""


def create_control_plane_app() -> Flask:
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "timestamp": utc_now_iso(), "service": "control-plane"})

    @app.get("/api/system/status")
    def api_system_status():
        return jsonify(system_control_status())

    @app.post("/api/system/control")
    def api_system_control():
        payload = request.get_json(force=True, silent=True) or {}
        action = str(payload.get("action") or "").strip()
        try:
            result = control_system(action)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify(result)

    @app.get("/")
    def index():
        return render_template_string(CONTROL_PLANE_TEMPLATE)

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone system control plane.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address.")
    parser.add_argument("--port", type=int, default=8788, help="Bind port.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    app = create_control_plane_app()
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
