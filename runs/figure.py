"""The run figure: "<mission> <start> - drawn vs planned vs walked".

The engineer's matplotlib plot, drawn by the backend with numpy only:

    survey map    free white, walls black, unknown grey (the floor's grid.pgm)
    route         as drawn: the start pose, then each waypoint in order (orange, dashed)
    waypoints     orange numbered markers inside their tolerance circles
    Nav2 plans    every plan the run made (thin green)
    walked        the AMCL pose from the trace (blue), or the history's 5 s samples

cropped to the route and walk plus 1 m, with a title, metre ticks and a
legend. Two renderings of one Scene: an SVG (the map cropped and embedded as a
PNG, everything else vector) and a PNG (map cells x4, lines rasterised here).
"""
from __future__ import annotations

import base64
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

import numpy as np

from history.db import epoch
from maps.store import FREE_THRESH, OCCUPIED_THRESH, parse_pgm
from shared.schemas.runs import MissionRun

from .png import encode_png, text_mask, text_size

logger = logging.getLogger("roverhub.runs")

Point = Tuple[float, float]

MARGIN_M = 1.0
CELL_PX = 4                 # map cells are drawn 4 x 4 pixels in the PNG
MAX_AXES_PX = 2400          # ... unless that makes the axes larger than this,
MIN_AXES_PX = 700           # or smaller than this (then cells are drawn larger)
WHITE, BLACK, GREY = 255, 0, 205
ORANGE = (255, 127, 14)     # matplotlib's tab10, as the reference figure used
GREEN = (44, 160, 44)
BLUE = (31, 119, 180)
FRAME = (0, 0, 0)
LEGEND_EDGE = (204, 204, 204)
L, T, R, B = 84, 60, 28, 64  # PNG/SVG margins around the axes, pixels


@dataclass
class Scene:
    title: str
    x0: float               # axes extent, metres
    x1: float
    y0: float
    y1: float
    grid: np.ndarray         # (rows, cols) uint8, row 0 at y1, one value per cell
    resolution: float
    route: List[Point] = field(default_factory=list)
    waypoints: List[Tuple[float, float, float, int]] = field(default_factory=list)  # x, y, tolerance, number
    plans: List[List[Point]] = field(default_factory=list)
    walked: List[List[Point]] = field(default_factory=list)
    walked_label: str = "walked (AMCL)"

    def legend(self) -> List[Tuple[str, str]]:
        entries = []
        if self.route:
            entries.append(("route", "route as drawn"))
        if self.waypoints:
            entries.append(("waypoint", "waypoint (tolerance)"))
        if self.plans:
            entries.append(("plan", "Nav2 plans"))
        if self.walked:
            entries.append(("walked", self.walked_label))
        return entries


class NoFigure(LookupError):
    """The run has no map to draw on; `str()` says why."""


# --- building the scene ------------------------------------------------------------


def title_for(run: MissionRun, tz_name: Optional[str]) -> str:
    name = (run.mission.name or run.mission.mission_id or run.mission_id).strip()
    start = epoch(run.started_at)
    when = ""
    if start is not None:
        moment = datetime.fromtimestamp(start, tz=timezone.utc)
        suffix = " UTC"
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                moment = moment.astimezone(ZoneInfo(tz_name))
                suffix = ""
            except Exception:
                logger.warning("unknown site timezone %r; the figure title is in UTC", tz_name)
        when = " " + moment.strftime("%Y-%m-%d %H:%M") + suffix
    return f"{name}{when} - drawn vs planned vs walked"


def _segments(xs: Sequence, ys: Sequence) -> List[List[Point]]:
    out: List[List[Point]] = []
    current: List[Point] = []
    for x, y in zip(xs, ys, strict=False):
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and math.isfinite(x) and math.isfinite(y):
            current.append((float(x), float(y)))
        elif current:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def _classify(pixels: np.ndarray) -> np.ndarray:
    occupancy = (255.0 - pixels.astype(np.float32)) / 255.0
    out = np.full(pixels.shape, GREY, dtype=np.uint8)
    out[occupancy > OCCUPIED_THRESH] = BLACK
    out[occupancy < FREE_THRESH] = WHITE
    return out


