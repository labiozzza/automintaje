"""Image generation and short-video rendering for the Elementary workflow."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from . import opencode_client


VARIANT_COUNT = 10
VIDEO_SECONDS = 6
VIDEO_FPS = 30
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
MAX_REFERENCE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 30_000_000
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_VARIANT_ART_DIRECTIONS = (
    "Cinematic editorial photography, low camera angle, deep navy shadows, restrained gold details, dramatic side lighting.",
    "Bright flat-vector illustration made from geometric shapes, overhead composition, coral, cobalt and mint palette.",
    "Tactile cut-paper collage, layered torn-paper shapes, warm yellow and blue, visible handmade paper texture.",
    "Pastel 3D clay miniature, playful financial staircase made of simple blocks, soft shadows, lilac and peach palette.",
    "Macro still-life photography of a notebook, calculator and a few coins, close crop, cool blue and silver palette.",
    "Loose watercolor illustration of a growing plant beside a simple savings jar, pale green and cream, organic brush texture.",
    "Top-down magazine still life with receipts, plant leaves and a wallet, warm terracotta and sage colors.",
    "Bold architectural abstraction with three ascending platforms, saturated violet and orange, strong diagonal perspective.",
    "Retro risograph print with visible grain and misregistered color shapes, red, teal and cream, poster-like composition.",
    "Surreal studio photograph of translucent glass forms and flowing light, high contrast magenta and emerald palette.",
)


def validate_reference(path: Path) -> tuple[int, int, str]:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REFERENCE_BYTES:
        raise ValueError("Референс должен быть изображением размером не более 12 МБ")
    try:
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Поддерживаются PNG, JPEG и WebP")
            width, height = image.size
            if width < 64 or height < 64 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Размер референса должен быть от 64 px и не более 30 Мп")
            image.verify()
            return width, height, image.format.lower()
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Не удалось прочитать файл референса") from exc


def run_imagegen(
    *,
    brief_path: Path,
    output_path: Path,
    reference_path: Optional[Path] = None,
    timeout: int = 900,
    executable: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Path:
    """Invoke the project's restricted OpenCode agent and validate its output."""
    output_path = Path(os.path.abspath(os.fspath(output_path)))
    brief_path = Path(brief_path).resolve()
    if not brief_path.is_file():
        raise ValueError("Не найден бриф генерации")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.is_symlink():
        raise ValueError("Путь результата не должен быть символической ссылкой")
    output_path.unlink(missing_ok=True)
    cli = executable or os.environ.get("OPENCODE_BIN") or shutil.which("opencode") or "opencode"
    command = [
        cli, "run", "--agent", "elementary-imagegen",
        "Создай изображение по приложенному брифу. Ответь только путём созданного файла.",
        "--file", str(brief_path),
    ]
    if reference_path is not None:
        ref = Path(reference_path)
        validate_reference(ref)
        ref = ref.resolve()
        command.extend(["--file", str(ref)])
    try:
        result = opencode_client.run_agent_command(
            command,
            cwd=Path(__file__).resolve().parents[1],
            timeout=max(30, min(int(timeout), 1800)),
            cancel_event=cancel_event,
        )
    except opencode_client.AgentCancelledError:
        output_path.unlink(missing_ok=True)
        raise
    except opencode_client.HardTimeoutError as exc:
        raise RuntimeError("Генерация изображения превысила лимит времени") from exc
    if result.returncode != 0:
        detail = ANSI_ESCAPE_RE.sub("", result.stderr or result.stdout or "").strip()[-600:]
        raise RuntimeError("OpenCode не смог создать изображение" + (f": {detail}" if detail else ""))
    if not output_path.is_file() or output_path.is_symlink() or output_path.stat().st_size == 0:
        raise RuntimeError("Агент завершился, но файл изображения не создан")
    try:
        with Image.open(output_path) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("Слишком большое изображение")
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("OpenCode создал некорректное изображение") from exc
    return output_path


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_lines(text: str, font: ImageFont.ImageFont, draw: ImageDraw.ImageDraw, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph:
            lines.append("")
            continue
        line = ""
        for token in re.findall(r"\s+|\S+", paragraph):
            bounds = draw.textbbox((0, 0), line + token, font=font)
            if bounds[2] - bounds[0] <= max_width or not line:
                line += token
                continue
            if line:
                lines.append(line)
                line = ""
            if token.isspace():
                line = token
                continue
            while token:
                cut = 1
                while cut < len(token):
                    bounds = draw.textbbox((0, 0), token[:cut + 1], font=font)
                    if bounds[2] - bounds[0] > max_width:
                        break
                    cut += 1
                piece, token = token[:cut], token[cut:]
                if token:
                    lines.append(piece)
                else:
                    line = piece
        if line:
            lines.append(line)
    return lines


def _memory_card_content(text: str) -> tuple[str, list[str], str]:
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    if not lines:
        raise ValueError("Текст для изображения не должен быть пустым")
    title = ""
    if len(lines) > 1 and len(lines[0]) <= 90:
        title = lines.pop(0)
    cta = ""
    if len(lines) > 1 and re.search(r"подписывай|следующ|смотри|продолжени", lines[-1], re.I):
        cta = lines.pop()

    items: list[str] = []
    starter = re.compile(r"(?i)(?<!\w)(?:если|когда)(?!\w)")
    for line in lines:
        matches = list(starter.finditer(line))
        if len(matches) > 1:
            starts = [0, *(match.start() for match in matches[1:])]
            ends = [*starts[1:], len(line)]
            items.extend(line[start:end].strip() for start, end in zip(starts, ends) if line[start:end].strip())
        else:
            items.append(line)
    if not items and title:
        items = [title]
        title = ""
    if len(items) > 4:
        group_size = (len(items) + 3) // 4
        items = ["\n".join(items[i:i + group_size]) for i in range(0, len(items), group_size)]
    return title or "Запомни главное", items, cta


def _memory_label(text: str, index: int) -> tuple[str, str, tuple[int, int, int]]:
    lower = text.lower()
    if "столько же" in lower or "равновес" in lower:
        return "РАСХОДЫ = ДОХОДЫ", "equal", (207, 145, 42)
    if "больше" in lower and ("получ" in lower or "доход" in lower):
        return "РАСХОДЫ ВЫШЕ ДОХОДОВ", "more", (207, 82, 75)
    if "меньше" in lower and ("получ" in lower or "доход" in lower):
        return "РАСХОДЫ НИЖЕ ДОХОДОВ", "less", (45, 145, 107)
    return f"КЛЮЧЕВОЙ ПУНКТ {index + 1}", "", ((103, 87, 190), (38, 135, 151), (207, 145, 42), (45, 145, 107))[index % 4]


def _visual_aid_guidance(text: str) -> str:
    _title, items, _cta = _memory_card_content(text)
    kinds = {_memory_label(item, index)[1] for index, item in enumerate(items)}
    if kinds == {"more", "equal", "less"}:
        return (
            "The topic compares spending with income. Add a clear nonverbal infographic metaphor: "
            "one set of expense/income bars with expenses higher, one balanced scale, and one with income higher; "
            "use small coin, scale, or plant symbols, never written labels or numbers."
        )
    lowered = text.lower()
    if any(word in lowered for word in ("если", "когда", "иначе", "сначала", "затем")):
        return "Turn the ideas into a simple visual flow or branching decision diagram using arrows, shapes, and small icons, without words or numbers."
    if any(word in lowered for word in ("рост", "увелич", "процент", "сравн", "больше", "меньше")):
        return "Use a small, accurate comparison or trend graphic with shapes/bars/arrows that makes the relationship intuitive, without labels or numbers."
    if any(word in lowered for word in ("шаг", "этап", "способ", "правил")):
        return "Use a compact step-by-step visual with distinct icons linked by a clear path; do not draw labels or numbers."
    return "Choose one strong visual metaphor for the main idea; add a small diagram, sequence, or comparison only if it genuinely improves understanding, not as decoration."


def _draw_lines(draw, text: str, font, x: int, y: int, max_width: int, line_height: int, fill, align="left") -> tuple[int, list[str]]:
    lines = _wrap_lines(text, font, draw, max_width)
    for line in lines:
        bounds = draw.textbbox((0, 0), line, font=font)
        width = bounds[2] - bounds[0]
        draw_x = x if align == "left" else x + (max_width - width) // 2
        draw.text((draw_x, y), line, font=font, fill=fill)
        y += line_height
    return y, lines


_VARIANT_LAYOUTS = (
    {"name": "paper", "blur": 10, "brightness": .58, "tint": (28, 26, 43, 48), "page": (250, 248, 242, 255), "inset": 72, "row": "light", "badge": "circle", "radius": 38, "title": (31, 34, 46, 255), "text": (45, 47, 57, 255), "accents": ((207, 82, 75), (207, 145, 42), (45, 145, 107)), "eyebrow": (232, 227, 250, 255), "eyebrow_text": (89, 69, 164, 255), "cta": (236, 229, 255, 255), "cta_text": (79, 58, 151, 255), "header": "pill", "align": "left", "offset": 0},
    {"name": "night", "blur": 3, "brightness": .34, "tint": (11, 17, 39, 118), "page": (17, 23, 43, 255), "inset": 64, "row": "dark", "badge": "square", "radius": 22, "title": (247, 248, 255, 255), "text": (238, 242, 250, 255), "accents": ((255, 116, 105), (255, 202, 87), (78, 225, 190)), "eyebrow": (35, 48, 75, 255), "eyebrow_text": (119, 223, 240, 255), "cta": (40, 55, 82, 255), "cta_text": (147, 234, 214, 255), "header": "bar", "align": "left", "offset": 0},
    {"name": "signal", "blur": 0, "brightness": .82, "tint": (255, 208, 45, 56), "page": None, "inset": 84, "row": "accent", "badge": "symbol", "radius": 12, "title": (34, 36, 48, 255), "text": (255, 255, 255, 255), "accents": ((220, 76, 67), (45, 102, 186), (39, 143, 93)), "eyebrow": (255, 242, 198, 255), "eyebrow_text": (89, 63, 24, 255), "cta": (255, 239, 190, 255), "cta_text": (63, 48, 30, 255), "header": "bar", "align": "center", "offset": 0},
    {"name": "offset", "blur": 18, "brightness": .68, "tint": (88, 28, 67, 56), "page": (255, 249, 244, 255), "inset": 72, "row": "tint", "badge": "giant", "radius": 34, "title": (56, 31, 49, 255), "text": (53, 43, 54, 255), "accents": ((191, 64, 88), (195, 132, 37), (52, 137, 113)), "eyebrow": (250, 223, 226, 255), "eyebrow_text": (135, 50, 77, 255), "cta": (249, 222, 230, 255), "cta_text": (120, 43, 76, 255), "header": "vertical", "align": "left", "offset": 34},
    {"name": "blueprint", "blur": 6, "brightness": .30, "tint": (7, 31, 71, 148), "page": (12, 39, 75, 255), "inset": 64, "row": "outline", "badge": "square", "radius": 8, "title": (237, 247, 255, 255), "text": (229, 241, 255, 255), "accents": ((75, 202, 255), (255, 205, 89), (124, 237, 178)), "eyebrow": (19, 68, 109, 255), "eyebrow_text": (119, 223, 255, 255), "cta": (18, 69, 104, 255), "cta_text": (167, 235, 255, 255), "header": "bar", "align": "left", "offset": 0},
    {"name": "photo-essay", "blur": 1, "brightness": .92, "tint": (251, 166, 76, 38), "page": None, "inset": 84, "row": "accent", "badge": "none", "radius": 2, "title": (38, 32, 29, 255), "text": (255, 255, 255, 255), "accents": ((194, 63, 48), (42, 96, 174), (31, 131, 79)), "eyebrow": (255, 248, 230, 255), "eyebrow_text": (85, 59, 35, 255), "cta": (255, 247, 224, 255), "cta_text": (63, 46, 35, 255), "header": "bar", "align": "left", "offset": 0},
    {"name": "mint", "blur": 22, "brightness": .83, "tint": (84, 181, 145, 62), "page": (224, 246, 237, 255), "inset": 60, "row": "light", "badge": "bar", "radius": 42, "title": (25, 74, 62, 255), "text": (38, 64, 56, 255), "accents": ((210, 83, 73), (201, 139, 44), (32, 132, 100)), "eyebrow": (190, 231, 214, 255), "eyebrow_text": (27, 106, 81, 255), "cta": (192, 232, 214, 255), "cta_text": (27, 97, 76, 255), "header": "vertical", "align": "left", "offset": 0},
    {"name": "sunburst", "blur": 2, "brightness": .76, "tint": (255, 158, 26, 68), "page": (255, 219, 89, 255), "inset": 72, "row": "accent", "badge": "none", "radius": 4, "title": (48, 38, 29, 255), "text": (42, 37, 33, 255), "accents": ((200, 66, 51), (50, 106, 179), (39, 133, 82)), "eyebrow": (51, 44, 38, 255), "eyebrow_text": (255, 235, 191, 255), "cta": (48, 43, 40, 255), "cta_text": (255, 234, 184, 255), "header": "bar", "align": "left", "offset": 0},
    {"name": "mono", "blur": 26, "brightness": .76, "tint": (238, 237, 229, 100), "page": (249, 248, 242, 255), "inset": 72, "row": "outline", "badge": "giant", "radius": 0, "title": (20, 22, 25, 255), "text": (24, 25, 28, 255), "accents": ((198, 52, 57), (32, 37, 43), (54, 119, 109)), "eyebrow": (28, 30, 34, 255), "eyebrow_text": (255, 255, 255, 255), "cta": (221, 218, 209, 255), "cta_text": (30, 32, 35, 255), "header": "line", "align": "left", "offset": 0},
    {"name": "collage", "blur": 4, "brightness": .28, "tint": (48, 9, 90, 132), "page": (35, 18, 67, 255), "inset": 72, "row": "accent", "badge": "symbol", "radius": 8, "title": (252, 245, 255, 255), "text": (255, 255, 255, 255), "accents": ((255, 72, 150), (208, 235, 63), (47, 221, 208)), "eyebrow": (79, 33, 111, 255), "eyebrow_text": (251, 202, 255, 255), "cta": (63, 31, 94, 255), "cta_text": (250, 215, 255, 255), "header": "bar", "align": "center", "offset": 22},
)


def overlay_exact_text(source: Path, text: str, output: Path, *, variant_index: int = 0) -> Path:
    """Render the same copy through one of ten visually distinct learning-card layouts."""
    if not text or not text.strip():
        raise ValueError("Текст для изображения не должен быть пустым")
    theme = _VARIANT_LAYOUTS[int(variant_index) % len(_VARIANT_LAYOUTS)]
    title, items, cta = _memory_card_content(text)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        background = ImageOps.fit(
            original.convert("RGB"),
            (VIDEO_WIDTH, VIDEO_HEIGHT),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.46),
        )
    background = ImageEnhance.Brightness(background.filter(ImageFilter.GaussianBlur(theme["blur"]))).enhance(theme["brightness"]).convert("RGBA")
    if theme["tint"]:
        tint = Image.new("RGBA", background.size, theme["tint"])
        background = Image.alpha_composite(background, tint)
    draw = ImageDraw.Draw(background, "RGBA")
    card_left, card_right = theme["inset"], VIDEO_WIDTH - theme["inset"]
    card_width = card_right - card_left
    page_margin = 60 if theme["page"] is None else 72
    inner_left, inner_width = card_left + page_margin, card_width - page_margin * 2
    top = 235
    title_size = (78 if len(title) < 45 else 62) if theme["name"] in {"sunburst", "mono"} else (66 if len(title) < 45 else 56)
    title_font = _font(title_size)
    title_lines = _wrap_lines(title, title_font, draw, inner_width - 40)
    title_line_height = title_size + (18 if theme["name"] in {"sunburst", "mono"} else 14)
    body_size = 35 if len(items) <= 3 else 30
    body_line_height = body_size + 13
    label_font = _font(19 if theme["name"] in {"mono", "sunburst"} else 21)
    number_font = _font(76 if theme["badge"] == "giant" else (64 if theme["badge"] == "symbol" else 30))
    summary_font = _font(18)
    cta_font = _font(27)
    body_indent = {"circle": 126, "square": 126, "giant": 154, "symbol": 154, "bar": 48, "none": 36}[theme["badge"]]
    body_top_offset = {"circle": 69, "square": 69, "giant": 102, "symbol": 102, "bar": 62, "none": 62}[theme["badge"]]
    row_widths = [inner_width - (theme["offset"] if index % 2 else 0) for index in range(len(items))]
    financial_sequence = len(items) == 3 and {
        _memory_label(item, i)[1] for i, item in enumerate(items)
    } == {"more", "equal", "less"}

    # Shrink body type as needed so every original sentence remains on the card.
    max_content_bottom = VIDEO_HEIGHT - 330
    while body_size >= 24:
        body_font = _font(body_size)
        body_line_height = body_size + 13
        card_heights = []
        for item, row_width in zip(items, row_widths):
            lines = _wrap_lines(item, body_font, draw, row_width - body_indent - 24)
            card_heights.append(max(154, body_top_offset + len(lines) * body_line_height + 24))
        needed = top + 54 + len(title_lines) * title_line_height + 36
        needed += sum(card_heights) + max(0, len(card_heights) - 1) * 18
        if financial_sequence:
            needed += 150
        if cta:
            needed += max(110, len(_wrap_lines(cta, cta_font, draw, inner_width - 64)) * 36 + 58)
        if needed <= max_content_bottom:
            break
        body_size -= 2
    if body_size < 24:
        raise ValueError("Текст слишком длинный для одной карточки. Разбейте его на несколько изображений.")

    card_bottom = needed + 40
    shadow = Image.new("RGBA", background.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    radius = theme["radius"]
    if theme["page"] is not None:
        shadow_draw.rounded_rectangle((card_left, top + 12, card_right, card_bottom + 12), radius=radius, fill=(0, 0, 0, 105))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    background = Image.alpha_composite(background, shadow)
    draw = ImageDraw.Draw(background, "RGBA")
    if theme["page"] is not None:
        draw.rounded_rectangle((card_left, top, card_right, card_bottom), radius=radius, fill=theme["page"])

    y = top + 38
    eyebrow_font = _font(17)
    eyebrow_text = "ЗАПОМНИ ГЛАВНОЕ" if theme["name"] not in {"blueprint", "sunburst"} else ("ФОРМУЛА ДЕНЕГ" if theme["name"] == "blueprint" else "ПРАВИЛО ДОХОДА")
    if theme["header"] == "bar":
        draw.rounded_rectangle((card_left, top, card_right, top + 15), radius=radius, fill=theme["accents"][variant_index % 3])
    elif theme["header"] == "vertical":
        draw.rounded_rectangle((card_left + 10, top + 22, card_left + 18, card_bottom - 22), radius=4, fill=theme["accents"][variant_index % 3])
    eyebrow_width = max(230, draw.textbbox((0, 0), eyebrow_text, font=eyebrow_font)[2] + 34)
    draw.rounded_rectangle((inner_left, y, inner_left + eyebrow_width, y + 36), radius=(18 if radius else 0), fill=theme["eyebrow"])
    draw.text((inner_left + 15, y + 8), eyebrow_text, font=eyebrow_font, fill=theme["eyebrow_text"])
    y += 63
    title_x = inner_left
    if theme["align"] == "center":
        y, _ = _draw_lines(draw, title, title_font, inner_left, y, inner_width, title_line_height, theme["title"], align="center")
    else:
        y, _ = _draw_lines(draw, title, title_font, title_x, y, inner_width - 36, title_line_height, theme["title"])
    y += 24
    divider = theme["accents"][variant_index % 3] if theme["header"] in {"line", "bar"} else (222, 219, 211, 255)
    draw.rounded_rectangle((inner_left, y, inner_left + inner_width, y + (4 if theme["header"] == "line" else 2)), radius=2, fill=divider)
    y += 26

    recognized = [_memory_label(item, i) for i, item in enumerate(items)]
    for index, item in enumerate(items):
        label, kind, _semantic_accent = recognized[index]
        accent = theme["accents"][index % 3] if kind else theme["accents"][index % len(theme["accents"])]
        row_left = inner_left + (theme["offset"] if index % 2 else 0)
        row_width = row_widths[index]
        item_lines = _wrap_lines(item, body_font, draw, row_width - body_indent - 24)
        height = card_heights[index]
        row_mode = theme["row"]
        if row_mode == "light":
            row_fill, text_color = (255, 255, 255, 255), theme["text"]
        elif row_mode == "dark":
            row_fill, text_color = (27, 32, 48, 242), theme["text"]
        elif row_mode == "accent":
            row_fill, text_color = (*accent, 255), (255, 255, 255, 255)
        elif row_mode == "tint":
            row_fill = (*tuple(int(channel * .16 + 255 * .84) for channel in accent), 255)
            text_color = theme["text"]
        elif row_mode == "outline":
            row_fill, text_color = theme["page"], theme["text"]
        else:
            row_fill, text_color = None, theme["text"]
        if row_fill is not None:
            draw.rounded_rectangle(
                (row_left, y, row_left + row_width, y + height),
                radius=theme["radius"] if theme["radius"] < 30 else 26,
                fill=row_fill,
                outline=(*accent, 255) if row_mode == "outline" else (224, 221, 214, 255),
                width=3 if row_mode == "outline" else 1,
            )
        if theme["badge"] == "circle":
            badge_left, badge_top = row_left + 23, y + 23
            draw.ellipse((badge_left, badge_top, badge_left + 58, badge_top + 58), fill=(*accent, 255))
            number = f"{index + 1:02d}"
            number_box = draw.textbbox((0, 0), number, font=label_font)
            draw.text((badge_left + (58 - (number_box[2] - number_box[0])) // 2, badge_top + 17), number, font=label_font, fill=(255, 255, 255, 255))
        elif theme["badge"] == "square":
            badge_left, badge_top = row_left + 22, y + 22
            draw.rounded_rectangle((badge_left, badge_top, badge_left + 62, badge_top + 62), radius=9, fill=(*accent, 255))
            number = f"{index + 1:02d}"
            draw.text((badge_left + 8, badge_top + 15), number, font=label_font, fill=(255, 255, 255, 255))
        elif theme["badge"] == "giant":
            number = f"{index + 1:02d}"
            draw.text((row_left + 22, y + 20), number, font=number_font, fill=(*accent, 255))
        elif theme["badge"] == "symbol":
            symbol = {"more": ">", "equal": "=", "less": "<"}.get(kind, f"{index + 1}")
            symbol_font = _font(72)
            draw.text((row_left + 28, y + 29), symbol, font=symbol_font, fill=(255, 255, 255, 255) if row_mode == "accent" else (*accent, 255))
        elif theme["badge"] == "bar":
            draw.rounded_rectangle((row_left + 17, y + 18, row_left + 25, y + height - 18), radius=4, fill=(*accent, 255))

        label_x = row_left + body_indent
        label_color = (255, 255, 255, 245) if row_mode == "accent" else (*accent, 255)
        if theme["badge"] == "none":
            label_x = row_left + 24
        draw.text((label_x, y + 22), label, font=label_font, fill=label_color)
        body_top = y + body_top_offset
        _draw_lines(draw, item, body_font, label_x, body_top, row_width - (label_x - row_left) - 22, body_line_height, text_color)
        if row_mode == "none":
            draw.rounded_rectangle((row_left, y + height - 3, row_left + row_width, y + height), radius=1, fill=(*accent, 255))
        y += height + 18

    if financial_sequence:
        summary_y = y - 1
        chips = [
            ("БОЛЬШЕ", "ПРОБЛЕМЫ", (54, 30), (207, 82, 75)),
            ("РАВНО", "РАВНОВЕСИЕ", (42, 42), (207, 145, 42)),
            ("МЕНЬШЕ", "ИЗОБИЛИЕ", (28, 56), (45, 145, 107)),
        ]
        chip_gap = 10
        chip_width = (inner_width - chip_gap * 2) // 3
        chip_height = 116
        for i, (relation, outcome, bar_heights, color) in enumerate(chips):
            left = inner_left + i * (chip_width + chip_gap)
            tint = tuple(int(channel * 0.12 + 255 * 0.88) for channel in color)
            draw.rounded_rectangle((left, summary_y, left + chip_width, summary_y + chip_height), radius=20, fill=(*tint, 255), outline=(*color, 180), width=2)
            base_y = summary_y + 89
            draw.line((left + 12, base_y, left + 65, base_y), fill=(104, 119, 138, 255), width=2)
            first_height, second_height = bar_heights
            draw.rounded_rectangle((left + 19, base_y - first_height, left + 37, base_y), radius=4, fill=(*color, 255))
            draw.rounded_rectangle((left + 43, base_y - second_height, left + 61, base_y), radius=4, fill=(102, 139, 178, 255))
            draw.text((left + 77, summary_y + 29), relation, font=summary_font, fill=(*color, 255))
            draw.text((left + 77, summary_y + 54), outcome, font=label_font, fill=(42, 45, 54, 255))
        y += chip_height + 18

    if cta:
        cta_lines = _wrap_lines(cta, cta_font, draw, inner_width - 74)
        cta_height = max(76, len(cta_lines) * 36 + 34)
        draw.rounded_rectangle((inner_left, y, inner_left + inner_width, y + cta_height), radius=min(theme["radius"], 24), fill=theme["cta"])
        _draw_lines(draw, cta, cta_font, inner_left + 28, y + 17, inner_width - 56, 36, theme["cta_text"])

    background.convert("RGB").save(output, format="JPEG", quality=94, optimize=True)
    return output


def render_video(image_path: Path, output_path: Path, *, duration: int = VIDEO_SECONDS) -> Path:
    """Create a silent, platform-ready 9:16 H.264 clip from one still image."""
    image_path = Path(image_path)
    output_path = Path(os.path.abspath(os.fspath(output_path)))
    if image_path.is_symlink() or not image_path.is_file():
        raise ValueError("Выбранное изображение недоступно")
    if output_path.is_symlink():
        raise ValueError("Путь результата не должен быть символической ссылкой")
    image_path = image_path.resolve()
    duration = max(5, min(int(duration), 7))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", str(VIDEO_FPS), "-i", str(image_path),
        "-t", str(duration), "-vf",
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},setsar=1,format=yuv420p",
        "-r", str(VIDEO_FPS), "-frames:v", str(duration * VIDEO_FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-an", "-movflags", "+faststart", str(output_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Рендер видео превысил лимит времени") from exc
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        detail = (result.stderr or "").strip()[-500:]
        raise RuntimeError("Не удалось собрать видео" + (f": {detail}" if detail else ""))
    return output_path


def write_brief(path: Path, *, exact_text: str, visual_direction: str, reference_path: Optional[Path], output_path: Path, index: int) -> Path:
    """Write a bounded, server-owned brief consumed as untrusted agent input."""
    visual_aid = _visual_aid_guidance(visual_direction)
    payload = {
        "index": int(index),
        "variant_number": int(index) + 1,
        "art_direction": _VARIANT_ART_DIRECTIONS[int(index) % len(_VARIANT_ART_DIRECTIONS)],
        "visual_direction": str(visual_direction),
        "visual_aid": visual_aid,
        "platform_safe_zone": {
            "x_percent": [12, 88],
            "y_percent": [14, 85],
            "note": "Keep the central card area low-detail and clear. Reels/Shorts interface controls cover the outer right edge, top bar, and lower caption area.",
        },
        "exact_overlay_text": str(exact_text),
        "reference_image_path": str(Path(reference_path).resolve()) if reference_path else None,
        "output_path": str(Path(output_path).resolve()),
        "requirements": [
            "Create one distinct portrait 9:16 visual background.",
            "Make this variant composition and art medium strongly different from every other variant in the batch, not just a minor color change.",
            "Follow the supplied art_direction for this variant; vary medium, palette, camera angle, focal object, and composition while preserving the user's concept.",
            "Use visual_direction as scene guidance and the reference only as visual inspiration.",
            "Do not make a blank background or a plain text poster. Include a situation-appropriate illustration or visual aid from visual_aid; use a flow, comparison, simple chart, or icon diagram when it clarifies the idea.",
            "Compose the supporting illustration around the platform_safe_zone so app text remains centered, readable, and clear of Reels/Shorts controls.",
            "Do not draw any text, lettering, logos, captions, or watermarks.",
            "The application will add exact_overlay_text locally after generation.",
            "Save the image only to output_path.",
            "Use portrait image dimensions suitable for a 9:16 crop.",
        ],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path
