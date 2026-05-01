"""
YT Downloader Pro — bulletproof multi-URL downloader
Dash + Flask, deployable on Render or any Linux server.
"""

import os
import re
import uuid
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import send_file as flask_send_file, abort
from dash import Dash, html, dcc, Input, Output, State, callback_context
import dash_mantine_components as dmc

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TMP_DIR = "/tmp/yt_downloads"
os.makedirs(TMP_DIR, exist_ok=True)

# Global download state
_lock = threading.Lock()
_queue = {}  # uid -> dict
_procs = {}  # uid -> subprocess.Popen
_stop = threading.Event()


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _safe(s: str) -> str:
    """Strip filesystem-unsafe characters from a string."""
    return re.sub(r'[\\/*?:"<>|\r\n]', "_", s).strip()[:100] or "download"


def _set(uid, **kwargs):
    """Thread-safe update of a queue entry."""
    with _lock:
        if uid in _queue:
            _queue[uid].update(kwargs)


def _run(cmd: list[str]) -> tuple[int, str, str]:
    """
    Run a subprocess, capture stdout + stderr fully without deadlocks.
    Returns (returncode, stdout, stderr).
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=600)  # 10 min hard limit per item
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return -1, stdout, f"TIMEOUT after 600s\n{stderr}"
    return proc.returncode, stdout, stderr


# ─────────────────────────────────────────────
# TITLE FETCH  (best-effort, never blocks download)
# ─────────────────────────────────────────────
def _fetch_title(uid: str, url: str):
    try:
        rc, out, _ = _run(
            [
                "yt-dlp",
                "--no-playlist",
                "--print",
                "%(title)s [%(duration_string)s]",
                url,
            ]
        )
        if rc == 0 and out.strip():
            _set(uid, title=out.strip().splitlines()[0])
    except Exception:
        pass


# ─────────────────────────────────────────────
# CORE DOWNLOAD
# ─────────────────────────────────────────────
def _download(uid: str, url: str, dtype: str, afmt: str, vres: str, embed_thumb: bool):
    if _stop.is_set():
        _set(uid, status="cancelled")
        return

    _set(uid, status="downloading", log=[])

    # Each download gets its own uniquely-named output file
    # so concurrent downloads never overwrite each other.
    out_stem = os.path.join(TMP_DIR, uid)  # e.g. /tmp/yt_downloads/<uuid>

    if dtype == "audio":
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-x",
            "--audio-format",
            afmt,
            "--audio-quality",
            "0",
            "-o",
            f"{out_stem}.%(ext)s",
        ]
        if embed_thumb:
            cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]

    else:  # video
        # Broad format selector with multiple fallbacks:
        # 1. Best split video+audio up to requested height
        # 2. Best combined stream up to requested height
        # 3. Absolute best available (no height constraint)
        if vres == "best":
            fmt = "bestvideo+bestaudio/best"
        else:
            fmt = f"bestvideo[height<={vres}]+bestaudio/best[height<={vres}]/best"

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f",
            fmt,
            "--merge-output-format",
            "mp4",
            # Re-encode to H.264+AAC so the file plays in every browser/player.
            # Targets the Merger post-processor specifically to avoid flag conflicts.
            "--postprocessor-args",
            "Merger+ffmpeg:-c:v libx264 -c:a aac -pix_fmt yuv420p -movflags +faststart",
            "-o",
            f"{out_stem}.%(ext)s",
        ]

    # --print after_move:filepath tells yt-dlp to print the final path after all
    # post-processing is done — 100% reliable way to find the output file.
    cmd += ["--print", "after_move:filepath", url]

    try:
        rc, stdout, stderr = _run(cmd)
    except FileNotFoundError:
        _set(
            uid,
            status="failed",
            log=[
                "ERROR: yt-dlp not found. Make sure it is installed (pip install yt-dlp)."
            ],
        )
        return
    except Exception as exc:
        _set(uid, status="failed", log=[f"ERROR: {exc}"])
        return

    # Build a clean log for display regardless of success/failure
    log_lines = []
    if stderr.strip():
        for line in stderr.strip().splitlines():
            line = line.strip()
            if line:
                log_lines.append(line)

    if rc != 0:
        # Surface the most useful error lines (last 10 non-blank lines of stderr)
        useful = [l for l in log_lines if l] or ["No stderr output captured."]
        _set(uid, status="failed", log=useful[-10:])
        return

    # Find the actual output file from yt-dlp's --print after_move output
    filepath = None
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line and os.path.isfile(line):
            filepath = line
            break

    if not filepath:
        # Fallback: scan TMP_DIR for files whose name starts with the uid
        candidates = [
            os.path.join(TMP_DIR, f)
            for f in os.listdir(TMP_DIR)
            if f.startswith(uid) and not f.endswith(".part")
        ]
        if candidates:
            filepath = max(candidates, key=os.path.getmtime)

    if not filepath or not os.path.isfile(filepath):
        _set(
            uid,
            status="failed",
            log=[
                "Download appeared to succeed but output file not found.",
                f"stdout: {stdout[:300]}",
                f"stderr tail: {stderr[-300:]}",
            ],
        )
        return

    _set(
        uid,
        status="done",
        progress=100,
        filepath=filepath,
        log=log_lines[-5:] if log_lines else [],
    )


# ─────────────────────────────────────────────
# BATCH RUNNER
# ─────────────────────────────────────────────
def _run_batch(uids, urls, dtype, afmt, vres, embed_thumb, max_workers):
    # Fetch titles first (all in parallel, non-blocking for downloads)
    with ThreadPoolExecutor(max_workers=min(4, len(uids))) as pool:
        for uid, url in zip(uids, urls):
            pool.submit(_fetch_title, uid, url)

    # Download in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {
            pool.submit(_download, uid, url, dtype, afmt, vres, embed_thumb): uid
            for uid, url in zip(uids, urls)
        }
        for fut in futs:
            try:
                fut.result()
            except Exception as exc:
                uid = futs[fut]
                _set(uid, status="failed", log=[f"Unhandled exception: {exc}"])


# ─────────────────────────────────────────────
# APP + THEME
# ─────────────────────────────────────────────
app = Dash(__name__)
app.title = "YT Downloader Pro"

GFONTS = html.Link(
    rel="stylesheet",
    href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Syne:wght@400;600;700&display=swap",
)

THEME = {
    "colorScheme": "dark",
    "primaryColor": "yellow",
    "fontFamily": "'Syne',sans-serif",
    "colors": {
        "dark": [
            "#e8e4dc",
            "#c9c5bd",
            "#a8a49e",
            "#888580",
            "#57554f",
            "#2a2a2a",
            "#1e1e1e",
            "#161616",
            "#101010",
            "#0e0e0e",
        ],
    },
}

_SLBL = lambda t: {
    "fontFamily": "'JetBrains Mono',monospace",
    "fontSize": "10px",
    "color": "#888",
    "textTransform": "uppercase",
    "letterSpacing": "0.5px",
    "marginBottom": "6px",
}

app.layout = dmc.MantineProvider(
    theme=THEME,
    children=[
        GFONTS,
        dcc.Interval(id="tick", interval=800, n_intervals=0),
        dmc.Container(
            size="sm",
            py=30,
            children=[
                # Brand
                dmc.Group(
                    mb=20,
                    align="center",
                    children=[
                        dmc.ThemeIcon(
                            size="lg",
                            radius="md",
                            color="yellow",
                            variant="filled",
                            children=dmc.Text("▶", size="sm", fw=700, c="dark.9"),
                        ),
                        dmc.Text(
                            "YT Downloader Pro",
                            size="xl",
                            fw=700,
                            style={
                                "color": "#f0f0ee",
                                "fontFamily": "'Syne',sans-serif",
                            },
                        ),
                        dmc.Badge(
                            "cloud",
                            color="yellow",
                            variant="outline",
                            style={"fontFamily": "'JetBrains Mono',monospace"},
                        ),
                    ],
                ),
                # Stats
                dmc.SimpleGrid(
                    cols=3,
                    mb=16,
                    children=[
                        html.Div(
                            [
                                html.Div("0", id="s-total", className="snum"),
                                html.Div("Queued", className="slbl"),
                            ],
                            className="stile",
                        ),
                        html.Div(
                            [
                                html.Div(
                                    "0",
                                    id="s-done",
                                    className="snum",
                                    style={"color": "#4caf7d"},
                                ),
                                html.Div("Done", className="slbl"),
                            ],
                            className="stile",
                        ),
                        html.Div(
                            [
                                html.Div(
                                    "0",
                                    id="s-fail",
                                    className="snum",
                                    style={"color": "#d45b5b"},
                                ),
                                html.Div("Failed", className="slbl"),
                            ],
                            className="stile",
                        ),
                    ],
                ),
                # URLs
                html.Span("URLs", className="lbl"),
                dmc.Paper(
                    className="card",
                    p="md",
                    mb=12,
                    children=[
                        dmc.Textarea(
                            id="urls",
                            minRows=4,
                            placeholder="One YouTube URL per line\nhttps://youtube.com/watch?v=AAA\nhttps://youtube.com/watch?v=BBB",
                            styles={
                                "input": {
                                    "fontFamily": "'JetBrains Mono',monospace",
                                    "fontSize": "12px",
                                }
                            },
                        ),
                    ],
                ),
                # Format
                html.Span("Format", className="lbl"),
                dmc.Paper(
                    className="card",
                    p="md",
                    mb=12,
                    children=[
                        dmc.SegmentedControl(
                            id="dtype",
                            value="audio",
                            fullWidth=True,
                            color="yellow",
                            mb=12,
                            data=[
                                {"label": "Audio", "value": "audio"},
                                {"label": "Video", "value": "video"},
                            ],
                        ),
                        dmc.Group(
                            grow=True,
                            children=[
                                dmc.Select(
                                    id="afmt",
                                    label="Audio format",
                                    data=["mp3", "wav", "flac", "m4a"],
                                    value="mp3",
                                    styles={"label": _SLBL("x")},
                                ),
                                dmc.Select(
                                    id="vres",
                                    label="Video resolution",
                                    data=["480", "720", "1080", "best"],
                                    value="1080",
                                    styles={"label": _SLBL("x")},
                                ),
                                dmc.Select(
                                    id="workers",
                                    label="Parallel",
                                    data=["1", "2", "3", "5"],
                                    value="2",
                                    styles={"label": _SLBL("x")},
                                ),
                            ],
                        ),
                    ],
                ),
                # Options
                html.Span("Options", className="lbl"),
                dmc.Paper(
                    className="card",
                    p="md",
                    mb=16,
                    children=[
                        dmc.Switch(
                            id="opt-thumb",
                            checked=True,
                            color="yellow",
                            label="Embed thumbnail (audio only)",
                            styles={
                                "label": {"color": "#bbb"},
                                "description": {"color": "#555"},
                            },
                        ),
                    ],
                ),
                # Buttons
                dmc.Group(
                    mb=16,
                    children=[
                        dmc.Button(
                            "Start Downloads",
                            id="btn-start",
                            color="yellow",
                            style={
                                "flex": "1",
                                "fontWeight": "700",
                                "color": "#0e0e0e",
                                "fontFamily": "'Syne',sans-serif",
                            },
                        ),
                        dmc.Button(
                            "Cancel All",
                            id="btn-cancel",
                            color="red",
                            variant="outline",
                            style={"fontFamily": "'Syne',sans-serif"},
                        ),
                    ],
                ),
                # Queue
                html.Span("Queue", className="lbl"),
                dmc.Paper(
                    className="card",
                    p="md",
                    id="queue-panel",
                    children=[
                        dmc.Text(
                            "No downloads yet.",
                            c="dimmed",
                            size="sm",
                            style={
                                "textAlign": "center",
                                "padding": "1.5rem 0",
                                "fontFamily": "'JetBrains Mono',monospace",
                            },
                        ),
                    ],
                ),
                html.Div(id="notif-area"),
            ],
        ),
    ],
)


# ─────────────────────────────────────────────
# FLASK SERVE ROUTE
# ─────────────────────────────────────────────
@app.server.route("/serve/<path:filename>")
def serve_file(filename):
    # Security: only serve files inside TMP_DIR
    filepath = os.path.join(TMP_DIR, filename)
    real = os.path.realpath(filepath)
    if not real.startswith(os.path.realpath(TMP_DIR)):
        abort(403)
    if not os.path.isfile(real):
        abort(404)
    return flask_send_file(real, as_attachment=True)


# ─────────────────────────────────────────────
# BUILD QUEUE UI
# ─────────────────────────────────────────────
def _build_queue(state: dict):
    if not state:
        return [
            dmc.Text(
                "No downloads yet.",
                c="dimmed",
                size="sm",
                style={
                    "textAlign": "center",
                    "padding": "1.5rem 0",
                    "fontFamily": "'JetBrains Mono',monospace",
                },
            )
        ]
    rows = []
    for uid, item in state.items():
        status = item.get("status", "queued")
        progress = item.get("progress", 0)
        title = item.get("title", item.get("url", uid))
        url_s = item.get("url", "")
        if len(url_s) > 52:
            url_s = url_s[:52] + "…"

        children = [
            dmc.Group(
                justify="space-between",
                align="center",
                mb=4,
                children=[
                    dmc.Text(
                        title,
                        size="sm",
                        style={
                            "color": "#ddd",
                            "flex": "1",
                            "overflow": "hidden",
                            "whiteSpace": "nowrap",
                            "textOverflow": "ellipsis",
                            "maxWidth": "320px",
                        },
                    ),
                    html.Span(status, className=f"bdg bdg-{status}"),
                ],
            ),
            dmc.Text(
                url_s,
                size="xs",
                style={"fontFamily": "'JetBrains Mono',monospace", "color": "#555"},
            ),
        ]

        if status == "downloading":
            children.append(
                dmc.Progress(
                    value=max(progress, 2),
                    color="yellow",
                    size="xs",
                    mt=6,
                    animated=True,
                    striped=True,
                )
            )

        elif status == "done":
            children.append(dmc.Progress(value=100, color="teal", size="xs", mt=6))
            fp = item.get("filepath")
            if fp and os.path.isfile(fp):
                fname = os.path.basename(fp)
                children.append(
                    html.A(f"⬇  {fname}", href=f"/serve/{fname}", className="dl-btn")
                )
            else:
                children.append(
                    dmc.Text(
                        "File ready but path missing — check server logs.",
                        size="xs",
                        mt=4,
                        style={
                            "fontFamily": "'JetBrains Mono',monospace",
                            "color": "#f0a500",
                        },
                    )
                )

        elif status == "failed":
            log = item.get("log", [])
            if log:
                children.append(
                    html.Div(
                        [html.Div(l, className="errline") for l in log],
                        className="errlog",
                    )
                )

        rows.append(html.Div(children, className="qrow"))
    return rows


# ─────────────────────────────────────────────
# POLL CALLBACK  — updates queue UI every tick
# ─────────────────────────────────────────────
@app.callback(
    Output("queue-panel", "children"),
    Output("s-total", "children"),
    Output("s-done", "children"),
    Output("s-fail", "children"),
    Input("tick", "n_intervals"),
)
def poll(_):
    with _lock:
        snap = {k: dict(v) for k, v in _queue.items()}
    total = len(snap)
    done = sum(1 for v in snap.values() if v["status"] == "done")
    failed = sum(1 for v in snap.values() if v["status"] == "failed")
    return _build_queue(snap), str(total), str(done), str(failed)


# ─────────────────────────────────────────────
# MAIN CALLBACK  — start / cancel
# ─────────────────────────────────────────────
@app.callback(
    Output("notif-area", "children"),
    Input("btn-start", "n_clicks"),
    Input("btn-cancel", "n_clicks"),
    State("urls", "value"),
    State("dtype", "value"),
    State("afmt", "value"),
    State("vres", "value"),
    State("workers", "value"),
    State("opt-thumb", "checked"),
    prevent_initial_call=True,
)
def on_action(_start, _cancel, urls, dtype, afmt, vres, workers, embed_thumb):

    def notif(msg, color="yellow"):
        return dmc.Notification(
            id="notif",
            title="YT Downloader",
            message=msg,
            color=color,
            action="show",
            autoClose=4000,
        )

    triggered = callback_context.triggered_id

    # ── Cancel ──────────────────────────────────────────────────────
    if triggered == "btn-cancel":
        _stop.set()
        with _lock:
            for v in _queue.values():
                if v["status"] in ("queued", "downloading"):
                    v["status"] = "cancelled"
        return notif("Cancelled all pending downloads.", "red")

    # ── Start ───────────────────────────────────────────────────────
    raw_urls = [u.strip() for u in (urls or "").splitlines() if u.strip()]
    if not raw_urls:
        return notif("Please enter at least one URL.", "red")

    _stop.clear()

    # Register all items up front so the UI shows them immediately
    uids = []
    for url in raw_urls:
        uid = str(uuid.uuid4())
        uids.append(uid)
        with _lock:
            _queue[uid] = {
                "url": url,
                "title": url,  # replaced by _fetch_title shortly after
                "status": "queued",
                "progress": 0,
                "filepath": None,
                "log": [],
            }

    max_w = int(workers or 2)

    threading.Thread(
        target=_run_batch,
        args=(uids, raw_urls, dtype, afmt, vres, bool(embed_thumb), max_w),
        daemon=True,
    ).start()

    return notif(f"Started {len(raw_urls)} download(s).", "yellow")


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8051))
    app.run(debug=False, host="0.0.0.0", port=port)