def build_scene(run: MissionRun, track: dict, map_store, tz_name: Optional[str] = None) -> Scene:
    """Raises NoFigure when the run has no map, or its grid is unreadable."""
    where = track.get("map")
    if not where or map_store is None:
        raise NoFigure("this run's mission has no map")
    site_map = map_store.get(where["map_id"])
    floor = next((f for f in (site_map.floors if site_map else []) if f.index == where["floor_index"]), None)
    path = map_store.artifact_path(where["map_id"], where["floor_index"], "grid") if floor else None
    if floor is None or path is None:
        raise NoFigure("the run's map or its grid is gone")
    try:
        width, height, raw = parse_pgm(path.read_bytes())
    except Exception as exc:
        raise NoFigure(f"the map grid is unreadable: {exc}") from None
    cells = _classify(np.frombuffer(raw, dtype=np.uint8).reshape(height, width))
    res = float(floor.resolution_m)
    ox, oy = float(floor.origin_xy_m[0]), float(floor.origin_xy_m[1])

    columns = track.get("columns") or {}
    walked = _segments(columns.get("x") or [], columns.get("y") or [])
    plans = [[(float(p[0]), float(p[1])) for p in plan.get("points") or []] for plan in track.get("plans") or []]
    plans = [p for p in plans if len(p) >= 2]
    waypoints = [(w.x_m, w.y_m, float(w.tolerance_m or 0.0), i + 1)
                 for i, w in enumerate(run.mission.waypoints or [])]
    route: List[Point] = []
    if waypoints:
        start = walked[0][0] if walked else None
        route = ([start] if start else []) + [(x, y) for x, y, _, _ in waypoints]

    xs: List[float] = []
    ys: List[float] = []
    for x, y, tol, _ in waypoints:
        xs += [x - tol, x + tol]
        ys += [y - tol, y + tol]
    for line in walked + plans + [route]:
        xs += [p[0] for p in line]
        ys += [p[1] for p in line]
    if xs:
        lo_x, hi_x, lo_y, hi_y = min(xs) - MARGIN_M, max(xs) + MARGIN_M, min(ys) - MARGIN_M, max(ys) + MARGIN_M
    else:  # nothing to draw but the map: all of it
        lo_x, hi_x, lo_y, hi_y = ox, ox + width * res, oy, oy + height * res
    c0, c1 = math.floor((lo_x - ox) / res), math.ceil((hi_x - ox) / res)
    j0, j1 = math.floor((lo_y - oy) / res), math.ceil((hi_y - oy) / res)
    c1, j1 = max(c1, c0 + 1), max(j1, j0 + 1)
    # The crop may reach past the floor's grid: that part is unknown.
    grid = np.full((j1 - j0, c1 - c0), GREY, dtype=np.uint8)
    src_c0, src_c1 = max(c0, 0), min(c1, width)
    src_j0, src_j1 = max(j0, 0), min(j1, height)
    if src_c0 < src_c1 and src_j0 < src_j1:
        # Floor row index from the top is height-1-j; crop row from the top is j1-1-j.
        block = cells[height - src_j1:height - src_j0, src_c0:src_c1]
        grid[j1 - src_j1:j1 - src_j0, src_c0 - c0:src_c1 - c0] = block
    label = "walked (5 s history samples)" if track.get("source") == "history" else "walked (AMCL)"
    return Scene(title=title_for(run, tz_name), x0=ox + c0 * res, x1=ox + c1 * res, y0=oy + j0 * res,
                 y1=oy + j1 * res, grid=grid, resolution=res, route=route, waypoints=waypoints, plans=plans,
                 walked=walked, walked_label=label)


# --- shared layout ----------------------------------------------------------------


@dataclass
class Layout:
    left: int               # the axes' left edge (centred under a wider title)
    cell_px: int
    ppm: float              # pixels per metre
    ax_w: int
    ax_h: int
    width: int
    height: int

    def to_px(self, scene: Scene, x: float, y: float) -> Tuple[float, float]:
        return (self.left + (x - scene.x0) * self.ppm, T + (scene.y1 - y) * self.ppm)


