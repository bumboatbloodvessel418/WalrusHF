from __future__ import annotations

import base64
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from task_store import (
    DATA_DIR,
    DOWNLOAD_DIR,
    FAILED_FILE,
    QUEUE_DIR,
    QUEUE_FILE,
    SESSION_DIR,
    clear_worker_pid,
    ensure_storage_dirs,
    human_size,
    is_cancelled,
    load_processing,
    load_runtime_settings,
    processing_task_is_active,
    queue_size,
    read_failed_entries,
    runtime_path,
)


load_dotenv()
ensure_storage_dirs()

BASE_DIR = Path(__file__).resolve().parent
LOG_LINES: deque[str] = deque(maxlen=250)
STATE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()

telegram_proc: subprocess.Popen | None = None
rubika_proc: subprocess.Popen | None = None
supervisor_started = False


def append_log(source: str, text: str) -> None:
    line = text.rstrip()
    if not line:
        return
    timestamp = time.strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {source}: {line}"
    print(formatted, flush=True)
    with STATE_LOCK:
        LOG_LINES.append(formatted)


def decode_secret_file(env_name: str, output_path: Path) -> None:
    encoded = os.getenv(env_name, "").strip()
    if not encoded or output_path.exists():
        return

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(encoded))
        append_log("setup", f"decoded {env_name} to {output_path.name}")
    except Exception as error:
        append_log("setup", f"failed to decode {env_name}: {error}")


def decode_session_secrets() -> None:
    settings = load_runtime_settings()
    rubika_session = runtime_path(settings["rubika_session"], SESSION_DIR)
    if rubika_session.suffix == "":
        rubika_session = rubika_session.with_suffix(".rp")

    telegram_session = runtime_path(
        os.getenv("TELEGRAM_SESSION", "walrus").strip() or "walrus",
        SESSION_DIR,
    )
    if telegram_session.suffix == "":
        telegram_session = telegram_session.with_suffix(".session")

    decode_secret_file("RUBIKA_SESSION_B64", rubika_session)
    decode_secret_file("TELEGRAM_SESSION_B64", telegram_session)


def stream_process_output(name: str, proc: subprocess.Popen) -> None:
    if proc.stdout is None:
        return

    for line in proc.stdout:
        append_log(name, line)


