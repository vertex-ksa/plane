"""Native bounded publication and all-or-nothing private provisioning.

Workspace locking serializes identifier/command reservation and permissions are
rechecked under transaction locks. Hashes detect stale material; never authorize.
"""
from django.db import transaction, IntegrityError
from uuid import UUID
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError
from plane.api.views.base import BaseAPIView
from plane.db.models import (Workspace, WorkspaceMember, Project, ProjectMember,
    ProjectIdentifier, Issue, IssueAssignee, IssueRelation, State, DEFAULT_STATES,
    ProjectBlueprintVersion, ProjectBlueprintCommand)
from plane.utils.project_blueprint import exact, definition, digest, plan, uuid, BlueprintInvalid


def workspace_actor(slug, actor):
    workspace = Workspace.objects.select_for_update().filter(slug=slug).first()
    if not workspace:
        raise NotFound()
    if not WorkspaceMember.objects.select_for_update().filter(workspace=workspace, member=actor, is_active=True, member__is_active=True, role__in=[15, 20]).exists():
        raise PermissionDenied()
    return workspace


def resolve(workspace, actor, version_id):
    try:
        version = ProjectBlueprintVersion.objects.select_for_update().get(id=version_id, workspace=workspace, archived=False)
    except ProjectBlueprintVersion.DoesNotExist:
        raise NotFound()
    project = Project.objects.select_for_update().filter(id=version.source_project_id, workspace=workspace, archived_at__isnull=True).first()
    if not project or not ProjectMember.objects.select_for_update().filter(project=project, member=actor, is_active=True, role__in=[15, 20]).exists():
        raise PermissionDenied()
    if digest(version.definition) != version.sha256:
        raise ValidationError("STALE_VERSION")
    return version


def make_plan(workspace, actor, value):
    exact(value, ["versionId", "versionSha256", "projects"])
    version = resolve(workspace, actor, uuid(value["versionId"]))
    if version.sha256 != value["versionSha256"]:
        raise ValidationError("STALE_VERSION")
    result = plan(str(version.id), version.sha256, version.definition, value["projects"], str(actor.id), str(workspace.id))
    members = {g["actorId"] for p in result["projects"] for g in p["grants"]}
    available = {str(m.member_id) for m in WorkspaceMember.objects.select_for_update().filter(workspace=workspace, member_id__in=members, is_active=True, member__is_active=True, role__in=[15, 20])}
    if members != available:
        raise PermissionDenied("ROLE_MEMBER_UNAVAILABLE")
    return version, result


def readback(workspace, actor, command):
    if command.actor_id != actor.id:
        raise PermissionDenied()
    resolve(workspace, actor, str(command.version_id))
    receipt = command.receipt
    for row in receipt["projects"]:
        p = Project.objects.select_for_update().filter(id=row["id"], workspace=workspace, archived_at__isnull=True, network=0).first()
        if not p or not ProjectMember.objects.filter(project=p, member=actor, is_active=True, role=20).exists():
            raise PermissionDenied("READBACK_UNAVAILABLE")
        grants = sorted((str(m.member_id), m.role) for m in ProjectMember.objects.filter(project=p, is_active=True))
        expected = sorted((g["actorId"], g["role"]) for g in row["grants"])
        if grants != expected or p.name != row["name"] or p.identifier != row["identifier"]:
            raise ValidationError("READBACK_CHANGED")
        members = {g["actorId"] for g in row["grants"]}
        available = {str(m.member_id) for m in WorkspaceMember.objects.select_for_update().filter(workspace=workspace, member_id__in=members, is_active=True, member__is_active=True, role__in=[15, 20])}
        if members != available:
            raise PermissionDenied("ROLE_MEMBER_UNAVAILABLE")
        if Issue.objects.filter(project=p).count() != len(row["tasks"]):
            raise ValidationError("READBACK_CHANGED")
        for task in row["tasks"]:
            issue = Issue.objects.filter(id=task["id"], project=p, name=task["name"], start_date=task["startDate"], target_date=task["finishDate"]).first()
            if not issue or list(IssueAssignee.objects.filter(issue=issue).values_list("assignee_id", flat=True)) != [UUID(task["ownerId"])]:
                raise ValidationError("READBACK_CHANGED")
        ids = {task["key"]: task["id"] for task in row["tasks"]}
        edges = sorted((str(edge.issue_id), str(edge.related_issue_id), edge.relation_type) for edge in IssueRelation.objects.filter(project=p))
        expected_edges = sorted((ids[e["successorKey"]], ids[e["predecessorKey"]], "blocked_by") for e in receipt["dependencies"])
        if edges != expected_edges:
            raise ValidationError("READBACK_CHANGED")
    return {**receipt, "readback": "VERIFIED_CURRENT", "atomic": True}


