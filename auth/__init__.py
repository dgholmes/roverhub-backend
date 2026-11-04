"""Sign-in and role checks (WBS M5.1, OD-10).

Username and password, checked by this service. A successful sign-in issues a
signed token that the browser sends on every REST call and uses as its broker
password, so the realtime path is covered by the same sign-in as REST (OD-10:
authenticating REST alone looks secure and is not, because the browser reads
telemetry from the broker directly).

Off unless AUTH_ENABLED is true, so pulling this code changes nothing until the
broker and both edge devices are switched over together. See
docs/superpowers/specs/2026-11-04-sign-in-design.md in the root repo.
"""
