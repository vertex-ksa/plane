"""Bounded immutable blueprint contract. Pure validation is never permission authority."""
import hashlib
import json
import re
from datetime import date, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo


class BlueprintInvalid(ValueError):
    pass


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise BlueprintInvalid("INVALID_INPUT")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise BlueprintInvalid("INVALID_MEMBER")
    return value


def symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise BlueprintInvalid("INVALID_KEY")
    return value


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise BlueprintInvalid("INVALID_NUMBER")
    return value


def day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (ValueError, TypeError):
        raise BlueprintInvalid("INVALID_CALENDAR")


def definition(value):
    exact(value, ["workPackages", "dependencies", "requiredRoleKeys", "calendar", "templateVersionRefs"])
    if value["templateVersionRefs"] != []:
        # No C03/C04 approved-version resolver exists in this installed edition.
        raise BlueprintInvalid("UNSUPPORTED_REFERENCE_AUTHORITY")
    packages = value["workPackages"]
    if not isinstance(packages, list) or not 1 <= len(packages) <= 100:
        raise BlueprintInvalid("INVALID_PACKAGES")
    roles = value["requiredRoleKeys"]
    if not isinstance(roles, list) or not 1 <= len(roles) <= 50 or any(symbol(r) != r for r in roles) or len(set(roles)) != len(roles):
        raise BlueprintInvalid("INVALID_ROLES")
    keys = set()
    for p in packages:
        exact(p, ["key", "name", "ownerRoleKey", "durationWorkingDays"])
        key = symbol(p["key"])
        if key in keys or p["ownerRoleKey"] not in roles or not isinstance(p["name"], str) or not p["name"].strip() or len(p["name"]) > 255:
            raise BlueprintInvalid("INVALID_PACKAGES")
        keys.add(key)
        integer(p["durationWorkingDays"], 1, 3660)
    edges = value["dependencies"]
    if not isinstance(edges, list) or len(edges) > 1000:
        raise BlueprintInvalid("INVALID_DEPENDENCY")
    seen = set()
    for e in edges:
        exact(e, ["predecessorKey", "successorKey", "type", "lagWorkingDays"])
        pair = (e["predecessorKey"], e["successorKey"])
        if e["type"] != "FINISH_START" or pair[0] not in keys or pair[1] not in keys or pair[0] == pair[1] or pair in seen:
            raise BlueprintInvalid("INVALID_DEPENDENCY")
        seen.add(pair)
        integer(e["lagWorkingDays"], 0, 3660)
    ordered = []
    while len(ordered) != len(keys):
        ready = sorted(k for k in keys - set(ordered) if all(e["predecessorKey"] in ordered for e in edges if e["successorKey"] == k))
        if not ready:
            raise BlueprintInvalid("DEPENDENCY_CYCLE")
        ordered.extend(ready)
    c = exact(value["calendar"], ["version", "timezone", "firstDate", "lastDate", "workingWeekdays", "holidays"])
    symbol(c["version"])
    try:
        ZoneInfo(c["timezone"])
    except (ValueError, TypeError, KeyError):
        raise BlueprintInvalid("INVALID_CALENDAR")
    first, last = day(c["firstDate"]), day(c["lastDate"])
    if not 0 <= (last - first).days < 3660:
        raise BlueprintInvalid("INVALID_CALENDAR")
    week = c["workingWeekdays"]
    if not isinstance(week, list) or not 1 <= len(week) <= 7 or any(type(d) is not int or d not in range(1, 8) for d in week) or len(set(week)) != len(week):
        raise BlueprintInvalid("INVALID_CALENDAR")
    holidays = c["holidays"]
    if not isinstance(holidays, list) or len(holidays) > 3660 or len(set(holidays)) != len(holidays) or any(not first <= day(d) <= last for d in holidays):
        raise BlueprintInvalid("INVALID_CALENDAR")
    return ordered


def plan(version_id, version_sha, material, projects, actor_id, workspace_id):
    order = definition(material)
    if not isinstance(projects, list) or len(projects) != 2:
        raise BlueprintInvalid("TWO_INDEPENDENT_PROJECTS_REQUIRED")
    c = material["calendar"]
    first, last = day(c["firstDate"]), day(c["lastDate"])
    holidays = set(c["holidays"])

    def next_working(d):
        while d <= last and (d.isoweekday() not in c["workingWeekdays"] or d.isoformat() in holidays):
            d += timedelta(days=1)
        if d > last:
            raise BlueprintInvalid("CALENDAR_OUT_OF_RANGE")
        return d

    def advance(d, n):
        for _ in range(n):
            d = next_working(d + timedelta(days=1))
        return d

    results = []
    identifiers = set()
    for p in projects:
        exact(p, ["name", "identifier", "startDate", "nonworkingStartPolicy", "roleBindings"])
        if not isinstance(p["name"], str) or not p["name"].strip() or len(p["name"]) > 255 or not isinstance(p["identifier"], str) or not re.fullmatch(r"[A-Z][A-Z0-9]{0,11}", p["identifier"]) or p["identifier"] in identifiers:
            raise BlueprintInvalid("INVALID_PROJECT")
        identifiers.add(p["identifier"])
        bindings = p["roleBindings"]
        if not isinstance(bindings, dict) or set(bindings) != set(material["requiredRoleKeys"]):
            raise BlueprintInvalid("MISSING_ROLE")
        for member in bindings.values():
            uuid(member)
        start = day(p["startDate"])
        if not first <= start <= last or p["nonworkingStartPolicy"] not in ["REJECT", "NEXT_WORKING_DAY"]:
            raise BlueprintInvalid("INVALID_START")
        anchor = next_working(start)
        if anchor != start and p["nonworkingStartPolicy"] == "REJECT":
            raise BlueprintInvalid("NONWORKING_START")
        dates = {}
        rows = []
        for key in order:
            package = next(x for x in material["workPackages"] if x["key"] == key)
            begin = max([anchor] + [advance(dates[e["predecessorKey"]][1], e["lagWorkingDays"] + 1) for e in material["dependencies"] if e["successorKey"] == key])
            finish = advance(begin, package["durationWorkingDays"] - 1)
            dates[key] = (begin, finish)
            rows.append({"key": key, "name": package["name"], "ownerId": bindings[package["ownerRoleKey"]], "startDate": begin.isoformat(), "finishDate": finish.isoformat()})
        grants = {member: 15 for member in bindings.values()}
        grants[actor_id] = 20
        results.append({"name": p["name"], "identifier": p["identifier"], "network": 0, "anchorShifted": start != anchor, "grants": [{"actorId": a, "role": r} for a, r in sorted(grants.items())], "workPackages": rows})
    value = {"contract": "project-blueprint/1", "versionId": version_id, "versionSha256": version_sha, "actorId": actor_id, "workspaceId": workspace_id, "calendar": c, "projects": results, "dependencies": material["dependencies"], "externalReferences": "NONE"}
    return {**value, "planSha256": digest(value)}
