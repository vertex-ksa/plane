"""Real loopback HTTP/API-key and concurrent-command proof on disposable PostgreSQL.

Django's threaded LiveServer serves the real URL configuration and middleware.
No force_authenticate, mock user, external service, or production credentials.
"""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from django.test import LiveServerTestCase
from django.utils import timezone

from plane.db.models import APIToken, User, Workspace, WorkspaceMember, Project, ProjectMember, ProjectBlueprintCommand, Issue
from plane.api.views import project_blueprint
from plane.tests.test_project_blueprint_contract import material


class BlueprintHttpTests(LiveServerTestCase):
    host = "127.0.0.1"

    def setUp(self):
        self.actor = User.objects.create(email="blueprint-http-admin@example.test", username="blueprint-http-admin", is_active=True)
        self.owner = User.objects.create(email="blueprint-http-owner@example.test", username="blueprint-http-owner", is_active=True)
        self.workspace = Workspace.objects.create(name="Blueprint HTTP isolated", slug="blueprint-http-isolated", owner=self.actor)
        for user in [self.actor, self.owner]:
            WorkspaceMember.objects.create(workspace=self.workspace, member=user, role=20 if user == self.actor else 15)
        self.source = Project.objects.create(workspace=self.workspace, name="HTTP authoring source", identifier="HTTPSOURCE", network=0)
        ProjectMember.objects.create(project=self.source, workspace=self.workspace, member=self.actor, role=20)
        # Native model generates the actual token; value never appears in assertions/logs.
        self.token = APIToken.objects.create(user=self.actor, workspace=self.workspace)
        self.root = "/api/v1/workspaces/" + self.workspace.slug + "/project-blueprints/"
        status, publication = self.http("POST", self.root, {"sourceProjectId": str(self.source.id), "title": "HTTP published version", "definition": material()})
        self.assertEqual(status, 201, "Native authenticated publication failed")
        self.input = {"versionId": publication["id"], "versionSha256": publication["sha256"], "projects": [{"name": "HTTP one", "identifier": "HTTPONE", "startDate": "2026-10-02", "nonworkingStartPolicy": "REJECT", "roleBindings": {"owner": str(self.owner.id)}}, {"name": "HTTP two", "identifier": "HTTPTWO", "startDate": "2026-10-07", "nonworkingStartPolicy": "REJECT", "roleBindings": {"owner": str(self.actor.id)}}]}
        status, self.preview = self.http("POST", self.root + "preview/", self.input)
        self.assertEqual(status, 200, "Native authenticated preview failed")
        self.body = {"input": self.input, "planSha256": self.preview["planSha256"], "confirmPrivateGrants": True}
        self.command = uuid4()
        self.path = self.root + "commands/" + str(self.command) + "/"

    def http(self, method, path, body=None, key=True):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key is True:
            headers["X-Api-Key"] = self.token.token
        elif isinstance(key, str):
            headers["X-Api-Key"] = key
        request = Request(self.live_server_url + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read(2_000_001)
            self.assertLessEqual(len(payload), 2_000_000)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            return response.status, json.loads(payload.decode("utf-8"))

    def no_creation(self):
        self.assertEqual(Project.objects.filter(workspace=self.workspace).count(), 1)
        self.assertFalse(ProjectBlueprintCommand.objects.exists())
        self.assertFalse(Issue.objects.exists())

    def test_http_api_key_catalogue_private_creation_and_readback(self):
        status, identity = self.http("GET", "/api/v1/users/me/")
        self.assertEqual(status, 200)
        self.assertEqual(identity["id"], str(self.actor.id))
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used)
        status, catalogue = self.http("GET", self.root)
        self.assertEqual(status, 200)
        self.assertEqual(catalogue["actorId"], str(self.actor.id))
        self.assertEqual(catalogue["workspaceId"], str(self.workspace.id))
        self.assertEqual([v["id"] for v in catalogue["versions"]], [self.input["versionId"]])
        status, receipt = self.http("POST", self.path, self.body)
        self.assertEqual(status, 201)
        self.assertEqual(receipt["readback"], "VERIFIED_CURRENT")
        self.assertEqual({p["network"] for p in receipt["projects"]}, {0})
        self.assertEqual(len({p["id"] for p in receipt["projects"]}), 2)
        expected = [{"actorId": str(self.actor.id), "role": 20}, {"actorId": str(self.owner.id), "role": 15}]
        self.assertEqual(sorted(receipt["projects"][0]["grants"], key=lambda g: g["actorId"]), sorted(expected, key=lambda g: g["actorId"]))
        status, recovered = self.http("GET", self.path)
        self.assertEqual(status, 200)
        self.assertEqual(recovered["projects"], receipt["projects"])
        self.assertEqual(recovered["planSha256"], self.preview["planSha256"])

    def test_http_rechecks_current_permissions_after_preview(self):
        WorkspaceMember.objects.filter(workspace=self.workspace, member=self.actor).update(is_active=False)
        self.assertEqual(self.http("GET", self.root)[0], 403)
        self.assertEqual(self.http("POST", self.path, self.body)[0], 403)
        self.no_creation()

    def test_http_missing_invalid_expired_and_disabled_user_tokens_fail_closed(self):
        self.assertIn(self.http("POST", self.path, self.body, key=False)[0], [401, 403])
        self.assertIn(self.http("POST", self.path, self.body, key="synthetic-invalid-key")[0], [401, 403])
        APIToken.objects.filter(id=self.token.id).update(expired_at=timezone.now() - timedelta(seconds=1))
        self.assertIn(self.http("POST", self.path, self.body)[0], [401, 403])
        APIToken.objects.filter(id=self.token.id).update(expired_at=None)
        User.objects.filter(id=self.actor.id).update(is_active=False)
        self.assertIn(self.http("POST", self.path, self.body)[0], [401, 403])
        self.no_creation()

    def test_concurrent_identical_http_command_commits_only_once(self):
        # Timing gate only: both authenticated requests reach native authority
        # before either takes the real workspace lock. No permission/CRUD mocks.
        entered = Barrier(2, timeout=10)
        original = project_blueprint.workspace_actor
        def synchronized_authority(slug, actor):
            entered.wait()
            return original(slug, actor)
        with patch.object(project_blueprint, "workspace_actor", synchronized_authority):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.http, "POST", self.path, copy.deepcopy(self.body)) for _ in range(2)]
                responses = [future.result(timeout=25) for future in futures]
        self.assertEqual(sorted(status for status, _ in responses), [200, 201])
        first, second = [body for _, body in responses]
        self.assertEqual(first["projects"], second["projects"])
        self.assertEqual(first["commandId"], str(self.command))
        self.assertEqual(Project.objects.filter(workspace=self.workspace).count(), 3)
        self.assertEqual(ProjectBlueprintCommand.objects.filter(workspace=self.workspace, command_id=self.command).count(), 1)
        self.assertEqual(Issue.objects.filter(workspace=self.workspace).count(), 4)
        self.assertEqual(self.http("GET", self.path)[1]["readback"], "VERIFIED_CURRENT")
