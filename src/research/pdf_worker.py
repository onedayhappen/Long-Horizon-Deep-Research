"""Private subprocess entry point: no network, models, or database access."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

from .visual_models import WorkerConfig
from .visual_models import PDFRegion, VisualError, check_bbox, digest

LABEL = re.compile(r"(?:Figure\s*|Fig\.\s*|图\s*)(\d+[A-Za-z]?)", re.IGNORECASE)


def scan(doc, raw_hash, config, version):
    blocks, pages, figures = [], [], {}
    chars = 0
    for page_index, page in enumerate(doc):
        rotation, crop = page.rotation, tuple(page.cropbox)
        page.set_rotation(0)
        pages.append({"page": page_index + 1, "size": tuple(page.rect)[2:], "cropbox": crop, "rotation": rotation})
        for index, block in enumerate(page.get_text("blocks", flags=0)):
            text = block[4].strip()
            if not text:
                continue
            chars += len(text)
            if chars > config.extraction.max_text_chars:
                raise VisualError("text_limit: document map would be incomplete")
            ref = f"p{page_index + 1}.b{index}"
            blocks.append({"id": ref, "page": page_index + 1, "bbox": block[:4], "text": text})
            for match in LABEL.finditer(text):
                label = "Figure " + match.group(1)
                item = figures.setdefault(label, {"id": digest([raw_hash, label]), "figure_label": label,
                    "caption_refs": [], "mention_refs": [], "pages": [], "extraction_status": "ambiguous"})
                key = "caption_refs" if match.start() == 0 else "mention_refs"
                if ref not in item[key]:
                    item[key].append(ref)
                if page_index + 1 not in item["pages"]:
                    item["pages"].append(page_index + 1)
    return {"raw_hash": raw_hash, "parser_version": version, "pages": pages, "blocks": blocks,
            "figures": list(figures.values()), "status": "text_available" if blocks else "needs_ocr"}


def render(doc, raw_hash, request, config, output, fitz):
    images, total = [], 0
    specs = request["regions"]
    if not 1 <= len(specs) <= config.visual.max_images_per_figure:
        raise VisualError("image_limit: too many regions")
    for index, spec in enumerate(specs):
        page_number = spec["page"]
        if type(page_number) is not int or not 1 <= page_number <= len(doc):
            raise VisualError("invalid_region: page outside PDF")
        page = doc[page_number - 1]
        rotation, crop = page.rotation, tuple(page.cropbox)
        # Render in a single, explicitly unrotated coordinate system.
        page.set_rotation(0)
        width, height = page.rect.width, page.rect.height
        bbox = tuple(spec.get("bbox") or (0, 0, width, height))
        check_bbox(bbox, width, height)
        dpi = config.visual.detail_dpi if spec.get("detail", False) else config.visual.dpi
        scale = dpi / 72
        x0, y0, x1, y1 = bbox
        pixels = (math.ceil(x1 * scale) - math.floor(x0 * scale)) * (math.ceil(y1 * scale) - math.floor(y0 * scale))
        if pixels > config.visual.max_image_pixels:
            raise VisualError("unreadable: region exceeds pixel budget; select a smaller region")
        pix = page.get_pixmap(clip=fitz.Rect(bbox), dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False)
        if pix.width * pix.height > config.visual.max_image_pixels:
            raise VisualError("image_limit: actual raster exceeds pixel budget")
        data = pix.tobytes("png")
        total += len(data)
        if len(data) > config.visual.max_image_bytes or total > config.extraction.max_total_render_bytes:
            raise VisualError("image_limit: PNG byte budget exceeded")
        name = f"image-{index}.png"
        (output / name).write_bytes(data)
        locator = PDFRegion(raw_hash=raw_hash, page=page_number, bbox=bbox, page_size=(width, height),
                            cropbox=crop, rotation=rotation)
        images.append({"file": name, "blob_hash": hashlib.sha256(data).hexdigest(), "locator": locator.model_dump(),
                       "width": pix.width, "height": pix.height, "dpi": dpi,
                       "pixel_to_pdf": [1 / scale, 0, 0, 1 / scale, pix.x / scale, pix.y / scale],
                       "purpose": "detail" if spec.get("detail", False) else "context"})
        page.set_rotation(rotation)
    return {"images": images, "render_profile": {"library": "PyMuPDF", "version": fitz.VersionBind,
             "colorspace": "RGB", "alpha": False, "annotations": False, "rotation": "unrotated",
             "dpi": sorted({i["dpi"] for i in images})}}


def main():
    request_path, result_path = map(Path, sys.argv[1:])
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        config = WorkerConfig.model_validate(request["config"])
        if sys.platform != "win32":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (config.extraction.max_memory_bytes,) * 2)
        import pymupdf as fitz
        data = Path(request["pdf_path"]).read_bytes()
        if len(data) > config.extraction.max_input_bytes:
            raise VisualError("input_limit: PDF too large")
        raw_hash = hashlib.sha256(data).hexdigest()
        if raw_hash != request["raw_hash"]:
            raise VisualError("integrity_error: original PDF hash mismatch")
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise VisualError("access_blocked: encrypted PDF")
            if len(doc) > config.extraction.max_pdf_pages:
                raise VisualError("page_limit: PDF too long")
            if request["operation"] == "scan":
                result = scan(doc, raw_hash, config, fitz.VersionBind)
            else:
                result = render(doc, raw_hash, request, config, result_path.parent, fitz)
        result_path.write_text(json.dumps({"result": result}), encoding="utf-8")
    except Exception as exc:
        result_path.write_text(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), encoding="utf-8")


if __name__ == "__main__":
    main()
