# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Finite real Windows client/gateway proof; exclusive disposable test DB only."""
import json
import os
from pathlib import Path
import time
from unittest import skipUnless

from django.conf import settings
from django.db import connection
from django.test import LiveServerTestCase

from plane.db.models import WorkspaceMember, Project, ProjectMember, ProjectBlueprintCommand, Issue, IssueRelation
from plane.tests import test_project_blueprint_http as http_fixture


@skipUnless(os.environ.get("TECHNOMINDS_BLUEPRINT_TEST_RUN") == "20261003", "Requires the explicit disposable Windows gateway proof controller")
class BlueprintGatewayProofTests(LiveServerTestCase):
    host = "0.0.0.0"
    port = 18874
    # Keep fixture publication HTTP local while the server listens on the
    # container interface for the single host-loopback published port.
    live_server_url = "http://127.0.0.1:18874"
    http = http_fixture.BlueprintHttpTests.http

    def setUp(self):
        self.assertEqual(os.environ.get("TECHNOMINDS_BLUEPRINT_TEST_RUN"), "20261003")
        self.assertEqual(settings.DATABASES["default"]["HOST"], "tm-projects-blueprints-tests-db-20261003")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database()")
            self.assertEqual(cursor.fetchone()[0], "test_tm_projects_blueprints_20261003")
        self.exchange = Path("/proof-exchange")
        self.owner_token = os.environ.get("BLUEPRINT_PROOF_OWNER")
        self.assertTrue(self.owner_token and self.owner_token.startswith("projects-blueprint-gateway-"))
        self.assertEqual((self.exchange / "owner.txt").read_text(), self.owner_token)
        http_fixture.BlueprintHttpTests.setUp(self)

    def write_private(self, name, value):
        target = self.exchange / name
        temporary = self.exchange / (name + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream)
        temporary.replace(target)

    def test_real_client_gateway_native_boundary(self):
        self.write_private("fixture.json", {
            "owner": self.owner_token,
            "profile": {
                "synthetic_only": True, "provider": "PLANE", "workspace": "main",
                "nativeUserId": str(self.actor.id), "nativeWorkspaceId": str(self.workspace.id),
                "nativeEmail": self.actor.email, "nativeWorkspaceSlug": self.workspace.slug,
                "nativeBaseUrl": "http://127.0.0.1:18874/", "apiKey": self.token.token,
            },
            "input": self.input, "commandId": str(self.command),
        })
        self.write_private("status.json", {"owner": self.owner_token, "phase": "READY"})
        deadline = time.monotonic() + 120
        revoked = False
        done = False
        while time.monotonic() < deadline:
            control = self.exchange / "control.json"
            if control.exists():
                value = json.loads(control.read_text())
                self.assertEqual(value.get("owner"), self.owner_token)
                if value.get("phase") == "REVOKE" and not revoked:
                    self.assertEqual(Project.objects.filter(workspace=self.workspace).count(), 3)
                    self.assertEqual(ProjectBlueprintCommand.objects.filter(workspace=self.workspace).count(), 1)
                    WorkspaceMember.objects.filter(workspace=self.workspace, member=self.actor).update(is_active=False)
                    revoked = True
                    self.write_private("status.json", {"owner": self.owner_token, "phase": "REVOKED"})
                elif value.get("phase") == "DONE":
                    self.assertTrue(revoked)
                    done = True
                    break
                elif value.get("phase") == "FAILED":
                    self.fail("Windows client/gateway proof failed; inspect sanitized proof receipt")
            time.sleep(0.1)
        self.assertTrue(done, "Finite Windows proof phase deadline exceeded")
        self.assertEqual(ProjectBlueprintCommand.objects.filter(workspace=self.workspace, command_id=self.command).count(), 1)
        self.assertEqual(ProjectBlueprintCommand.objects.filter(workspace=self.workspace).count(), 1)
        projects = list(Project.objects.filter(workspace=self.workspace).exclude(id=self.source.id).order_by("identifier"))
        self.assertEqual(len(projects), 2)
        self.assertEqual([p.network for p in projects], [0, 0])
        self.assertEqual(Project.objects.filter(workspace=self.workspace).count(), 3)
        self.assertEqual(Issue.objects.filter(workspace=self.workspace).count(), 4)
        self.assertEqual(IssueRelation.objects.filter(workspace=self.workspace).count(), 2)
        self.assertEqual(set(ProjectMember.objects.filter(project=projects[0]).values_list("member_id", "role")), {(self.actor.id, 20), (self.owner.id, 15)})
        self.assertEqual(set(ProjectMember.objects.filter(project=projects[1]).values_list("member_id", "role")), {(self.actor.id, 20)})
        self.write_private("status.json", {"owner": self.owner_token, "phase": "VERIFIED"})