def layout_for(scene: Scene) -> Layout:
    rows, cols = scene.grid.shape
    longest = max(rows, cols, 1)
    cell_px = CELL_PX
    if longest * cell_px < MIN_AXES_PX:
        cell_px = math.ceil(MIN_AXES_PX / longest)
    cell_px = max(1, min(cell_px, MAX_AXES_PX // longest))
    ax_w, ax_h = cols * cell_px, rows * cell_px
    width = max(L + ax_w + R, text_size(scene.title, 2)[0] + 40)
    left = L + (width - (L + ax_w + R)) // 2
    return Layout(left, cell_px, cell_px / scene.resolution, ax_w, ax_h, width, T + ax_h + B)


def nice_ticks(lo: float, hi: float, target: int = 7) -> Tuple[List[float], float]:
    span = max(hi - lo, 1e-9)
    raw = span / target
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    ticks = []
    value = first
    while value <= hi + 1e-9:
        ticks.append(round(value, 6))
        value += step
    return ticks, step


def _tick_label(value: float, step: float) -> str:
    if step >= 1:
        return f"{value:.0f}".replace("-0", "0") if abs(value) < 0.5 else f"{value:.0f}"
    return f"{value:.1f}"


def _legend_corner(scene: Scene, lay: Layout, box_w: int, box_h: int) -> Tuple[int, int]:
    """The axes corner whose box covers the fewest drawn points."""
    points = [lay.to_px(scene, x, y) for line in scene.walked + scene.plans + [scene.route] for x, y in line]
    points += [lay.to_px(scene, x, y) for x, y, _, _ in scene.waypoints]
    pad = 10
    corners = [
        (lay.left + lay.ax_w - box_w - pad, T + pad),           # upper right first, as matplotlib's eye goes
        (lay.left + pad, T + pad),
        (lay.left + lay.ax_w - box_w - pad, T + lay.ax_h - box_h - pad),
        (lay.left + pad, T + lay.ax_h - box_h - pad),
    ]

    def covered(corner):
        cx, cy = corner
        return sum(1 for px, py in points if cx - 6 <= px <= cx + box_w + 6 and cy - 6 <= py <= cy + box_h + 6)

    return min(corners, key=covered)


LEGEND_ROW, LEGEND_SAMPLE = 22, 36


def _legend_box(scene: Scene, svg: bool = False) -> Tuple[int, int]:
    entries = scene.legend()
    # The SVG's 13 px sans-serif is about 7 px a character; the PNG's font is 12.
    label_w = max(((int(len(label) * 7.2) if svg else text_size(label, 2)[0]) for _, label in entries), default=0)
    return (LEGEND_SAMPLE + label_w + 30, LEGEND_ROW * len(entries) + 12)


# --- PNG ------------------------------------------------------------------------------


class Canvas:
    def __init__(self, width: int, height: int):
        self.img = np.full((height, width, 3), 255, dtype=np.uint8)
        self.h, self.w = height, width

    def paint(self, mask: np.ndarray, color, alpha: float = 1.0) -> None:
        if alpha >= 1.0:
            self.img[mask] = color
        else:
            sel = self.img[mask].astype(np.float32)
            self.img[mask] = (sel * (1 - alpha) + np.asarray(color, np.float32) * alpha + 0.5).astype(np.uint8)

    def stamp(self, xs: np.ndarray, ys: np.ndarray, radius: float, color, alpha: float = 1.0,
              clip: Optional[Tuple[int, int, int, int]] = None) -> None:
        """Disks of `radius` at every (xs, ys), painted once (no double blending)."""
        if len(xs) == 0:
            return
        r = int(math.ceil(radius))
        dy, dx = np.mgrid[-r:r + 1, -r:r + 1]
        keep = dx * dx + dy * dy <= radius * radius + 0.25
        dx, dy = dx[keep], dy[keep]
        px = (np.rint(xs).astype(np.int64)[:, None] + dx[None, :]).ravel()
        py = (np.rint(ys).astype(np.int64)[:, None] + dy[None, :]).ravel()
        x_lo, y_lo, x_hi, y_hi = clip or (0, 0, self.w, self.h)
        ok = (px >= x_lo) & (px < x_hi) & (py >= y_lo) & (py < y_hi)
        mask = np.zeros((self.h, self.w), dtype=bool)
        mask[py[ok], px[ok]] = True
        self.paint(mask, color, alpha)

    def text(self, x: int, y: int, text: str, scale: int = 1, color=FRAME, anchor: str = "tl",
             rotate: bool = False) -> None:
        mask = text_mask(text, scale)
        if rotate:
            mask = np.rot90(mask)
        h, w = mask.shape
        if "c" in anchor:
            x -= w // 2
        if "r" in anchor:
            x -= w
        if "m" in anchor:
            y -= h // 2
        if "b" in anchor:
            y -= h
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, self.w), min(y + h, self.h)
        if x0 >= x1 or y0 >= y1:
            return
        full = np.zeros((self.h, self.w), dtype=bool)
        full[y0:y1, x0:x1] = mask[y0 - y:y1 - y, x0 - x:x1 - x]
        self.paint(full, color)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color, fill: bool = False) -> None:
        if fill:
            self.img[max(y0, 0):y1, max(x0, 0):x1] = color
        else:
            self.img[y0, x0:x1] = color
            self.img[y1 - 1, x0:x1] = color
            self.img[y0:y1, x0] = color
            self.img[y0:y1, x1 - 1] = color


