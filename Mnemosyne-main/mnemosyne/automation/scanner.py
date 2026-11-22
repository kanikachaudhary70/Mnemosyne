"""
Mnemosyne Automation - Multi-Layer Bug & Security Scanner
Orchestrates:
1. Syntax validation (AST, JSON, JS/TS parser)
2. Static code analysis (Ruff if present, deterministic AST/regex linter)
3. Advanced security vulnerability scanning (expanded patterns & heuristics)
4. Cognee historical bug & rule recall
5. AI-assisted analysis via configured Ollama/LLM model
Produces unified, deduplicated ScanFinding schemas with memory links.
"""
import os
import re
import ast
import json
import uuid
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from mnemosyne.automation.models import (
    ScanFinding, Severity, DetectorSource, FixEligibility, VerificationStatus
)
from mnemosyne.memory.client import MemoryClient
from mnemosyne.memory.schemas import MemoryType, MemoryRecord
from mnemosyne.utils.security_scanner import SecurityScanner


class MultiLayerScanner:
    """Multi-layer code analyzer combining static checks, security scanning, memory, and AI."""

    def __init__(self, memory_client: Optional[MemoryClient] = None, repo_root: Optional[Path] = None):
        self.repo_root = repo_root or Path.cwd()
        self.memory_client = memory_client or MemoryClient(self.repo_root)
        self.legacy_security_scanner = SecurityScanner(self.memory_client)

    def scan_code(self, content: str, file_path: str, is_diff: bool = False) -> List[ScanFinding]:
        """Execute all scanning layers on a file content or diff."""
        findings: List[ScanFinding] = []

        # Layer 1: Syntax Analysis
        if not is_diff:
            syntax_findings = self._check_syntax(content, file_path)
            findings.extend(syntax_findings)

        # Layer 2: Static Analysis (Ruff or AST/Regex fallback)
        static_findings = self._run_static_analysis(content, file_path, is_diff=is_diff)
        findings.extend(static_findings)

        # Layer 3: Security Scanning (Expanded heuristics)
        security_findings = self._run_security_scan(content, file_path, is_diff=is_diff)
        findings.extend(security_findings)

        # Layer 4: Cognee Historical Memory Recall & Association
        self._enrich_with_historical_memory(findings, content, file_path)

        # Layer 5: AI-assisted analysis (if enabled and not offline)
        if os.getenv("MNEMOSYNE_OFFLINE") != "1" and self._is_ai_available():
            ai_findings = self._run_ai_analysis(content, file_path, findings)
            findings.extend(ai_findings)

        # Deduplicate and sort by severity
        return self._deduplicate_findings(findings)

    # -------------------------------------------------------------------------
    # Layer 1: Syntax Analysis
    # -------------------------------------------------------------------------
    def _check_syntax(self, content: str, file_path: str) -> List[ScanFinding]:
        findings: List[ScanFinding] = []
        ext = Path(file_path).suffix.lower()

        if ext == ".py":
            try:
                ast.parse(content, filename=file_path)
            except SyntaxError as e:
                findings.append(ScanFinding(
                    finding_id=f"SYN-{uuid.uuid4().hex[:8]}",
                    file_path=file_path,
                    line_number=e.lineno or 1,
                    end_line_number=e.end_lineno or e.lineno or 1,
                    rule_id="PY-SYNTAX-ERR",
                    severity=Severity.CRITICAL,
                    category="Syntax Error",
                    description=f"Python syntax error: {e.msg}",
                    evidence=e.text.strip() if e.text else None,
                    detector=DetectorSource.SYNTAX,
                    suggested_fix="Fix syntax mistake before execution or compilation.",
                    confidence=1.0,
                    fix_eligibility=FixEligibility.AUTO_FIXABLE,
                ))
        elif ext == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as e:
                findings.append(ScanFinding(
                    finding_id=f"SYN-{uuid.uuid4().hex[:8]}",
                    file_path=file_path,
                    line_number=e.lineno,
                    rule_id="JSON-SYNTAX-ERR",
                    severity=Severity.HIGH,
                    category="Syntax Error",
                    description=f"Invalid JSON syntax: {e.msg} (col {e.colno})",
                    detector=DetectorSource.SYNTAX,
                    suggested_fix="Ensure valid JSON quotes, trailing commas removal, and balanced brackets.",
                    confidence=1.0,
                    fix_eligibility=FixEligibility.AUTO_FIXABLE,
                ))
        elif ext in [".js", ".ts", ".jsx", ".tsx"]:
            # Basic delimiter balance check for JS/TS
            stack = []
            pairs = {')': '(', '}': '{', ']': '['}
            lines = content.splitlines()
            for idx, line in enumerate(lines, 1):
                clean_line = re.sub(r'//.*$|/\*.*?\*/', '', line)
                for char in clean_line:
                    if char in "({[":
                        stack.append((char, idx))
                    elif char in ")}]":
                        if not stack or stack[-1][0] != pairs[char]:
                            findings.append(ScanFinding(
                                finding_id=f"SYN-{uuid.uuid4().hex[:8]}",
                                file_path=file_path,
                                line_number=idx,
                                rule_id="JS-UNBALANCED-DELIM",
                                severity=Severity.HIGH,
                                category="Syntax Error",
                                description=f"Unmatched closing bracket '{char}'",
                                detector=DetectorSource.SYNTAX,
                                confidence=0.9,
                                fix_eligibility=FixEligibility.MANUAL_REVIEW,
                            ))
                            break
                        else:
                            stack.pop()

        return findings

    # -------------------------------------------------------------------------
    # Layer 2: Static Analysis
    # -------------------------------------------------------------------------
    def _run_static_analysis(self, content: str, file_path: str, is_diff: bool = False) -> List[ScanFinding]:
        findings: List[ScanFinding] = []
        ext = Path(file_path).suffix.lower()

        # Check Python static issues using AST and rules
        if ext == ".py" and not is_diff:
            try:
                tree = ast.parse(content, filename=file_path)

                # Collect defined and used identifiers
                imports: Dict[str, Tuple[int, str]] = {}  # alias_name -> (line, import_type)
                used_names: set = set()

                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            name = alias.asname or alias.name
                            imports[name] = (node.lineno, f"import {alias.name}")
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            name = alias.asname or alias.name
                            imports[name] = (node.lineno, f"from {node.module} import {alias.name}")
                    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                        used_names.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        # E.g. os.path
                        if isinstance(node.value, ast.Name):
                            used_names.add(node.value.id)
                    elif isinstance(node, ast.ExceptHandler):
                        # Detect bare except
                        if node.type is None:
                            findings.append(ScanFinding(
                                finding_id=f"LINT-{uuid.uuid4().hex[:8]}",
                                file_path=file_path,
                                line_number=node.lineno,
                                rule_id="PY-BARE-EXCEPT",
                                severity=Severity.MEDIUM,
                                category="Code Quality",
                                description="Found bare 'except:' clause which catches system exit and keyboard interrupts.",
                                evidence="except:",
                                detector=DetectorSource.STATIC_LINT,
                                suggested_fix="Replace with 'except Exception:' to preserve system signals.",
                                confidence=0.95,
                                fix_eligibility=FixEligibility.AUTO_FIXABLE,
                            ))

                # Identify unused imports
                for name, (lineno, src) in imports.items():
                    if name not in used_names and name not in ["__all__"]:
                        findings.append(ScanFinding(
                            finding_id=f"LINT-{uuid.uuid4().hex[:8]}",
                            file_path=file_path,
                            line_number=lineno,
                            rule_id="PY-UNUSED-IMPORT",
                            severity=Severity.LOW,
                            category="Code Cleanliness",
                            description=f"Imported '{name}' is never used in file.",
                            evidence=src,
                            detector=DetectorSource.STATIC_LINT,
                            suggested_fix=f"Remove unused import '{name}'.",
                            confidence=0.9,
                            fix_eligibility=FixEligibility.AUTO_FIXABLE,
                        ))

            except Exception:
                pass

        # Try running external ruff if available
        ruff_exe = shutil.which("ruff")
        if ruff_exe and not is_diff and ext == ".py":
            try:
                proc = subprocess.run(
                    [ruff_exe, "check", "--output-format=json", "-"],
                    input=content,
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                if proc.stdout.strip():
                    ruff_json = json.loads(proc.stdout)
                    for item in ruff_json:
                        findings.append(ScanFinding(
                            finding_id=f"RUFF-{item.get('code', 'LINT')}-{uuid.uuid4().hex[:6]}",
                            file_path=file_path,
                            line_number=item.get("location", {}).get("row"),
                            rule_id=f"RUFF-{item.get('code')}",
                            severity=Severity.LOW if item.get("code", "").startswith("I") or item.get("code", "").startswith("F401") else Severity.MEDIUM,
                            category="Lint",
                            description=item.get("message", "Lint finding from Ruff"),
                            detector=DetectorSource.STATIC_LINT,
                            suggested_fix=item.get("fix", {}).get("applicability") if item.get("fix") else None,
                            confidence=0.95,
                            fix_eligibility=FixEligibility.AUTO_FIXABLE if item.get("fix") else FixEligibility.MANUAL_REVIEW,
                        ))
            except Exception:
                pass

        return findings

    # -------------------------------------------------------------------------
    # Layer 3: Security Scanning (Expanded Heuristics)
    # -------------------------------------------------------------------------
    def _run_security_scan(self, content: str, file_path: str, is_diff: bool = False) -> List[ScanFinding]:
        findings: List[ScanFinding] = []
        lines = content.splitlines()

        # Run legacy scanner for backward compatibility
        legacy_issues = (
            self.legacy_security_scanner.scan_diff(content)
            if is_diff else
            self.legacy_security_scanner._run_heuristic_scan(content, file_path)
        )
        for issue in legacy_issues:
            sev = Severity.HIGH
            if issue.severity.lower() == "critical":
                sev = Severity.CRITICAL
            elif issue.severity.lower() == "medium":
                sev = Severity.MEDIUM
            elif issue.severity.lower() == "low":
                sev = Severity.LOW

            # Determine fix eligibility: hardcoded secrets need rotation/env-vars
            fix_elig = FixEligibility.MANUAL_REVIEW if "secret" in issue.vulnerability_type.lower() else FixEligibility.AUTO_FIXABLE

            findings.append(ScanFinding(
                finding_id=f"SEC-{uuid.uuid4().hex[:8]}",
                file_path=file_path,
                line_number=issue.line_number,
                rule_id=f"SEC-{issue.vulnerability_type.replace(' ', '-').upper()}",
                severity=sev,
                category="Security Vulnerability",
                description=issue.description,
                detector=DetectorSource.SECURITY,
                suggested_fix=issue.proposed_fix,
                matching_memory_ids=[issue.matching_memory_id] if issue.matching_memory_id else [],
                confidence=0.95,
                fix_eligibility=fix_elig,
            ))

        # Additional Expanded Security Patterns:
        # 1. Path traversal risk: open(...) with unvalidated path joining or format
        path_traversal_re = re.compile(r'open\s*\(\s*(f["\'].*?\{.*?\}["\']|.*?\.format\(|.*?\+.*?\))\s*,')
        # 2. Insecure deserialization: pickle.loads, yaml.load without SafeLoader
        unsafe_pickle_re = re.compile(r'\b(pickle|cPickle|_pickle)\s*\.\s*(loads?|load)\b')
        unsafe_yaml_re = re.compile(r'\byaml\s*\.\s*load\s*\([^,)]+\)')
        # 3. Insecure temp file usage: os.tempnam, tempfile.mktemp
        unsafe_tmp_re = re.compile(r'\b(os\.tempnam|tempfile\.mktemp)\b')
        # 4. Command injection via os.system or os.popen
        os_cmd_re = re.compile(r'\bos\s*\.\s*(system|popen)\s*\(')

        for idx, line in enumerate(lines, 1):
            if path_traversal_re.search(line) and not any(kw in line for kw in ["sanitize", "resolve", "Path"]):
                findings.append(ScanFinding(
                    finding_id=f"SEC-{uuid.uuid4().hex[:8]}",
                    file_path=file_path,
                    line_number=idx,
                    rule_id="SEC-PATH-TRAVERSAL",
                    severity=Severity.HIGH,
                    category="Path Traversal",
                    description="Dynamic file path passed directly to open(). Risk of directory traversal (../).",
                    evidence=line.strip(),
                    detector=DetectorSource.SECURITY,
                    suggested_fix="Validate and resolve paths against an allowed base directory using Path.resolve().",
                    confidence=0.85,
                    fix_eligibility=FixEligibility.AUTO_FIXABLE,
                ))

            if unsafe_pickle_re.search(line):
                findings.append(ScanFinding(
                    finding_id=f"SEC-{uuid.uuid4().hex[:8]}",
                    file_path=file_path,
                    line_number=idx,
                    rule_id="SEC-UNSAFE-DESERIALIZATION",
                    severity=Severity.CRITICAL,
                    category="Insecure Deserialization",
                    description="Use of pickle deserialization on untrusted input allows remote code execution.",
                    evidence=line.strip(),
                    detector=DetectorSource.SECURITY,
                    suggested_fix="Use safe serialization formats like JSON, MessagePack, or protobuf.",
                    confidence=0.95,
                    fix_eligibility=FixEligibility.MANUAL_REVIEW,
                ))

            if unsafe_yaml_re.search(line):
                findings.append(ScanFinding(
                    finding_id=f"SEC-{uuid.uuid4().hex[:8]}",
                    file_path=file_path,
                    line_number=idx,
                    rule_id="SEC-UNSAFE-YAML",
                    severity=Severity.HIGH,
                    category="Insecure YAML Load",
                    description="yaml.load() without Loader=yaml.SafeLoader can execute arbitrary Python objects.",
                    evidence=line.strip(),
                    detector=DetectorSource.SECURITY,
                    suggested_fix="Use yaml.safe_load(data) or pass Loader=yaml.SafeLoader.",
                    confidence=0.95,
                    fix_eligibility=FixEligibility.AUTO_FIXABLE,
                ))

            if os_cmd_re.search(line):
                findings.append(ScanFinding(
                    finding_id=f"SEC-{uuid.uuid4().hex[:8]}",
                    file_path=file_path,
                    line_number=idx,
                    rule_id="SEC-COMMAND-INJECTION",
                    severity=Severity.CRITICAL,
                    category="Command Injection",
                    description="Execution of commands via os.system or os.popen is vulnerable to command injection.",
                    evidence=line.strip(),
                    detector=DetectorSource.SECURITY,
                    suggested_fix="Use subprocess.run(..., shell=False) with command arguments in a list.",
                    confidence=0.9,
                    fix_eligibility=FixEligibility.AUTO_FIXABLE,
                ))

            if unsafe_tmp_re.search(line):
                findings.append(ScanFinding(
                    finding_id=f"SEC-{uuid.uuid4().hex[:8]}",
                    file_path=file_path,
                    line_number=idx,
                    rule_id="SEC-INSECURE-TMP",
                    severity=Severity.MEDIUM,
                    category="Insecure Temp File",
                    description="tempfile.mktemp or os.tempnam are insecure because a race condition exists before file creation.",
                    evidence=line.strip(),
                    detector=DetectorSource.SECURITY,
                    suggested_fix="Use tempfile.NamedTemporaryFile() or tempfile.TemporaryDirectory().",
                    confidence=0.9,
                    fix_eligibility=FixEligibility.AUTO_FIXABLE,
                ))

        return findings

    # -------------------------------------------------------------------------
    # Layer 4: Cognee Historical Memory Recall
    # -------------------------------------------------------------------------
    def _enrich_with_historical_memory(self, findings: List[ScanFinding], content: str, file_path: str) -> None:
        """Query Cognee knowledge graph and local memories to associate past bugs with findings."""
        if not self.memory_client:
            return

        try:
            # Gather relevant memories based on file name or bug categories
            query = f"{file_path} " + " ".join([f.category for f in findings[:3]])
            recalled = self.memory_client.recall(query=query, file_context=file_path, top_k=5)
            if not recalled:
                return

            mem_ids = [m.id for m in recalled]
            for f in findings:
                if not f.matching_memory_ids:
                    # Match by category or file
                    matched = [
                        m.id for m in recalled
                        if f.category.lower() in m.title.lower() or f.category.lower() in m.content.lower()
                    ]
                    f.matching_memory_ids = matched if matched else mem_ids[:2]
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Layer 5: AI-Assisted Analysis
    # -------------------------------------------------------------------------
    def _is_ai_available(self) -> bool:
        from mnemosyne.config import load_config
        config = load_config(self.repo_root)
        provider = config.get("llm_provider", "ollama")
        if provider == "ollama":
            # Quick check if Ollama server responds
            import urllib.request
            try:
                base_url = config.get("ollama_base_url", "http://localhost:11434/v1")
                # Strip /v1 if present for raw check
                check_url = base_url.replace("/v1", "") + "/api/tags"
                req = urllib.request.Request(check_url, headers={"User-Agent": "Mnemosyne"})
                with urllib.request.urlopen(req, timeout=1) as res:
                    return res.status == 200
            except Exception:
                return False
        return bool(os.getenv("OPENAI_API_KEY") or config.get("llm_api_key"))

    def _run_ai_analysis(self, content: str, file_path: str, existing_findings: List[ScanFinding]) -> List[ScanFinding]:
        """Perform AI code inspection using Ollama or configured LLM."""
        findings: List[ScanFinding] = []
        try:
            from mnemosyne.config import load_config
            import urllib.request
            config = load_config(self.repo_root)
            model = config.get("llm_model", "llama3.1:8b")
            base_url = config.get("ollama_base_url", "http://localhost:11434/v1")

            prompt = f"""You are a precise automated code reviewer. Analyze the following code from '{file_path}'.
Identify subtle logic bugs, unhandled null/None cases, or regression issues.
Return a STRICT JSON array of objects with keys: line_number, severity (low, medium, high), category, description, suggested_fix.
Do NOT repeat these existing findings: {[f.description for f in existing_findings]}
If no issues found, return [].

Code:
```
{content[:2500]}
```"""

            url = f"{base_url}/chat/completions"
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a code analyzer. Respond ONLY with valid JSON array."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.1,
                "max_tokens": 500,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=8) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                text = res_data["choices"][0]["message"]["content"]
                # Extract json array
                match = re.search(r'\[.*\]', text, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
                    for item in parsed:
                        findings.append(ScanFinding(
                            finding_id=f"AI-{uuid.uuid4().hex[:8]}",
                            file_path=file_path,
                            line_number=item.get("line_number"),
                            rule_id="AI-LOGIC-CHECK",
                            severity=Severity(item.get("severity", "medium").lower()),
                            category=item.get("category", "Logic Bug"),
                            description=item.get("description", "AI detected potential logic issue"),
                            detector=DetectorSource.AI_ANALYSIS,
                            suggested_fix=item.get("suggested_fix"),
                            confidence=0.75,  # Clearly marked as uncertain AI candidate
                            fix_eligibility=FixEligibility.MANUAL_REVIEW,
                        ))
        except Exception:
            pass
        return findings

    # -------------------------------------------------------------------------
    # Deduplication
    # -------------------------------------------------------------------------
    def _deduplicate_findings(self, findings: List[ScanFinding]) -> List[ScanFinding]:
        seen = set()
        unique = []
        for f in findings:
            key = (f.file_path, f.line_number, f.rule_id, f.category)
            if key not in seen:
                seen.add(key)
                unique.append(f)

        # Severity sort order
        severity_order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        unique.sort(key=lambda x: severity_order.get(x.severity, 4))
        return unique
