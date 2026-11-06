"""Invoice PDF: Jinja2 template -> WeasyPrint."""

import asyncio
from pathlib import Path

import jinja2
import weasyprint  # type: ignore[import-untyped]

from billing.app.invoice_doc import InvoiceDocument

TEMPLATES = Path(__file__).resolve().parent / "templates"


class WeasyPrintRenderer:
    def __init__(self) -> None:
        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES),
            autoescape=jinja2.select_autoescape(["html"]),
            undefined=jinja2.StrictUndefined,
        )

    def render_sync(self, doc: InvoiceDocument) -> bytes:
        html = self._env.get_template("invoice.html").render(d=doc)
        pdf: bytes = weasyprint.HTML(string=html).write_pdf()
        return pdf

    async def render(self, doc: InvoiceDocument) -> bytes:
        # WeasyPrint is CPU-bound and blocking: keep it off the event loop
        return await asyncio.to_thread(self.render_sync, doc)
