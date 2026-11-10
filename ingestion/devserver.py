#!/usr/bin/env python3
"""devserver.py — local server for `wehpah preview` and `wehpah studio`.

Serves the explorer from the project root, maps /dist_data/ to the dossier's
configured output folder with real HTTP 206 range responses (Python's built-in
http.server ignores Range; DuckDB-Wasm and PDF.js need it), and hosts the Studio
page plus its small JSON API. It listens on 127.0.0.1 only.
"""

from __future__ import annotations

import errno
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

INGESTION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = INGESTION_DIR.parent
DATA_PREFIX = "/dist_data"


class Job:
    """One build or publish subprocess, with its output kept for the Studio."""

    def __init__(self, kind: str, root: Path, args: list[str]):
        self.kind, self.lines, self.code = kind, [], None
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(INGESTION_DIR / "wehpah.py"), "--dir", str(root), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))
            del self.lines[:-4000]  # ponytail: keep the tail only; the full log is the terminal's job
        self.code = self.proc.wait()

    @property
    def running(self):
        return self.code is None


_hash_memo: dict[tuple, str] = {}


def _sha(path: Path) -> str:
    import build_corpus
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    if key not in _hash_memo:
        _hash_memo[key] = build_corpus.sha256_file(path)
    return _hash_memo[key]


def studio_state(root: Path) -> dict:
    import build_corpus
    import wehpah
    state = {"config_file": str(root / wehpah.CONFIG_NAME), "config_exists": (root / wehpah.CONFIG_NAME).exists()}
    try:
        s = wehpah.load_settings(root)
    except Exception as exc:
        return {**state, "error": f"{wehpah.CONFIG_NAME}: {exc}"}
    state["settings"] = {"title": s.title, "description": s.description, "publish_to": s.publish_to,
                         "pdfs": str(s.pdfs), "output": str(s.output), "removed_dir": REMOVED_DIR}
    try:
        files = build_corpus.list_pdfs(s.pdfs)
        config = build_corpus.extraction_config(s.full_page_ocr)
        docs = []
        for p in files:
            meta = build_corpus.document_meta(p, s)
            _, cache_file = build_corpus.extraction_cache_file(s.cache, _sha(p), meta, config)
            docs.append({"file": p.name, "title": meta[1], "date": meta[2], "doc_id": meta[3],
                         "size": p.stat().st_size, "status": "read" if cache_file.exists() else "new"})
        state["documents"] = docs
    except Exception as exc:
        state["documents"], state["documents_error"] = [], str(exc)
    try:
        manifest = json.loads((s.output / "dossier.json").read_text())
        build = manifest.get("build") or {}
        state["release"] = {"title": manifest.get("title"), "documents": manifest.get("document_count"),
                            "passages": manifest.get("passage_count"), "trial_limit": build.get("trial_limit"),
                            "pages_without_text": sum(len(v) for v in (build.get("pages_without_text") or {}).values()),
                            "doc_ids": [f["doc_id"] for f in build.get("source_files", [])],
                            "built": os.path.getmtime(s.output / "dossier.json")}
    except (OSError, ValueError):
        state["release"] = None
    try:
        from huggingface_hub import get_token
        state["signed_in"] = bool(get_token())
    except Exception:
        state["signed_in"] = False
    return state


REMOVED_DIR = "Removed PDFs"


def safe_pdf_name(name: str, folder: Path) -> str:
    """A plain .pdf file name that does not clash with another document's ID."""
    import build_corpus
    name = name.strip()
    if not name or name != Path(name).name or name.startswith(".") or not name.lower().endswith(".pdf"):
        raise ValueError("Only PDF files can be added.")
    slug = build_corpus.slugify(name)
    for other in folder.glob("*"):
        if other.is_file() and other.suffix.lower() == ".pdf" and other.name != name and build_corpus.slugify(other.name) == slug:
            raise ValueError(f"{name} is too similar to the existing {other.name}; rename one of them.")
    return name