def _polyline_samples(points: Sequence[Tuple[float, float]], step: float = 0.5):
    """Sample points every `step` pixels along the polyline, with arc length."""
    xs, ys, ss = [], [], []
    run = 0.0
    for (ax, ay), (bx, by) in zip(points, points[1:], strict=False):
        length = math.hypot(bx - ax, by - ay)
        n = max(2, int(math.ceil(length / step)) + 1)
        t = np.linspace(0.0, 1.0, n)
        xs.append(ax + (bx - ax) * t)
        ys.append(ay + (by - ay) * t)
        ss.append(run + length * t)
        run += length
    if not xs:
        if points:
            return np.array([points[0][0]]), np.array([points[0][1]]), np.array([0.0])
        return np.array([]), np.array([]), np.array([])
    return np.concatenate(xs), np.concatenate(ys), np.concatenate(ss)


def _draw_line(canvas: Canvas, points, radius: float, color, alpha: float = 1.0,
               dash: Optional[Tuple[float, float]] = None, clip=None) -> None:
    xs, ys, ss = _polyline_samples(points)
    if dash is not None and len(ss):
        on, off = dash
        keep = (ss % (on + off)) < on
        xs, ys = xs[keep], ys[keep]
    canvas.stamp(xs, ys, radius, color, alpha, clip)


def _circle(cx: float, cy: float, r: float, n: int = 0) -> List[Tuple[float, float]]:
    n = n or max(24, int(r * 2))
    angles = np.linspace(0.0, 2 * math.pi, n + 1)
    return list(zip((cx + r * np.cos(angles)).tolist(), (cy + r * np.sin(angles)).tolist(), strict=True))


