import hashlib
import inspect
import json
import stat
import subprocess
from pathlib import Path

import pytest

from fastfix.approval.actions import ApprovalActionError, ApprovalActionManager
from fastfix.approval.models import ApplicationRecord, ApprovalDecision, DecisionRecord, RollbackRecord
from fastfix.workspace.candidate import CandidateWorkspace, CandidateWorkspaceError, CandidateWorkspaceManager
from tests.fastfix.approval.test_package import REQUEST_ID, FixedIdManager, repository, validated_state


def git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )


def hashes(path: Path) -> dict[str, str]:
    return {
        file.relative_to(path).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in path.rglob("*")
        if file.is_file()
    }


@pytest.fixture
def action_case(tmp_path: Path):
    source = repository(tmp_path / "source repository")
    candidates = tmp_path / "candidate repositories"
    packages = tmp_path / "approval packages"
    actions = tmp_path / "approval actions"
    candidates.mkdir()
    packages.mkdir()
    actions.mkdir()
    candidate = CandidateWorkspaceManager(candidates).create(
        source,
        target=candidates / "candidate repository",
    )
    (candidate.path / "app" / "main.py").write_text("value = 2\n", encoding="utf-8")
    package_manager = FixedIdManager(packages)
    package = package_manager.create(
        task_id="FF-001",
        source=source,
        candidate=candidate.path,
        source_head=candidate.source_head,
        repair_state=validated_state(),
    )
    yield source, candidate, package, ApprovalActionManager(actions, package_manager=package_manager), actions
    candidate.cleanup()


def approval(package: Path, *, decision: str = "approve", request_id: str = REQUEST_ID, patch_hash: str | None = None):
    request = json.loads((package / "approval-request.json").read_text(encoding="utf-8"))
    return ApprovalDecision(
        decision=decision,
        request_id=request_id,
        expected_patch_sha256=patch_hash if patch_hash is not None else request["patch_sha256"],
        actor="fastfix-reviewer",
        note="reviewed",
    )


def test_approve_applies_unstaged_patch_and_writes_hashed_audit(action_case) -> None:
    source, candidate, package, manager, actions = action_case
    head = git(source, "rev-parse", "HEAD").stdout.strip()
    result = manager.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    assert result.status == "approved" and result.cleanup_warning is None
    assert not candidate.path.exists()
    assert git(source, "rev-parse", "HEAD").stdout.strip() == head
    assert git(source, "diff", "--cached", "--name-only").stdout == ""
    assert git(source, "ls-files", "--others", "--exclude-standard").stdout == ""
    assert git(source, "diff", "--name-only").stdout.strip() == "app/main.py"
    action = actions / REQUEST_ID
    decision = DecisionRecord.model_validate_json((action / "decision.json").read_text(encoding="utf-8"))
    application_bytes = (action / "application.json").read_bytes()
    application = ApplicationRecord.model_validate_json(application_bytes)
    reverse = (action / "reverse.patch").read_bytes()
    assert decision.application_sha256 == hashlib.sha256(application_bytes).hexdigest()
    assert application.reverse_patch_sha256 == hashlib.sha256(reverse).hexdigest()
    assert application.source_head_before == application.source_head_after == head
    assert application.changed_files == ["app/main.py"]
    manager._apply(source, reverse.decode("utf-8"), check=True)


def test_rollback_restores_clean_source_and_records_action(action_case) -> None:
    source, candidate, package, manager, actions = action_case
    manager.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    result = manager.rollback(
        package=package,
        source=source,
        request_id=REQUEST_ID,
        actor="fastfix-reviewer",
        note="rollback-approved",
    )
    assert result.status == "rolled_back"
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    record = RollbackRecord.model_validate_json((actions / REQUEST_ID / "rollback.json").read_text(encoding="utf-8"))
    assert record.request_id == REQUEST_ID and record.source_head_before == record.source_head_after


