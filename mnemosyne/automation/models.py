"""
Mnemosyne Automation Package - Core Models and Schemas
Typed contracts for jobs, multi-layer scan findings, verification, and GitHub actions.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


class JobStatus(str, Enum):
    QUEUED = "queued"
    SCANNING = "scanning"
    GENERATING_FIXES = "generating_fixes"
    APPLYING = "applying"
    VERIFYING = "verifying"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    REVIEW_REQUIRED = "review_required"
    CANCELLED = "cancelled"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DetectorSource(str, Enum):
    SYNTAX = "syntax"
    STATIC_LINT = "static_lint"
    SECURITY = "security"
    MEMORY_RECALL = "memory_recall"
    AI_ANALYSIS = "ai_analysis"


class FixEligibility(str, Enum):
    AUTO_FIXABLE = "auto_fixable"
    MANUAL_REVIEW = "manual_review"
    UNFIXABLE = "unfixable"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    PARTIALLY_VERIFIED = "partially_verified"
    FAILED = "failed"
    REGRESSION = "regression"


class ScanFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    finding_id: str = Field(..., description="Unique deterministic or UUID finding ID")
    file_path: str = Field(..., description="Target file path relative to workspace or repository")
    line_number: Optional[int] = Field(None, description="Starting line number")
    end_line_number: Optional[int] = Field(None, description="Ending line number if range")
    rule_id: str = Field(..., description="Rule or check identifier e.g. SEC-001, RUFF-E501")
    severity: Severity = Field(Severity.MEDIUM, description="Severity: low, medium, high, critical")
    category: str = Field("General", description="Bug/vulnerability category e.g. SQL Injection, Unused Import")
    description: str = Field(..., description="Clear explanation of the problem")
    evidence: Optional[str] = Field(None, description="Offending code snippet or AST context")
    detector: DetectorSource = Field(DetectorSource.SECURITY, description="Analyzer layer that generated this finding")
    matching_memory_ids: List[str] = Field(default_factory=list, description="IDs of historical Cognee/JSON memories")
    suggested_fix: Optional[str] = Field(None, description="Human or AI suggested fix guidance")
    confidence: float = Field(1.0, description="Confidence score from 0.0 to 1.0")
    fix_eligibility: FixEligibility = Field(FixEligibility.AUTO_FIXABLE, description="Eligibility for automated patching")
    verification_status: VerificationStatus = Field(VerificationStatus.UNVERIFIED, description="Current verification state")
    applied_fix: Optional[str] = Field(None, description="Summary of applied fix if any")


class PatchDiff(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_path: str
    original_content: str
    patched_content: str
    diff_unified: str
    is_safe: bool = True
    fix_type: str = "deterministic"  # deterministic | ai_patch | historical_pattern

    @property
    def diff(self) -> str:
        return self.diff_unified


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    passed: bool = False
    status: VerificationStatus = VerificationStatus.UNVERIFIED
    checks_run: List[str] = Field(default_factory=list)
    syntax_valid: bool = False
    security_clean: bool = False
    tests_passed: bool = True
    test_output: Optional[str] = None
    resolved_finding_ids: List[str] = Field(default_factory=list)
    remaining_finding_ids: List[str] = Field(default_factory=list)
    new_findings: List[ScanFinding] = Field(default_factory=list)
    retry_count: int = 0
    message: str = ""


class GitHubPRResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    branch_name: Optional[str] = None
    commit_hash: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    status: str = "pending"  # pending | published | skipped | auth_required | error
    error_message: Optional[str] = None


class AutomationJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    status: JobStatus = JobStatus.QUEUED
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_files: List[str] = Field(default_factory=list)
    workspace_path: Optional[str] = None
    auto_fix: bool = True
    create_pr: bool = False
    repo_name: Optional[str] = None
    target_branch: str = "main"
    current_step: str = "Job queued"
    progress_percent: int = 0
    findings: List[ScanFinding] = Field(default_factory=list)
    patches: List[PatchDiff] = Field(default_factory=list)
    verification: Optional[VerificationResult] = None
    github_result: Optional[GitHubPRResult] = None
    memory_updated: bool = False
    error: Optional[str] = None

    def update_step(self, step: str, progress: int, status: Optional[JobStatus] = None) -> None:
        self.current_step = step
        self.progress_percent = max(0, min(100, progress))
        if status:
            self.status = status
        self.updated_at = datetime.now(timezone.utc).isoformat()
