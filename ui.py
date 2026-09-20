"""
ui.py
=====

Draws the text overlay on top of the live camera frame.

Kept separate from main.py so the main loop stays readable. Nothing in here
touches hardware - it only draws on a numpy image with OpenCV.

Target layout:

    -----------------------------------------
    | FPS 28.4                              |
    |             LIVE CAMERA               |
    |                                       |
    | Distance: 72 cm                       |
    | Status: CAUTION                       |
    | Camera: OK                            |
    | Ultrasonic: OK                        |
    | Audio: OK                             |
    | Press Q to quit                       |
    -----------------------------------------
"""

import cv2

FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.55
FONT_THICKNESS = 1
LINE_HEIGHT = 24
PADDING = 10

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def _draw_text(frame, text, origin, color):
    """Draw text with a dark outline so it stays readable over any scene."""
    cv2.putText(frame, text, origin, FONT, FONT_SCALE, BLACK, FONT_THICKNESS + 2,
                cv2.LINE_AA)
    cv2.putText(frame, text, origin, FONT, FONT_SCALE, color, FONT_THICKNESS,
                cv2.LINE_AA)


def _draw_panel(frame, x, y, width, height, alpha=0.45):
    """Darken a rectangle behind the text so it contrasts with the video."""
    height_limit, width_limit = frame.shape[:2]
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(width_limit, x + width)
    y1 = min(height_limit, y + height)
    if x1 <= x0 or y1 <= y0:
        return

    region = frame[y0:y1, x0:x1]
    darkened = (region * (1.0 - alpha)).astype(region.dtype)
    frame[y0:y1, x0:x1] = darkened


def draw_hud(frame, lines, fps=None, footer="Press Q to quit"):
    """Draw the status block on the frame, in place.

    `lines` is a list of (text, bgr_color) pairs, drawn top to bottom in the
    bottom-left corner of the frame.
    """
    all_lines = list(lines)
    if footer:
        all_lines.append((footer, WHITE))

    if not all_lines:
        return frame

    # Size the panel to fit the widest line.
    widest = 0
    for text, _ in all_lines:
        (text_width, _), _ = cv2.getTextSize(text, FONT, FONT_SCALE, FONT_THICKNESS)
        widest = max(widest, text_width)

    panel_width = widest + 2 * PADDING
    panel_height = len(all_lines) * LINE_HEIGHT + 2 * PADDING
    panel_x = PADDING
    panel_y = frame.shape[0] - panel_height - PADDING

    _draw_panel(frame, panel_x, panel_y, panel_width, panel_height)

    text_y = panel_y + PADDING + LINE_HEIGHT - 6
    for text, color in all_lines:
        _draw_text(frame, text, (panel_x + PADDING, text_y), color)
        text_y += LINE_HEIGHT

    if fps is not None:
        label = "FPS {:.1f}".format(fps)
        (text_width, _), _ = cv2.getTextSize(label, FONT, FONT_SCALE, FONT_THICKNESS)
        _draw_panel(frame, PADDING, PADDING, text_width + 2 * PADDING,
                    LINE_HEIGHT + PADDING)
        _draw_text(frame, label, (PADDING * 2, PADDING + LINE_HEIGHT - 2), WHITE)

    return frame
