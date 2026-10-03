"""PostgreSQL native acceptance tests. Never run against the retained fixture DB."""
import copy
from uuid import uuid4
from unittest.mock import patch
from django.test import TransactionTestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from plane.db.models import User, Workspace, WorkspaceMember, Project, ProjectMember, ProjectIdentifier, ProjectBlueprintVersion, ProjectBlueprintCommand, Issue, IssueRelation
from plane.api.views.project_blueprint import ProjectBlueprintEndpoint, ProjectBlueprintPreviewEndpoint, ProjectBlueprintCommandEndpoint
from plane.tests.test_project_blueprint_contract import material


class NativeBlueprintTests(TransactionTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.actor = User.objects.create(email="blueprint-admin@example.test", username="blueprint-admin", is_active=True)
        self.owner = User.objects.create(email="blueprint-owner@example.test", username="blueprint-owner", is_active=True)
        self.workspace = Workspace.objects.create(name="Blueprint isolated test", slug="blueprint-isolated-test", owner=self.actor)
        for user in [self.actor, self.owner]:
            WorkspaceMember.objects.create(workspace=self.workspace, member=user, role=20 if user == self.actor else 15)
        self.source = Project.objects.create(workspace=self.workspace, name="Authoring source", identifier="SOURCE", network=0)
        ProjectMember.objects.create(project=self.source, workspace=self.workspace, member=self.actor, role=20)
        publication = self.call(ProjectBlueprintEndpoint, "post", {"sourceProjectId": str(self.source.id), "title": "Published version", "definition": material()})
        self.assertEqual(publication.status_code, 201)
        self.input = {"versionId": publication.data["id"], "versionSha256": publication.data["sha256"], "projects": [{"name": "One", "identifier": "ONE", "startDate": "2026-10-02", "nonworkingStartPolicy": "REJECT", "roleBindings": {"owner": str(self.owner.id)}}, {"name": "Two", "identifier": "TWO", "startDate": "2026-10-07", "nonworkingStartPolicy": "REJECT", "roleBindings": {"owner": str(self.actor.id)}}]}
        preview = self.call(ProjectBlueprintPreviewEndpoint, "post", self.input)
        self.assertEqual(preview.status_code, 200)
        self.body = {"input": self.input, "planSha256": preview.data["planSha256"], "confirmPrivateGrants": True}
        self.command = uuid4()

    def call(self, view, method, data=None, actor=None, command=None):
        request = getattr(self.factory, method)("/", data=data, format="json")
        force_authenticate(request, user=actor or self.actor)
        kwargs = {"slug": self.workspace.slug}
        if command:
            kwargs["command_id"] = command
        return view.as_view()(request, **kwargs)

    def create(self, body=None):
        return self.call(ProjectBlueprintCommandEndpoint, "post", body or self.body, command=self.command)

    def assert_no_effects(self):
        self.assertEqual(Project.objects.filter(workspace=self.workspace).count(), 1)
        self.assertFalse(ProjectBlueprintCommand.objects.exists())
        self.assertFalse(Issue.objects.exists())

    def test_catalogue_owner_choices_are_current_scoped_and_sanitized(self):
        self.actor.display_name = "  \u202eAdmin\t  مدير "
        self.actor.save()
        # Native User.save supplies an email-derived default; exercise the
        # genuinely blank stored-name fallback without invoking that default.
        User.objects.filter(id=self.owner.id).update(display_name="", first_name="  Amina\n", last_name="  Hassan  ")
        for index, (role, membership_active, user_active) in enumerate([(5, True, True), (15, False, True), (20, True, False)]):
            user = User.objects.create(email=f"excluded-{index}@example.test", username=f"excluded-{index}", is_active=user_active)
            WorkspaceMember.objects.create(workspace=self.workspace, member=user, role=role, is_active=membership_active)
        outsider = User.objects.create(email="outsider@example.test", username="outsider", is_active=True)
        other = Workspace.objects.create(name="Other", slug="other-owner-choices", owner=outsider)
        WorkspaceMember.objects.create(workspace=other, member=outsider, role=20)
        result = self.call(ProjectBlueprintEndpoint, "get")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data["ownerChoices"], [{"actorId": str(self.actor.id), "label": "Admin مدير"}, {"actorId": str(self.owner.id), "label": "Amina Hassan"}])
        self.assertFalse(result.data["ownerChoicesTruncated"])
        self.assertEqual(result.data["ownerChoicesLimit"], 200)
        WorkspaceMember.objects.filter(workspace=self.workspace, member=self.owner).update(is_active=False)
        self.assertEqual(self.call(ProjectBlueprintEndpoint, "get").data["ownerChoices"], [{"actorId": str(self.actor.id), "label": "Admin مدير"}])
        self.assertEqual(self.call(ProjectBlueprintEndpoint, "get", actor=outsider).status_code, 403)

    def test_catalogue_owner_choices_bound_and_always_include_actor_first(self):
        users = [User(id=uuid4(), email=f"bounded-{i}@example.test", username=f"bounded-{i}", display_name="x" * 255, is_active=True) for i in range(199)]
        User.objects.bulk_create(users)
        WorkspaceMember.objects.bulk_create([WorkspaceMember(workspace=self.workspace, member=u, role=15) for u in users])
        result = self.call(ProjectBlueprintEndpoint, "get").data
        choices = result["ownerChoices"]
        self.assertEqual(len(choices), 200)
        self.assertEqual(choices[0]["actorId"], str(self.actor.id))
        self.assertEqual(len({c["actorId"] for c in choices}), 200)
        self.assertTrue(result["ownerChoicesTruncated"])
        self.assertTrue(all(0 < len(c["label"]) <= 160 for c in choices))
        self.assertEqual([c["actorId"] for c in choices[1:]], sorted(c["actorId"] for c in choices[1:]))

    def test_two_private_projects_readback_and_idempotent_retry(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["projects"]), 2)
        self.assertEqual({p["network"] for p in response.data["projects"]}, {0})
        ids = {p["id"] for p in response.data["projects"]}
        self.assertEqual(len(ids), 2)
        self.assertEqual(Issue.objects.count(), 4)
        self.assertEqual(IssueRelation.objects.count(), 2)
        replay = self.create()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual({p["id"] for p in replay.data["projects"]}, ids)
        self.assertEqual(ProjectBlueprintCommand.objects.count(), 1)
        recovered = self.call(ProjectBlueprintCommandEndpoint, "get", command=self.command)
        self.assertEqual(recovered.data["readback"], "VERIFIED_CURRENT")

    def test_insert_is_private_from_start(self):
        original = Project.save
        seen = []
        def inspect(instance, *args, **kwargs):
            if instance._state.adding:
                seen.append(instance.network)
            return original(instance, *args, **kwargs)
        with patch.object(Project, "save", inspect):
            self.assertEqual(self.create().status_code, 201)
        self.assertEqual(seen, [0, 0])

    def test_partial_native_failure_rolls_back_both_and_receipt(self):
        original = Project.save
        count = [0]
        def failure(instance, *args, **kwargs):
            count[0] += 1
            if count[0] == 2:
                raise RuntimeError("synthetic second project failure")
            return original(instance, *args, **kwargs)
        # Native BaseAPIView converts unexpected failures to a 500 response.
        with patch.object(Project, "save", failure):
            self.assertEqual(self.create().status_code, 500)
        self.assert_no_effects()

    def test_stale_plan_and_mutated_version_denied(self):
        changed = copy.deepcopy(self.body)
        changed["input"]["projects"][0]["name"] = "Changed after preview"
        self.assertEqual(self.create(changed).status_code, 400)
        self.assert_no_effects()
        ProjectBlueprintVersion.objects.filter(id=self.input["versionId"]).update(definition={})
        self.assertEqual(self.create().status_code, 400)
        self.assert_no_effects()

    def test_current_owner_membership_revocation_denied(self):
        WorkspaceMember.objects.filter(workspace=self.workspace, member=self.owner).update(is_active=False)
        self.assertEqual(self.create().status_code, 403)
        self.assert_no_effects()

    def test_source_archive_or_project_permission_revocation_denied(self):
        ProjectMember.objects.filter(project=self.source, member=self.actor).update(is_active=False)
        self.assertEqual(self.create().status_code, 403)
        self.assert_no_effects()

    def test_conflicting_command_never_duplicates(self):
        self.assertEqual(self.create().status_code, 201)
        changed = copy.deepcopy(self.body)
        changed["input"]["projects"][0]["name"] = "Changed"
        self.assertEqual(self.create(changed).status_code, 400)
        self.assertEqual(Project.objects.count(), 3)

    def test_recovery_wrong_actor_denied_and_missing_is_safe(self):
        self.assertEqual(self.create().status_code, 201)
        result = self.call(ProjectBlueprintCommandEndpoint, "get", actor=self.owner, command=self.command)
        self.assertEqual(result.status_code, 403)
        missing = self.call(ProjectBlueprintCommandEndpoint, "get", command=uuid4())
        self.assertEqual(missing.data["status"], "NOT_COMMITTED")

    def test_changed_private_grants_not_claimed_verified(self):
        response = self.create()
        Project.objects.filter(id=response.data["projects"][0]["id"]).update(network=2)
        result = self.call(ProjectBlueprintCommandEndpoint, "get", command=self.command)
        self.assertEqual(result.status_code, 403)

    def test_identifier_collision_rolls_back(self):
        ProjectIdentifier.objects.create(project=self.source, workspace=self.workspace, name="ONE")
        self.assertEqual(self.create().status_code, 400)
        self.assert_no_effects()
