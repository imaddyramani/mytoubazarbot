import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"MyTourBazar bot is running")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[BOOT] Back4App health server listening on port {port}", flush=True)
    server.serve_forever()


def startup_checks():
    # Create folders used for temporary/generated work.
    for rel in ("data/generated", "data/incoming", "data/records", "temp", "tmp"):
        (BASE_DIR / rel).mkdir(parents=True, exist_ok=True)

    # These are known V167 files required by different PDF workflows.
    required = [
        "bot.py",
        "requirements.txt",
        "template.py",
        "footer2_overlay.py",
        "footer_overlay.py",
        "footer_bar_overlay.py",
        "watermark_overlay.py",
        "assets/mytourbazar_footer2_clean.png",
        "assets/mytourbazar_footer.png",
        "assets/mytourbazar_watermark.png",
        "assets/mytourbazar_contact_bar_orange.png",
        "data/T&C NON GOOGLE.pdf",
        "data/B2B.pdf",
        "data/without_footer.pdf",
        "data/logo_default.png",
    ]

    missing = [rel for rel in required if not (BASE_DIR / rel).exists()]
    if missing:
        print("[BOOT] WARNING - missing V167 files:", flush=True)
        for rel in missing:
            print(f"[BOOT]   MISSING: {rel}", flush=True)
    else:
        print("[BOOT] Required V167 PDF assets found.", flush=True)

    # Verify WeasyPrint after the container has actually started.
    try:
        from weasyprint import HTML
        test_pdf = HTML(string="<html><body>MyTourBazar PDF check</body></html>").write_pdf()
        print(f"[BOOT] WeasyPrint OK ({len(test_pdf)} bytes test PDF).", flush=True)
    except Exception as exc:
        print(f"[BOOT] FATAL - WeasyPrint failed: {type(exc).__name__}: {exc}", flush=True)
        raise


def main():
    startup_checks()

    health_thread = threading.Thread(
        target=run_health_server,
        name="back4app-health",
        daemon=True,
    )
    health_thread.start()

    # Import only after PDF + asset checks so startup errors are clear in logs.
    import bot
    print("[BOOT] Starting MyTourBazar Telegram bot...", flush=True)
    bot.main()


if __name__ == "__main__":
    main()
