"""Bounded, disposable PDF renderer shared by every document workflow."""
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

_LOCK = threading.Lock()
log = logging.getLogger('mytourbazar.pdf')


def write_pdf(html, output_path, base_url=None):
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not _LOCK.acquire(timeout=5):
        raise RuntimeError('Another PDF is being prepared. Please retry after it finishes.')
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='mtb_pdf_', dir=output.parent) as folder:
            source = Path(folder)/'document.html'
            target = Path(folder)/'document.pdf'
            source.write_text(html, encoding='utf-8')
            env = os.environ.copy()
            env.update(OMP_THREAD_LIMIT='1', OMP_NUM_THREADS='1', MALLOC_ARENA_MAX='2')
            log.info('PDF_STAGE render_start')
            try:
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), str(source), str(target),
                     str(base_url or Path(__file__).resolve().parent)],
                    env=env, capture_output=True, text=True,
                    timeout=max(15, min(180, int(os.getenv('PDF_RENDER_TIMEOUT_SECONDS', '90')))))
            except subprocess.TimeoutExpired as exc:
                log.error('PDF_STAGE render_timeout')
                raise RuntimeError('PDF rendering timed out. Please retry with fewer pages.') from exc
            if result.returncode:
                # Do not dump HTML, booking details or fetched URLs into logs.
                log.error('PDF_STAGE render_failed exit_code=%s', result.returncode)
                raise RuntimeError(f'PDF renderer stopped (exit {result.returncode}). Check hosting memory and PDF_STAGE logs.')
            import fitz
            with fitz.open(target) as doc:
                if not len(doc):
                    raise RuntimeError('PDF renderer produced an empty document.')
                pages = len(doc)
            os.replace(target, output)
            log.info('PDF_STAGE render_complete pages=%s seconds=%.1f', pages, time.monotonic()-started)
    finally:
        _LOCK.release()


if __name__ == '__main__':
    # Only the worker imports WeasyPrint; no Telegram/AI/application imports.
    from weasyprint import HTML
    HTML(string=Path(sys.argv[1]).read_text(encoding='utf-8'), base_url=sys.argv[3]).write_pdf(
        sys.argv[2], dpi=150, optimize_images=False)
