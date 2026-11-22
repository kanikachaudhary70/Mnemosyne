"""
Mnemosyne Automation - Central Workflow Orchestrator (AutomationEngine)
Coordinates:
1. Intake into isolated WorkspaceManager
2. Multi-layer scanning (Syntax, Lint, Security, Memory, AI)
3. Hierarchical AutoFixEngine (Deterministic -> Historical -> AI)
4. VerificationEngine (Syntax validation, re-scan, tests, rollback)
5. GitHubManager (Dedicated branch, commit, PR creation)
6. Cognee Memory Ingestion (Auto-remembers verified fixes)
Persists jobs to .mnemosyne/automation_jobs.json and provides non-blocking lifecycle management.
"""
import os
import json
import uuid
import logging
import threading
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from concurrent.futures import ThreadPoolExecutor

from mnemosyne.automation.models import (
    AutomationJob, JobStatus, ScanFinding, PatchDiff, VerificationResult,
    VerificationStatus, GitHubPRResult
)
from mnemosyne.automation.workspace import WorkspaceManager
from mnemosyne.automation.scanner import MultiLayerScanner
from mnemosyne.automation.fixer import AutoFixEngine
from mnemosyne.automation.validator import VerificationEngine
from mnemosyne.automation.github import GitHubManager
from mnemosyne.memory.client import MemoryClient
from mnemosyne.memory.schemas import BugFixMemory
from mnemosyne.memory.consolidator import MemoryConsolidator
from mnemosyne.config import get_mnemosyne_dir, find_project_root

logger = logging.getLogger(__name__)


