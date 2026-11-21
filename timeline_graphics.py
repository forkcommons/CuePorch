"""Original, resizable timeline graphics and authenticated previews."""
import math
import re
import unicodedata
from io import BytesIO

from PIL import Image, ImageColor, ImageDraw, ImageFont
from flask import abort, request, Response

GRAPHICS = (
    {"id": "frame", "title": "Frame", "category": "frame"},
    {"id": "gradient", "title": "Gradient", "category": "gradient"},
    {"id": "arrow", "title": "Arrow", "category": "arrow"},
    {"id": "bubble", "title": "Bubble", "category": "bubble"},
    {"id": "button", "title": "Button", "category": "button"},
    {"id": "callout", "title": "Callout", "category": "callout"},
    {"id": "circle", "title": "Circle", "category": "circle"},
    {"id": "star", "title": "Star", "category": "star"},
    {"id": "smile", "title": "Smile", "category": "smile"},
)

GRAPHIC_IDS = {graphic["id"] for graphic in GRAPHICS}

POSITIONS = (
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
    "center",
)

LABELED = {"bubble", "button", "callout"}


def normalize_graphic(raw):
    if raw is None:
        return None

    if not isinstance(raw, dict):
        abort(422, "graphic must be an object")

    if "id" not in raw:
        abort(422, "id is required")

    graphic_id = raw["id"]
    if not isinstance(graphic_id, str):
        abort(422, "id must be a string")

    if graphic_id not in GRAPHIC_IDS:
        abort(422, "unknown id")

    color = raw.get("color")
    if not isinstance(color, str):
        abort(422, "color must be a string")

    if re.fullmatch(r"[0-9a-f]{6}", color) is None:
        abort(422, "color must be six lowercase hex characters without #")

    if "position" in raw:
        position = raw["position"]
        if not isinstance(position, str):
            abort(422, "position must be a string")
        if position not in POSITIONS:
            abort(422, "invalid position")
    else:
        position = "center"

    if "text" in raw:
        text = raw["text"]
        if not isinstance(text, str):
            abort(422, "text must be a string")
    else:
        text = ""

    for char in text:
        if unicodedata.category(char) in ("Cc", "Cs"):
            abort(422, "text contains control or surrogate characters")

    if len(text) > 60:
        abort(422, "text must be 0 to 60 characters")

    text = text.strip() if graphic_id in LABELED else ""

    return {
        "id": graphic_id,
        "position": position,
        "color": color,
        "text": text,
    }


def _place(size, position, scale=0.34):
    width = max(1, int(size[0]))
    height = max(1, int(size[1]))
    shorter_side = min(width, height)
    margin = max(2, int(round(shorter_side * 0.08)))
    inner_left = margin
    inner_top = margin
    inner_right = max(margin, width - margin)
    inner_bottom = max(margin, height - margin)
    radius = max(3, int(round(shorter_side * scale)))
    diameter = 2 * radius
    if "left" in position:
        box_left = inner_left
    elif "right" in position:
        box_left = inner_right - diameter
    else:
        box_left = (inner_left + inner_right) // 2 - radius
    if "top" in position:
        box_top = inner_top
    elif "bottom" in position:
        box_top = inner_bottom - diameter
    else:
        box_top = (inner_top + inner_bottom) // 2 - radius
    box = (
        max(0, int(box_left)),
        max(0, int(box_top)),
        min(width - 1, int(box_left + diameter)),
        min(height - 1, int(box_top + diameter)),
    )
    return width, height, shorter_side, box


def _label(draw, box, text, color):
    if not text:
        return
    x0, y0, x1, y1 = [int(value) for value in box]
    box_width = x1 - x0
    box_height = y1 - y0
    if box_width < 6 or box_height < 6:
        return
    luminance = 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]
    if luminance > 140:
        fill = (0, 0, 0, 255)
    else:
        fill = (255, 255, 255, 255)
    for font_size in range(max(6, int(box_height * 0.55)), 5, -1):
        font = ImageFont.load_default(size=font_size)
        text_bounds = draw.textbbox((0, 0), text, font=font)
        text_width = text_bounds[2] - text_bounds[0]
        text_height = text_bounds[3] - text_bounds[1]
        if text_width <= box_width - 4 and text_height <= box_height - 4:
            text_x = x0 + (box_width - text_width) // 2 - text_bounds[0]
            text_y = y0 + (box_height - text_height) // 2 - text_bounds[1]
            draw.text((text_x, text_y), text, font=font, fill=fill)
            return
    font = ImageFont.load_default(size=6)
    draw.text((x0 + 2, y0 + 2), text, font=font, fill=fill)