class ProjectBlueprintEndpoint(BaseAPIView):
    def get(self, request, slug):
        with transaction.atomic():
            w = workspace_actor(slug, request.user)
            versions = ProjectBlueprintVersion.objects.filter(workspace=w, archived=False, source_project__archived_at__isnull=True,
                source_project__project_projectmember__member=request.user, source_project__project_projectmember__is_active=True,
                source_project__project_projectmember__role__in=[15, 20]).order_by("-created_at")[:101]
            rows = [{"id": str(v.id), "sha256": v.sha256, "title": v.title, "definition": v.definition} for v in versions if digest(v.definition) == v.sha256]
            return Response({"contract": "project-blueprint/1", "actorId": str(request.user.id), "workspaceId": str(w.id), "versions": rows[:100], "truncated": len(rows) > 100, "visibility": "PRIVATE", "externalReferences": "UNSUPPORTED"})

    def post(self, request, slug):
        try:
            with transaction.atomic():
                w = workspace_actor(slug, request.user)
                # Publication is an administrative native project action.
                data = exact(request.data, ["sourceProjectId", "title", "definition"])
                source = Project.objects.select_for_update().filter(id=uuid(data["sourceProjectId"]), workspace=w, archived_at__isnull=True).first()
                if not source or not ProjectMember.objects.select_for_update().filter(project=source, member=request.user, role=20, is_active=True).exists():
                    raise PermissionDenied()
                if not isinstance(data["title"], str) or not data["title"].strip() or len(data["title"]) > 255:
                    raise ValidationError("INVALID_TITLE")
                definition(data["definition"])
                v = ProjectBlueprintVersion.objects.create(workspace=w, source_project=source, title=data["title"], definition=data["definition"], sha256=digest(data["definition"]), published_by=request.user)
                return Response({"id": str(v.id), "sha256": v.sha256}, status=201)
        except (BlueprintInvalid, TypeError) as e:
            raise ValidationError(str(e))


class ProjectBlueprintPreviewEndpoint(BaseAPIView):
    def post(self, request, slug):
        try:
            with transaction.atomic():
                w = workspace_actor(slug, request.user)
                _, result = make_plan(w, request.user, request.data)
                return Response(result)
        except (BlueprintInvalid, TypeError) as e:
            raise ValidationError(str(e))


class ProjectBlueprintCommandEndpoint(BaseAPIView):
    def get(self, request, slug, command_id):
        with transaction.atomic():
            w = workspace_actor(slug, request.user)
            command = ProjectBlueprintCommand.objects.select_for_update().filter(workspace=w, command_id=command_id).first()
            if not command:
                return Response({"status": "NOT_COMMITTED", "commandId": str(command_id), "atomic": True})
            return Response(readback(w, request.user, command))

    def post(self, request, slug, command_id):
        try:
            with transaction.atomic():
                w = workspace_actor(slug, request.user)
                data = exact(request.data, ["input", "planSha256", "confirmPrivateGrants"])
                if data["confirmPrivateGrants"] is not True:
                    raise ValidationError("CONFIRMATION_REQUIRED")
                request_sha = digest(data)
                old = ProjectBlueprintCommand.objects.select_for_update().filter(workspace=w, command_id=command_id).first()
                if old:
                    if old.request_sha256 != request_sha:
                        raise ValidationError("COMMAND_CONFLICT")
                    return Response(readback(w, request.user, old))
                version, result = make_plan(w, request.user, data["input"])
                if result["planSha256"] != data["planSha256"]:
                    raise ValidationError("STALE_PLAN")
                if ProjectIdentifier.objects.filter(workspace=w, name__in=[p["identifier"] for p in result["projects"]]).exists():
                    raise ValidationError("IDENTIFIER_UNAVAILABLE")
                rows = []
                for p in result["projects"]:
                    # Private is set in the initial insert. No public-default window.
                    project = Project.objects.create(workspace=w, name=p["name"], identifier=p["identifier"], network=0, timezone=result["calendar"]["timezone"])
                    ProjectIdentifier.objects.create(workspace=w, project=project, name=p["identifier"])
                    for g in p["grants"]:
                        ProjectMember.objects.create(project=project, workspace=w, member_id=g["actorId"], role=g["role"])
                    states = [State.objects.create(project=project, workspace=w, **s) for s in DEFAULT_STATES]
                    initial = next(s for s in states if s.default)
                    tasks = {}
                    for t in p["workPackages"]:
                        issue = Issue.objects.create(project=project, workspace=w, name=t["name"], state=initial, start_date=t["startDate"], target_date=t["finishDate"])
                        IssueAssignee.objects.create(issue=issue, project=project, workspace=w, assignee_id=t["ownerId"])
                        tasks[t["key"]] = {**t, "id": str(issue.id)}
                    for e in result["dependencies"]:
                        IssueRelation.objects.create(project=project, workspace=w, issue_id=tasks[e["successorKey"]]["id"], related_issue_id=tasks[e["predecessorKey"]]["id"], relation_type="blocked_by")
                    rows.append({"id": str(project.id), "name": p["name"], "identifier": p["identifier"], "network": 0, "grants": p["grants"], "tasks": list(tasks.values())})
                receipt = {"status": "COMMITTED", "commandId": str(command_id), "actorId": str(request.user.id), "workspaceId": str(w.id), "versionId": str(version.id), "planSha256": result["planSha256"], "dependencies": result["dependencies"], "projects": rows}
                command = ProjectBlueprintCommand.objects.create(workspace=w, actor=request.user, command_id=command_id, request_sha256=request_sha, version=version, receipt=receipt)
                return Response(readback(w, request.user, command), status=201)
        except (BlueprintInvalid, TypeError) as e:
            raise ValidationError(str(e))
        except IntegrityError:
            raise ValidationError("IDENTIFIER_OR_COMMAND_CONFLICT")
