import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("db", "0122_alter_draftissue_assignees_alter_issue_assignees_and_more"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="ProjectBlueprintVersion", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("title", models.CharField(max_length=255)),
            ("definition", models.JSONField()), ("sha256", models.CharField(max_length=64)),
            ("archived", models.BooleanField(default=False)),
            ("workspace", models.ForeignKey(to="db.workspace", on_delete=django.db.models.deletion.CASCADE)),
            ("source_project", models.ForeignKey(to="db.project", on_delete=django.db.models.deletion.PROTECT)),
            ("published_by", models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT)),
        ], options={"db_table": "project_blueprint_versions"}),
        migrations.CreateModel(name="ProjectBlueprintCommand", fields=[
            ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("command_id", models.UUIDField()), ("request_sha256", models.CharField(max_length=64)),
            ("receipt", models.JSONField()),
            ("workspace", models.ForeignKey(to="db.workspace", on_delete=django.db.models.deletion.CASCADE)),
            ("actor", models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT)),
            ("version", models.ForeignKey(to="db.projectblueprintversion", on_delete=django.db.models.deletion.PROTECT)),
        ], options={"db_table": "project_blueprint_commands", "constraints": [models.UniqueConstraint(fields=("workspace", "command_id"), name="project_blueprint_workspace_command_unique")]}),
    ]