def start_process(script_name: str, name: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-u", str(BASE_DIR / script_name)],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    threading.Thread(target=stream_process_output, args=(name, proc), daemon=True).start()
    append_log("supervisor", f"started {name} with pid {proc.pid}")
    return proc


def required_env_status() -> list[str]:
    checks = {
        "API_ID": os.getenv("API_ID", "").strip(),
        "API_HASH": os.getenv("API_HASH", "").strip(),
        "BOT_TOKEN": os.getenv("BOT_TOKEN", "").strip(),
    }
    missing = []
    for name, value in checks.items():
        if not value or value == "0" or value == name or value.startswith("your_"):
            missing.append(name)
    return missing


def supervisor_loop() -> None:
    global telegram_proc, rubika_proc

    decode_session_secrets()
    logged_missing: tuple[str, ...] | None = None

    while not STOP_EVENT.is_set():
        missing = tuple(required_env_status())
        if missing:
            if missing != logged_missing:
                append_log("setup", f"missing required secrets: {', '.join(missing)}")
                logged_missing = missing
            time.sleep(5)
            continue

        logged_missing = None

        if telegram_proc is None:
            telegram_proc = start_process("telegram_bot.py", "telegram")
        elif telegram_proc.poll() is not None:
            append_log("telegram", f"exited with code {telegram_proc.returncode}")
            telegram_proc = None

        if rubika_proc is None:
            rubika_proc = start_process("rubika_worker.py", "rubika")
        elif rubika_proc.poll() is not None:
            append_log("rubika", f"exited with code {rubika_proc.returncode}; restarting")
            clear_worker_pid()
            rubika_proc = None

        time.sleep(2)


def ensure_supervisor() -> None:
    global supervisor_started
    if supervisor_started:
        return

    supervisor_started = True
    threading.Thread(target=supervisor_loop, daemon=True).start()


def proc_label(proc: subprocess.Popen | None) -> str:
    if proc is None:
        return "not started"
    code = proc.poll()
    if code is None:
        return f"running (pid {proc.pid})"
    return f"stopped (exit {code})"


def storage_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def dashboard_snapshot() -> dict:
    ensure_supervisor()
    settings = load_runtime_settings()
    processing = load_processing()
    failed_count = len(read_failed_entries()) if FAILED_FILE.exists() else 0
    queue_count = queue_size() if QUEUE_FILE.exists() else 0

    upload_percent = 0
    active = "none"
    stale_processing = bool(
        processing
        and (
            not processing_task_is_active(processing)
            or is_cancelled(processing.get("task_id", ""))
        )
    )
    if processing and not stale_processing:
        upload_percent = int(processing.get("upload_percent", 0) or 0)
        active = (
            f"{processing.get('file_name') or Path(processing.get('path', '')).name} "
            f"({upload_percent}%)"
        )

    missing = required_env_status()
    config_text = "ok" if not missing else f"missing {', '.join(missing)}"
    telegram_label = proc_label(telegram_proc)
    rubika_label = proc_label(rubika_proc)
    runtime_storage = (
        storage_size(DOWNLOAD_DIR)
        + storage_size(QUEUE_DIR)
        + storage_size(SESSION_DIR)
    )
    status = "\n".join(
        [
            f"Telegram bot: {telegram_label}",
            f"Rubika worker: {rubika_label}",
            f"Config: {config_text}",
            f"Rubika session: {settings['rubika_session']}",
            f"Destination: {settings['rubika_target_title']} ({settings['rubika_target']})",
            f"Data dir: {DATA_DIR}",
            f"Queue: {queue_count}",
            f"Active upload: {active}",
            f"Stale upload state: {'yes, run /cleanup confirm' if stale_processing else 'no'}",
            f"Failed transfers: {failed_count}",
            f"Runtime storage: {human_size(runtime_storage)}",
        ]
    )

    with STATE_LOCK:
        logs = "\n".join(LOG_LINES) or "No logs yet."
    return {
        "status": status,
        "logs": logs,
        "updated_at": time.strftime("%H:%M:%S"),
        "metrics": {
            "telegram": telegram_label,
            "rubika": rubika_label,
            "config": config_text,
            "rubika_session": settings["rubika_session"],
            "destination": f"{settings['rubika_target_title']} ({settings['rubika_target']})",
            "data_dir": str(DATA_DIR),
            "queue": queue_count,
            "active_upload": active,
            "failed": failed_count,
            "runtime_storage": human_size(runtime_storage),
            "upload_percent": upload_percent,
            "stale_processing": stale_processing,
        },
    }


def dashboard_text() -> tuple[str, str]:
    snapshot = dashboard_snapshot()
    return snapshot["status"], snapshot["logs"]


def dashboard_payload() -> dict:
    return dashboard_snapshot()


def render_dashboard() -> bytes:
    payload = dashboard_payload()
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WalrusHF</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07100f;
      --bg-2: #0b1718;
      --panel: rgba(13, 28, 29, 0.82);
      --panel-strong: rgba(18, 39, 39, 0.94);
      --line: rgba(180, 221, 209, 0.16);
      --line-strong: rgba(180, 221, 209, 0.28);
      --text: #f4f0df;
      --muted: #9fb0a8;
      --accent: #65e0af;
      --accent-2: #e8c36d;
      --danger: #ff7a7a;
      --warn: #f6c66a;
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.42);
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Avenir Next", "Trebuchet MS", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background:
        radial-gradient(circle at 12% 8%, rgba(101, 224, 175, 0.18), transparent 28rem),
        radial-gradient(circle at 85% 18%, rgba(232, 195, 109, 0.13), transparent 24rem),
        linear-gradient(135deg, var(--bg), var(--bg-2) 52%, #102020);
      color: var(--text);
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(244, 240, 223, 0.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(244, 240, 223, 0.025) 1px, transparent 1px);
      background-size: 34px 34px;
      mask-image: linear-gradient(to bottom, rgba(0,0,0,0.85), transparent 78%);
    }}
    main {{
      position: relative;
      max-width: 1180px;
      margin: 0 auto;
      padding: 34px 20px 48px;
    }}
    .hero {{
      position: relative;
      display: grid;
      grid-template-columns: auto 1fr auto;
      align-items: center;
      gap: 20px;
      min-height: 176px;
      padding: 28px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background:
        linear-gradient(135deg, rgba(12, 34, 34, 0.92), rgba(11, 24, 26, 0.78)),
        repeating-linear-gradient(120deg, rgba(255,255,255,0.03) 0 1px, transparent 1px 18px);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .hero::after {{
      content: "";
      position: absolute;
      width: 360px;
      height: 360px;
      right: max(-120px, -9vw);
      top: -145px;
      border: 1px solid rgba(232, 195, 109, 0.22);
      border-radius: 50%;
      box-shadow: inset 0 0 0 36px rgba(232, 195, 109, 0.03);
      pointer-events: none;
    }}
    .mark {{
      display: grid;
      place-items: center;
      width: 72px;
      height: 72px;
      border: 1px solid rgba(232, 195, 109, 0.42);
      border-radius: 8px;
      background: linear-gradient(145deg, rgba(232, 195, 109, 0.18), rgba(101, 224, 175, 0.08));
      font-size: 38px;
      box-shadow: 0 14px 36px rgba(0, 0, 0, 0.28);
    }}
    .kicker {{
      margin: 0 0 7px;
      color: var(--accent-2);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0 0 8px;
      font-family: Georgia, "Times New Roman", serif;
      font-size: clamp(40px, 7vw, 78px);
      line-height: 0.92;
      font-weight: 800;
      letter-spacing: 0;
    }}
    p {{
      color: var(--muted);
      margin: 0;
      max-width: 660px;
      line-height: 1.6;
    }}
    .live {{
      display: flex;
      align-items: center;
      gap: 9px;
      flex: 0 0 auto;
      align-self: start;
      color: var(--text);
      border: 1px solid rgba(101, 224, 175, 0.28);
      border-radius: 8px;
      padding: 10px 12px;
      background: rgba(101, 224, 175, 0.08);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
    }}
    .live::before {{
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 18px var(--accent);
    }}
    .live[data-state="stale"] {{
      color: var(--warn);
      border-color: rgba(246, 198, 106, 0.35);
      background: rgba(246, 198, 106, 0.08);
    }}
    .live[data-state="stale"]::before {{
      background: var(--warn);
      box-shadow: 0 0 18px var(--warn);
    }}
    .status-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0;
    }}
    .tile, section {{
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      box-shadow: 0 12px 42px rgba(0, 0, 0, 0.2);
      backdrop-filter: blur(12px);
    }}
    .tile {{
      min-height: 116px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .tile span, .row span {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .tile strong {{
      display: block;
      margin-top: 14px;
      color: var(--text);
      font-size: 20px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }}
    .tile[data-good="true"] strong {{
      color: var(--accent);
    }}
    .tile[data-warn="true"] strong {{
      color: var(--warn);
    }}
    .deck-grid {{
      display: grid;
      grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.08fr);
      gap: 16px;
      align-items: stretch;
    }}
    section {{
      overflow: hidden;
    }}
    h2 {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      font-size: 13px;
      font-weight: 900;
      color: var(--accent-2);
      margin: 0;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      text-transform: uppercase;
      letter-spacing: 0.1em;
      background: rgba(255, 255, 255, 0.025);
    }}
    .panel-body {{
      padding: 10px 16px 16px;
    }}
    .row {{
      display: grid;
      grid-template-columns: 138px minmax(0, 1fr);
      gap: 14px;
      align-items: start;
      padding: 12px 0;
      border-bottom: 1px solid rgba(180, 221, 209, 0.1);
    }}
    .row:last-child {{
      border-bottom: 0;
    }}
    .row strong {{
      overflow-wrap: anywhere;
      font-size: 14px;
      line-height: 1.45;
    }}
    .progress-shell {{
      height: 12px;
      margin-top: 12px;
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid rgba(101, 224, 175, 0.25);
      background: rgba(0, 0, 0, 0.28);
    }}
    .progress-fill {{
      width: 0%;
      height: 100%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      box-shadow: 0 0 20px rgba(101, 224, 175, 0.5);
      transition: width 350ms ease;
    }}
    pre {{
      margin: 0;
      padding: 16px;
      max-height: 460px;
      overflow: auto;
      white-space: pre-wrap;
      color: #d5e3dc;
      background:
        linear-gradient(180deg, rgba(0, 0, 0, 0.24), rgba(0, 0, 0, 0.08));
      font: 13px/1.6 "SF Mono", "Cascadia Code", ui-monospace, Menlo, Consolas, monospace;
    }}
    .raw-status {{
      margin-top: 16px;
    }}
    a {{ color: var(--accent); }}
    @media (max-width: 860px) {{
      .hero {{
        grid-template-columns: 1fr;
        min-height: auto;
      }}
      .mark {{
        width: 58px;
        height: 58px;
        font-size: 30px;
      }}
      .live {{
        justify-self: start;
      }}
      .status-grid, .deck-grid {{
        grid-template-columns: 1fr;
      }}
      .row {{
        grid-template-columns: 1fr;
        gap: 4px;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header class="hero">
      <div class="mark" aria-hidden="true">⛵</div>
      <div>
        <p class="kicker">Hugging Face Control Deck</p>
        <h1>WalrusHF</h1>
        <p>This Space keeps the Telegram bot and Rubika upload worker running. Use Telegram as the control panel while this deck watches the machinery.</p>
      </div>
      <span id="live" class="live">Live</span>
    </header>

    <div class="status-grid" aria-label="Service status">
      <article class="tile" id="telegram-card">
        <span>Telegram</span>
        <strong id="telegram-value">{html.escape(payload["metrics"]["telegram"])}</strong>
      </article>
      <article class="tile" id="rubika-card">
        <span>Rubika Worker</span>
        <strong id="rubika-value">{html.escape(payload["metrics"]["rubika"])}</strong>
      </article>
      <article class="tile" id="queue-card">
        <span>Queue</span>
        <strong id="queue-value">{html.escape(str(payload["metrics"]["queue"]))}</strong>
      </article>
      <article class="tile" id="failed-card">
        <span>Failed</span>
        <strong id="failed-value">{html.escape(str(payload["metrics"]["failed"]))}</strong>
      </article>
    </div>

    <div class="deck-grid">
      <section>
        <h2>Ship Systems</h2>
        <div class="panel-body">
          <div class="row"><span>Config</span><strong id="config-value">{html.escape(payload["metrics"]["config"])}</strong></div>
          <div class="row"><span>Session</span><strong id="session-value">{html.escape(payload["metrics"]["rubika_session"])}</strong></div>
          <div class="row"><span>Destination</span><strong id="destination-value">{html.escape(payload["metrics"]["destination"])}</strong></div>
          <div class="row"><span>Storage</span><strong id="storage-value">{html.escape(payload["metrics"]["runtime_storage"])}</strong></div>
          <div class="row"><span>Stale State</span><strong id="stale-value">{"yes - run /cleanup confirm" if payload["metrics"]["stale_processing"] else "none"}</strong></div>
          <div class="row"><span>Data Dir</span><strong id="data-dir-value">{html.escape(payload["metrics"]["data_dir"])}</strong></div>
        </div>
      </section>

      <section>
        <h2>Active Upload</h2>
        <div class="panel-body">
          <div class="row"><span>Task</span><strong id="active-value">{html.escape(payload["metrics"]["active_upload"])}</strong></div>
          <div class="row"><span>Progress</span><strong id="upload-percent-value">{html.escape(str(payload["metrics"]["upload_percent"]))}%</strong></div>
          <div class="progress-shell" aria-hidden="true"><div id="upload-bar" class="progress-fill"></div></div>
        </div>
      </section>
    </div>

    <section class="raw-status">
      <h2>Raw Status</h2>
      <pre id="status">{html.escape(payload["status"])}</pre>
    </section>

    <section>
      <h2>Logs</h2>
      <pre id="logs">{html.escape(payload["logs"])}</pre>
    </section>
    <noscript>
      <p>JavaScript is disabled. Refresh the page to update status.</p>
    </noscript>
  </main>
  <script>
    const statusEl = document.getElementById("status");
    const logsEl = document.getElementById("logs");
    const liveEl = document.getElementById("live");
    const fields = {{
      telegram: document.getElementById("telegram-value"),
      rubika: document.getElementById("rubika-value"),
      queue: document.getElementById("queue-value"),
      failed: document.getElementById("failed-value"),
      config: document.getElementById("config-value"),
      session: document.getElementById("session-value"),
      destination: document.getElementById("destination-value"),
      storage: document.getElementById("storage-value"),
      stale: document.getElementById("stale-value"),
      dataDir: document.getElementById("data-dir-value"),
      active: document.getElementById("active-value"),
      uploadPercent: document.getElementById("upload-percent-value"),
    }};
    const cards = {{
      telegram: document.getElementById("telegram-card"),
      rubika: document.getElementById("rubika-card"),
      queue: document.getElementById("queue-card"),
      failed: document.getElementById("failed-card"),
    }};
    const uploadBar = document.getElementById("upload-bar");

    function setText(element, value) {{
      if (element) element.textContent = value ?? "";
    }}

    function updateCardState(card, value, goodTest) {{
      if (!card) return;
      const text = String(value ?? "");
      card.dataset.good = goodTest(text) ? "true" : "false";
      card.dataset.warn = !goodTest(text) && text !== "0" ? "true" : "false";
    }}

    async function refreshDashboard() {{
      try {{
        const response = await fetch("/status.json", {{ cache: "no-store" }});
        if (!response.ok) throw new Error(`HTTP ${{response.status}}`);
        const data = await response.json();
        const metrics = data.metrics || {{}};
        statusEl.textContent = data.status || "";
        logsEl.textContent = data.logs || "";
        setText(fields.telegram, metrics.telegram);
        setText(fields.rubika, metrics.rubika);
        setText(fields.queue, metrics.queue);
        setText(fields.failed, metrics.failed);
        setText(fields.config, metrics.config);
        setText(fields.session, metrics.rubika_session);
        setText(fields.destination, metrics.destination);
        setText(fields.storage, metrics.runtime_storage);
        setText(fields.stale, metrics.stale_processing ? "yes - run /cleanup confirm" : "none");
        setText(fields.dataDir, metrics.data_dir);
        setText(fields.active, metrics.active_upload);
        setText(fields.uploadPercent, `${{metrics.upload_percent || 0}}%`);
        uploadBar.style.width = `${{Math.max(0, Math.min(100, metrics.upload_percent || 0))}}%`;
        updateCardState(cards.telegram, metrics.telegram, value => value.includes("running"));
        updateCardState(cards.rubika, metrics.rubika, value => value.includes("running"));
        updateCardState(cards.queue, metrics.queue, value => Number(value) === 0);
        updateCardState(cards.failed, metrics.failed, value => Number(value) === 0);
        liveEl.textContent = `Live · ${{data.updated_at || "--:--:--"}}`;
        liveEl.dataset.state = "live";
      }} catch (error) {{
        liveEl.textContent = "Live paused";
        liveEl.dataset.state = "stale";
      }}
    }}

    refreshDashboard();
    setInterval(refreshDashboard, 2000);
  </script>
</body>
</html>
"""
    return page.encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    def send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_body(b"ok\n", "text/plain; charset=utf-8")
            return
        if path == "/status.json":
            self.send_body(
                json.dumps(dashboard_payload()).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        self.send_body(render_dashboard(), "text/html; charset=utf-8")

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def log_message(self, _format: str, *_args) -> None:
        return


if __name__ == "__main__":
    ensure_supervisor()
    port = int(os.getenv("PORT", "7860"))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    append_log("web", f"serving dashboard on 0.0.0.0:{port}")
    server.serve_forever()
