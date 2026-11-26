"""Product pictures: reduced and compressed before they are stored, whatever their source."""
import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 512            # the longest edge, in pixels: plenty for a list or a detail view
QUALITY = 80
MAX_PIXELS = 50_000_000   # refuse absurdly large images instead of decoding them

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class BadImage(ValueError):
    pass


def reduce_image(raw: bytes, max_side: int = MAX_SIDE, keep_alpha: bool = False) -> tuple[bytes, int, int]:
    """WebP bytes no larger than `max_side` on a side, with the orientation applied and all metadata
    (camera, location) dropped. A photo is flattened onto white; a logo (`keep_alpha`) keeps its
    transparency. Raises BadImage for anything that is not a readable picture."""
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            picture = ImageOps.exif_transpose(source)
            if keep_alpha:
                picture = picture.convert("RGBA")
            elif picture.mode in ("RGBA", "LA", "P"):
                picture = picture.convert("RGBA")
                flat = Image.new("RGB", picture.size, "white")   # keep a white shelf, not black
                flat.paste(picture, mask=picture.getchannel("A"))
                picture = flat
            else:
                picture = picture.convert("RGB")
            picture.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            picture.save(out, "WEBP", quality=QUALITY, method=6)
            return out.getvalue(), picture.width, picture.height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise BadImage("That is not a picture we can read") from exc
