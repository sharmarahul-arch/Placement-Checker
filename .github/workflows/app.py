#!/usr/bin/env python3
"""Web UI for the Amazon Keyword Placement Checker.

Run:  python3 app.py
Then a form opens at http://localhost:5077 — enter brand name + keywords,
click Run, and the Excel report downloads when the check finishes.

The check runs in a background thread; the page polls /status and downloads
the file from /download when done (so the browser never waits on a long
request).
"""

import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import pandas as pd
from flask import Flask, request, send_file, Response, jsonify
from playwright.sync_api import sync_playwright

from queue import Queue, Empty

from placement_checker import check_keyword, human_pause, make_context

app = Flask(__name__)


@app.after_request
def allow_cors(resp):
    """Let a Netlify-hosted frontend call this local app from the browser."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # Chrome Private Network Access: HTTPS page -> localhost needs this on preflight
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


@app.route("/run", methods=["OPTIONS"])
@app.route("/status", methods=["OPTIONS"])
@app.route("/download", methods=["OPTIONS"])
def preflight():
    return Response("", status=204)

REPORTS_DIR = Path(__file__).resolve().parent / "reports"

JOB = {
    "state": "idle",        # idle | running | done | error
    "text": "",
    "file": None,           # Path of finished xlsx
    "lock": threading.Lock(),
}

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Amazon Placement Checker</title>
<style>
  body { font-family: -apple-system, Helvetica, Arial, sans-serif; background:#f4f6f8;
         margin:0; padding:40px; color:#1a1a2e; }
  .card { max-width:640px; margin:0 auto; background:#fff; border-radius:12px;
          padding:32px 36px; box-shadow:0 2px 12px rgba(0,0,0,.08); }
  h1 { font-size:22px; margin:0 0 4px; } .sub { color:#667; margin:0 0 24px; font-size:14px; }
  label { display:block; font-weight:600; font-size:13px; margin:16px 0 6px; }
  input, textarea, select { width:100%; box-sizing:border-box; padding:10px 12px;
         border:1px solid #ccd; border-radius:8px; font-size:14px; font-family:inherit; }
  textarea { resize:vertical; }
  .row { display:flex; gap:16px; } .row > div { flex:1; }
  .hint { font-size:12px; color:#889; margin-top:4px; }
  button { margin-top:24px; width:100%; padding:13px; border:0; border-radius:8px;
           background:#ff9900; color:#111; font-size:16px; font-weight:700; cursor:pointer; }
  button:hover { background:#ffad33; } button:disabled { background:#e3e6ea; color:#99a; cursor:default; }
  #status { margin-top:16px; font-size:14px; text-align:center; color:#456; min-height:20px; }
  .err { color:#c0392b; }
  .spin { display:inline-block; width:14px; height:14px; border:2px solid #ccd;
          border-top-color:#ff9900; border-radius:50%; animation:s 1s linear infinite;
          vertical-align:-2px; margin-right:8px; }
  @keyframes s { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="card">
  <h1>Amazon Keyword Placement Checker</h1>
  <p class="sub">Enter your brand and keywords &mdash; a browser will open, check every keyword, and the Excel report will download automatically.</p>

  <label>Brand name</label>
  <input id="brand" placeholder="e.g. MuscleBlaze">

  <label>Your ASINs <span style="font-weight:400">(optional, one per line or comma separated)</span></label>
  <textarea id="asins" rows="3" placeholder="B0XXXXXXXX&#10;B0YYYYYYYY"></textarea>
  <div class="hint">ASINs give exact matching; brand name matches by product title.</div>

  <label>Keywords (one per line)</label>
  <textarea id="keywords" rows="6" placeholder="whey protein&#10;protein powder 1kg"></textarea>

  <div class="row">
    <div>
      <label>Marketplace</label>
      <select id="marketplace">
        <option value="https://www.amazon.in" selected>Amazon.in (India)</option>
        <option value="https://www.amazon.com">Amazon.com (US)</option>
        <option value="https://www.amazon.ae">Amazon.ae (UAE)</option>
        <option value="https://www.amazon.co.uk">Amazon.co.uk (UK)</option>
        <option value="https://www.amazon.de">Amazon.de (Germany)</option>
      </select>
    </div>
    <div>
      <label>Pages to scan per keyword</label>
      <select id="pages">
        <option>1</option><option>2</option><option selected>3</option>
        <option>4</option><option>5</option>
      </select>
    </div>
    <div>
      <label>Speed</label>
      <select id="workers">
        <option value="1">1 window (safest)</option>
        <option value="2" selected>2 windows (fast)</option>
        <option value="3">3 windows (faster)</option>
        <option value="4">4 windows (fastest)</option>
      </select>
    </div>
  </div>
  <div class="hint">More windows = keywords checked in parallel = faster. If Amazon starts showing CAPTCHAs, drop back to 1&ndash;2.</div>

  <button id="run" onclick="run()">Run placement check</button>
  <div id="status"></div>
</div>

<script>
const statusEl = document.getElementById('status');
const btn = document.getElementById('run');
let timer = null;

function setStatus(html, isErr) {
  statusEl.innerHTML = html;
  statusEl.className = isErr ? 'err' : '';
}

async function pollOnce() {
  const r = await fetch('/status');
  return await r.json();
}

function startPolling() {
  btn.disabled = true;
  timer = setInterval(async () => {
    let s;
    try { s = await pollOnce(); }
    catch (e) { setStatus('Lost connection to the app &mdash; is the Terminal window still open?', true); return; }
    if (s.state === 'running') {
      setStatus('<span class="spin"></span>' + s.text);
    } else if (s.state === 'done') {
      clearInterval(timer); timer = null;
      btn.disabled = false;
      setStatus('Done &mdash; downloading Excel…');
      const a = document.createElement('a');
      a.href = '/download';
      a.click();
      setTimeout(() => setStatus('Done &mdash; Excel downloaded (also saved in the reports folder).'), 1500);
    } else if (s.state === 'error') {
      clearInterval(timer); timer = null;
      btn.disabled = false;
      setStatus('Error: ' + s.text, true);
    }
  }, 2000);
}

async function run() {
  const brand = document.getElementById('brand').value.trim();
  const keywords = document.getElementById('keywords').value.split('\\n').map(s => s.trim()).filter(Boolean);
  const asins = document.getElementById('asins').value.split(/[\\n,]/).map(s => s.trim()).filter(Boolean);
  if (!brand && !asins.length) { setStatus('Enter a brand name or at least one ASIN.', true); return; }
  if (!keywords.length) { setStatus('Enter at least one keyword.', true); return; }

  let resp;
  try {
    resp = await fetch('/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        brand: brand, asins: asins, keywords: keywords,
        marketplace: document.getElementById('marketplace').value,
        pages: parseInt(document.getElementById('pages').value),
        workers: parseInt(document.getElementById('workers').value)
      })
    });
  } catch (e) {
    setStatus('Could not reach the app &mdash; is the Terminal window still open?', true);
    return;
  }
  if (!resp.ok) { setStatus('Error: ' + await resp.text(), true); return; }
  setStatus('<span class="spin"></span>Starting browser…');
  startPolling();
}

// If a run is already in progress (e.g. page was reloaded), resume showing it.
window.addEventListener('load', async () => {
  try {
    const s = await pollOnce();
    if (s.state === 'running') { setStatus('<span class="spin"></span>' + s.text); startPolling(); }
  } catch (e) {}
});
</script>
</body>
</html>"""


