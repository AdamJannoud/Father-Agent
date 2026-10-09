"""PDF text extraction template for the ``pdf`` capability pack."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

logger = logging.getLogger(__name__)


class PdfTextExtractor:
    """Extracts the text of every page of a local PDF."""

    async def pages(self, path: Path) -> list[str]:
        """One string per page; empty when the file cannot be read."""
        try:
            reader = await asyncio.to_thread(PdfReader, str(path))
            return await asyncio.to_thread(lambda: [p.extract_text() or "" for p in reader.pages])
        except (OSError, PdfReadError) as exc:
            logger.warning("could not read %s: %s", path, exc)
            return []