def render_png(scene: Scene) -> bytes:
    lay = layout_for(scene)
    canvas = Canvas(lay.width, lay.height)
    grid = scene.grid.repeat(lay.cell_px, axis=0).repeat(lay.cell_px, axis=1)
    canvas.img[T:T + lay.ax_h, lay.left:lay.left + lay.ax_w] = grid[:, :, None]
    clip = (lay.left, T, lay.left + lay.ax_w, T + lay.ax_h)
    px = lambda line: [lay.to_px(scene, x, y) for x, y in line]  # noqa: E731

    for x, y, tol, _ in scene.waypoints:
        if tol > 0:
            cx, cy = lay.to_px(scene, x, y)
            _draw_line(canvas, _circle(cx, cy, tol * lay.ppm), 0.8, ORANGE, 0.8, clip=clip)
    for plan in scene.plans:
        _draw_line(canvas, px(plan), 0.8, GREEN, 0.7, clip=clip)
    if scene.route:
        _draw_line(canvas, px(scene.route), 1.3, ORANGE, 1.0, dash=(10.0, 6.0), clip=clip)
    for line in scene.walked:
        _draw_line(canvas, px(line), 1.5, BLUE, 1.0, clip=clip)
    for x, y, _, number in scene.waypoints:
        cx, cy = lay.to_px(scene, x, y)
        radius = 11.0 if number < 10 else 14.0
        canvas.stamp(np.array([cx]), np.array([cy]), radius + 1, FRAME, 1.0, clip)
        canvas.stamp(np.array([cx]), np.array([cy]), radius, ORANGE, 1.0, clip)
        canvas.text(int(round(cx)) + 1, int(round(cy)) + 1, str(number), 2, (255, 255, 255), anchor="cm")

    # Frame, ticks, labels, title.
    canvas.rect(lay.left - 1, T - 1, lay.left + lay.ax_w + 1, T + lay.ax_h + 1, FRAME)
    ticks, step = nice_ticks(scene.x0, scene.x1)
    for value in ticks:
        x = int(round(lay.left + (value - scene.x0) * lay.ppm))
        canvas.rect(x, T + lay.ax_h, x + 1, T + lay.ax_h + 6, FRAME, fill=True)
        canvas.text(x, T + lay.ax_h + 10, _tick_label(value, step), 2, anchor="tc")
    ticks, step = nice_ticks(scene.y0, scene.y1)
    for value in ticks:
        y = int(round(T + (scene.y1 - value) * lay.ppm))
        canvas.rect(lay.left - 6, y, lay.left, y + 1, FRAME, fill=True)
        canvas.text(lay.left - 10, y, _tick_label(value, step), 2, anchor="rm")
    canvas.text(lay.left + lay.ax_w // 2, T + lay.ax_h + 34, "x [m]", 2, anchor="tc")
    canvas.text(lay.left - L + 14, T + lay.ax_h // 2, "y [m]", 2, anchor="lm", rotate=True)
    canvas.text(lay.width // 2, 20, scene.title, 2, anchor="tc")

    entries = scene.legend()
    if entries:
        box_w, box_h = _legend_box(scene)
        bx, by = _legend_corner(scene, lay, box_w, box_h)
        canvas.rect(bx, by, bx + box_w, by + box_h, (255, 255, 255), fill=True)
        canvas.rect(bx, by, bx + box_w, by + box_h, LEGEND_EDGE)
        for i, (kind, label) in enumerate(entries):
            cy = by + 6 + LEGEND_ROW * i + LEGEND_ROW // 2
            sx0, sx1 = bx + 10, bx + 10 + LEGEND_SAMPLE - 8
            if kind == "route":
                _draw_line(canvas, [(sx0, cy), (sx1, cy)], 1.3, ORANGE, dash=(8.0, 4.0))
            elif kind == "plan":
                _draw_line(canvas, [(sx0, cy), (sx1, cy)], 0.6, GREEN, 0.8)
            elif kind == "walked":
                _draw_line(canvas, [(sx0, cy), (sx1, cy)], 1.5, BLUE)
            else:
                mid = (sx0 + sx1) / 2
                _draw_line(canvas, _circle(mid, cy, 9.0), 0.8, ORANGE, 0.8)
                canvas.stamp(np.array([mid]), np.array([float(cy)]), 5.0, ORANGE)
            canvas.text(bx + 10 + LEGEND_SAMPLE, cy, label, 2, anchor="lm")
    return encode_png(canvas.img)


# --- SVG ------------------------------------------------------------------------------


def _path(points: Sequence[Tuple[float, float]]) -> str:
    return "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in points)


def render_svg(scene: Scene) -> str:
    lay = layout_for(scene)
    font = 'font-family="DejaVu Sans, Helvetica, Arial, sans-serif"'
    px = lambda line: [lay.to_px(scene, x, y) for x, y in line]  # noqa: E731
    grid_png = base64.b64encode(encode_png(scene.grid)).decode("ascii")
    hexc = lambda c: "#%02x%02x%02x" % c  # noqa: E731
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{lay.width}" height="{lay.height}" '
        f'viewBox="0 0 {lay.width} {lay.height}">',
        f"<title>{escape(scene.title)}</title>",
        f'<rect width="{lay.width}" height="{lay.height}" fill="#ffffff"/>',
        '<defs><clipPath id="axes">'
        f'<rect x="{lay.left}" y="{T}" width="{lay.ax_w}" height="{lay.ax_h}"/></clipPath></defs>',
        f'<image x="{lay.left}" y="{T}" width="{lay.ax_w}" height="{lay.ax_h}" preserveAspectRatio="none" '
        f'style="image-rendering:pixelated" href="data:image/png;base64,{grid_png}"/>',
        '<g clip-path="url(#axes)" fill="none" stroke-linecap="round" stroke-linejoin="round">',
    ]
    for x, y, tol, _ in scene.waypoints:
        if tol > 0:
            cx, cy = lay.to_px(scene, x, y)
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{tol * lay.ppm:.1f}" stroke="{hexc(ORANGE)}" '
                       'stroke-width="1.5" stroke-opacity="0.8"/>')
    if scene.plans:
        out.append(f'<g stroke="{hexc(GREEN)}" stroke-width="1.2" stroke-opacity="0.6">')
        out += [f'<path d="{_path(px(plan))}"/>' for plan in scene.plans]
        out.append("</g>")
    if scene.route:
        out.append(f'<path d="{_path(px(scene.route))}" stroke="{hexc(ORANGE)}" stroke-width="2.5" '
                   'stroke-dasharray="10 6"/>')
    for line in scene.walked:
        if len(line) == 1:
            cx, cy = lay.to_px(scene, *line[0])
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="1.5" fill="{hexc(BLUE)}"/>')
        else:
            out.append(f'<path d="{_path(px(line))}" stroke="{hexc(BLUE)}" stroke-width="3"/>')
    for x, y, _, number in scene.waypoints:
        cx, cy = lay.to_px(scene, x, y)
        out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{11 if number < 10 else 13}" fill="{hexc(ORANGE)}" '
                   'stroke="#000000" stroke-width="1"/>')
        out.append(f'<text x="{cx:.1f}" y="{cy + 5:.1f}" {font} font-size="14" font-weight="bold" '
                   f'fill="#ffffff" text-anchor="middle">{number}</text>')
    out.append("</g>")
    out.append(f'<rect x="{lay.left - 0.5}" y="{T - 0.5}" width="{lay.ax_w + 1}" height="{lay.ax_h + 1}" '
               'fill="none" stroke="#000000" stroke-width="1"/>')
    ticks, step = nice_ticks(scene.x0, scene.x1)
    for value in ticks:
        x = lay.left + (value - scene.x0) * lay.ppm
        out.append(f'<line x1="{x:.1f}" y1="{T + lay.ax_h}" x2="{x:.1f}" y2="{T + lay.ax_h + 6}" stroke="#000"/>')
        out.append(f'<text x="{x:.1f}" y="{T + lay.ax_h + 22}" {font} font-size="13" '
                   f'text-anchor="middle">{_tick_label(value, step)}</text>')
    ticks, step = nice_ticks(scene.y0, scene.y1)
    for value in ticks:
        y = T + (scene.y1 - value) * lay.ppm
        out.append(f'<line x1="{lay.left - 6}" y1="{y:.1f}" x2="{lay.left}" y2="{y:.1f}" stroke="#000"/>')
        out.append(f'<text x="{lay.left - 10}" y="{y + 4:.1f}" {font} font-size="13" '
                   f'text-anchor="end">{_tick_label(value, step)}</text>')
    out.append(f'<text x="{lay.left + lay.ax_w / 2:.1f}" y="{T + lay.ax_h + 48}" {font} font-size="14" '
               'text-anchor="middle">x [m]</text>')
    label_x = lay.left - L + 22
    out.append(f'<text x="{label_x}" y="{T + lay.ax_h / 2:.1f}" {font} font-size="14" text-anchor="middle" '
               f'transform="rotate(-90 {label_x} {T + lay.ax_h / 2:.1f})">y [m]</text>')
    out.append(f'<text x="{lay.width / 2:.1f}" y="34" {font} font-size="17" '
               f'text-anchor="middle">{escape(scene.title)}</text>')
    entries = scene.legend()
    if entries:
        box_w, box_h = _legend_box(scene, svg=True)
        bx, by = _legend_corner(scene, lay, box_w, box_h)
        out.append(f'<g><rect x="{bx}" y="{by}" width="{box_w}" height="{box_h}" rx="3" fill="#ffffff" '
                   f'fill-opacity="0.9" stroke="{hexc(LEGEND_EDGE)}"/>')
        for i, (kind, label) in enumerate(entries):
            cy = by + 6 + LEGEND_ROW * i + LEGEND_ROW // 2
            sx0, sx1 = bx + 10, bx + 10 + LEGEND_SAMPLE - 8
            if kind == "route":
                out.append(f'<line x1="{sx0}" y1="{cy}" x2="{sx1}" y2="{cy}" stroke="{hexc(ORANGE)}" '
                           'stroke-width="2.5" stroke-dasharray="8 4"/>')
            elif kind == "plan":
                out.append(f'<line x1="{sx0}" y1="{cy}" x2="{sx1}" y2="{cy}" stroke="{hexc(GREEN)}" '
                           'stroke-width="1.2"/>')
            elif kind == "walked":
                out.append(f'<line x1="{sx0}" y1="{cy}" x2="{sx1}" y2="{cy}" stroke="{hexc(BLUE)}" '
                           'stroke-width="3"/>')
            else:
                mid = (sx0 + sx1) / 2
                out.append(f'<circle cx="{mid}" cy="{cy}" r="9" fill="none" stroke="{hexc(ORANGE)}" '
                           'stroke-width="1.5"/>')
                out.append(f'<circle cx="{mid}" cy="{cy}" r="5" fill="{hexc(ORANGE)}"/>')
            out.append(f'<text x="{bx + 10 + LEGEND_SAMPLE}" y="{cy + 4}" {font} font-size="13">'
                       f"{escape(label)}</text>")
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)
