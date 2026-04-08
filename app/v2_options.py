"""Typed option schemas for the v2 HTTP contract."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from fastapi import Depends, Form
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.api_errors import ApiError
from app.http_utils import parse_options_json

JsonScalar = str | int | float | bool | None


class StrictOptionsModel(BaseModel):
    """Base model for v2 option validation."""

    model_config = ConfigDict(extra="forbid")


class EmptyOptions(StrictOptionsModel):
    """Model for tools that do not accept additional options."""


class ImageFormat(StrEnum):
    png = "png"
    jpeg = "jpeg"


class OcrLanguage(StrEnum):
    english = "english"
    spanish = "spanish"
    french = "french"
    german = "german"
    portuguese = "portuguese"
    italian = "italian"
    chinese = "chinese"
    jpn = "jpn"


class PdfaConformance(StrEnum):
    pdfa_1b = "pdfa-1b"
    pdfa_2b = "pdfa-2b"
    pdfa_3b = "pdfa-3b"


class RedactionStrategy(StrEnum):
    email = "email"
    phone = "phone"
    custom = "custom"
    regex = "regex"


class PdfToImageOptions(StrictOptionsModel):
    format: ImageFormat = ImageFormat.png


class OcrOptions(StrictOptionsModel):
    language: OcrLanguage = OcrLanguage.english


class PdfaOptions(StrictOptionsModel):
    conformance: PdfaConformance = PdfaConformance.pdfa_2b


class ProtectOptions(StrictOptionsModel):
    userPassword: Annotated[str, Field(min_length=1)]


class FillFormOptions(StrictOptionsModel):
    fields: dict[str, JsonScalar] = Field(min_length=1)


class RedactOptions(StrictOptionsModel):
    strategy: RedactionStrategy = RedactionStrategy.email
    customText: str = ""
    regexPattern: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_strategy_inputs(self) -> RedactOptions:
        if self.strategy == RedactionStrategy.custom and not self.customText.strip():
            raise ValueError("customText is required when strategy='custom'.")
        if self.strategy == RedactionStrategy.regex and not self.regexPattern.strip():
            raise ValueError("regexPattern is required when strategy='regex'.")
        return self


def options_dependency[OptionsModel: StrictOptionsModel](
    model_type: type[OptionsModel],
) -> Any:
    """Create a FastAPI dependency that validates the JSON options payload."""

    async def dependency(options: Annotated[str, Form()] = "{}") -> OptionsModel:
        raw_options = parse_options_json(options)
        try:
            return model_type.model_validate(raw_options)
        except ValidationError as exc:
            raise ApiError(
                status_code=422,
                code="invalid_options",
                message="Options validation failed.",
                details=exc.errors(),
            ) from exc

    return Depends(dependency)
