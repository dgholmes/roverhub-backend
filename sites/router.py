"""Sites: where robots work, as records an admin creates and edits (2026-11-26).

The store is sites/store.py; the wire types are shared/schemas/sites.py; the
design is docs/superpowers/specs/2026-11-26-map-workspace-and-sites-design.md
§2. Access rules are in auth/policy.py: reading is for anyone signed in, and
shows only the sites they may see; creating, editing and deleting are for org
owners and admins. With sign-in off (AUTH_ENABLED unset), everyone may do
everything, as everywhere else in this backend.

Endpoints:
- GET    /sites            -> {"items": [Site, ...]}, by name. Org owners/admins
                              see every site; anyone else the sites where they
                              hold a role (a "*" role is every site).
- GET    /sites/{site_id}  -> Site; 404 if there is none or the caller may not
                              see it (the same answer, so IDs cannot be probed).
- POST   /sites            -> 201 Site. Body: SiteCreate. With no site_id, one
                              is made from the name ("PICT Square" ->
                              "pict-square"). 409 if the ID is taken (case is
                              ignored); 422 for a bad name, timezone, address,
                              location or ID.
- PATCH  /sites/{site_id}  -> Site. Body: SiteUpdate; a field left out is kept,
                              an explicit null clears address or location.
                              site_id cannot change (422 if the body names
                              another). 404 if there is no such site.
- DELETE /sites/{site_id}  -> 204. 409 while the site is in use -- a robot is
                              registered there, a map belongs to it, or someone
                              holds a role there -- with a detail that names
                              them. 404 if there is no such site.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError

from api.common import read_bounded_body
from auth.deps import principal_of
from history.audit import AuditFn
from shared.schemas.sites import SiteCreate, SiteUpdate
from sites.store import SiteExists, SiteIdNotStorable, SiteStore, new_site_id

logger = logging.getLogger("roverhub.sites")

# A site is a few short strings. Bounded before pydantic parses anything, like
# every other body this backend stores (api/common.py).
MAX_SITE_BODY_BYTES = 16 * 1024

# site_id -> why it cannot be deleted, one short clause per reason (empty when
# it can be). api/app.py supplies it: robots, maps, user roles.
InUseFn = Callable[[str], List[str]]


def may_see_site(request: Request, site_id: str) -> bool:
    """Whether the caller may see the site `site_id`: everyone with sign-in off
    (or in a router mounted without the guard, as in unit tests); org owners
    and admins every site; anyone else a site where they hold a role. The
    guard lets only signed-in people reach these routes (Rule.signed_in), so a
    device never gets here."""
    principal = principal_of(request)
    if principal is None or principal.kind == "dev":
        return True
    if principal.kind != "user":
        return False
    return principal.is_org_admin or principal.visible_site(site_id)


def _validation_422(exc: ValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail=json.loads(exc.json(include_url=False)))


async def _json_body(request: Request) -> dict:
    raw = await read_bounded_body(request, MAX_SITE_BODY_BYTES, "site")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=422, detail="the body is not JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="the body must be a JSON object")
    return body


def build_sites_router(store: SiteStore, in_use: Optional[InUseFn] = None,
                       audit: Optional[AuditFn] = None) -> APIRouter:
    """`in_use` names what keeps a site from being deleted; with None, nothing
    does. `audit` (history/audit.py AuditRecorder.record) is told every create,
    edit and delete."""
    router = APIRouter()
    router.store = store  # type: ignore[attr-defined]  # as maps_router.store: app.py reads it

    def _audit(action: str, site_id: str, detail: dict) -> None:
        if audit is not None:
            audit(action, site_id, "ok", detail, site_id=site_id)

    def _get_visible(request: Request, site_id: str):
        site = store.get(site_id)
        if site is None or not may_see_site(request, site_id):
            raise HTTPException(status_code=404, detail=f"site {site_id} not found")
        return site

    @router.get("/sites")
    def list_sites(request: Request):
        return {"items": [site.model_dump(mode="json") for site in store.list()
                          if may_see_site(request, site.site_id)]}

    @router.get("/sites/{site_id}")
    def get_site(site_id: str, request: Request):
        return _get_visible(request, site_id).model_dump(mode="json")

    @router.post("/sites", status_code=201)
    async def create_site(request: Request):
        try:
            body = SiteCreate.model_validate(await _json_body(request))
        except ValidationError as exc:
            raise _validation_422(exc) from None
        site_id = new_site_id(body.name, body.site_id)
        try:
            site = store.create(body, site_id)
        except SiteExists as taken:
            same = taken.existing == site_id
            raise HTTPException(
                status_code=409,
                detail=(f"site {site_id} already exists" if same
                        else f"site {site_id} would clash with site {taken.existing} (IDs ignore case)")
                + ("" if body.site_id else "; give it another name or choose its ID"),
            ) from None
        except SiteIdNotStorable:
            raise HTTPException(status_code=422, detail=f"{site_id!r} cannot be used as a site ID") from None
        _audit("site.create", site.site_id, {"name": site.name, "timezone": site.timezone})
        return site.model_dump(mode="json")

    @router.patch("/sites/{site_id}")
    async def update_site(site_id: str, request: Request):
        raw = await _json_body(request)
        if "site_id" in raw:
            if raw["site_id"] != site_id:
                raise HTTPException(status_code=422, detail="site_id cannot change")
            raw = {k: v for k, v in raw.items() if k != "site_id"}
        try:
            update = SiteUpdate.model_validate(raw)
        except ValidationError as exc:
            raise _validation_422(exc) from None
        _get_visible(request, site_id)
        before = store.get(site_id)
        site = store.update(site_id, update)
        if site is None or before is None:
            raise HTTPException(status_code=404, detail=f"site {site_id} not found")
        changes = update.changes()
        if changes:
            _audit("site.update", site_id, {
                "changed": sorted(changes),
                **({"previous_name": before.name} if "name" in changes and before.name != site.name else {}),
            })
        return site.model_dump(mode="json")

    @router.delete("/sites/{site_id}", status_code=204)
    def delete_site(site_id: str, request: Request):
        site = _get_visible(request, site_id)
        reasons = in_use(site_id) if in_use is not None else []
        if reasons:
            raise HTTPException(status_code=409,
                                detail=f"site {site_id} is in use: {'; '.join(reasons)}")
        if not store.delete(site_id):
            raise HTTPException(status_code=404, detail=f"site {site_id} not found")
        _audit("site.delete", site_id, {"name": site.name})
        return Response(status_code=204)

    return router


__all__ = ["MAX_SITE_BODY_BYTES", "build_sites_router", "may_see_site"]