def keyword_worker(cfg, kw_queue, results, progress, progress_lock):
    """One worker = one browser window. Pulls keywords off the queue until empty.
    Each thread needs its own Playwright instance (sync API is not thread-safe)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False,
                                    args=["--disable-blink-features=AutomationControlled"])
        page = make_context(browser).new_page()
        while True:
            try:
                kw = kw_queue.get_nowait()
            except Empty:
                break
            with progress_lock:
                JOB["text"] = "Checked %d of %d keywords — now checking: %s" % (
                    progress["done"], progress["total"], kw)
            try:
                rows = check_keyword(page, cfg, kw)
            except Exception as e:
                rows = [{"keyword": kw, "asin": "-", "product_title": "ERROR: %s" % e,
                         "placement_type": "-", "page": "-", "position_on_page": "-",
                         "overall_position": "-", "organic_rank": "-", "matched_by": "-"}]
            with progress_lock:
                results.extend(rows)
                progress["done"] += 1
                JOB["text"] = "Checked %d of %d keywords…" % (progress["done"], progress["total"])
            human_pause(cfg)
        browser.close()


def run_check(cfg):
    """Background job: fan keywords out to parallel workers, save xlsx, update JOB."""
    try:
        kw_queue = Queue()
        for kw in cfg["keywords"]:
            kw_queue.put(kw)
        results = []
        progress = {"done": 0, "total": len(cfg["keywords"])}
        progress_lock = threading.Lock()
        n_workers = min(cfg["workers"], len(cfg["keywords"]))
        JOB["text"] = "Opening %d browser window(s)…" % n_workers
        threads = [threading.Thread(target=keyword_worker,
                                    args=(cfg, kw_queue, results, progress, progress_lock),
                                    daemon=True)
                   for _ in range(n_workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        JOB["text"] = "Building Excel report…"
        order = {kw: i for i, kw in enumerate(cfg["keywords"])}
        results.sort(key=lambda r: order.get(r["keyword"], 999))
        df = pd.DataFrame(results)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        REPORTS_DIR.mkdir(exist_ok=True)
        out = REPORTS_DIR / ("placements_%s.xlsx" % stamp)
        df.to_excel(out, index=False)
        JOB["file"] = out
        JOB["state"] = "done"
        JOB["text"] = "done"
    except Exception as e:
        JOB["state"] = "error"
        JOB["text"] = str(e)
    finally:
        JOB["lock"].release()


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/status")
def status():
    return jsonify({"state": JOB["state"], "text": JOB["text"]})


@app.route("/download")
def download():
    if JOB["file"] and Path(JOB["file"]).exists():
        return send_file(JOB["file"], as_attachment=True,
                         download_name=Path(JOB["file"]).name)
    return Response("No report available yet.", status=404)


@app.route("/run", methods=["POST"])
def run():
    if not JOB["lock"].acquire(blocking=False):
        return Response("A check is already running — wait for it to finish.", status=409)
    try:
        data = request.get_json(force=True)
        cfg = {
            "marketplace": data.get("marketplace", "https://www.amazon.in"),
            "brand_name": (data.get("brand") or "").strip(),
            "brand_asins": [a.strip().upper() for a in data.get("asins", []) if a.strip()],
            "keywords": [k.strip() for k in data.get("keywords", []) if k.strip()],
            "max_pages": max(1, min(int(data.get("pages", 3)), 10)),
            "workers": max(1, min(int(data.get("workers", 2)), 4)),
            "min_delay_seconds": 1,
            "max_delay_seconds": 2.5,
        }
        if not cfg["keywords"] or (not cfg["brand_name"] and not cfg["brand_asins"]):
            JOB["lock"].release()
            return Response("Give at least one keyword, and a brand name or ASIN.", status=400)
    except Exception as e:
        JOB["lock"].release()
        return Response("Bad request: %s" % e, status=400)

    JOB["state"] = "running"
    JOB["text"] = "Starting browser…"
    JOB["file"] = None
    threading.Thread(target=run_check, args=(cfg,), daemon=True).start()
    return jsonify({"started": True})


if __name__ == "__main__":
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5077")).start()
    print("\nAmazon Placement Checker running at  http://localhost:5077\n")
    app.run(host="127.0.0.1", port=5077, debug=False)