def draw_graphic(graphic, size):
    width, height, shorter_side, box = _place(size, graphic.get("position", "center"))
    rgb = ImageColor.getrgb("#" + graphic["color"])
    color = rgb + (255,)
    gid = graphic["id"]

    if gid == "frame":
        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        thickness = max(2, shorter_side // 24)
        draw.rectangle([0, 0, width - 1, height - 1], outline=color, width=thickness)
        return img

    if gid == "gradient":
        alpha_mask = Image.linear_gradient("L").resize((width, height))
        solid = Image.new("RGB", (width, height), rgb)
        red_channel, green_channel, blue_channel = solid.split()
        return Image.merge("RGBA", (red_channel, green_channel, blue_channel, alpha_mask))

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return img

    if gid == "arrow":
        box_width = x1 - x0
        box_height = y1 - y0
        center_y = (y0 + y1) // 2
        shaft_half = box_height * 0.34
        head_width = box_width * 0.42
        head_start_x = x1 - head_width
        points = [
            (x0, center_y - shaft_half / 2),
            (head_start_x, center_y - shaft_half / 2),
            (head_start_x, center_y - box_height / 2),
            (x1, center_y),
            (head_start_x, center_y + box_height / 2),
            (head_start_x, center_y + shaft_half / 2),
            (x0, center_y + shaft_half / 2),
        ]
        draw.polygon([(int(px), int(py)) for px, py in points], fill=color)
    elif gid == "bubble":
        corner_radius = max(3, int((y1 - y0) * 0.18))
        tail_size = max(4, int((y1 - y0) * 0.22))
        body = (x0, y0, x1, y1 - tail_size)
        draw.rounded_rectangle(body, radius=corner_radius, fill=color)
        tail_x = x0 + int((x1 - x0) * 0.28)
        tail_points = [
            (tail_x, y1 - tail_size),
            (tail_x + tail_size, y1 - tail_size),
            (tail_x, y1),
        ]
        draw.polygon(tail_points, fill=color)
        label_box = (body[0] + 4, body[1] + 4, body[2] - 4, body[3] - 4)
        _label(draw, label_box, graphic.get("text", ""), color)
    elif gid == "button":
        corner_radius = max(3, int((y1 - y0) * 0.22))
        draw.rounded_rectangle([x0, y0, x1, y1], radius=corner_radius, fill=color)
        label_box = (x0 + 4, y0 + 4, x1 - 4, y1 - 4)
        _label(draw, label_box, graphic.get("text", ""), color)
    elif gid == "callout":
        tail_height = max(4, int((y1 - y0) * 0.22))
        draw.rectangle([x0, y0, x1, y1 - tail_height], fill=color)
        tail_x = x0 + int((x1 - x0) * 0.18)
        tail_points = [
            (tail_x, y1 - tail_height),
            (tail_x + tail_height, y1 - tail_height),
            (tail_x, y1),
        ]
        draw.polygon(tail_points, fill=color)
        label_box = (x0 + 4, y0 + 4, x1 - 4, y1 - tail_height - 4)
        _label(draw, label_box, graphic.get("text", ""), color)
    elif gid == "circle":
        draw.ellipse([x0, y0, x1, y1], fill=color)
    elif gid == "star":
        center_x = (x0 + x1) / 2
        center_y = (y0 + y1) / 2
        outer_radius = min(x1 - x0, y1 - y0) / 2
        inner_radius = outer_radius * 0.45
        points = []
        for point_index in range(10):
            angle = -math.pi / 2 + point_index * math.pi / 5
            if point_index % 2 == 0:
                radius = outer_radius
            else:
                radius = inner_radius
            points.append((center_x + radius * math.cos(angle), center_y + radius * math.sin(angle)))
        draw.polygon(points, fill=color)
    elif gid == "smile":
        thickness = max(2, min(x1 - x0, y1 - y0) // 10)
        draw.ellipse([x0, y0, x1, y1], outline=color, width=thickness)
        center_x = (x0 + x1) // 2
        center_y = (y0 + y1) // 2
        eye_width = max(2, (x1 - x0) // 8)
        eye_height = max(2, (y1 - y0) // 8)
        eye_y = center_y - (y1 - y0) // 6
        for side in (-1, 1):
            eye_x = center_x + side * (x1 - x0) // 6
            eye_box = (
                eye_x - eye_width // 2,
                eye_y - eye_height // 2,
                eye_x + eye_width // 2,
                eye_y + eye_height // 2,
            )
            draw.ellipse(eye_box, fill=color)
        mouth_box = [
            center_x - (x1 - x0) // 3,
            center_y - (y1 - y0) // 6,
            center_x + (x1 - x0) // 3,
            center_y + (y1 - y0) // 2,
        ]
        draw.arc(mouth_box, 20, 160, fill=color, width=thickness)
    return img


def register_graphics(app, staff, sizes):
    @app.get("/api/library/timeline-graphics")
    def get_timeline_graphics():
        staff()
        return dict(
            assets=[
                dict(asset, text=asset["id"] in LABELED)
                for asset in GRAPHICS
            ]
        )

    @app.post("/api/library/timeline-graphics/preview")
    def preview_timeline_graphic():
        staff()
        request.max_content_length = 4096
        body = request.get_json()
        if not isinstance(body, dict):
            return Response(status=400)
        ratio = body.get("ratio")
        if not isinstance(ratio, str) or ratio not in sizes:
            return Response(status=400)
        graphic = normalize_graphic(body.get("graphic"))
        if graphic is None:
            return Response(status=400)
        ratio_w, ratio_h = sizes[ratio]
        if ratio_w >= ratio_h:
            size = (240, max(1, int(round(ratio_h * 240.0 / ratio_w))))
        else:
            size = (max(1, int(round(ratio_w * 240.0 / ratio_h))), 240)
        buf = BytesIO()
        draw_graphic(graphic, size).save(buf, "PNG")
        return Response(
            buf.getvalue(),
            mimetype="image/png",
            headers={"Cache-Control": "private, no-store"},
        )
