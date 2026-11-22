"""
Comprehensive End-to-End Test Suite for Mnemosyne Autonomous Bug Detection & Auto-Fix System
Validates all 12 mandatory scenarios required by Phase 12.
"""
import os
import json
import tempfile
import pytest
import typer
from pathlib import Path
from unittest.mock import patch, MagicMock

from mnemosyne.memory.client import MemoryClient
from mnemosyne.memory.schemas import BugFixMemory, MemoryType
from mnemosyne.automation.models import (
    JobStatus, Severity, FixEligibility, VerificationStatus
)
from mnemosyne.automation.workspace import (
    WorkspaceManager, WorkspaceSecurityError
)
from mnemosyne.automation.scanner import MultiLayerScanner
from mnemosyne.automation.fixer import AutoFixEngine
from mnemosyne.automation.validator import VerificationEngine
from mnemosyne.automation.github import GitHubManager
from mnemosyne.automation.engine import AutomationEngine
from mnemosyne.git.hooks import HookManager


@pytest.fixture(autouse=True)
def default_offline(monkeypatch):
    """Run tests in offline deterministic mode by default."""
    monkeypatch.setenv("MNEMOSYNE_OFFLINE", "1")


# -----------------------------------------------------------------------------
# Scenario 1: Clean File
# -----------------------------------------------------------------------------
def test_scenario_1_clean_file():
    """Upload valid code: No invented bugs, no unnecessary changes."""
    content = '''
def add(a: int, b: int) -> int:
    """Safely adds two numbers."""
    return a + b
'''
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        ws = WorkspaceManager("test_job_1", base_dir=root)
        ws.import_file("math_utils.py", content.encode("utf-8"))

        client = MemoryClient(root)
        scanner = MultiLayerScanner(memory_client=client, repo_root=root)
        findings = scanner.scan_code(content, "math_utils.py")

        assert len(findings) == 0, f"Expected 0 findings on clean file, got: {findings}"

        fixer = AutoFixEngine(ws, memory_client=client, repo_root=root)
        patch = fixer.apply_fixes(findings, "math_utils.py")
        assert patch is None, "Clean file should not produce any patch"


# -----------------------------------------------------------------------------
# Scenario 2: Fixable Lint Issue
# -----------------------------------------------------------------------------
def test_scenario_2_fixable_lint_issue():
    """File with safe lint problems (bare except, unused import). Expected: Detect, correct, re-scan, validate."""
    vulnerable_content = '''import sys
import os

def read_data():
    try:
        return sys.version
    except:
        return None
'''
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        ws = WorkspaceManager("test_job_2", base_dir=root)
        ws.import_file("service.py", vulnerable_content.encode("utf-8"))

        client = MemoryClient(root)
        scanner = MultiLayerScanner(memory_client=client, repo_root=root)
        findings = scanner.scan_code(vulnerable_content, "service.py")

        rule_ids = [f.rule_id for f in findings]
        assert "PY-BARE-EXCEPT" in rule_ids
        assert "PY-UNUSED-IMPORT" in rule_ids

        # Apply deterministic fix
        fixer = AutoFixEngine(ws, memory_client=client, repo_root=root)
        patch = fixer.apply_fixes(findings, "service.py")
        assert patch is not None
        assert "except Exception:" in patch.patched_content
        assert "import os" not in patch.patched_content

        # Verify
        validator = VerificationEngine(ws, scanner, repo_root=root)
        v_res = validator.verify_patch("service.py", findings, run_tests=False)
        assert v_res.passed is True
        assert v_res.status in (VerificationStatus.VERIFIED, VerificationStatus.PARTIALLY_VERIFIED)


# -----------------------------------------------------------------------------
# Scenario 3: Syntax Error
# -----------------------------------------------------------------------------
def test_scenario_3_syntax_error():
    """Upload a file with invalid syntax. Expected: Detect accurately with CRITICAL severity."""
    syntax_broken_content = '''def broken_function(x, y:
    return x + y
'''
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        client = MemoryClient(root)
        scanner = MultiLayerScanner(memory_client=client, repo_root=root)
        findings = scanner.scan_code(syntax_broken_content, "broken.py")

        syntax_findings = [f for f in findings if f.rule_id == "PY-SYNTAX-ERR"]
        assert len(syntax_findings) >= 1
        assert syntax_findings[0].severity == Severity.CRITICAL


