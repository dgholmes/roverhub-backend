"""Send to robot (schema 0.0.26): SYNC_MAP through POST /robots/{id}/commands
-- who may send it, its params, the bridge-version gate, the map and floor it
names, the floor's version -- and the map reads the robot then makes with its
device key (GET /maps/{id}, .../floors/{i}/planning, .../floors/{i}/grid)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("numpy")

import api.app as backend  # noqa: E402
from auth.policy import ROUTES, command_capabilities  # noqa: E402
from control.router import SYNC_MAP_SCHEMA_VERSION  # noqa: E402
from shared.schemas.commands import WIRED_COMMAND_TYPES, command_ttl_s  # noqa: E402
from tests import test_site_setup as site_setup  # noqa: E402
from tests.test_site_setup import DEVICE_A, SITE, _bearer, _command  # noqa: E402

# The site-setup tests' world: rover-a at SITE on this schema, a map at each
# site, commands captured rather than sent.
world = site_setup.world
auth_on = site_setup.auth_on


def _floor_hash(map_id, index=0):
    floor = next(f for f in backend.maps_router.store.get(map_id).floors if f.index == index)
    return floor.content_hash


def _params(world, **over):
    params = {"map_id": world["here"], "floor_index": 0, "map_hash": _floor_hash(world["here"])}
    params.update(over)
    return params


def test_sync_map_is_wired_and_is_the_engineers():
    assert "SYNC_MAP" in WIRED_COMMAND_TYPES
    assert command_capabilities("SYNC_MAP") == ("configure",)
    assert command_ttl_s("SYNC_MAP") == 10.0
    assert SYNC_MAP_SCHEMA_VERSION == "0.0.26"


def test_the_floor_has_a_hash_to_send(world):
    assert _floor_hash(world["here"]) is not None


def test_sign_in_off_sends_it_with_its_params(world):
    params = _params(world)
    response = _command(TestClient(backend.app), "SYNC_MAP", params)
    assert response.status_code == 200, response.text
    sent, = world["sent"]
    assert sent.type == "SYNC_MAP" and sent.params == params and sent.ttl_s == 10.0


@pytest.mark.parametrize("user, allowed", [
    ("eve", True),                          # engineer: configure
    ("sue", False),                         # supervisor: draws zones, does not configure robots
    ("ann", False), ("tom", False), ("vic", False),
    ("far", False),                         # an engineer, at another site
])
def test_only_an_engineer_at_the_site_may_send_a_map(auth_on, world, user, allowed):
    client = TestClient(backend.app)
    response = _command(client, "SYNC_MAP", _params(world), headers=_bearer(client, user))
    assert response.status_code == (200 if allowed else 403), (user, response.text)
    if not allowed and response.status_code == 403 and user in ("sue", "ann", "tom"):
        assert response.json()["detail"] == "your role does not allow SYNC_MAP"


def test_a_device_key_cannot_send_it(auth_on, world):
    assert _command(TestClient(backend.app), "SYNC_MAP", _params(world), headers=DEVICE_A).status_code == 403


@pytest.mark.parametrize("params, field", [
    ({"floor_index": 0, "map_hash": "ab" * 32}, "map_id"),
    ({"map_id": "m", "floor_index": -1, "map_hash": "ab" * 32}, "floor_index"),
    ({"map_id": "m", "map_hash": "not-a-hash"}, "map_hash"),
    ({"map_id": "m"}, "map_hash"),
])
def test_unusable_params_are_a_422_in_plain_words(world, params, field):
    response = _command(TestClient(backend.app), "SYNC_MAP", params)
    assert response.status_code == 422
    assert response.json()["detail"] == "SYNC_MAP params are not a map to send (%s)" % field
    assert world["sent"] == []


def test_a_bridge_older_than_0_0_26_is_refused_with_the_reason(world):
    backend.registry.upsert_from_registration("rover-025", SITE, "quad", schema_version="0.0.25")
    response = _command(TestClient(backend.app), "SYNC_MAP", _params(world), robot="rover-025")
    assert response.status_code == 409
    assert response.json()["detail"] == ("update the edge device: it runs schema 0.0.25, sending it a map "
                                         "needs 0.0.26")
    assert world["sent"] == []


def test_a_robot_set_up_for_another_map_is_refused(world):
    backend.registry.upsert_from_registration("rover-nav", SITE, "quad", schema_version="0.0.26",
                                              nav_map_id="map-elsewhere")
    response = _command(TestClient(backend.app), "SYNC_MAP", _params(world), robot="rover-nav")
    assert response.status_code == 409
    assert response.json()["detail"] == "rover-nav is set up for map map-elsewhere, not %s" % world["here"]
    backend.registry.upsert_from_registration("rover-nav", SITE, "quad", schema_version="0.0.26",
                                              nav_map_id=world["here"])
    assert _command(TestClient(backend.app), "SYNC_MAP", _params(world), robot="rover-nav").status_code == 200


def test_the_map_and_floor_must_be_at_the_robots_site(world):
    client = TestClient(backend.app)
    for map_id in ("map-nowhere", world["there"]):
        response = _command(client, "SYNC_MAP", _params(world, map_id=map_id))
        assert response.status_code == 404
        assert response.json()["detail"] == "map %s not found at this robot's site" % map_id
    response = _command(client, "SYNC_MAP", _params(world, floor_index=3))
    assert response.status_code == 404
    assert response.json()["detail"] == "map %s has no floor 3" % world["here"]
    assert world["sent"] == []


def test_another_version_of_the_floor_is_refused(world):
    current = _floor_hash(world["here"])
    response = _command(TestClient(backend.app), "SYNC_MAP", _params(world, map_hash="ab" * 32))
    assert response.status_code == 409
    assert response.json()["detail"] == ("map %s floor 0 is now version %s, not abababababab -- reload the map "
                                         "and send it again" % (world["here"], current[:12]))
    assert world["sent"] == []


def test_the_robot_may_read_what_it_downloads_at_its_own_site(auth_on, world):
    for path in ("/maps/{map_id}", "/maps/{map_id}/floors/{index}/planning",
                 "/maps/{map_id}/floors/{index}/{kind}"):
        assert ROUTES[("GET", path)].device_own_site, path
    client = TestClient(backend.app)
    here, there = world["here"], world["there"]
    assert client.get("/maps/%s" % here, headers=DEVICE_A).status_code == 200
    assert client.get("/maps/%s/floors/0/grid" % here, headers=DEVICE_A).status_code == 200
    planning = client.get("/maps/%s/floors/0/planning" % here, headers=DEVICE_A)
    assert planning.status_code == 200 and planning.content.startswith(b"P5")
    # never another site's map
    assert client.get("/maps/%s" % there, headers=DEVICE_A).status_code == 403
    assert client.get("/maps/%s/floors/0/grid" % there, headers=DEVICE_A).status_code == 403
