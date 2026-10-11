"""Send-time long-edge cap in ``load_image_bytes_for_llm``.

Stored originals keep full resolution; only the copy sent to the LLM is shrunk
to ``LLM_IMAGE_MAX_LONG_EDGE``. Small images must pass through byte-identical
and the shrunk output must be deterministic (prompt caching compares content).
"""

from io import BytesIO

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from saiverse import media_utils  # noqa: E402
from saiverse.media_utils import (  # noqa: E402
    LLM_IMAGE_MAX_LONG_EDGE,
    load_image_bytes_for_llm,
)


def _noise_image(mode: str, size, seed: int = 0) -> Image.Image:
    """An image with real detail so encoders cannot compress it to nothing."""
    import random

    rng = random.Random(seed)
    w, h = size
    bands = len(mode)
    raw = rng.randbytes(w * h * bands)
    return Image.frombytes(mode, size, raw)


def _save(img: Image.Image, path, fmt: str, **kwargs):
    img.save(path, format=fmt, **kwargs)
    return path


def _open(data: bytes) -> Image.Image:
    img = Image.open(BytesIO(data))
    img.load()
    return img


def test_large_opaque_png_is_capped_to_jpeg(tmp_path):
    path = _save(Image.new("RGB", (4000, 2000), (10, 120, 200)), tmp_path / "big.png", "PNG")

    data, mime = load_image_bytes_for_llm(path, "image/png")

    assert mime == "image/jpeg"
    img = _open(data)
    assert img.format == "JPEG"
    assert img.size == (LLM_IMAGE_MAX_LONG_EDGE, 1024)


def test_large_transparent_png_keeps_alpha(tmp_path):
    img = Image.new("RGBA", (3000, 1000), (255, 0, 0, 255))
    img.paste((0, 0, 0, 0), (0, 0, 1500, 1000))  # left half fully transparent
    path = _save(img, tmp_path / "alpha.png", "PNG")

    data, mime = load_image_bytes_for_llm(path, "image/png")

    assert mime == "image/png"
    out = _open(data)
    assert out.format == "PNG"
    assert out.width == LLM_IMAGE_MAX_LONG_EDGE
    assert out.height in (682, 683)
    assert "A" in out.getbands()
    assert out.getchannel("A").getextrema()[0] == 0


def test_large_opaque_rgba_screenshot_becomes_jpeg(tmp_path):
    # Screenshots are often RGBA with a fully opaque alpha channel.
    path = _save(Image.new("RGBA", (3840, 2160), (30, 30, 30, 255)), tmp_path / "shot.png", "PNG")

    data, mime = load_image_bytes_for_llm(path, "image/png")

    assert mime == "image/jpeg"
    assert _open(data).size == (LLM_IMAGE_MAX_LONG_EDGE, 1152)


def test_image_within_cap_is_byte_identical(tmp_path):
    path = _save(_noise_image("RGB", (1000, 800)), tmp_path / "small.png", "PNG")

    data, mime = load_image_bytes_for_llm(path, "image/png")

    assert mime == "image/png"
    assert data == path.read_bytes()


def test_image_exactly_at_cap_is_byte_identical(tmp_path):
    path = _save(Image.new("RGB", (LLM_IMAGE_MAX_LONG_EDGE, 500)), tmp_path / "edge.jpg", "JPEG")

    data, mime = load_image_bytes_for_llm(path, "image/jpeg")

    assert mime == "image/jpeg"
    assert data == path.read_bytes()


def test_capped_output_is_deterministic(tmp_path):
    path = _save(_noise_image("RGB", (2600, 1400)), tmp_path / "det.png", "PNG")

    first = load_image_bytes_for_llm(path, "image/png")
    # Bypass the remembered copy so the encoder itself is checked.
    media_utils._LLM_IMAGE_CAP_CACHE.clear()
    second = load_image_bytes_for_llm(path, "image/png")

    assert first == second
    assert _open(first[0]).size[0] == LLM_IMAGE_MAX_LONG_EDGE


def test_capped_copy_is_reused_and_refreshed_when_file_changes(tmp_path, monkeypatch):
    path = _save(_noise_image("RGB", (2600, 1400), seed=1), tmp_path / "re.png", "PNG")
    calls = []
    real = media_utils._cap_image_long_edge_for_llm

    def counting(data, mime, p):
        calls.append(p)
        return real(data, mime, p)

    monkeypatch.setattr(media_utils, "_cap_image_long_edge_for_llm", counting)

    first = load_image_bytes_for_llm(path, "image/png")
    again = load_image_bytes_for_llm(path, "image/png")
    assert again == first
    assert len(calls) == 1

    _save(_noise_image("RGB", (3000, 1000), seed=2), path, "PNG")
    changed = load_image_bytes_for_llm(path, "image/png")
    assert len(calls) == 2
    assert _open(changed[0]).size == (LLM_IMAGE_MAX_LONG_EDGE, 683)


def test_large_gif_is_not_capped(tmp_path):
    path = _save(Image.new("P", (3000, 300), 1), tmp_path / "big.gif", "GIF")

    data, mime = load_image_bytes_for_llm(path, "image/gif")

    # GIF keeps its pre-existing handling (flattened to PNG, no resize).
    assert mime == "image/png"
    assert _open(data).size == (3000, 300)


def test_large_animated_webp_is_not_capped(tmp_path):
    frames = [Image.new("RGB", (2500, 300), c) for c in ((255, 0, 0), (0, 255, 0))]
    path = tmp_path / "anim.webp"
    frames[0].save(path, format="WEBP", save_all=True, append_images=frames[1:], duration=100)

    data, mime = load_image_bytes_for_llm(path, "image/webp")

    assert mime == "image/webp"
    assert data == path.read_bytes()


def test_max_bytes_still_applies_after_cap(tmp_path):
    path = _save(_noise_image("RGB", (4000, 2000)), tmp_path / "noise.png", "PNG")
    max_bytes = 400_000

    data, mime = load_image_bytes_for_llm(path, "image/png", max_bytes=max_bytes)

    assert mime == "image/jpeg"
    assert len(data) <= max_bytes
    assert max(_open(data).size) < LLM_IMAGE_MAX_LONG_EDGE
