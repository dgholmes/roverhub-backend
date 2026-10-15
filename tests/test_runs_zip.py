import io
import zipfile

from tests.test_runs_files import RID, _client


def test_zip_contains_the_folder(tmp_path):
    c = _client(tmp_path)
    c.post(f"/runs/{RID}/events", json={"events": [
        {"run_id": RID, "seq": 0, "at": "2026-10-16T06:00:00Z", "kind": "run_started"}]})
    c.put(f"/runs/{RID}/files/photos/01_gate_front_060001.jpg", content=b"jpg")
    c.put(f"/runs/{RID}/files/video/run_front.mp4", content=b"mp4")
    r = c.get(f"/runs/{RID}/zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert f'filename="{RID}.zip"' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = sorted(z.namelist())
    assert names == sorted([f"{RID}/run.json", f"{RID}/events.jsonl",
                            f"{RID}/photos/01_gate_front_060001.jpg", f"{RID}/video/run_front.mp4"])
    assert z.read(f"{RID}/video/run_front.mp4") == b"mp4"
    assert z.getinfo(f"{RID}/video/run_front.mp4").compress_type == zipfile.ZIP_STORED


def test_zip_unknown_run_404(tmp_path):
    assert _client(tmp_path).get("/runs/nope/zip").status_code == 404


def test_zip_build_failure_leaves_no_temp_file(tmp_path, monkeypatch):
    import pytest
    import tempfile
    import zipfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_mkstemp = tempfile.mkstemp
    monkeypatch.setattr(tempfile, "mkstemp",
                        lambda **kw: real_mkstemp(**{**kw, "dir": str(scratch)}))

    def boom(self, *args, **kwargs):
        raise OSError("disk full")

    c = _client(tmp_path)
    c.put(f"/runs/{RID}/files/photos/01_gate_front_060001.jpg", content=b"jpg")
    monkeypatch.setattr(zipfile.ZipFile, "write", boom)
    with pytest.raises(OSError):
        c.get(f"/runs/{RID}/zip")
    assert list(scratch.iterdir()) == []