class AutomationEngine:
    """Central singleton service orchestrating the autonomous bug detection & fix pipeline."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, repo_root: Optional[Path] = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(AutomationEngine, cls).__new__(cls)
                cls._instance._init_engine(repo_root)
            return cls._instance

    def _init_engine(self, repo_root: Optional[Path] = None):
        self.repo_root = (repo_root or find_project_root()).resolve()
        self.mnemosyne_dir = get_mnemosyne_dir(self.repo_root)
        self.jobs_file = self.mnemosyne_dir / "automation_jobs.json"
        self._jobs: Dict[str, AutomationJob] = {}
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mnemosyne_job")
        self._load_jobs()

    def _load_jobs(self) -> None:
        if self.jobs_file.exists():
            try:
                data = json.loads(self.jobs_file.read_text(encoding="utf-8"))
                for item in data:
                    job = AutomationJob(**item)
                    self._jobs[job.job_id] = job
            except Exception as e:
                logger.debug(f"Failed to load jobs from {self.jobs_file}: {e}")

    def _save_jobs(self) -> None:
        try:
            data = [j.model_dump() for j in self._jobs.values()]
            self.jobs_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.debug(f"Failed to save jobs: {e}")

    def get_job(self, job_id: str) -> Optional[AutomationJob]:
        if job_id in self._jobs:
            return self._jobs[job_id]
        # Match by prefix (e.g. from truncated CLI tables)
        clean_prefix = job_id.rstrip("…").strip()
        matches = [j for j in self._jobs.values() if j.job_id.startswith(clean_prefix)]
        if matches:
            return matches[0]
        return None

    def list_jobs(self) -> List[AutomationJob]:
        return sorted(list(self._jobs.values()), key=lambda j: j.created_at, reverse=True)

    def create_job(
        self,
        source_files: List[str],
        auto_fix: bool = True,
        create_pr: bool = False,
        target_branch: str = "main",
    ) -> AutomationJob:
        """Create and queue a new automation job."""
        job_id = f"job_{uuid.uuid4().hex[:10]}"
        job = AutomationJob(
            job_id=job_id,
            status=JobStatus.QUEUED,
            source_files=source_files,
            auto_fix=auto_fix,
            create_pr=create_pr,
            target_branch=target_branch,
            current_step="Queued for analysis",
            progress_percent=5,
        )
        self._jobs[job_id] = job
        self._save_jobs()
        return job

    def run_job_async(self, job_id: str, file_payloads: Optional[Dict[str, bytes]] = None) -> None:
        """Dispatch job execution in background thread."""
        self._executor.submit(self.run_job_sync, job_id, file_payloads)

    def run_job_sync(self, job_id: str, file_payloads: Optional[Dict[str, bytes]] = None) -> AutomationJob:
        """Execute full automated lifecycle synchronously."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        workspace = WorkspaceManager(job_id, base_dir=self.repo_root)
        job.workspace_path = str(workspace.root)

        memory_client = MemoryClient(self.repo_root)
        scanner = MultiLayerScanner(memory_client=memory_client, repo_root=self.repo_root)
        fixer = AutoFixEngine(workspace, memory_client=memory_client, repo_root=self.repo_root)
        validator = VerificationEngine(workspace, scanner, repo_root=self.repo_root)
        github = GitHubManager(repo_root=self.repo_root)

        try:
            # 1. Intake Files
            job.update_step("Importing files into isolated sandbox", 15, JobStatus.SCANNING)
            self._save_jobs()

            rel_files = []
            if file_payloads:
                for rel_path, content_bytes in file_payloads.items():
                    workspace.import_file(rel_path, content_bytes)
                    rel_files.append(rel_path)
            else:
                for rel_path in job.source_files:
                    local_target = (self.repo_root / rel_path).resolve()
                    if local_target.exists():
                        workspace.copy_from_repo(local_target, rel_path)
                        rel_files.append(rel_path)

            job.source_files = rel_files

            # 2. Multi-layer Scanning
            job.update_step("Scanning code across 5 analysis layers", 30)
            self._save_jobs()

            all_findings: List[ScanFinding] = []
            for rf in rel_files:
                code = workspace.read_file(rf)
                findings = scanner.scan_code(code, rf)
                all_findings.extend(findings)

            job.findings = all_findings

            if not all_findings:
                job.update_step("Scan completed: Clean code, no bugs detected", 100, JobStatus.COMPLETED)
                self._save_jobs()
                return job

            # Check if any auto-fixable findings exist
            fixable = [f for f in all_findings if f.fix_eligibility == "auto_fixable"]
            if not job.auto_fix or not fixable:
                job.update_step(f"Scan complete: Found {len(all_findings)} issues (Manual review required)", 100, JobStatus.REVIEW_REQUIRED)
                self._save_jobs()
                return job

            # 3. Generate & Apply Fixes
            job.update_step(f"Generating candidate fixes for {len(fixable)} issue(s)", 50, JobStatus.GENERATING_FIXES)
            self._save_jobs()

            patches: List[PatchDiff] = []
            for rf in rel_files:
                file_findings = [f for f in all_findings if f.file_path == rf]
                patch = fixer.apply_fixes(file_findings, rf)
                if patch:
                    patches.append(patch)

            job.patches = patches
            job.update_step(f"Applied fixes across {len(patches)} file(s)", 65, JobStatus.VERIFYING)
            self._save_jobs()

            # 4. Verification
            job.update_step("Running automated verification and regression tests", 75)
            self._save_jobs()

            all_passed = True
            verification_summary = None
            for patch in patches:
                orig_file_findings = [f for f in all_findings if f.file_path == patch.file_path]
                res = validator.verify_patch(patch.file_path, orig_file_findings, run_tests=True)
                verification_summary = res
                if not res.passed:
                    all_passed = False

            job.verification = verification_summary

            if not all_passed:
                job.update_step("Verification failed: Rolled back candidate fixes", 100, JobStatus.FAILED)
                self._save_jobs()
                return job

            # 5. Ingest Verified Fixes into Cognee Memory
            job.update_step("Saving verified bug fixes into Cognee long-term memory", 85)
            self._save_jobs()

            try:
                for patch in patches:
                    rel_name = patch.file_path
                    resolved_findings = [
                        f for f in all_findings
                        if f.file_path == rel_name and (verification_summary and f.finding_id in verification_summary.resolved_finding_ids)
                    ]
                    for rf in resolved_findings:
                        bug_mem = BugFixMemory(
                            file_path=rf.file_path,
                            title=f"Auto-fixed: {rf.category} in {rf.file_path}",
                            root_cause=rf.description,
                            fix_description=rf.applied_fix or "Applied verified automated patch",
                            severity=rf.severity.value,
                            tags=["autofix", rf.rule_id.lower()],
                        )
                        memory_client.remember_bug(bug_mem)

                # Consolidate rules if threshold reached
                consolidator = MemoryConsolidator(memory_client)
                consolidator.reflect()
                job.memory_updated = True
            except Exception as e:
                logger.debug(f"Failed to record memory update: {e}")

            # 6. Publish to GitHub if requested
            if job.create_pr and patches:
                job.update_step("Creating dedicated automation branch and GitHub PR", 90, JobStatus.PUBLISHING)
                self._save_jobs()

                pr_res = github.publish_verified_fixes(
                    job_id=job_id,
                    patches=patches,
                    findings=all_findings,
                    verification=verification_summary,
                    target_branch=job.target_branch,
                )
                job.github_result = pr_res

            job.update_step("Autonomous detection, auto-fix, and verification completed", 100, JobStatus.COMPLETED)
            self._save_jobs()
            return job

        except Exception as e:
            logger.error(f"Job {job_id} encountered unexpected error: {e}", exc_info=True)
            job.error = str(e)
            job.update_step(f"Failed: {e}", job.progress_percent, JobStatus.FAILED)
            self._save_jobs()
            return job