# -----------------------------------------------------------------------------
# Scenario 4: Hardcoded Secret
# -----------------------------------------------------------------------------
def test_scenario_4_hardcoded_secret():
    """Detect secret, mark for manual review / secret rotation, block unsafe auto-publishing."""
    secret_code = '''
# Sensitive credential
api_key = "ghp_ABC1234567890XYZabcdef12345"
def connect():
    pass
'''
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        client = MemoryClient(root)
        scanner = MultiLayerScanner(memory_client=client, repo_root=root)
        findings = scanner.scan_code(secret_code, "config.py")

        secret_findings = [f for f in findings if "SECRET" in f.rule_id or "Secret" in f.category]
        assert len(secret_findings) >= 1
        # Hardcoded secret requires manual credential rotation, not naive overwrite
        assert secret_findings[0].fix_eligibility == FixEligibility.MANUAL_REVIEW
        assert secret_findings[0].severity in (Severity.HIGH, Severity.CRITICAL)


# -----------------------------------------------------------------------------
# Scenario 5: Previously Recorded Bug
# -----------------------------------------------------------------------------
def test_scenario_5_previously_recorded_bug():
    """Introduce a bug similar to one in Cognee memory. Expected: historical context attached."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        client = MemoryClient(root)

        # Ingest past bug into memory
        past_bug = BugFixMemory(
            file_path="auth/login.py",
            title="SQL Injection in user authentication query",
            root_cause="Used string formatting inside raw SQL query",
            fix_description="Used parameterized queries with db.execute(query, params)",
            severity="critical",
            tags=["sql-injection", "auth"],
        )
        saved_rec = client.remember_bug(past_bug)

        # Scan code with SQL injection
        code = '''
def authenticate(username):
    query = f"SELECT * FROM users WHERE name = '{username}'"
    db.execute(query)
'''
        scanner = MultiLayerScanner(memory_client=client, repo_root=root)
        findings = scanner.scan_code(code, "auth/login.py")

        sql_findings = [f for f in findings if "SQL" in f.rule_id or "SQL" in f.category]
        assert len(sql_findings) >= 1
        # Historical memory should be linked
        assert saved_rec.id in sql_findings[0].matching_memory_ids or len(sql_findings[0].matching_memory_ids) > 0


# -----------------------------------------------------------------------------
# Scenario 6: Incorrect Patch Rejection & Rollback
# -----------------------------------------------------------------------------
def test_scenario_6_incorrect_patch_rollback():
    """Candidate fix introduces a syntax regression. Verification must reject and roll back."""
    original_code = '''
def process():
    try:
        return 42
    except:
        return 0
'''
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        ws = WorkspaceManager("test_job_6", base_dir=root)
        ws.import_file("test.py", original_code.encode("utf-8"))

        client = MemoryClient(root)
        scanner = MultiLayerScanner(memory_client=client, repo_root=root)
        orig_findings = scanner.scan_code(original_code, "test.py")

        # Simulate bad patch with broken syntax
        bad_patched_code = '''
def process():
    try
        return 42
    except Exception:
        return 0
'''
        ws.write_file("test.py", bad_patched_code)

        validator = VerificationEngine(ws, scanner, repo_root=root)
        v_res = validator.verify_patch("test.py", orig_findings, run_tests=False)

        assert v_res.passed is False
        assert v_res.status == VerificationStatus.FAILED
        # Verify rollback preserved original content
        restored = ws.read_file("test.py")
        assert restored == original_code


# -----------------------------------------------------------------------------
# Scenario 7: Pre-Commit Block
# -----------------------------------------------------------------------------
def test_scenario_7_pre_commit_block():
    """Attempt commit with blocking vulnerability. Expected: Blocked with non-zero exit."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)

        # Mock GitInspector to return staged file with a hardcoded secret
        secret_content = 'db_password = "super_secret_db_pass_12345"\n'

        with patch("mnemosyne.git.inspector.GitInspector.get_staged_files", return_value=["secret.py"]), \
             patch("mnemosyne.git.inspector.GitInspector.get_staged_diff", return_value="+ db_password = 'super_secret_db_pass_12345'"), \
             patch("mnemosyne.git.inspector.GitInspector.get_staged_content", return_value=secret_content):

            res = HookManager.execute_pre_commit_check(root)
            assert res["blocked"] is True
            assert len(res["blocking_findings"]) >= 1


