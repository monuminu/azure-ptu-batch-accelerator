"""Published wire schemas; persistence-only attributes never enter these models."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Expiration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor: Literal["created_at"]
    seconds: int = Field(ge=1209600, le=2592000)


class CreateBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_file_id: str
    endpoint: Literal["/chat/completions", "/v1/chat/completions", "/responses", "/v1/responses"]
    completion_window: Literal["24h"]
    metadata: dict[str, str] | None = Field(default=None, max_length=16)
    output_expires_after: Expiration | None = None


class FileObject(BaseModel):
    id: str
    object: Literal["file"] = "file"
    bytes: int
    created_at: int
    expires_at: int | None = None
    filename: str
    purpose: str
    status: Literal["uploaded", "processed", "error"] = "processed"
    status_details: str | None = None


class RequestCounts(BaseModel):
    total: int
    completed: int
    failed: int


class BatchObject(BaseModel):
    id: str
    object: Literal["batch"] = "batch"
    endpoint: str | None
    input_file_id: str
    completion_window: Literal["24h"]
    created_at: int
    status: Literal[
        "validating",
        "failed",
        "in_progress",
        "finalizing",
        "completed",
        "expired",
        "cancelling",
        "cancelled",
    ]
    request_counts: RequestCounts
    output_file_id: str | None = None
    error_file_id: str | None = None
    metadata: dict[str, str] | None = None
    errors: dict[str, Any] | None = None
    cancelled_at: int | None = None
    cancelling_at: int | None = None
    completed_at: int | None = None
    expired_at: int | None = None
    expires_at: int | None = Field(
        default=None, description="Informational 24h target timestamp; jobs continue beyond it."
    )
    failed_at: int | None = None
    finalizing_at: int | None = None
    in_progress_at: int | None = None


class FileList(BaseModel):
    object: Literal["list"] = "list"
    data: list[FileObject]
    first_id: str | None
    last_id: str | None
    has_more: bool


class BatchList(BaseModel):
    object: Literal["list"] = "list"
    data: list[BatchObject]
    first_id: str | None
    last_id: str | None
    has_more: bool


class FileDeleted(BaseModel):
    id: str
    object: Literal["file"] = "file"
    deleted: bool


class ErrorDetail(BaseModel):
    message: str
    type: str
    param: str | None
    code: str | None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


def install_openapi(app):
    from fastapi.openapi.utils import get_openapi

    def generate():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        definitions = schema.setdefault("components", {}).setdefault("schemas", {})
        for model in (CreateBatchRequest, Expiration, ErrorEnvelope):
            value = model.model_json_schema(ref_template="#/components/schemas/{model}")
            definitions.update(value.pop("$defs", {}))
            definitions[model.__name__] = value
        upload = schema["paths"]["/openai/v1/files"]["post"]
        upload["requestBody"] = {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file", "purpose"],
                        "additionalProperties": False,
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "purpose": {"type": "string", "const": "batch"},
                            "expires_after[anchor]": {"type": "string", "const": "created_at"},
                            "expires_after[seconds]": {
                                "type": "integer",
                                "minimum": 1209600,
                                "maximum": 2592000,
                            },
                        },
                    }
                }
            },
        }
        schema["paths"]["/openai/v1/batches"]["post"]["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/CreateBatchRequest"}}
            },
        }
        for path in schema["paths"].values():
            for operation in path.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                operation["responses"].pop("422", None)
                for status in ("400", "401", "403", "404", "409", "413", "500", "503"):
                    operation["responses"][status] = {
                        "description": "OpenAI-compatible error",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                            }
                        },
                    }
        schema["paths"]["/openai/v1/files/{file_id}/content"]["get"]["responses"]["200"] = {
            "description": "Raw JSONL bytes",
            "content": {"application/jsonl": {"schema": {"type": "string"}}},
        }
        app.openapi_schema = schema
        return schema

    app.openapi = generate
