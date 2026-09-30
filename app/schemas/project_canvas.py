from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBool, field_validator, model_validator

from app.schemas.base import SchemaBaseModel
from app.schemas.canvas_generation import CanvasGenerationSettings


class CanvasSchema(SchemaBaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, allow_inf_nan=False)


class CanvasViewport(CanvasSchema):
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    zoom: float = Field(default=1, ge=0.05, le=8)


class CanvasCreate(CanvasSchema):
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("画布名称不能为空")
        return value.strip()


class CanvasContent(CanvasSchema):
    text: str = Field(default="", max_length=10000)
    generation: CanvasGenerationSettings | None = None


class CanvasNodeInput(CanvasSchema):
    id: UUID
    kind: Literal["text", "image", "video", "group"]
    title: str = Field(default="", max_length=128)
    x: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    y: float = Field(default=0, ge=-1_000_000, le=1_000_000)
    width: float = Field(default=320, ge=1, le=10000)
    height: float = Field(default=240, ge=1, le=10000)
    parent_id: UUID | None = None
    media_id: UUID | None = None
    content: CanvasContent = Field(default_factory=CanvasContent)


class CanvasEdgeInput(CanvasSchema):
    id: UUID
    source_id: UUID
    target_id: UUID
    input: Literal["reference", "first_frame", "last_frame", "text"] = "reference"
    media_id: UUID | None = None
    position: int = Field(default=0, ge=0, le=2999, strict=True)
    text_source: Literal["input", "result"] | None = None
    refresh_text: StrictBool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def validate_text_input(self):
        if self.input == "text":
            if self.text_source is None or self.media_id is not None:
                raise ValueError("文本连接必须指定text_source且不能绑定媒体")
        elif self.text_source is not None or self.refresh_text:
            raise ValueError("仅文本连接支持text_source/refresh_text")
        return self


class CanvasTextSnapshot(CanvasSchema):
    text: str = Field(max_length=10000)
    generation_id: UUID | None = None


class CanvasEdgeOut(CanvasEdgeInput):
    text_snapshot: CanvasTextSnapshot | None = None


class CanvasContentPatch(CanvasSchema):
    text: str | None = Field(default=None, max_length=10000)
    generation: CanvasGenerationSettings | None = None

    @field_validator("text")
    @classmethod
    def reject_null_text(cls, value):
        if value is None:
            raise ValueError("文本不能为 null，请用空字符串清空")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("至少需要一个内容字段")
        return self


class CanvasNodePatch(CanvasSchema):
    id: UUID
    title: str | None = Field(default=None, max_length=128)
    parent_id: UUID | None = None
    media_id: UUID | None = None
    content: CanvasContentPatch | None = None

    @field_validator("title", "content")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("字段不能为 null")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if self.model_fields_set == {"id"}:
            raise ValueError("至少需要一个节点修改字段")
        return self


class CanvasPatch(CanvasSchema):
    expected_revision: int = Field(ge=1, strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    viewport: CanvasViewport | None = None
    upsert_nodes: list[CanvasNodeInput] = Field(default_factory=list, max_length=500)
    update_nodes: list[CanvasNodePatch] = Field(default_factory=list, max_length=500)
    delete_node_ids: list[UUID] = Field(default_factory=list, max_length=500)
    upsert_edges: list[CanvasEdgeInput] = Field(default_factory=list, max_length=500)
    delete_edge_ids: list[UUID] = Field(default_factory=list, max_length=500)

    @field_validator("name", "viewport")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("字段不能为 null")
        if isinstance(value, str):
            return CanvasCreate(name=value).name
        return value

    @model_validator(mode="after")
    def validate_operations(self):
        ids = [
            [n.id for n in self.upsert_nodes],
            self.delete_node_ids,
            [e.id for e in self.upsert_edges],
            self.delete_edge_ids,
            [n.id for n in self.update_nodes],
        ]
        if sum(map(len, ids)) > 500:
            raise ValueError("每次最多 500 个画布操作")
        if any(len(values) != len(set(values)) for values in ids):
            raise ValueError("操作 ID 不能重复")
        if (set(ids[0]) & set(ids[1]) or set(ids[4]) & (set(ids[0]) | set(ids[1]))
                or set(ids[2]) & set(ids[3])):
            raise ValueError("同一对象不能出现在多个修改或删除列表中")
        if not any(ids) and self.name is None and self.viewport is None:
            raise ValueError("至少需要一个修改")
        if len(self.model_dump_json().encode()) > 2 * 1024 * 1024:
            raise ValueError("画布修改内容不能超过 2MB")
        return self


class CanvasNodeOut(CanvasNodeInput):
    content_revision: int
    import_record_id: UUID | None = None
    latest_generation_id: UUID | None = None
    selected_generation_id: UUID | None = None


class CanvasNodeLayoutPatch(CanvasSchema):
    id: UUID
    x: float | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    y: float | None = Field(default=None, ge=-1_000_000, le=1_000_000)
    width: float | None = Field(default=None, ge=1, le=10000)
    height: float | None = Field(default=None, ge=1, le=10000)

    @field_validator("x", "y", "width", "height")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("布局字段不能为 null")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not (self.model_fields_set - {"id"}):
            raise ValueError("至少需要一个布局字段")
        return self


class CanvasLayoutPatch(CanvasSchema):
    expected_revision: int = Field(ge=1, strict=True)
    nodes: list[CanvasNodeLayoutPatch] = Field(default_factory=list, max_length=500)
    viewport: CanvasViewport | None = None

    @field_validator("viewport")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("视口不能为 null")
        return value

    @model_validator(mode="after")
    def validate_changes(self):
        if not self.nodes and self.viewport is None:
            raise ValueError("至少需要一个布局修改")
        if len({node.id for node in self.nodes}) != len(self.nodes):
            raise ValueError("节点 ID 不能重复")
        return self


class CanvasSummary(CanvasSchema):
    id: UUID
    project_id: UUID
    name: str
    revision: int
    viewport: CanvasViewport
    created_at: datetime
    updated_at: datetime


class CanvasOut(CanvasSummary):
    nodes: list[CanvasNodeOut]
    edges: list[CanvasEdgeOut]
