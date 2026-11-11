"""File-backed map store (maps/store.py)."""
from __future__ import annotations

import json
import os

import pytest

from maps.store import MAX_CLOUD_BYTES, BundleError, MapStore
from tests.map_bundles import CLOUD, PGM, PNG, make_bundle


def test_save_bundle_writes_a_readable_map(tmp_path):
    store = MapStore(tmp_path / "maps")
    site_map = store.save_bundle(make_bundle(tmp_path / "b"))
    assert site_map.map_id == "map-venue"
    loaded = store.get("map-venue")
    assert loaded.name == "Venue"
    assert loaded.status == "draft"
    assert len(loaded.floors) == 1
    assert loaded.floors[0].free_cells == 3
    assert loaded.floors[0].floor_z_m == -0.34


def test_get_and_list_see_what_was_saved(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "a", map_id="map-a"))
    store.save_bundle(make_bundle(tmp_path / "b", map_id="map-b"))
    assert sorted(m.map_id for m in store.list()) == ["map-a", "map-b"]
    assert store.get("map-nope") is None


def test_the_artifacts_are_stored_byte_for_byte(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b", cloud=True))
    assert store.artifact_path("map-venue", 0, "grid").read_bytes() == PGM
    assert store.artifact_path("map-venue", 0, "preview").read_bytes() == PNG
    assert store.artifact_path("map-venue", 0, "cloud").read_bytes() == CLOUD


def test_a_floor_without_a_cloud_has_no_cloud_artifact(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b", cloud=False))
    assert store.artifact_path("map-venue", 0, "cloud") is None
    assert store.artifact_path("map-venue", 0, "grid") is not None


def test_resaving_a_map_id_replaces_it_atomically(tmp_path):
    # A half-replaced bundle must never be servable: after the second save the
    # map is exactly the one-floor bundle, with nothing left of floor 1.
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "two", map_id="map-venue", floors=2, cloud=True))
    assert store.artifact_path("map-venue", 1, "grid") is not None
    store.save_bundle(make_bundle(tmp_path / "one", map_id="map-venue", floors=1))
    assert len(store.get("map-venue").floors) == 1
    assert store.artifact_path("map-venue", 1, "grid") is None
    assert store.artifact_path("map-venue", 0, "cloud") is None  # the old cloud went too
    assert not (tmp_path / "maps" / "map-venue" / "floor1").exists()
    assert [m.map_id for m in store.list()] == ["map-venue"]


def test_a_failed_resave_leaves_the_old_map_untouched(tmp_path):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "good", floors=2))
    bad = make_bundle(tmp_path / "bad", floors=1)
    (bad / "floor0" / "grid.pgm").unlink()
    with pytest.raises(BundleError):
        store.save_bundle(bad)
    assert len(store.get("map-venue").floors) == 2
    assert store.artifact_path("map-venue", 1, "grid") is not None


def test_saving_leaves_no_scratch_directories_behind(tmp_path):
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "a"))
    store.save_bundle(make_bundle(tmp_path / "b"))
    assert os.listdir(root) == ["map-venue"]


@pytest.mark.parametrize("kind", [
    "../../etc/passwd", "..", "grid.pgm", "GRID", "grid/", "grid\x00", "", "site_map", "map",
])
def test_artifact_path_refuses_any_kind_but_the_three(tmp_path, kind):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b", cloud=True))
    assert store.artifact_path("map-venue", 0, kind) is None


@pytest.mark.parametrize("map_id", [
    "..", ".", "../maps", "..\\maps", "map-venue/..", "/etc", "C:\\Windows", "C:/Windows",
    "a/b", "a\\b", ".hidden", "map-venue.", "map venue", "", "x" * 200, "con", "NUL",
])
def test_artifact_path_and_get_refuse_ids_that_could_leave_the_store(tmp_path, map_id):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b", cloud=True))
    for kind in ("grid", "preview", "cloud"):
        assert store.artifact_path(map_id, 0, kind) is None
    assert store.get(map_id) is None
    assert store.delete(map_id) is False
    assert store.get("map-venue") is not None  # and nothing was removed by trying


@pytest.mark.parametrize("floor", [-1, 1, 99, "0", "../0", 0.0, None, True])
def test_artifact_path_refuses_a_floor_that_is_not_a_listed_index(tmp_path, floor):
    store = MapStore(tmp_path / "maps")
    store.save_bundle(make_bundle(tmp_path / "b"))
    assert store.artifact_path("map-venue", floor, "grid") is None


@pytest.mark.parametrize("map_id", ["..", "../escape", "a/b", "C:\\x", "/abs"])
def test_a_bundle_cannot_name_itself_outside_the_store(tmp_path, map_id):
    root = tmp_path / "maps"
    store = MapStore(root)
    with pytest.raises(BundleError):
        store.save_bundle(make_bundle(tmp_path / "b", map_id=map_id))
    assert os.listdir(root) == []
    assert not (tmp_path / "escape").exists()


def test_delete_removes_the_directory(tmp_path):
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "b", cloud=True))
    assert store.delete("map-venue") is True
    assert store.get("map-venue") is None
    assert store.list() == []
    assert os.listdir(root) == []
    assert store.delete("map-venue") is False