def test_reject_preserves_source_and_cleans_candidate(action_case) -> None:
    source, candidate, package, manager, actions = action_case
    before = git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout
    result = manager.decide(
        package=package,
        source=source,
        candidate=candidate,
        decision=approval(package, decision="reject"),
    )
    assert result.status == "rejected" and not candidate.path.exists()
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout == before
    assert {path.name for path in (actions / REQUEST_ID).iterdir()} == {"decision.json"}


def test_missing_decision_and_wrong_identity_cannot_apply(action_case) -> None:
    source, candidate, package, manager, _ = action_case
    with pytest.raises(ApprovalActionError) as missing:
        manager.decide(package=package, source=source, candidate=candidate, decision=None)
    with pytest.raises(ApprovalActionError) as request:
        manager.decide(
            package=package,
            source=source,
            candidate=candidate,
            decision=approval(package, request_id="00000000-0000-4000-8000-000000000000"),
        )
    with pytest.raises(ApprovalActionError) as patch:
        manager.decide(
            package=package,
            source=source,
            candidate=candidate,
            decision=approval(package, patch_hash="0" * 64),
        )
    assert {missing.value.code, request.value.code, patch.value.code} == {"approval_invalid"}
    assert git(source, "status", "--porcelain").stdout == ""


def test_tampered_package_is_rejected(action_case) -> None:
    source, candidate, package, manager, _ = action_case
    patch = package / "patch.diff"
    patch.chmod(patch.stat().st_mode | stat.S_IWRITE)
    patch.write_bytes(patch.read_bytes() + b"x")
    with pytest.raises(ApprovalActionError) as error:
        manager.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    assert error.value.code == "approval_package_invalid"
    assert git(source, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("change", ["dirty", "head"])
def test_dirty_or_changed_head_source_is_rejected(action_case, change: str) -> None:
    source, candidate, package, manager, _ = action_case
    if change == "dirty":
        (source / "app" / "main.py").write_text("dirty = True\n", encoding="utf-8")
    else:
        (source / "app" / "new.py").write_text("new = True\n", encoding="utf-8")
        git(source, "add", "app/new.py")
        git(source, "commit", "-q", "-m", "source moved")
    with pytest.raises(ApprovalActionError) as error:
        manager.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    assert error.value.code == "source_changed"


class CheckFailManager(ApprovalActionManager):
    def _apply(self, source: Path, patch: str, *, reverse: bool = False, check: bool = False) -> None:
        if check and not reverse:
            raise ApprovalActionError("patch_check_failed", "injected")
        super()._apply(source, patch, reverse=reverse, check=check)


def test_patch_check_failure_leaves_source_clean(action_case) -> None:
    source, candidate, package, manager, actions = action_case
    failing = CheckFailManager(actions, package_manager=manager.package_manager)
    with pytest.raises(ApprovalActionError) as error:
        failing.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    assert error.value.code == "patch_check_failed"
    assert git(source, "status", "--porcelain").stdout == "" and list(actions.iterdir()) == []


class VerificationFailManager(ApprovalActionManager):
    def _live_patch(self, source: Path) -> str:
        value = super()._live_patch(source)
        return value + "mismatch" if value else value


def test_apply_verification_failure_automatically_restores_source(action_case) -> None:
    source, candidate, package, manager, actions = action_case
    failing = VerificationFailManager(actions, package_manager=manager.package_manager)
    with pytest.raises(ApprovalActionError) as error:
        failing.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    assert error.value.code == "apply_verification_failed"
    assert git(source, "status", "--porcelain").stdout == "" and list(actions.iterdir()) == []


class AuditFailManager(ApprovalActionManager):
    writes = 0

    def _write_file(self, path: Path, content: bytes) -> None:
        self.writes += 1
        if self.writes == 2:
            raise OSError("injected audit failure")
        super()._write_file(path, content)


def test_audit_failure_automatically_restores_source(action_case) -> None:
    source, candidate, package, manager, actions = action_case
    failing = AuditFailManager(actions, package_manager=manager.package_manager)
    with pytest.raises(ApprovalActionError) as error:
        failing.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    assert error.value.code == "audit_write_failed", str(error.value)
    assert git(source, "status", "--porcelain").stdout == "" and list(actions.iterdir()) == []


@pytest.mark.parametrize(("first", "second"), [("approve", "approve"), ("approve", "reject"), ("reject", "approve")])
def test_decisions_are_final_and_cannot_be_repeated(action_case, first: str, second: str) -> None:
    source, candidate, package, manager, _ = action_case
    manager.decide(package=package, source=source, candidate=candidate, decision=approval(package, decision=first))
    with pytest.raises(ApprovalActionError) as error:
        manager.decide(package=package, source=source, candidate=candidate, decision=approval(package, decision=second))
    assert error.value.code == "approval_already_decided"


def test_rollback_rejects_additional_user_change(action_case) -> None:
    source, candidate, package, manager, _ = action_case
    manager.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    (source / "app" / "spare.py").write_text("user_change = True\n", encoding="utf-8")
    with pytest.raises(ApprovalActionError) as error:
        manager.rollback(package=package, source=source, request_id=REQUEST_ID, actor="reviewer")
    assert error.value.code == "rollback_not_safe"


@pytest.mark.parametrize("filename", ["reverse.patch", "application.json"])
def test_rollback_rejects_tampered_audit(action_case, filename: str) -> None:
    source, candidate, package, manager, actions = action_case
    manager.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    target = actions / REQUEST_ID / filename
    target.chmod(target.stat().st_mode | stat.S_IWRITE)
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(ApprovalActionError) as error:
        manager.rollback(package=package, source=source, request_id=REQUEST_ID, actor="reviewer")
    assert error.value.code == "rollback_not_safe"


def test_second_rollback_is_rejected(action_case) -> None:
    source, candidate, package, manager, _ = action_case
    manager.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    manager.rollback(package=package, source=source, request_id=REQUEST_ID, actor="reviewer")
    with pytest.raises(ApprovalActionError) as error:
        manager.rollback(package=package, source=source, request_id=REQUEST_ID, actor="reviewer")
    assert error.value.code == "rollback_not_safe"


class CleanupFailsOnce(CandidateWorkspaceManager):
    failed = False

    def cleanup(self, candidate: CandidateWorkspace) -> None:
        if not self.failed:
            self.failed = True
            raise CandidateWorkspaceError("cleanup_failed", "injected")
        super().cleanup(candidate)


def test_cleanup_failure_preserves_rejection_and_can_be_retried(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    candidates = tmp_path / "candidates"
    packages = tmp_path / "packages"
    actions = tmp_path / "actions"
    candidates.mkdir()
    packages.mkdir()
    actions.mkdir()
    candidate = CleanupFailsOnce(candidates).create(source)
    (candidate.path / "app" / "main.py").write_text("value = 2\n", encoding="utf-8")
    package_manager = FixedIdManager(packages)
    package = package_manager.create(
        task_id="FF-001",
        source=source,
        candidate=candidate.path,
        source_head=candidate.source_head,
        repair_state=validated_state(),
    )
    manager = ApprovalActionManager(actions, package_manager=package_manager)
    result = manager.decide(
        package=package,
        source=source,
        candidate=candidate,
        decision=approval(package, decision="reject"),
    )
    assert result.cleanup_warning and candidate.path.exists()
    assert (actions / REQUEST_ID / "decision.json").is_file()
    assert git(source, "status", "--porcelain").stdout == ""
    manager.retry_candidate_cleanup(
        request_id=REQUEST_ID,
        package=package,
        source=source,
        candidate=candidate,
    )
    assert not candidate.path.exists()


def test_package_hashes_and_space_paths_are_preserved(action_case) -> None:
    source, candidate, package, manager, _ = action_case
    before = hashes(package)
    manager.decide(package=package, source=source, candidate=candidate, decision=approval(package))
    assert hashes(package) == before


def test_git_execution_has_no_shell_or_destructive_commands() -> None:
    source = inspect.getsource(ApprovalActionManager)
    assert "shell=False" in source
    assert '"reset"' not in source and '"clean"' not in source and '"checkout"' not in source