def choose_folder(start: Path) -> str:
    """Show the system's folder picker on this computer; '' when cancelled or unavailable."""
    prompt = "Choose the folder that holds the dossier's PDFs"
    if sys.platform == "darwin":
        script = (f'POSIX path of (choose folder with prompt "{prompt}" '
                  f'default location (POSIX file "{start if start.is_dir() else Path.home()}"))')
        cmd = ["osascript", "-e", script]
    elif sys.platform == "win32":
        cmd = ["powershell", "-NoProfile", "-Command",
               "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
               f"$d.Description = '{prompt}'; if ($d.ShowDialog() -eq 'OK') {{ $d.SelectedPath }}"]
    else:
        cmd = ["zenity", "--file-selection", "--directory", f"--title={prompt}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return ""  # no picker on this system: the path can still be typed in
    return result.stdout.strip().rstrip("/") if result.returncode == 0 else ""


def remove_pdf(folder: Path, name: str) -> str:
    """Move a PDF into "Removed PDFs" inside the folder (never deleted; builds skip subfolders)."""
    source = folder / Path(name).name
    if not name or Path(name).name != name or not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("That PDF is no longer in the folder.")
    bin_dir = folder / REMOVED_DIR
    bin_dir.mkdir(exist_ok=True)
    target, n = bin_dir / name, 1
    while target.exists():
        n += 1
        target = bin_dir / f"{source.stem} ({n}){source.suffix}"
    os.replace(source, target)
    return str(target)


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    # ---- static files, with /dist_data mapped to the dossier output ----
    def translate_path(self, path):
        clean = urllib.parse.unquote(urllib.parse.urlsplit(path).path)
        if clean == DATA_PREFIX or clean.startswith(DATA_PREFIX + "/"):
            out = self.server.output.resolve()
            target = (out / clean[len(DATA_PREFIX):].lstrip("/")).resolve()
            return str(target if target == out or out in target.parents else out / "__outside__")
        if clean in ("/studio", "/studio/"):
            return str(INGESTION_DIR / "studio.html")
        return super().translate_path(path)

    def local_host(self) -> bool:
        # Refuse API calls through any other host name (DNS-rebinding protection).
        return self.headers.get("Host", "") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):  # noqa: N802
        route = urllib.parse.urlsplit(self.path)
        if route.path.startswith("/api/") and not self.local_host():
            return self.send_json({"error": "Forbidden"}, 403)
        if route.path == "/api/ping":
            q = urllib.parse.parse_qs(route.query)
            self.server.windows.note((q.get("id") or ["?"])[0], bool(q.get("bye")))
            return self.send_json({"ok": True})
        if route.path == "/api/state":
            return self.send_json(studio_state(self.server.root))
        if route.path == "/api/job":
            job = self.server.job
            since = int((urllib.parse.parse_qs(route.query).get("since") or ["0"])[0] or 0)
            if not job:
                return self.send_json({"job": None})
            return self.send_json({"job": job.kind, "running": job.running, "code": job.code,
                                   "total": len(job.lines), "lines": job.lines[since:]})
        if not self.headers.get("Range"):
            return super().do_GET()
        self.handle_range()

    def do_HEAD(self):  # noqa: N802
        if not self.headers.get("Range"):
            return super().do_HEAD()
        self.handle_range(head_only=True)

    # ---- Studio actions ----
    def do_POST(self):  # noqa: N802
        # Only the Studio page itself may act: a JSON body plus a same-origin check blocks
        # other websites from triggering builds or uploads through this local port.
        origin = self.headers.get("Origin", "")
        allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        route = urllib.parse.urlsplit(self.path)
        ctype = "application/pdf" if route.path == "/api/upload" else "application/json"
        if not self.local_host() or origin not in allowed or self.headers.get("Content-Type", "").split(";")[0] != ctype:
            return self.send_json({"error": "Forbidden"}, 403)
        if route.path in ("/api/upload", "/api/remove") and self.server.job and self.server.job.running:
            return self.send_json({"error": "Wait for the current build or upload to finish."}, 409)
        if route.path == "/api/upload":
            return self.upload(urllib.parse.parse_qs(route.query).get("name", [""])[0])
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.send_json({"error": "Bad request"}, 400)
        root, path = self.server.root, urllib.parse.urlsplit(self.path).path
        import wehpah
        if path == "/api/settings":
            values = {k: str(body.get(k, "")).strip() for k in ("title", "description", "publish_to", "pdfs") if k in body}
            try:
                if values.get("pdfs"):
                    folder = Path(values["pdfs"]).expanduser()
                    folder = (folder if folder.is_absolute() else root / folder).resolve()
                    if not folder.is_dir():
                        raise ValueError(f"There is no folder at {folder}")
                    # Store folders inside the dossier relative to it, so the dossier can be moved.
                    values["pdfs"] = str(folder.relative_to(root.resolve())) if folder.is_relative_to(root.resolve()) else str(folder)
                previous = (root / wehpah.CONFIG_NAME).read_text() if (root / wehpah.CONFIG_NAME).exists() else None
                wehpah.save_settings(root, **values)
                try:
                    wehpah.load_settings(root)
                except ValueError:
                    if previous is None:
                        (root / wehpah.CONFIG_NAME).unlink()
                    else:
                        (root / wehpah.CONFIG_NAME).write_text(previous)
                    raise
                self.server.output = wehpah.load_settings(root).output
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 400)
            return self.send_json({"ok": True})
        if path == "/api/remove":
            try:
                where = remove_pdf(self.pdf_dir(), str(body.get("file", "")))
            except (ValueError, OSError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            return self.send_json({"ok": True, "moved_to": where})
        if path == "/api/login":
            # The key is checked with Hugging Face, saved where `./wehpah login` saves it, and never sent back.
            token = str(body.get("token", "")).strip()
            try:
                from huggingface_hub import HfApi, login
                who = HfApi().whoami(token=token)
                role = (who.get("auth") or {}).get("accessToken", {}).get("role")
                if role == "read":
                    raise ValueError("That key can only read. Create a key with write access and try again.")
                login(token=token, add_to_git_credential=False)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            except Exception:
                return self.send_json({"error": "Hugging Face did not accept that key."}, 400)
            return self.send_json({"ok": True, "user": who.get("name", "")})
        if path == "/api/choose-folder":
            return self.send_json({"folder": choose_folder(self.pdf_dir())})
        if path == "/api/open-folder":
            folder = self.pdf_dir()
            opener = {"darwin": ["open"], "win32": ["explorer"]}.get(sys.platform, ["xdg-open"])
            subprocess.Popen([*opener, str(folder)])
            return self.send_json({"ok": True})
        if path in ("/api/build", "/api/publish"):
            if self.server.job and self.server.job.running:
                return self.send_json({"error": "Something is already running."}, 409)
            if path == "/api/build":
                args = ["build"] + (["--force"] if body.get("force") else [])
            else:
                if body.get("confirm") is not True:
                    return self.send_json({"error": "Confirm that these documents may be made public."}, 400)
                args = ["publish", "--yes"]
            self.server.job = Job(args[0], root, args)
            return self.send_json({"ok": True})
        self.send_json({"error": "Not found"}, 404)

    def pdf_dir(self) -> Path:
        import wehpah
        return wehpah.load_settings(self.server.root).pdfs

    def upload(self, name: str):
        """Save one PDF into the PDF folder; a file with the same name is replaced."""
        length = int(self.headers.get("Content-Length") or 0)
        try:
            folder = self.pdf_dir()
            target = folder / safe_pdf_name(name, folder)
        except (ValueError, OSError) as exc:
            self.rfile.read(length)
            return self.send_json({"error": str(exc)}, 400)
        tmp = folder / f".upload-{os.getpid()}-{threading.get_ident()}.part"
        try:
            with tmp.open("wb") as out:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        raise OSError("The upload was interrupted.")
                    out.write(chunk)
                    remaining -= len(chunk)
            with tmp.open("rb") as check:
                if check.read(5) != b"%PDF-":
                    raise ValueError(f"{name} is not a PDF file.")
            os.replace(tmp, target)
        except (ValueError, OSError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        finally:
            tmp.unlink(missing_ok=True)
        return self.send_json({"ok": True})

    def send_json(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_range(self, head_only: bool = False):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not os.path.isfile(path):
            self.send_error(404)
            return

        size = os.path.getsize(path)
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        start, end = 0, size - 1
        if m:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):  # bytes=-N (suffix)
                start = max(0, size - int(m.group(2)))
        if start >= size:
            self.send_error(416, "Requested Range Not Satisfiable")
            return

        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path) or "application/octet-stream")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if head_only:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def end_headers(self):
        if self.path.startswith(DATA_PREFIX):
            self.send_header("Access-Control-Allow-Origin", "*")
            # CORS hides non-safelisted headers from JS — without this expose list the
            # browser's DuckDB-Wasm can't see Accept-Ranges and falls back to full GETs.
            self.send_header("Access-Control-Expose-Headers",
                             "Accept-Ranges, Content-Range, Content-Length, Content-Type, ETag")
            self.send_header("Accept-Ranges", "bytes")
        # Require revalidation so clients actually issue range requests (206) and see fresh builds.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/"):
            return
        r = self.headers.get("Range")
        sys.stderr.write("%s - %s%s\n" % (self.address_string(), fmt % args, f"  [Range: {r}]" if r else ""))


