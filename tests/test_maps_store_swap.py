"""The swap, the restore, what a re-upload keeps, and the store's own cloud limit.

Split from test_maps_store.py because these inject failures into os.replace and
so are about the one piece of code that protects an existing map.
"""
from __future__ import annotations

import json
import os

import pytest

from maps.store import MAX_CLOUD_BYTES, BundleError, MapStore
from tests.map_bundles import CLOUD, PGM, make_bundle


def _fail_replace_on(monkeypatch, call_number):
    """Make the Nth os.replace raise, as a locked file or a full disk would."""
    real = os.replace
    calls = []

    def replace(src, dst, *args, **kwargs):
        calls.append((str(src), str(dst)))
        if len(calls) == call_number:
            raise OSError("injected failure on os.replace call %d" % call_number)
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    return calls


def test_a_failure_putting_the_new_map_in_restores_the_old_one(tmp_path, monkeypatch):
    # Call 1 moves the old map aside; call 2 (scratch -> directory) fails. The old
    # map must be back, serving its artifacts, with nothing left lying around.
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "old", floors=2, cloud=True))
    bundle = make_bundle(tmp_path / "new", floors=1)
    calls = _fail_replace_on(monkeypatch, 2)
    with pytest.raises(OSError):
        store.save_bundle(bundle)
    assert len(calls) == 3  # aside, the failed move in, the restore
    monkeypatch.undo()
    assert len(store.get("map-venue").floors) == 2
    assert store.artifact_path("map-venue", 1, "grid").read_bytes() == PGM
    assert store.artifact_path("map-venue", 0, "cloud").read_bytes() == CLOUD
    assert os.listdir(root) == ["map-venue"]  # no .incoming-* or .replaced-*


def test_a_failure_moving_the_old_map_aside_leaves_it_untouched(tmp_path, monkeypatch):
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "old", floors=2))
    bundle = make_bundle(tmp_path / "new", floors=1)
    _fail_replace_on(monkeypatch, 1)
    with pytest.raises(OSError):
        store.save_bundle(bundle)
    monkeypatch.undo()
    assert len(store.get("map-venue").floors) == 2
    assert store.artifact_path("map-venue", 1, "grid") is not None
    assert os.listdir(root) == ["map-venue"]  # in particular, no leaked .replaced-*


def test_a_failure_saving_a_new_map_leaves_nothing_behind(tmp_path, monkeypatch):
    root = tmp_path / "maps"
    store = MapStore(root)
    bundle = make_bundle(tmp_path / "new")
    _fail_replace_on(monkeypatch, 1)
    with pytest.raises(OSError):
        store.save_bundle(bundle)
    monkeypatch.undo()
    assert store.list() == []
    assert os.listdir(root) == []


def test_if_the_restore_fails_too_the_old_map_is_kept_and_recovered_on_restart(tmp_path, monkeypatch):
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "old", floors=2))
    bundle = make_bundle(tmp_path / "new", floors=1)
    real = os.replace
    calls = []

    def replace(src, dst, *args, **kwargs):
        calls.append(1)
        if len(calls) >= 2:
            raise OSError("disk went away")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError):
        store.save_bundle(bundle)
    monkeypatch.undo()
    # The only copy is in the .replaced-* directory, and was not deleted...
    leftovers = [n for n in os.listdir(root) if n.startswith(".replaced-")]
    assert len(leftovers) == 1 and (root / leftovers[0] / "map-venue" / "map.json").is_file()
    # ...and starting the store again puts it back.
    restarted = MapStore(root)
    assert len(restarted.get("map-venue").floors) == 2
    assert os.listdir(root) == ["map-venue"]


def test_starting_the_store_clears_what_a_crash_left_behind(tmp_path):
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "b"))
    for name in (".incoming-abc", ".upload-abc", ".deleted-abc"):
        (root / name / "floor0").mkdir(parents=True)
        (root / name / "floor0" / "grid.pgm").write_bytes(PGM)
    # A stale .replaced-* whose map is already back in place is just removed.
    (root / ".replaced-abc" / "map-venue").mkdir(parents=True)
    MapStore(root)
    assert os.listdir(root) == ["map-venue"]
    assert store.get("map-venue") is not None


