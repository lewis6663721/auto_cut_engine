from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class TimestampMixin:
    created_at = fields.DatetimeField(auto_now_add=True)


class User(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    username = fields.CharField(max_length=64, unique=True)
    password_hash = fields.CharField(max_length=255)
    is_admin = fields.BooleanField(default=False)

    tasks: fields.ReverseRelation["RenderTask"]
    ai_provider_credentials: fields.ReverseRelation["AiProviderCredential"]

    class Meta:
        table = "ace_users"


class AiProviderCredential(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="ai_provider_credentials", on_delete=fields.CASCADE
    )
    provider_key = fields.CharField(max_length=48)
    label = fields.CharField(max_length=120)
    base_url = fields.CharField(max_length=500)
    api_key_secret = fields.TextField(default="")
    default_chat_model = fields.CharField(max_length=120, default="")
    default_asr_model = fields.CharField(max_length=120, default="")
    default_image_model = fields.CharField(max_length=120, default="")
    default_video_model = fields.CharField(max_length=120, default="")
    capabilities = fields.JSONField(default=list)
    is_enabled = fields.BooleanField(default=True)
    last_status = fields.CharField(max_length=24, default="unchecked")
    last_message = fields.TextField(default="")
    last_checked_at = fields.DatetimeField(null=True)

    class Meta:
        table = "ace_ai_provider_credentials"
        unique_together = (("user", "provider_key", "label"),)


class Template(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=120)
    category = fields.CharField(max_length=32)
    description = fields.TextField(default="")
    demo_original_url = fields.CharField(max_length=500, null=True)
    demo_result_url = fields.CharField(max_length=500, null=True)
    thumbnail_url = fields.CharField(max_length=500, null=True)
    config_schema = fields.JSONField(default=dict)
    sort_order = fields.IntField(default=0)
    is_active = fields.BooleanField(default=True)

    class Meta:
        table = "ace_templates"
        ordering = ["sort_order", "id"]


class RemotionTemplate(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="remotion_templates", on_delete=fields.CASCADE
    )
    title = fields.CharField(max_length=160)
    category = fields.CharField(max_length=48, default="motion")
    description = fields.TextField(default="")
    source_prompt = fields.TextField(default="")
    source_type = fields.CharField(max_length=32, default="description")
    reference_files = fields.JSONField(default=list)
    blueprint = fields.JSONField(default=dict)
    props_schema = fields.JSONField(default=dict)
    remotion_code = fields.TextField(default="")
    preview_html = fields.TextField(default="")
    preview_url = fields.CharField(max_length=800, null=True)
    effect_keys = fields.JSONField(default=list)
    transition_keys = fields.JSONField(default=list)
    status = fields.CharField(max_length=24, default="draft")
    version = fields.IntField(default=1)

    class Meta:
        table = "ace_remotion_templates"
        ordering = ["-created_at"]


class Asset(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=160)
    file_path = fields.CharField(max_length=800)
    asset_type = fields.CharField(max_length=32)
    tags = fields.JSONField(default=list)

    class Meta:
        table = "ace_assets"


class RenderTask(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="tasks", on_delete=fields.CASCADE
    )
    template: fields.ForeignKeyNullableRelation[Template] = fields.ForeignKeyField(
        "models.Template", related_name="tasks", null=True, on_delete=fields.SET_NULL
    )
    source_asset: fields.ForeignKeyNullableRelation[Asset] = fields.ForeignKeyField(
        "models.Asset", related_name="tasks", null=True, on_delete=fields.SET_NULL
    )
    applied_config = fields.JSONField(default=dict)
    ai_context = fields.JSONField(default=dict)
    status = fields.CharField(max_length=24, default="pending")
    progress = fields.IntField(default=0)
    result_url = fields.CharField(max_length=800, null=True)
    error_log = fields.TextField(null=True)
    completed_at = fields.DatetimeField(null=True)

    edit_details: fields.ReverseRelation["EditDetail"]

    class Meta:
        table = "ace_render_tasks"
        ordering = ["-created_at"]


class TimelineProject(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="timeline_projects", on_delete=fields.CASCADE
    )
    name = fields.CharField(max_length=160)
    description = fields.TextField(default="")
    canvas_width = fields.IntField(default=1080)
    canvas_height = fields.IntField(default=1920)
    fps = fields.IntField(default=30)
    duration = fields.FloatField(default=30)
    cover_url = fields.CharField(max_length=800, null=True)
    status = fields.CharField(max_length=24, default="draft")
    timeline_meta = fields.JSONField(default=dict)

    tracks: fields.ReverseRelation["TimelineTrack"]

    class Meta:
        table = "ace_timeline_projects"
        ordering = ["-created_at"]


class TimelineTrack(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    project: fields.ForeignKeyRelation[TimelineProject] = fields.ForeignKeyField(
        "models.TimelineProject", related_name="tracks", on_delete=fields.CASCADE
    )
    name = fields.CharField(max_length=120)
    track_type = fields.CharField(max_length=32, default="video")
    sort_order = fields.IntField(default=0)
    muted = fields.BooleanField(default=False)
    locked = fields.BooleanField(default=False)

    clips: fields.ReverseRelation["TimelineClip"]

    class Meta:
        table = "ace_timeline_tracks"
        ordering = ["sort_order", "id"]


class TimelineClip(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    track: fields.ForeignKeyRelation[TimelineTrack] = fields.ForeignKeyField(
        "models.TimelineTrack", related_name="clips", on_delete=fields.CASCADE
    )
    asset: fields.ForeignKeyNullableRelation[Asset] = fields.ForeignKeyField(
        "models.Asset", related_name="timeline_clips", null=True, on_delete=fields.SET_NULL
    )
    name = fields.CharField(max_length=160)
    clip_type = fields.CharField(max_length=32, default="video")
    start_time = fields.FloatField(default=0)
    duration = fields.FloatField(default=5)
    source_start = fields.FloatField(default=0)
    source_duration = fields.FloatField(null=True)
    z_index = fields.IntField(default=0)
    params = fields.JSONField(default=dict)
    notes = fields.TextField(default="")

    class Meta:
        table = "ace_timeline_clips"
        ordering = ["start_time", "z_index", "id"]


class EditDetail(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    task: fields.ForeignKeyRelation[RenderTask] = fields.ForeignKeyField(
        "models.RenderTask", related_name="edit_details", on_delete=fields.CASCADE
    )
    start_time = fields.FloatField(default=0)
    end_time = fields.FloatField(default=0)
    edit_type = fields.CharField(max_length=64)
    edit_detail = fields.TextField(default="")
    edit_reason = fields.TextField(default="")
    affects = fields.CharField(max_length=16, default="both")
    source = fields.CharField(max_length=24, default="template")
    dimension_key = fields.CharField(max_length=64, null=True)
    sort_order = fields.IntField(default=0)

    class Meta:
        table = "ace_edit_details"
        ordering = ["sort_order", "id"]


class CreativeWork(Model, TimestampMixin):
    id = fields.IntField(primary_key=True)
    user: fields.ForeignKeyRelation[User] = fields.ForeignKeyField(
        "models.User", related_name="creative_works", on_delete=fields.CASCADE
    )
    title = fields.CharField(max_length=160)
    description = fields.TextField()
    original_video_path = fields.CharField(max_length=800, null=True)
    result_video_path = fields.CharField(max_length=800, null=True)
    result_video_url = fields.CharField(max_length=800, null=True)
    video_style = fields.CharField(max_length=120, null=True)
    content_category = fields.CharField(max_length=120, null=True)
    mood = fields.CharField(max_length=120, null=True)
    pacing = fields.CharField(max_length=120, null=True)
    duration = fields.FloatField(null=True)
    ai_analysis = fields.JSONField(default=dict)
    parsed_config = fields.JSONField(default=dict)
    tags = fields.JSONField(default=list)
    status = fields.CharField(max_length=24, default="draft")
    view_count = fields.IntField(default=0)
    like_count = fields.IntField(default=0)

    class Meta:
        table = "ace_creative_works"
        ordering = ["-created_at"]


class DimensionGroup(Model):
    id = fields.IntField(primary_key=True)
    key = fields.CharField(max_length=64, unique=True)
    label = fields.CharField(max_length=120)
    icon = fields.CharField(max_length=24, default="")
    sort_order = fields.IntField(default=0)
    is_active = fields.BooleanField(default=True)

    class Meta:
        table = "ace_dimension_groups"
        ordering = ["sort_order", "id"]


class DimensionDef(Model):
    id = fields.IntField(primary_key=True)
    key = fields.CharField(max_length=64, unique=True)
    label = fields.CharField(max_length=120)
    group: fields.ForeignKeyRelation[DimensionGroup] = fields.ForeignKeyField(
        "models.DimensionGroup", related_name="dimensions", to_field="key", source_field="group_key", on_delete=fields.CASCADE
    )
    icon = fields.CharField(max_length=24, default="")
    description = fields.TextField(default="")
    params_schema = fields.JSONField(default=dict)
    sort_order = fields.IntField(default=0)
    is_active = fields.BooleanField(default=True)

    class Meta:
        table = "ace_dimension_defs"
        ordering = ["sort_order", "id"]