class Windows:
    """Which browser windows are open. A window pings every few seconds and says goodbye on close."""
    STALE = 90   # seconds without a ping (hidden tabs are throttled, a crash never says goodbye)
    GRACE = 4    # seconds to wait after the last goodbye: a reload or a link to another page pings again

    def __init__(self):
        self.seen: dict[str, float] = {}   # window id -> last ping
        self.ever = False                  # never quit before a window has opened
        self.empty_since = 0.0

    def note(self, wid: str, bye: bool) -> None:
        now = time.monotonic()
        if bye:
            self.seen.pop(wid, None)
        else:
            self.seen[wid], self.ever = now, True
        if not self.seen:
            self.empty_since = now

    def gone(self) -> bool:
        now = time.monotonic()
        self.seen = {w: t for w, t in self.seen.items() if now - t < self.STALE}
        if not self.ever or self.seen:
            return False
        return now - max(self.empty_since, 0) > self.GRACE


def serve(root: Path, port: int = 8410, open_path: str = "/studio", open_browser: bool = True) -> None:
    sys.path.insert(0, str(INGESTION_DIR))
    import wehpah
    for candidate in range(port, port + 20):  # port busy (often an earlier ./wehpah still running): try the next
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
    else:
        raise SystemExit(f"Ports {port}-{port + 19} are all busy. Stop the other WeHPAH! (Ctrl+C in its window) or pass --port.")
    if candidate != port:
        print(f"Port {port} is busy (another WeHPAH! may already be running); using {candidate} instead.")
    port = candidate
    server.root, server.job, server.windows = root, None, Windows()
    try:
        server.output = wehpah.load_settings(root).output
    except Exception:
        server.output = root / "dist_data"
    url = f"http://127.0.0.1:{port}{open_path}"
    print(f"WeHPAH! Studio  : http://127.0.0.1:{port}/studio")
    print(f"Local preview   : http://127.0.0.1:{port}/?local")
    print(f"Release folder  : {server.output}")
    print("Ctrl+C to stop (it also stops when you close the browser window).")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    def quit_when_closed():
        while True:
            time.sleep(1)
            busy = server.job and server.job.running  # never stop in the middle of a build or publish
            if server.windows.gone() and not busy:
                print("Browser window closed; stopping WeHPAH!.")
                server.shutdown()
                return
    threading.Thread(target=quit_when_closed, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        if server.job and server.job.running:
            server.job.proc.terminate()


if __name__ == "__main__":
    serve(PROJECT_ROOT, int(sys.argv[1]) if len(sys.argv) > 1 else 8410, "/?local", open_browser=False)
