"""
server.py — PrismFind Flask backend for Replit
Serves the HTML frontend and the JSON API used by it.

Routes:
  GET  /              → amazon_finder.html
  POST /search        → start job, return {job_id}
  GET  /status/<id>   → poll {status, progress, stage, log, error}
  GET  /result/<id>   → final result JSON
"""

import os
import uuid
import threading
from flask import Flask, request, jsonify, send_from_directory
from scraper import run_search

app = Flask(__name__, static_folder=None)
HERE = os.path.dirname(os.path.abspath(__file__))

jobs: dict = {}
jobs_lock = threading.Lock()


# ── HTML ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(HERE, "amazon_finder.html")


# ── API ─────────────────────────────────────────────────────────────────────

@app.route("/search", methods=["POST"])
def search():
    data = request.get_json(force=True) or {}

    product = data.get("product", "").strip()
    if not product:
        return jsonify({"error": "No product specified"}), 400

    include_kw = [k.strip() for k in data.get("include_kw", "").split(",") if k.strip()]
    exclude_kw = [k.strip() for k in data.get("exclude_kw", "").split(",") if k.strip()]
    max_pages  = max(1, min(5, int(data.get("max_pages", 3))))
    sleep_sec  = max(1.0, min(15.0, float(data.get("sleep_sec", 4))))
    rw         = max(0.0, min(1.0, float(data.get("rw", 0.6))))

    job_id = uuid.uuid4().hex[:10]
    with jobs_lock:
        jobs[job_id] = {
            "status":   "running",
            "progress": 0,
            "stage":    "Starting…",
            "log":      [],
            "result":   None,
            "error":    None,
        }

    def _run():
        def log_fn(msg):
            with jobs_lock:
                jobs[job_id]["log"].append(msg)

        def progress_fn(pct):
            with jobs_lock:
                jobs[job_id]["progress"] = pct

        try:
            result = run_search(
                product=product,
                include_kw=include_kw,
                exclude_kw=exclude_kw,
                max_pages=max_pages,
                sleep_sec=sleep_sec,
                rating_weight=rw,
                log_fn=log_fn,
                progress_fn=progress_fn,
            )
            with jobs_lock:
                if "error" in result:
                    jobs[job_id]["status"] = "error"
                    jobs[job_id]["error"]  = result["error"]
                else:
                    jobs[job_id]["status"] = "done"
                    jobs[job_id]["result"] = result
        except Exception as exc:
            with jobs_lock:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["error"]  = str(exc)
                jobs[job_id]["log"].append(f"✗ Fatal error: {exc}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status":   job["status"],
        "progress": job["progress"],
        "stage":    job["stage"],
        "log":      list(job["log"]),
        "error":    job.get("error"),
    })


@app.route("/result/<job_id>")
def result(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": "Result not ready yet"}), 202
    return jsonify(job["result"])


# ── RUN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"PrismFind running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
