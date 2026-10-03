"""Pure unit checks runnable without Django, database, or native process."""
import copy
import unittest
from plane.utils.project_blueprint import plan, definition, digest, BlueprintInvalid

ACTOR = "10000000-0000-4000-8000-000000000001"
OWNER = "10000000-0000-4000-8000-000000000002"
WORKSPACE = "10000000-0000-4000-8000-000000000003"
VERSION = "10000000-0000-4000-8000-000000000004"


def material():
    return {"workPackages": [{"key": "a", "name": "Design", "ownerRoleKey": "owner", "durationWorkingDays": 2}, {"key": "b", "name": "Build", "ownerRoleKey": "owner", "durationWorkingDays": 1}], "dependencies": [{"predecessorKey": "a", "successorKey": "b", "type": "FINISH_START", "lagWorkingDays": 1}], "requiredRoleKeys": ["owner"], "calendar": {"version": "calendar-1", "timezone": "UTC", "firstDate": "2026-10-01", "lastDate": "2026-12-31", "workingWeekdays": [1, 2, 3, 4, 5], "holidays": ["2026-10-05"]}, "templateVersionRefs": []}


def projects():
    return [{"name": "One", "identifier": "ONE", "startDate": "2026-10-02", "nonworkingStartPolicy": "REJECT", "roleBindings": {"owner": OWNER}}, {"name": "Two", "identifier": "TWO", "startDate": "2026-10-07", "nonworkingStartPolicy": "REJECT", "roleBindings": {"owner": ACTOR}}]


class ContractTests(unittest.TestCase):
    def preview(self, m=None, p=None):
        m = m or material()
        return plan(VERSION, digest(m), m, p or projects(), ACTOR, WORKSPACE)

    def test_two_independent_private_projects_exact_grants(self):
        r = self.preview()
        self.assertEqual([p["network"] for p in r["projects"]], [0, 0])
        self.assertEqual(r["projects"][0]["grants"], [{"actorId": ACTOR, "role": 20}, {"actorId": OWNER, "role": 15}])
        self.assertEqual(r["projects"][1]["grants"], [{"actorId": ACTOR, "role": 20}])
        self.assertEqual(r["projects"][0]["workPackages"][1]["startDate"], "2026-10-08")
        self.assertEqual(r["projects"][1]["workPackages"][0]["startDate"], "2026-10-07")

    def test_input_unchanged(self):
        m, p = material(), projects()
        before = copy.deepcopy((m, p))
        self.preview(m, p)
        self.assertEqual((m, p), before)

    def test_hash_binds_actor_workspace_versions_dates_roles_names(self):
        m, p = material(), projects()
        baseline = self.preview(m, p)["planSha256"]
        for key, value in [("name", "Changed"), ("identifier", "CHANGED"), ("startDate", "2026-10-06")]:
            changed = copy.deepcopy(p)
            changed[0][key] = value
            self.assertNotEqual(baseline, self.preview(m, changed)["planSha256"])
        self.assertNotEqual(baseline, plan(VERSION, digest(m), m, p, OWNER, WORKSPACE)["planSha256"])
        self.assertNotEqual(baseline, plan(VERSION, digest(m), m, p, ACTOR, OWNER)["planSha256"])
        self.assertNotEqual(baseline, plan(OWNER, digest(m), m, p, ACTOR, WORKSPACE)["planSha256"])

    def test_rejects_unsupported_external_authority(self):
        m = material()
        m["templateVersionRefs"] = [{"contract": "C03", "versionId": VERSION}]
        with self.assertRaisesRegex(BlueprintInvalid, "UNSUPPORTED_REFERENCE_AUTHORITY"):
            self.preview(m)

    def test_rejects_public_or_extra_fields(self):
        p = projects()
        p[0]["network"] = 2
        with self.assertRaises(BlueprintInvalid):
            self.preview(p=p)

    def test_rejects_cycles_duplicate_ids_and_missing_roles(self):
        m = material()
        m["dependencies"].append({"predecessorKey": "b", "successorKey": "a", "type": "FINISH_START", "lagWorkingDays": 0})
        with self.assertRaisesRegex(BlueprintInvalid, "DEPENDENCY_CYCLE"):
            self.preview(m)
        p = projects()
        p[1]["identifier"] = "ONE"
        with self.assertRaises(BlueprintInvalid):
            self.preview(p=p)
        p = projects()
        p[0]["roleBindings"] = {}
        with self.assertRaisesRegex(BlueprintInvalid, "MISSING_ROLE"):
            self.preview(p=p)

    def test_shift_and_calendar_bounds(self):
        p = projects()
        p[0]["startDate"] = "2026-10-03"
        with self.assertRaisesRegex(BlueprintInvalid, "NONWORKING_START"):
            self.preview(p=p)
        p[0]["nonworkingStartPolicy"] = "NEXT_WORKING_DAY"
        r = self.preview(p=p)
        self.assertTrue(r["projects"][0]["anchorShifted"])
        self.assertEqual(r["projects"][0]["workPackages"][0]["startDate"], "2026-10-06")
        p[0]["startDate"] = "2027-01-01"
        with self.assertRaises(BlueprintInvalid):
            self.preview(p=p)

    def test_boolean_duration_not_integer(self):
        m = material()
        m["workPackages"][0]["durationWorkingDays"] = True
        with self.assertRaises(BlueprintInvalid):
            definition(m)


if __name__ == "__main__":
    unittest.main()
