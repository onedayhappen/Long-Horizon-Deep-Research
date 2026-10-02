from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import psutil

from .visual_models import WorkerConfig
from .visual_models import DocumentMap, FigureArtifact, RegionProposal, RenderedImage, VisualError, canonical, digest


async def pdf_job(pdf_path: Path, raw_hash: str, config: WorkerConfig, operation: str, **kwargs):
    """Bound parser wall time, RSS and output bytes without blocking the event loop."""
    with tempfile.TemporaryDirectory(prefix="lh-visual-") as tmp:
        root = Path(tmp)
        request_path, result_path = root / "request.json", root / "result.json"
        request_path.write_text(canonical({"pdf_path": str(pdf_path.resolve()), "raw_hash": raw_hash,
            "config": config.model_dump(), "operation": operation, **kwargs}), encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "src.research.pdf_worker",
            str(request_path), str(result_path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.extraction.max_parse_seconds
        try:
            monitor = psutil.Process(proc.pid)
            while proc.returncode is None:
                if loop.time() >= deadline:
                    raise VisualError("parse_timeout: PDF worker exceeded deadline")
                try:
                    if monitor.memory_info().rss > config.extraction.max_memory_bytes:
                        raise VisualError("memory_limit: PDF worker exceeded memory budget")
                except psutil.NoSuchProcess:
                    pass
                # Includes JSON overhead, not just raw text and PNGs.
                if sum(p.stat().st_size for p in root.iterdir()) > config.extraction.max_total_render_bytes + config.extraction.max_text_chars * 20 + 1048576:
                    raise VisualError("output_limit: parser output too large")
                await asyncio.sleep(0.02)
            if proc.returncode or not result_path.exists():
                raise VisualError("parse_failed: PDF subprocess exited without a result")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if "error" in result:
                raise VisualError(result["error"])
            value = result["result"]
            blobs = {}
            for image in value.get("images", []):
                data = (root / image.pop("file")).read_bytes()
                if hashlib.sha256(data).hexdigest() != image["blob_hash"]:
                    raise VisualError("integrity_error: rendered image changed")
                blobs[image["blob_hash"]] = data
            return value, blobs
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()


def discover_html_figures(raw: bytes, base_url: str):
    """Discover associations only; never bypass the configured FetchProvider."""
    from selectolax.parser import HTMLParser
    from urllib.parse import urljoin, urlsplit
    tree = HTMLParser(raw)
    result = []
    for index, node in enumerate(tree.css('figure')):
        caption = node.css_first('figcaption')
        ref = node.attributes.get('id', f'figure-{index}')
        mentions = [link.parent.text() for link in tree.css('a') if link.attributes.get('href') == '#' + ref]
        urls = [urljoin(base_url, img.attributes['src']) for img in node.css('img') if img.attributes.get('src')]
        urls = [url for url in urls if urlsplit(url).scheme in {'http', 'https'}]
        result.append({'id': ref, 'caption': caption.text() if caption else '', 'mentions': mentions,
                       'image_urls': urls, 'status': 'requires_validated_image_fetch'})
    return result