# --- what a re-upload keeps -------------------------------------------------


def _edit_stored(root, edit):
    path = root / "map-venue" / "map.json"
    doc = json.loads(path.read_text())
    edit(doc)
    path.write_text(json.dumps(doc))


def test_a_reupload_keeps_zones_and_creation_date_but_drops_active_to_draft(tmp_path):
    # Zones were drawn in the old survey's frame, so they are kept (work is not
    # thrown away). "active" says a person checked them against *that* survey and
    # is what missions plan against, so it does not survive a new one.
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "first", created_at="2026-11-01T00:00:00Z",
                                  updated_at="2026-11-01T00:00:00Z"))
    zone = {"zone_id": "z1", "map_id": "map-venue", "kind": "no_go", "label": "Stage",
            "polygon": [[0, 0], [1, 0], [1, 1]]}
    _edit_stored(root, lambda d: d.update(status="active", zones=[zone]))

    saved = store.save_bundle(make_bundle(tmp_path / "second", floors=2,
                                          created_at="2026-11-08T00:00:00Z",
                                          updated_at="2026-11-08T00:00:00Z"))

    for loaded in (saved, store.get("map-venue")):
        assert loaded.status == "draft"
        assert [z.zone_id for z in loaded.zones] == ["z1"]
        assert loaded.created_at == "2026-11-01T00:00:00Z"
        assert loaded.updated_at == "2026-11-08T00:00:00Z"   # this one moves
        assert len(loaded.floors) == 2                        # and the survey is the new one


def test_a_first_upload_takes_its_dates_and_status_from_the_bundle(tmp_path):
    store = MapStore(tmp_path / "maps")
    saved = store.save_bundle(make_bundle(tmp_path / "b", created_at="2026-11-02T00:00:00Z"))
    assert saved.created_at == "2026-11-02T00:00:00Z"
    assert saved.status == "draft" and saved.zones == []


# --- the cloud limit, in the store too --------------------------------------


def test_the_store_refuses_a_cloud_over_the_limit(tmp_path):
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b", cloud=True)
    (bundle / "floor0" / "cloud.bin").write_bytes(b"\x00" * (MAX_CLOUD_BYTES + 12))
    with pytest.raises(BundleError, match="cloud"):
        store.save_bundle(bundle)
    assert store.list() == []
    assert os.listdir(tmp_path / "maps") == []


def test_the_store_accepts_a_cloud_right_at_the_limit(tmp_path):
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b", cloud=True)
    (bundle / "floor0" / "cloud.bin").write_bytes(b"\x00" * MAX_CLOUD_BYTES)
    store.save_bundle(bundle)
    assert store.artifact_path("map-venue", 0, "cloud").stat().st_size == MAX_CLOUD_BYTES


# --- a store that cannot clean up must still start --------------------------


def test_a_failure_restoring_a_map_at_start_does_not_stop_the_store(tmp_path, monkeypatch, caplog):
    # `api/app.py` builds the store at import: an OSError here is a backend that
    # does not start. The one copy of the map is in `.replaced-*` and must stay
    # there, for the next start.
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "b"))
    (root / ".replaced-abc").mkdir()
    os.replace(root / "map-venue", root / ".replaced-abc" / "map-venue")
    assert os.listdir(root) == [".replaced-abc"]

    real = os.replace

    def failing(src, dst, *a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", failing)
    with caplog.at_level("ERROR", logger="roverhub.maps"):
        MapStore(root)  # must not raise
    monkeypatch.setattr(os, "replace", real)

    assert (root / ".replaced-abc" / "map-venue" / "map.json").is_file()
    assert any("map-venue" in r.getMessage() for r in caplog.records)
    assert MapStore(root).get("map-venue") is not None  # and the next start recovers it