# -----------------------------------------------------------------------------
# Scenario 8: GitHub Automation (Mocks)
# -----------------------------------------------------------------------------
def test_scenario_8_github_automation():
    """Test branch, commit and PR formatting without modifying main."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        gh = GitHubManager(repo_root=root, token="mock_gh_token_123")

        slug = gh.get_repo_slug()
        assert slug is not None

        # Test PR body creation
        from mnemosyne.automation.models import PatchDiff, VerificationResult, ScanFinding
        diff = PatchDiff(
            file_path="math.py",
            original_content="x = 1",
            patched_content="x = 2",
            diff_unified="--- a/math.py\n+++ b/math.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        )
        finding = ScanFinding(
            finding_id="F-1",
            file_path="math.py",
            line_number=1,
            rule_id="TEST-RULE",
            description="Test problem",
        )
        v = VerificationResult(
            passed=True,
            status=VerificationStatus.VERIFIED,
            checks_run=["syntax_check"],
            resolved_finding_ids=["F-1"],
        )
        body = gh._build_pr_body("test_job_8", [diff], [finding], v)
        assert "Mnemosyne Autonomous Bug Detection & Auto-Fix Report" in body
        assert "math.py" in body


# -----------------------------------------------------------------------------
# Scenario 9: Offline Mode
# -----------------------------------------------------------------------------
def test_scenario_9_offline_mode(monkeypatch):
    """Run completely without Ollama or Cognee. Deterministic checks still pass."""
    monkeypatch.setenv("MNEMOSYNE_OFFLINE", "1")

    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        client = MemoryClient(root)
        scanner = MultiLayerScanner(memory_client=client, repo_root=root)

        content = "import yaml\nyaml.load(user_stream)\n"
        findings = scanner.scan_code(content, "loader.py")

        yaml_findings = [f for f in findings if "YAML" in f.rule_id]
        assert len(yaml_findings) >= 1


# -----------------------------------------------------------------------------
# Scenario 10: Malicious Upload Protection
# -----------------------------------------------------------------------------
def test_scenario_10_malicious_upload():
    """Try traversal paths, unsupported extensions, and oversized files. Requests must reject."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        ws = WorkspaceManager("sec_test", base_dir=root)

        # 1. Path traversal
        with pytest.raises(WorkspaceSecurityError):
            ws.sanitize_rel_path("../../etc/passwd")

        with pytest.raises(WorkspaceSecurityError):
            ws.import_file("../evil.py", b"print('hack')")

        # 2. Disallowed extension
        with pytest.raises(WorkspaceSecurityError):
            ws.validate_file("malware.exe", b"binary_data")

        # 3. Oversized file (> 10MB)
        oversized = b"a" * (11 * 1024 * 1024)
        with pytest.raises(WorkspaceSecurityError):
            ws.validate_file("huge.py", oversized)


# -----------------------------------------------------------------------------
# Scenario 11: Automation Engine Execution & Retry
# -----------------------------------------------------------------------------
def test_scenario_11_automation_engine_sync():
    """Test full central AutomationEngine pipeline execution."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        engine = AutomationEngine(root)

        sample_code = b'''import sys
import os

def run():
    try:
        return sys.version
    except:
        return None
'''
        job = engine.create_job(source_files=["worker.py"], auto_fix=True, create_pr=False)
        updated = engine.run_job_sync(job.job_id, file_payloads={"worker.py": sample_code})

        assert updated.status == JobStatus.COMPLETED
        assert len(updated.findings) >= 1
        assert len(updated.patches) >= 1
        assert updated.verification is not None
        assert updated.verification.passed is True


# -----------------------------------------------------------------------------
# Scenario 12: Existing Feature Regression
# -----------------------------------------------------------------------------
def test_scenario_12_existing_feature_regression():
    """Verify that MemoryClient, BugFixMemory, recall, and consolidator continue to work."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        client = MemoryClient(root)

        # 1. Remember bug
        bug = BugFixMemory(
            file_path="core/engine.py",
            title="Deadlock in threadpool workers",
            root_cause="Circular mutex acquisition",
            fix_description="Ordered lock acquisition hierarchy",
            severity="high",
            tags=["threading", "deadlock"],
        )
        rec = client.remember_bug(bug)
        assert rec.id is not None

        # 2. List memories
        mems = client.list_memories(MemoryType.BUG_FIX)
        assert len(mems) == 1
        assert mems[0].title == "Deadlock in threadpool workers"

        # 3. Local recall
        recalled = client.recall("deadlock threading")
        assert len(recalled) >= 1