def test_list_skips_a_directory_that_is_not_a_readable_map(tmp_path):
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "b"))
    (root / "junk").mkdir()
    (root / "broken").mkdir()
    (root / "broken" / "map.json").write_text("{not json")
    (root / "stray.txt").write_text("x")
    assert [m.map_id for m in store.list()] == ["map-venue"]
    assert store.get("broken") is None


def test_a_map_whose_file_names_another_id_is_not_listed_or_served(tmp_path):
    root = tmp_path / "maps"
    store = MapStore(root)
    store.save_bundle(make_bundle(tmp_path / "b"))
    (root / "map-venue").rename(root / "map-other")
    assert store.list() == []
    assert store.get("map-other") is None


def _break(bundle, edit):
    path = bundle / "site_map.json"
    doc = json.loads(path.read_text())
    edit(doc)
    path.write_text(json.dumps(doc))


@pytest.mark.parametrize("edit", [
    lambda d: d["floors"].clear() or d["floors"].append({"index": 1}),            # gap in indexes
    lambda d: d["floors"][0].pop("resolution_m"),                                 # not a MapFloor
    lambda d: d["site_map"].pop("name"),                                          # not a SiteMap
    lambda d: d.pop("floors"),
    lambda d: d.pop("site_map"),
])
def test_a_bundle_that_does_not_validate_is_refused(tmp_path, edit):
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b")
    _break(bundle, edit)
    with pytest.raises(BundleError):
        store.save_bundle(bundle)
    assert store.list() == []


@pytest.mark.parametrize("resolution", [0, -0.05, float("nan"), float("inf")])
def test_a_floor_whose_resolution_is_not_a_positive_number_is_refused(tmp_path, resolution):
    # MapFloor carries no value constraints; the bundle is where these get caught.
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b")
    _break(bundle, lambda d: d["floors"][0].update(resolution_m=resolution))
    with pytest.raises(BundleError, match="resolution"):
        store.save_bundle(bundle)
    assert store.list() == []


def test_a_floor_with_a_negative_cell_count_is_refused(tmp_path):
    # unknown 5 + free 1 + occupied 2 still fills a 4 x 2 grid: only the sign is wrong.
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b")
    _break(bundle, lambda d: d["floors"][0].update(free_cells=-1, unknown_cells=7))
    with pytest.raises(BundleError, match="negative"):
        store.save_bundle(bundle)
    assert store.list() == []


@pytest.mark.parametrize("edit", [
    lambda f: f.update(unknown_cells=4),        # counts add up to 9 on an 8-cell grid
    lambda f: f.update(size_cells=[4, 3]),      # grid is 12 cells, counts add up to 8
    lambda f: f.update(size_cells=[0, 0], free_cells=0, occupied_cells=0, unknown_cells=0),
])
def test_a_floor_whose_counts_do_not_fill_its_grid_is_refused(tmp_path, edit):
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b")
    _break(bundle, lambda d: edit(d["floors"][0]))
    with pytest.raises(BundleError):
        store.save_bundle(bundle)
    assert store.list() == []


def test_a_floor_whose_numbers_all_hold_is_still_stored(tmp_path):
    store = MapStore(tmp_path / "maps")
    assert store.save_bundle(make_bundle(tmp_path / "b", floors=2)).floors[1].resolution_m == 0.05


def test_a_bundle_with_no_site_map_json_is_refused(tmp_path):
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b")
    (bundle / "site_map.json").unlink()
    with pytest.raises(BundleError):
        store.save_bundle(bundle)


def test_a_bundle_with_unparseable_site_map_json_is_refused(tmp_path):
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b")
    (bundle / "site_map.json").write_text("{nope")
    with pytest.raises(BundleError):
        store.save_bundle(bundle)


@pytest.mark.parametrize("missing", ["grid.pgm", "grid.png"])
def test_a_floor_missing_its_grid_or_preview_is_refused(tmp_path, missing):
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b", floors=2)
    (bundle / "floor1" / missing).unlink()
    with pytest.raises(BundleError):
        store.save_bundle(bundle)
    assert store.list() == []


def test_files_the_layout_does_not_name_are_not_kept(tmp_path):
    store = MapStore(tmp_path / "maps")
    bundle = make_bundle(tmp_path / "b")
    (bundle / "notes.txt").write_text("x")
    (bundle / "floor0" / "extra.bin").write_bytes(b"x")
    (bundle / "floor7").mkdir()
    (bundle / "floor7" / "grid.pgm").write_bytes(PGM)
    store.save_bundle(bundle)
    kept = sorted(str(p.relative_to(tmp_path / "maps" / "map-venue")).replace("\\", "/")
                  for p in (tmp_path / "maps" / "map-venue").rglob("*") if p.is_file())
    assert kept == ["floor0/grid.pgm", "floor0/grid.png", "map.json"]


def test_the_cloud_limit_is_the_packers_expression():
    # tools/map_prep/pack_map.py: MAX_CLOUD_BYTES = 8 * 1024 * 1024. A different
    # figure here (8_000_000, say) would let a cloud pass packing and fail serving.
    assert MAX_CLOUD_BYTES == 8 * 1024 * 1024
