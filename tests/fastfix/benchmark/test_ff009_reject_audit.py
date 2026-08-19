import hashlib
import json
from pathlib import Path

RUN = Path(__file__).resolve().parents[3] / "benchmarks" / "results" / "ff-009-current-baseline" / "run-001"


def payload(name: str) -> dict:
    return json.loads((RUN / name).read_text(encoding="utf-8"))


def content(name: str) -> bytes:
    return (RUN / name).read_bytes().replace(b"\r\n", b"\n")


def sha256(name: str) -> str:
    return hashlib.sha256(content(name)).hexdigest()


def test_ff009_release_reject_audit_is_self_contained() -> None:
    assessment = payload("assessment.json")
    audit = payload("reject-audit.json")
    decision = payload("reject-decision.json")
    manifest = payload("approval-package-manifest.json")
    request = payload("approval-request.json")

    assert sha256("approval-package-manifest.json") == audit["approval_package_manifest"]["sha256"]
    assert sha256("reject-decision.json") == audit["decision_record"]["sha256"]
    for entry in manifest["files"]:
        assert len(content(entry["path"])) == entry["bytes"]
        assert sha256(entry["path"]) == entry["sha256"]

    assert request["request_id"] == manifest["request_id"] == decision["request_id"]
    assert request["patch_sha256"] == decision["patch_sha256"] == audit["candidate_patch"]["sha256"]
    assert request["task_id"] == audit["task_id"] == assessment["task_id"]
    assert assessment["session_id"] == audit["session_id"]
    assert decision["package_manifest_sha256"] == audit["approval_package_manifest"]["sha256"]
    assert decision["decision"] == audit["decision_record"]["decision"] == assessment["decision"] == "reject"
    assert decision["actor"] == audit["decision_record"]["actor"] == assessment["actor"]
    assert decision["decided_at"] == audit["decision_record"]["decided_at"] == assessment["reject_completed_at"]
    assert decision["application_sha256"] is None
    assert audit["runtime_status_before_closeout"] == "approval_pending"
    assert audit["runtime_status_after_closeout"] == "rejected"
    assert audit["candidate_applied"] is False
    assert audit["candidate_cleaned"] is True
    assert assessment["approval_action"] == "rejected_during_release_audit_closeout"
    assert "did not occur during the original baseline run" in decision["note"]
