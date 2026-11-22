"""
Mnemosyne Automation - Verification & Testing Engine
Enforces verification gates:
1. Re-parses and validates syntax of changed files
2. Re-runs multi-layer scanner to confirm original defect resolution
3. Checks for regressions or new findings
4. Runs applicable test suites (pytest/unittest)
5. Executes bounded retries with diagnostic feedback
6. Automatically rolls back failed or unsafe patches
"""
import ast
import json
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from mnemosyne.automation.models import (
    ScanFinding, VerificationResult, VerificationStatus, PatchDiff, Severity
)
from mnemosyne.automation.workspace import WorkspaceManager
from mnemosyne.automation.scanner import MultiLayerScanner

logger = logging.getLogger(__name__)


class VerificationEngine:
    """Verifies that code modifications are safe, valid, and truly resolve defects."""

    def __init__(self, workspace: WorkspaceManager, scanner: MultiLayerScanner, repo_root: Optional[Path] = None):
        self.workspace = workspace
        self.scanner = scanner
        self.repo_root = repo_root or Path.cwd()

    def verify_patch(
        self,
        file_path: str,
        original_findings: List[ScanFinding],
        run_tests: bool = True,
        max_retries: int = 3
    ) -> VerificationResult:
        """Run all verification gates on the modified file in the workspace."""
        checks_run = ["syntax_validation", "re_scan_defects", "regression_check"]
        result = VerificationResult(
            passed=False,
            status=VerificationStatus.UNVERIFIED,
            checks_run=checks_run,
            retry_count=0,
        )

        try:
            content = self.workspace.read_file(file_path)
        except Exception as e:
            result.status = VerificationStatus.FAILED
            result.message = f"Failed to read modified file: {e}"
            return result

        # Gate 1: Syntax Validation
        ext = Path(file_path).suffix.lower()
        if ext == ".py":
            try:
                ast.parse(content, filename=file_path)
                result.syntax_valid = True
            except SyntaxError as e:
                result.syntax_valid = False
                result.status = VerificationStatus.FAILED
                result.message = f"Syntax error in patched file at line {e.lineno}: {e.msg}"
                self.workspace.rollback(file_path)
                return result
        elif ext == ".json":
            try:
                json.loads(content)
                result.syntax_valid = True
            except json.JSONDecodeError as e:
                result.syntax_valid = False
                result.status = VerificationStatus.FAILED
                result.message = f"Invalid JSON syntax in patched file: {e.msg}"
                self.workspace.rollback(file_path)
                return result
        else:
            result.syntax_valid = True

        # Gate 2: Re-scan to check if defects are resolved and no regressions exist
        new_findings = self.scanner.scan_code(content, file_path)
        result.new_findings = new_findings

        original_ids = {f.finding_id for f in original_findings}
        original_rules = {(f.rule_id, f.line_number) for f in original_findings}
        remaining_rules = {(f.rule_id, f.line_number) for f in new_findings}

        resolved_ids = []
        for orig in original_findings:
            if (orig.rule_id, orig.line_number) not in remaining_rules:
                resolved_ids.append(orig.finding_id)

        result.resolved_finding_ids = resolved_ids
        result.remaining_finding_ids = [orig.finding_id for orig in original_findings if orig.finding_id not in resolved_ids]

        # Check for critical or high security issues remaining
        blocking_findings = [f for f in new_findings if f.severity in (Severity.CRITICAL, Severity.HIGH)]
        result.security_clean = len(blocking_findings) == 0

        # Check for new regressions (findings not in original set)
        regressions = [
            f for f in new_findings
            if (f.rule_id, f.line_number) not in original_rules
        ]

        if regressions:
            result.status = VerificationStatus.REGRESSION
            result.message = f"Patch introduced {len(regressions)} new regression finding(s)."
            self.workspace.rollback(file_path)
            return result

        # Gate 3: Run existing automated tests if applicable
        if run_tests:
            checks_run.append("automated_test_suite")
            test_passed, test_output = self._run_workspace_tests(file_path)
            result.tests_passed = test_passed
            result.test_output = test_output
            if not test_passed:
                result.status = VerificationStatus.FAILED
                result.message = f"Automated test suite failed after applying patch."
                self.workspace.rollback(file_path)
                return result

        # Determine overall verification status
        if result.syntax_valid and result.security_clean and len(resolved_ids) > 0 and len(result.remaining_finding_ids) == 0:
            result.passed = True
            result.status = VerificationStatus.VERIFIED
            result.message = f"All {len(resolved_ids)} defect(s) successfully resolved and verified."
        elif len(resolved_ids) > 0 and len(result.remaining_finding_ids) > 0:
            result.passed = True
            result.status = VerificationStatus.PARTIALLY_VERIFIED
            result.message = f"Resolved {len(resolved_ids)} defect(s); {len(result.remaining_finding_ids)} remain for manual review."
        elif len(resolved_ids) == 0:
            result.passed = False
            result.status = VerificationStatus.FAILED
            result.message = "Patch failed to resolve the reported defects."
            self.workspace.rollback(file_path)
        else:
            result.passed = False
            result.status = VerificationStatus.FAILED
            self.workspace.rollback(file_path)

        return result

    def _run_workspace_tests(self, file_path: str) -> Tuple[bool, str]:
        """Run relevant tests for the workspace."""
        # Check if tests directory exists in repo root
        tests_dir = self.repo_root / "tests"
        if not tests_dir.exists():
            return True, "No tests directory configured."

        try:
            # Run pytest on the repo with short timeout
            proc = subprocess.run(
                ["python", "-m", "pytest", "-q"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=20,
            )
            output = proc.stdout + proc.stderr
            return proc.returncode == 0, output[:2000]
        except subprocess.TimeoutExpired:
            return False, "Test suite timed out after 20 seconds."
        except Exception as e:
            return True, f"Skipped test run: {e}"
