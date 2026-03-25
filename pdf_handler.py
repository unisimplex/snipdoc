from __future__ import annotations

import fitz          
from PIL import Image as PILImage

from PyQt6.QtGui import QImage
from PyQt6.QtCore import QRectF
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import threading

# ── Supported formats ─────────────────────────────────────────────────────────
PDF_SUFFIXES   = {".pdf"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


# ── Unit conversion utilities ─────────────────────────────────────────────────

UNIT_TO_POINTS: dict[str, float] = {
    "pt":     1.0,
    "pixels": 1.0,       # 1 px = 1 pt at 72 DPI (PDF default)
    "inches": 72.0,
    "in":     72.0,
    "cm":     72.0 / 2.54,
    "mm":     72.0 / 25.4,
}


def to_points(value: float, unit: str) -> float:
    """Convert a measurement in *unit* to PDF points (72 pt = 1 inch)."""
    factor = UNIT_TO_POINTS.get(unit.lower())
    if factor is None:
        raise ValueError(f"Unknown unit '{unit}'. "
                         f"Valid units: {', '.join(UNIT_TO_POINTS)}")
    return value * factor


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class CropRegion:
    """A single crop selection on a specific page (or the whole image = page 0)."""
    page_index: int
    rect: QRectF            # Rectangle in document-coordinate space (points / px)
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = f"Page {self.page_index + 1} — crop"


# ── Page cache ────────────────────────────────────────────────────────────────

class PageCache:
    """Thread-safe LRU-style cache for rendered page images."""

    def __init__(self, max_size: int = 10):
        self._cache: dict[tuple, QImage] = {}
        self._order: list[tuple] = []
        self._max   = max_size
        self._lock  = threading.Lock()

    def get(self, key: tuple) -> Optional[QImage]:
        with self._lock:
            return self._cache.get(key)

    def put(self, key: tuple, image: QImage):
        with self._lock:
            if key in self._cache:
                self._order.remove(key)
            elif len(self._cache) >= self._max:
                oldest = self._order.pop(0)
                del self._cache[oldest]
            self._cache[key] = image
            self._order.append(key)

    def invalidate(self):
        with self._lock:
            self._cache.clear()
            self._order.clear()


# ── Document handler ──────────────────────────────────────────────────────────

class PDFHandler:
    """
    Unified document manager.  Handles PDFs and raster images (JPG/PNG)
    through the same public API.

    Public API
    ----------
    open(path) -> int            — open file; returns page count
    close()
    is_open, page_count, file_name, file_type ('pdf' | 'image')

    render_page(index, zoom) -> QImage
    get_page_size(index) -> (w, h)  — in document units (points or pixels)

    export_regions_as_pdf(regions, out_path) -> bool
    export_region_as_image(region, out_path, dpi) -> bool

    resize_document(width, height, unit, out_path, out_format,
                    keep_aspect) -> bool
    """

    def __init__(self):
        self._doc:       Optional[fitz.Document] = None   # PDF only
        self._pil_img:   Optional[PILImage.Image] = None  # image only
        self._path:      Optional[Path] = None
        self._file_type: str = ""     # "pdf" | "image"
        self._cache = PageCache(max_size=12)

    # ------------------------------------------------------------------ #
    #  Document lifecycle
    # ------------------------------------------------------------------ #

    def open(self, path: str) -> int:
        """
        Open a PDF or image file.
        Returns page count (always 1 for images).
        Raises ValueError for unsupported file types.
        """
        self.close()
        p = Path(path)
        suffix = p.suffix.lower()

        if suffix in PDF_SUFFIXES:
            self._file_type = "pdf"
            self._path = p
            self._doc  = fitz.open(str(p))
            self._cache.invalidate()
            return len(self._doc)

        elif suffix in IMAGE_SUFFIXES:
            self._file_type = "image"
            self._path      = p
            self._pil_img   = PILImage.open(str(p)).convert("RGB")
            self._cache.invalidate()
            return 1   # images are always single-page

        else:
            raise ValueError(
                f"Unsupported file type '{suffix}'. "
                f"Supported: {PDF_SUFFIXES | IMAGE_SUFFIXES}"
            )

    def close(self):
        if self._doc:
            self._doc.close()
            self._doc = None
        self._pil_img   = None
        self._file_type = ""
        self._path      = None
        self._cache.invalidate()

    @property
    def is_open(self) -> bool:
        return self._file_type != ""

    @property
    def file_type(self) -> str:
        """'pdf' | 'image' | '' (nothing open)"""
        return self._file_type

    @property
    def page_count(self) -> int:
        if self._file_type == "pdf":
            return len(self._doc) if self._doc else 0
        if self._file_type == "image":
            return 1 if self._pil_img is not None else 0
        return 0

    @property
    def file_name(self) -> str:
        return self._path.name if self._path else ""

    # ------------------------------------------------------------------ #
    #  Rendering
    # ------------------------------------------------------------------ #

    def render_page(self, page_index: int, zoom: float = 1.5) -> Optional[QImage]:
        """
        Render a page/image to a QImage at the given zoom level.
        Results are cached — repeated calls at the same zoom are free.
        """
        if not self.is_open or not (0 <= page_index < self.page_count):
            return None

        cache_key = (page_index, round(zoom, 3))
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        if self._file_type == "pdf":
            img = self._render_pdf_page(page_index, zoom)
        else:
            img = self._render_image(zoom)

        if img is not None:
            self._cache.put(cache_key, img)
        return img

    def _render_pdf_page(self, page_index: int, zoom: float) -> Optional[QImage]:
        page = self._doc[page_index]
        mat  = fitz.Matrix(zoom, zoom)
        pix  = page.get_pixmap(matrix=mat, alpha=False)
        img  = QImage(pix.samples, pix.width, pix.height,
                      pix.stride, QImage.Format.Format_RGB888)
        return img.copy()   # detach from pix memory

    def _render_image(self, zoom: float) -> Optional[QImage]:
        if self._pil_img is None:
            return None
        w = int(self._pil_img.width  * zoom)
        h = int(self._pil_img.height * zoom)
        scaled = self._pil_img.resize((w, h), PILImage.LANCZOS)
        data   = scaled.tobytes("raw", "RGB")
        img    = QImage(data, w, h, w * 3, QImage.Format.Format_RGB888)
        return img.copy()

    def get_page_size(self, page_index: int) -> tuple[float, float]:
        """Return (width, height) in document units (points for PDF, px for image)."""
        if not self.is_open or not (0 <= page_index < self.page_count):
            return (0.0, 0.0)
        if self._file_type == "pdf":
            r = self._doc[page_index].rect
            return (r.width, r.height)
        else:
            return (float(self._pil_img.width), float(self._pil_img.height))

    # ------------------------------------------------------------------ #
    #  Crop / Export
    # ------------------------------------------------------------------ #

    def export_regions_as_pdf(self, regions: list[CropRegion],
                               output_path: str) -> bool:
        """
        Export one or more CropRegions as a new PDF.
        Works for both PDF source documents and images.
        Each region becomes a separate page sized to match the crop rect.
        """
        if not self.is_open or not regions:
            return False
        try:
            if self._file_type == "pdf":
                return self._export_pdf_regions_as_pdf(regions, output_path)
            else:
                return self._export_image_regions_as_pdf(regions, output_path)
        except Exception as e:
            print(f"[PDFHandler] Export error: {e}")
            return False

    def _export_pdf_regions_as_pdf(self, regions: list[CropRegion],
                                    output_path: str) -> bool:
        out_doc = fitz.open()
        for region in regions:
            src_page = self._doc[region.page_index]
            clip = fitz.Rect(
                region.rect.x(), region.rect.y(),
                region.rect.x() + region.rect.width(),
                region.rect.y() + region.rect.height()
            )
            clip = clip & src_page.rect
            if clip.is_empty:
                continue
            new_page = out_doc.new_page(width=clip.width, height=clip.height)
            new_page.show_pdf_page(new_page.rect, self._doc,
                                   region.page_index, clip=clip)
        out_doc.save(output_path, garbage=4, deflate=True)
        out_doc.close()
        return True

    def _export_image_regions_as_pdf(self, regions: list[CropRegion],
                                      output_path: str) -> bool:
        out_doc = fitz.open()
        for region in regions:
            rx = int(region.rect.x());  ry = int(region.rect.y())
            rw = int(region.rect.width()); rh = int(region.rect.height())
            # Clamp to image bounds
            iw, ih = self._pil_img.width, self._pil_img.height
            rx = max(0, min(rx, iw)); ry = max(0, min(ry, ih))
            x2 = max(0, min(rx + rw, iw)); y2 = max(0, min(ry + rh, ih))
            if x2 <= rx or y2 <= ry:
                continue
            cropped = self._pil_img.crop((rx, ry, x2, y2))
            import io
            buf = io.BytesIO()
            cropped.save(buf, format="PNG")
            buf.seek(0)

            new_page = out_doc.new_page(width=cropped.width, height=cropped.height)
            rect = fitz.Rect(0, 0, cropped.width, cropped.height)
            new_page.insert_image(rect, stream=buf.read())
        out_doc.save(output_path, garbage=4, deflate=True)
        out_doc.close()
        return True

    def export_region_as_image(self, region: CropRegion, output_path: str,
                                dpi: int = 150) -> bool:
        """Export a single CropRegion as a PNG/JPEG image."""
        if not self.is_open:
            return False
        try:
            if self._file_type == "pdf":
                return self._export_pdf_region_as_image(region, output_path, dpi)
            else:
                return self._export_image_region_as_image(region, output_path)
        except Exception as e:
            print(f"[PDFHandler] Image export error: {e}")
            return False

    def _export_pdf_region_as_image(self, region: CropRegion,
                                     output_path: str, dpi: int) -> bool:
        zoom = dpi / 72.0
        page = self._doc[region.page_index]
        clip = fitz.Rect(
            region.rect.x(), region.rect.y(),
            region.rect.x() + region.rect.width(),
            region.rect.y() + region.rect.height()
        )
        clip = clip & page.rect
        mat  = fitz.Matrix(zoom, zoom)
        pix  = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        pix.save(output_path)
        return True

    def _export_image_region_as_image(self, region: CropRegion,
                                       output_path: str) -> bool:
        rx = int(region.rect.x());   ry = int(region.rect.y())
        rw = int(region.rect.width()); rh = int(region.rect.height())
        iw, ih = self._pil_img.width, self._pil_img.height
        rx = max(0, min(rx, iw)); ry = max(0, min(ry, ih))
        x2 = max(0, min(rx + rw, iw)); y2 = max(0, min(ry + rh, ih))
        if x2 <= rx or y2 <= ry:
            return False
        cropped = self._pil_img.crop((rx, ry, x2, y2))
        # Preserve original format when possible
        suffix = Path(output_path).suffix.lower()
        fmt = "JPEG" if suffix in (".jpg", ".jpeg") else "PNG"
        if fmt == "JPEG":
            cropped = cropped.convert("RGB")
        cropped.save(output_path, format=fmt)
        return True

    # ------------------------------------------------------------------ #
    #  Resize
    # ------------------------------------------------------------------ #

    def resize_document(self,
                        width: float, height: float, unit: str,
                        output_path: str, out_format: str = "pdf",
                        keep_aspect: bool = False) -> bool:
        """
        Resize the entire document and export to output_path.

        Parameters
        ----------
        width, height : target dimensions in *unit*
        unit          : 'mm' | 'cm' | 'inches' | 'in' | 'pixels' | 'pt'
        output_path   : destination file
        out_format    : 'pdf' | 'png' | 'jpg'  (case-insensitive)
        keep_aspect   : if True, height is adjusted to preserve aspect ratio

        Returns True on success.
        """
        if not self.is_open:
            return False
        if width <= 0 or height <= 0:
            return False
        try:
            out_format = out_format.lower().strip()
            if out_format not in ("pdf", "png", "jpg", "jpeg"):
                raise ValueError(f"Unsupported output format '{out_format}'")

            w_pt = to_points(width,  unit)
            h_pt = to_points(height, unit)

            if keep_aspect:
                src_w, src_h = self.get_page_size(0)
                if src_w > 0 and src_h > 0:
                    h_pt = w_pt * (src_h / src_w)

            if self._file_type == "pdf":
                return self._resize_pdf(w_pt, h_pt, output_path, out_format)
            else:
                # For images, points == pixels at 72 dpi convention
                return self._resize_image(int(w_pt), int(h_pt),
                                          output_path, out_format)
        except Exception as e:
            print(f"[PDFHandler] Resize error: {e}")
            return False

    def _resize_pdf(self, w_pt: float, h_pt: float,
                    output_path: str, out_format: str) -> bool:
        out_doc = fitz.open()
        for i in range(len(self._doc)):
            src_page = self._doc[i]
            src_rect = src_page.rect
            # Build a scaling matrix that maps src page → target size
            sx = w_pt / src_rect.width  if src_rect.width  else 1.0
            sy = h_pt / src_rect.height if src_rect.height else 1.0

            new_page = out_doc.new_page(width=w_pt, height=h_pt)
            new_page.show_pdf_page(
                fitz.Rect(0, 0, w_pt, h_pt),
                self._doc, i
            )

        if out_format == "pdf":
            out_doc.save(output_path, garbage=4, deflate=True)
            out_doc.close()
            return True

        # Rasterise to image
        import io
        images: list[PILImage.Image] = []
        for i in range(len(out_doc)):
            pg  = out_doc[i]
            pix = pg.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            buf = io.BytesIO(pix.tobytes("png"))
            images.append(PILImage.open(buf).copy())
        out_doc.close()

        if not images:
            return False
        fmt = "JPEG" if out_format in ("jpg", "jpeg") else "PNG"
        if len(images) == 1:
            img = images[0].convert("RGB") if fmt == "JPEG" else images[0]
            img.save(output_path, format=fmt, quality=95)
        else:
            # Multi-page → save first + append_images (PDF) or only first (img)
            first = images[0].convert("RGB") if fmt == "JPEG" else images[0]
            rest  = [im.convert("RGB") if fmt == "JPEG" else im
                     for im in images[1:]]
            first.save(output_path, format=fmt, quality=95,
                       save_all=True, append_images=rest)
        return True

    def _resize_image(self, w_px: int, h_px: int,
                      output_path: str, out_format: str) -> bool:
        resized = self._pil_img.resize((w_px, h_px), PILImage.LANCZOS)
        fmt = "JPEG" if out_format in ("jpg", "jpeg") else "PNG"
        if fmt == "JPEG":
            resized = resized.convert("RGB")
        resized.save(output_path, format=fmt, quality=95)
        return True

    def get_resized_preview_size(self, width: float, height: float,
                                  unit: str, keep_aspect: bool = False
                                  ) -> tuple[float, float]:
        """
        Return (w_pt, h_pt) that would be used for a resize call —
        useful for showing a preview label before the user commits.
        Returns (0, 0) on bad input.
        """
        try:
            w_pt = to_points(width,  unit)
            h_pt = to_points(height, unit)
            if keep_aspect:
                src_w, src_h = self.get_page_size(0)
                if src_w > 0 and src_h > 0:
                    h_pt = w_pt * (src_h / src_w)
            return (w_pt, h_pt)
        except Exception:
            return (0.0, 0.0)
