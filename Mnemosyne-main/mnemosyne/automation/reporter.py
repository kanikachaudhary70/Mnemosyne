"""
Mnemosyne Automation - Reporter
Produces structured human-readable and machine-readable scan, fix, and audit reports.
"""
from typing import Dict, Any, List
from mnemosyne.automation.models import AutomationJob, ScanFinding, VerificationResult


class AutomationReporter:
    """Formats automation outcomes into structured audit entries and terminal reports."""

    @staticmethod
    def format_job_summary(job: AutomationJob) -> Dict[str, Any]:
        """Return clean dictionary summary for JSON APIs and CLI."""
        return {
            "job_id": job.job_id,
            "status": job.status.value,
            "current_step": job.current_step,
            "progress_percent": job.progress_percent,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "total_findings": len(job.findings),
            "findings": [f.model_dump() for f in job.findings],
            "patches_count": len(job.patches),
            "patches": [
                {
                    "file_path": p.file_path,
                    "diff": p.diff_unified,
                    "fix_type": p.fix_type,
                }
                for p in job.patches
            ],
            "verification": job.verification.model_dump() if job.verification else None,
            "github": job.github_result.model_dump() if job.github_result else None,
            "memory_updated": job.memory_updated,
            "error": job.error,
        }

    @staticmethod
    def format_console_text(job: AutomationJob) -> str:
        """Return formatted plaintext summary for terminal output."""
        lines = [
            f"=== Mnemosyne Automation Job: {job.job_id} ===",
            f"Status: {job.status.value.upper()} ({job.progress_percent}%)",
            f"Current Step: {job.current_step}",
            f"Findings Detected: {len(job.findings)}",
        ]
        for idx, f in enumerate(job.findings, 1):
            lines.append(f"  {idx}. [{f.severity.value.upper()}] {f.file_path}:{f.line_number or 1} - {f.description}")
            if f.matching_memory_ids:
                lines.append(f"     Historical match: {', '.join(f.matching_memory_ids)}")

        if job.patches:
            lines.append(f"\nApplied Patches: {len(job.patches)}")
            for p in job.patches:
                lines.append(f"  - {p.file_path} ({p.fix_type})")

        if job.verification:
            lines.append(f"\nVerification: {job.verification.status.value.upper()}")
            lines.append(f"Message: {job.verification.message}")

        if job.github_result and job.github_result.status != "pending":
            lines.append(f"\nGitHub: {job.github_result.status.upper()}")
            if job.github_result.pr_url:
                lines.append(f"Pull Request: {job.github_result.pr_url}")
            elif job.github_result.error_message:
                lines.append(f"Notice: {job.github_result.error_message}")

        return "\n".join(lines)
