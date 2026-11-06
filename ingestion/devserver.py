#!/usr/bin/env python3
"""devserver.py — Range-capable static server for local development & verification.

Serves the project root so that:
    /web_app/           -> the dossier explorer
    /dist_data/         -> corpus.parquet, centroids.json, pdfs/ (HTTP 206 range streaming)

Python's built-in http.server ignores Range headers; DuckDB-Wasm and PDF.js need
real 206 Partial Content responses to avoid downloading whole files.

Usage:  uv run python devserver.py [port]     (default 8410)
"""

import http.server
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        if not self.headers.get("Range"):
            return super().do_GET()
        self.handle_range()

    def do_HEAD(self):  # noqa: N802
        if not self.headers.get("Range"):
            return super().do_HEAD()
        self.handle_range(head_only=True)

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
        ctype = self.guess_type(path) or "application/octet-stream"
        self.send_response(206)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
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
        self.send_header("Access-Control-Allow-Origin", "*")
        # CORS hides non-safelisted headers from JS — without this expose list the
        # browser's DuckDB-Wasm can't see Accept-Ranges and falls back to full GETs.
        self.send_header("Access-Control-Expose-Headers",
                         "Accept-Ranges, Content-Range, Content-Length, Content-Type, ETag")
        # Advertise + require revalidation so clients actually issue range requests (206).
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):  # show 206s for the §6 checklist
        extra = ""
        if "dist_data" in (fmt % args if args else ""):
            r = self.headers.get("Range")
            if r:
                extra = f"  [Range: {r}]"
        sys.stderr.write("%s - %s%s\n" % (self.address_string(), fmt % args, extra))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8410
    os.chdir(PROJECT_ROOT)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), RangeHandler)
    print(f"Serving {PROJECT_ROOT}")
    print(f"  WeHPAH!  : http://127.0.0.1:{port}/web_app/")
    print(f"  Data     : http://127.0.0.1:{port}/dist_data/corpus.parquet")
    print("Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
