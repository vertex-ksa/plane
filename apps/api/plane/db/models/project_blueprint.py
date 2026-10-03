from django.conf import settings
from django.db import models
from uuid import uuid4


class ProjectBlueprintVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    workspace = models.ForeignKey("db.Workspace", on_delete=models.CASCADE)
    source_project = models.ForeignKey("db.Project", on_delete=models.PROTECT)
    title = models.CharField(max_length=255)
    definition = models.JSONField()
    sha256 = models.CharField(max_length=64)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    # Publication creates a new version. No update endpoint; archive revokes use.
    archived = models.BooleanField(default=False)

    class Meta:
        db_table = "project_blueprint_versions"


class ProjectBlueprintCommand(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    workspace = models.ForeignKey("db.Workspace", on_delete=models.CASCADE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    command_id = models.UUIDField()
    request_sha256 = models.CharField(max_length=64)
    version = models.ForeignKey(ProjectBlueprintVersion, on_delete=models.PROTECT)
    receipt = models.JSONField()

    class Meta:
        db_table = "project_blueprint_commands"
        constraints = [models.UniqueConstraint(fields=["workspace", "command_id"], name="project_blueprint_workspace_command_unique")]
