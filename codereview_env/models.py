from enum import Enum
from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, model_validator


class TaskId(str, Enum):
    BUG_DETECTION        = "bug_detection"
    SECURITY_AUDIT       = "security_audit"
    ARCHITECTURAL_REVIEW = "architectural_review"


class ActionType(str, Enum):
    COMMENT         = "comment"
    FLAG_ISSUE      = "flag_issue"
    REQUEST_CHANGES = "request_changes"
    APPROVE         = "approve"
    ASK_QUESTION    = "ask_question"


class Severity(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class Category(str, Enum):
    BUG          = "bug"
    SECURITY     = "security"
    STYLE        = "style"
    PERFORMANCE  = "performance"
    ARCHITECTURE = "architecture"
    DESIGN       = "design"


class Verdict(str, Enum):
    LGTM            = "LGTM"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    NEEDS_DISCUSSION = "NEEDS_DISCUSSION"


class FileChange(BaseModel):
    filename:  str
    patch:     str
    additions: int = 0
    deletions: int = 0


class GroundTruthIssue(BaseModel):
    id:               str
    category:         Category
    severity:         Severity
    filename:         str
    line_number:      int
    description:      str
    keywords:         List[str]
    required_verdict: Optional[Verdict] = None


class ActionRecord(BaseModel):
    action_type: ActionType
    body:        str
    filename:    Optional[str]      = None
    line_number: Optional[int]      = None
    severity:    Optional[Severity] = None
    category:    Optional[Category] = None
    verdict:     Optional[Verdict]  = None


class Action(BaseModel):
    action_type: ActionType
    body:        str
    filename:    Optional[str]      = None
    line_number: Optional[int]      = None
    severity:    Optional[Severity] = None
    category:    Optional[Category] = None
    verdict:     Optional[Verdict]  = None

    @model_validator(mode='after')
    def validate_action(self) -> 'Action':
        if self.action_type == ActionType.FLAG_ISSUE:
            if not self.severity or not self.category:
                raise ValueError("flag_issue requires severity and category")
            if not self.filename or not self.line_number:
                raise ValueError("flag_issue requires filename and line_number")

        if self.action_type in (ActionType.APPROVE, ActionType.REQUEST_CHANGES):
            if not self.verdict:
                raise ValueError(f"{self.action_type.value} requires a verdict")

        return self


class Observation(BaseModel):
    task_id:              TaskId
    pr_title:             str
    pr_description:       str
    diff:                 str
    files_changed:        List[FileChange]
    step_count:           int
    max_steps:            int
    history:              List[ActionRecord]
    noise_budget:         int
    # ── Context-enriched fields (blast radius / service metadata) ──────────
    affected_users:       int                           = 0
    service_criticality:  Literal["low", "medium", "high", "critical"] = "medium"
    blast_radius:         Literal["low", "medium", "high", "critical"] = "medium"
    service_name:         str                           = "unknown-service"


class ResetResult(BaseModel):
    observation:    Observation
    task_id:        TaskId
    seed:           int
    scenario_hash:  str


class StepResult(BaseModel):
    observation: Observation
    reward:      float           # incremental reward delta for this step
    done:        bool
    info:        Dict[str, Any]


class EpisodeResult(BaseModel):
    task_id:         TaskId
    seed:            int
    total_steps:     int
    final_score:     float
    issues_found:    List[str]   # IDs of ground truth issues correctly found
    issues_missed:   List[str]   # IDs of ground truth issues missed
    false_positives: List[str]   # descriptions of false-positive actions
    verdict_correct: Optional[bool] = None


class StateResult(BaseModel):
    """Snapshot of current episode state — required by OpenEnv /state endpoint."""
    episode_id:       str
    task_id:          TaskId
    step:             int
    max_steps:        int
    scenario_hash:    str
    cumulative_score: float
    noise_budget:     int
    issues_found:     List[str]
    done:             bool


class Scenario(BaseModel):
    task_id:               TaskId
    pr_title:              str
    pr_description:        str
    files_changed:         List[FileChange]
    ground_truth_issues:   List[GroundTruthIssue]
    hash:                  str
    # ── Scenario-level blast radius metadata ──────────────────────────────
    affected_users:        int                                          = 0
    service_criticality:   Literal["low", "medium", "high", "critical"] = "medium"
    blast_radius:          Literal["low", "medium", "high", "critical"] = "medium"
    service_name:          str                                          = "unknown-service"
