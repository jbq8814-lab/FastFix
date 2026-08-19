from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApprovalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationResultSummary(ApprovalModel):
    passed: bool
    returncode: int
    timed_out: bool
    duration_seconds: float
    passed_count: int | None = None
    failed_count: int | None = None
    skipped_count: int | None = None


class ReopenAuditEntry(ApprovalModel):
    reopen_count: int = Field(ge=1)
    revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    validation_epoch_before: int = Field(ge=0)
    validation_epoch_after: int = Field(ge=1)


class ValidationSummary(ApprovalModel):
    revision: int
    validation_epoch: int = Field(ge=0)
    reopen_count: int = Field(ge=0)
    last_reopen_reason: str | None = Field(default=None, max_length=500)
    reopen_history: list[ReopenAuditEntry]
    sandbox_image_id: str
    sandbox_network_mode: Literal["none"]
    targeted: ValidationResultSummary
    regression: ValidationResultSummary
    ruff: ValidationResultSummary

    @model_validator(mode="after")
    def validate_reopen_audit(self) -> "ValidationSummary":
        if self.reopen_count != len(self.reopen_history):
            raise ValueError("Reopen count does not match reopen history.")
        if self.reopen_count:
            if [entry.reopen_count for entry in self.reopen_history] != list(range(1, self.reopen_count + 1)):
                raise ValueError("Reopen history is not sequential.")
            if any(entry.validation_epoch_after != entry.validation_epoch_before + 1 for entry in self.reopen_history):
                raise ValueError("Reopen validation epochs are invalid.")
            if self.reopen_history[-1].validation_epoch_after > self.validation_epoch:
                raise ValueError("Reopen audit is newer than validation.")
            if self.last_reopen_reason != self.reopen_history[-1].reason:
                raise ValueError("Last reopen reason does not match reopen history.")
        elif self.last_reopen_reason is not None:
            raise ValueError("Last reopen reason requires reopen history.")
        return self


class ApprovalRequest(ApprovalModel):
    request_id: str
    schema_version: Literal["1.0"] = "1.0"
    task_id: str
    created_at: datetime
    source_head: str
    candidate_head: str
    patch_sha256: str
    changed_files: list[str]
    added_lines: int
    deleted_lines: int
    targeted_tests_passed: bool
    regression_tests_passed: bool
    ruff_passed: bool
    validation_revision: int
    validation_epoch: int = Field(ge=0)
    reopen_count: int = Field(ge=0)
    sandbox_image_id: str
    sandbox_network_mode: Literal["none"]
    risk_notes: list[str] = Field(default_factory=list)
    status: Literal["pending"] = "pending"


class ManifestEntry(ApprovalModel):
    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PackageManifest(ApprovalModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    files: list[ManifestEntry]


class ApprovalDecision(ApprovalModel):
    decision: Literal["approve", "reject"]
    request_id: str
    expected_patch_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=100, pattern=r"^[^/\\\r\n]+$")
    note: str = Field(default="", max_length=500, pattern=r"^[^/\\\r\n]*$")

    @model_validator(mode="after")
    def require_approved_patch_hash(self) -> "ApprovalDecision":
        if self.decision == "approve" and self.expected_patch_sha256 is None:
            raise ValueError("Approve decisions require expected_patch_sha256.")
        return self


class DecisionRecord(ApprovalModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    decision: Literal["approve", "reject"]
    decided_at: datetime
    actor: str
    note: str
    patch_sha256: str
    package_manifest_sha256: str
    source_head: str
    application_sha256: str | None = None


class ApplicationRecord(ApprovalModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    applied_at: datetime
    status: Literal["applied"] = "applied"
    patch_sha256: str
    reverse_patch_sha256: str
    package_manifest_sha256: str
    source_head_before: str
    source_head_after: str
    changed_files: list[str]
    added_lines: int
    deleted_lines: int


class RollbackRecord(ApprovalModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    rolled_back_at: datetime
    status: Literal["rolled_back"] = "rolled_back"
    actor: str
    note: str
    application_sha256: str
    reverse_patch_sha256: str
    source_head_before: str
    source_head_after: str


class ApprovalActionResult(ApprovalModel):
    request_id: str
    status: Literal["approved", "rejected", "rolled_back"]
    cleanup_warning: str | None = None
