"""The run figure (runs/figure.py), its routes, the trace/ media dir, the zip."""
import io
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from maps.store import MapStore
from runs.router import build_runs_router
from tests.run_trace_fixtures import MISSION, RID, write_map, write_run, write_trace


def _client(tmp_path, tz=None, history=None):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(exist_ok=True)
    write_map(tmp_path / "maps")
    app = FastAPI()
    app.include_router(build_runs_router(
        str(runs_dir), map_store=MapStore(tmp_path / "maps"),
        history_samples=lambda *a: history or [],
        site_timezone=(lambda site_id: tz) if tz else None))
    return TestClient(app), runs_dir


def decode_png(data: bytes):
    """(width, height, color type, raw rows) of a non-interlaced 8-bit PNG."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, header = 8, b"", None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        assert zlib.crc32(kind + body) & 0xFFFFFFFF == crc
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    width, height, depth, color = header[:4]
    assert depth == 8
    raw = zlib.decompress(idat)
    channels = {0: 1, 2: 3, 6: 4}[color]
    assert len(raw) == height * (1 + width * channels)
    return width, height, color, raw


def test_svg_has_the_title_and_parses(tmp_path):
    client, runs_dir = _client(tmp_path, tz="Asia/Seoul")
    write_trace(write_run(runs_dir))
    r = client.get(f"/runs/{RID}/figure.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    root = ET.fromstring(r.content)
    assert root.tag.endswith("svg")
    text = r.text
    # 10:34:26 UTC is 19:34 in Seoul.
    assert "Pictlab 2026-11-24 19:34 - drawn vs planned vs walked" in text
    assert "data:image/png;base64," in text
    assert "walked (AMCL" in text and "Nav2 plans" in text and "route as drawn" in text


def test_title_is_utc_without_a_site_timezone(tmp_path):
    client, runs_dir = _client(tmp_path)
    write_trace(write_run(runs_dir))
    assert "Pictlab 2026-11-24 10:34 UTC - drawn vs planned vs walked" in client.get(f"/runs/{RID}/figure.svg").text


def test_png_decodes(tmp_path):
    client, runs_dir = _client(tmp_path)
    write_trace(write_run(runs_dir))
    r = client.get(f"/runs/{RID}/figure.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    width, height, color, raw = decode_png(r.content)
    assert color == 2 and width > 200 and height > 150
    # Somewhere there is the walked line's blue (31, 119, 180).
    assert bytes((31, 119, 180)) in raw


def test_history_figure_labels_the_coarse_line(tmp_path):
    rows = [{"at": "2026-11-24T10:34:3%dZ" % i, "x_m": 1.0 + i * 0.5, "y_m": 1.0, "heading_deg": 0.0,
             "speed_mps": 0.3, "abstract_state": "AUTO_MISSION", "control_owner": None} for i in range(2, 8)]
    client, runs_dir = _client(tmp_path, history=rows)
    write_run(runs_dir)
    text = client.get(f"/runs/{RID}/figure.svg").text
    assert "walked (5 s history samples)" in text
    assert "Nav2 plans" not in text  # history has no plans
    assert client.get(f"/runs/{RID}/figure.png").status_code == 200


def test_figure_with_no_walk_and_no_waypoints_still_draws(tmp_path):
    client, runs_dir = _client(tmp_path)
    write_run(runs_dir, mission={**MISSION, "waypoints": []})
    assert client.get(f"/runs/{RID}/figure.svg").status_code == 200
    assert client.get(f"/runs/{RID}/figure.png").status_code == 200


def test_figure_404_without_a_map(tmp_path):
    client, runs_dir = _client(tmp_path)
    write_run(runs_dir, mission={**MISSION, "map_id": ""})
    assert client.get(f"/runs/{RID}/figure.svg").status_code == 404
    assert client.get(f"/runs/{RID}/figure.png").status_code == 404
    assert client.get("/runs/task-zzz-20261124T000000Z/figure.png").status_code == 404


def test_zip_has_figures_and_trace(tmp_path):
    client, runs_dir = _client(tmp_path)
    write_trace(write_run(runs_dir))
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/runs/{RID}/zip").content))
    names = set(z.namelist())
    for name in ("figure.svg", "figure.png", "trace/samples.csv", "trace/plans.jsonl", "trace/legs.jsonl",
                 "trace/log.txt", "trace/meta.json", "video/run_front.mp4", "run.json", "events.jsonl"):
        assert f"{RID}/{name}" in names, name
    assert z.read(f"{RID}/figure.png")[:8] == b"\x89PNG\r\n\x1a\n"


def test_zip_without_a_map_has_no_figure_and_still_works(tmp_path):
    client, runs_dir = _client(tmp_path)
    write_run(runs_dir, mission={**MISSION, "map_id": ""})
    r = client.get(f"/runs/{RID}/zip")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert not any("figure" in n for n in names)


def test_trace_dir_accepts_uploads_with_content_types(tmp_path):
    client, runs_dir = _client(tmp_path)
    write_run(runs_dir)
    for name, kind in (("samples.csv", "text/csv"), ("plans.jsonl", "application/x-ndjson"),
                       ("meta.json", "application/json"), ("log.txt", "text/plain; charset=utf-8")):
        assert client.put(f"/runs/{RID}/files/trace/{name}", content=b"x").status_code == 200
        assert client.get(f"/runs/{RID}/files/trace/{name}").headers["content-type"].startswith(kind)
    assert {"path": "trace/samples.csv", "size_bytes": 1} in client.get(f"/runs/{RID}").json()["files"]


def test_trace_dir_refuses_oversize(tmp_path, monkeypatch):
    import runs.store as store
    assert store.MAX_FILE_BYTES["trace"] == 200 * 1024 * 1024
    monkeypatch.setitem(store.MAX_FILE_BYTES, "trace", 4)
    client, runs_dir = _client(tmp_path)
    write_run(runs_dir)
    assert client.put(f"/runs/{RID}/files/trace/samples.csv", content=b"12345").status_code == 413


def test_svg_content_type_is_known():
    from runs.store import CONTENT_TYPES
    assert CONTENT_TYPES[".svg"] == "image/svg+xml"


def test_new_routes_have_access_rules():
    from auth.policy import ROUTES, _READ
    for path in ("/runs/{run_id}/track", "/runs/{run_id}/figure.svg", "/runs/{run_id}/figure.png"):
        assert ROUTES[("GET", path)] == _READ
