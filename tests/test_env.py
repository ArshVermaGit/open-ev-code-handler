import pytest
from codereview_env.env import CodeReviewEnv
from codereview_env.models import (
    TaskId, Action, ActionType, Category, Severity, Verdict, StateResult
)


# ─────────────────────────────────────────────────────────────────────────────
# Reset tests
# ─────────────────────────────────────────────────────────────────────────────

def test_env_reset():
    env = CodeReviewEnv()
    res = env.reset(TaskId.BUG_DETECTION, seed=0)
    assert res.task_id == TaskId.BUG_DETECTION
    assert res.seed == 0
    assert res.observation.step_count == 0
    assert res.observation.noise_budget == 5


def test_env_reset_populates_blast_radius():
    """Observation should carry blast-radius metadata from the scenario."""
    env = CodeReviewEnv()
    res = env.reset(TaskId.SECURITY_AUDIT, seed=0)
    obs = res.observation
    assert obs.blast_radius in ("low", "medium", "high", "critical")
    assert obs.service_criticality in ("low", "medium", "high", "critical")
    assert isinstance(obs.affected_users, int)
    assert obs.service_name != ""


# ─────────────────────────────────────────────────────────────────────────────
# Step tests
# ─────────────────────────────────────────────────────────────────────────────

def test_env_step_bug_detection():
    env = CodeReviewEnv()
    env.reset(TaskId.BUG_DETECTION, seed=1)
    # seed=1 → bug_003: None dereference in auth.py

    action = Action(
        action_type=ActionType.FLAG_ISSUE,
        body="None dereference null check guard clause AttributeError",
        filename="auth.py",
        line_number=16,
        category=Category.BUG,
        severity=Severity.HIGH
    )
    step_res = env.step(action)
    assert step_res.observation.step_count == 1
    assert step_res.reward > 0, "Correct issue flag should give positive reward delta"
    assert step_res.done == False

    # Terminal action
    step_term = env.step(Action(
        action_type=ActionType.APPROVE,
        body="LGTM",
        verdict=Verdict.LGTM
    ))
    assert step_term.done == True

    final = env.get_final_result()
    assert final.final_score > 0


def test_env_step_reward_is_incremental_not_cumulative():
    """Each step reward should be a delta (positive or zero or penalty), not a running total."""
    env = CodeReviewEnv()
    # seed=1 selects bug_003: None dereference in auth.py at line 16
    env.reset(TaskId.BUG_DETECTION, seed=1)

    correct_action = Action(
        action_type=ActionType.FLAG_ISSUE,
        body="None dereference null check guard clause AttributeError",
        filename="auth.py",
        line_number=16,
        category=Category.BUG,
        severity=Severity.HIGH
    )
    step1 = env.step(correct_action)
    # First correct flag → positive incremental delta
    assert step1.reward > 0, f"Correct issue flag should give positive reward delta, got {step1.reward}"

    # Second identical flag on same file/line — already matched, counts as FP
    step2 = env.step(correct_action)
    # Already matched → false positive → -0.05 penalty
    assert step2.reward == -0.05


def test_env_step_false_positive_penalty():
    """False positives should decrement noise_budget and return negative reward."""
    env = CodeReviewEnv()
    env.reset(TaskId.BUG_DETECTION, seed=0)

    fp_action = Action(
        action_type=ActionType.FLAG_ISSUE,
        body="completely wrong flag",
        filename="nonexistent_file.py",
        line_number=999,
        category=Category.BUG,
        severity=Severity.LOW
    )
    step_res = env.step(fp_action)
    assert step_res.reward == -0.05
    assert step_res.observation.noise_budget == 4


def test_env_noise_budget_exhaustion():
    env = CodeReviewEnv()
    env.reset(TaskId.BUG_DETECTION, seed=0)

    fp_action = Action(
        action_type=ActionType.FLAG_ISSUE,
        body="fp",
        filename="nonexistent",
        line_number=999,
        category=Category.BUG,
        severity=Severity.LOW
    )

    for i in range(4):
        res = env.step(fp_action)
        assert res.done == False
        assert res.observation.noise_budget == 5 - (i + 1)

    res_final = env.step(fp_action)
    assert res_final.done == True
    assert res_final.observation.noise_budget == 0


def test_env_max_steps():
    env = CodeReviewEnv()
    env.reset(TaskId.BUG_DETECTION, seed=0)

    action = Action(action_type=ActionType.ASK_QUESTION, body="what's this?")
    for i in range(9):
        res = env.step(action)
        assert res.done == False

    res_final = env.step(action)
    assert res_final.done == True
    assert res_final.observation.step_count == 10


# ─────────────────────────────────────────────────────────────────────────────
# get_state() tests — required by OpenEnv /state endpoint
# ─────────────────────────────────────────────────────────────────────────────

def test_get_state_returns_state_result():
    env = CodeReviewEnv()
    env.reset(TaskId.BUG_DETECTION, seed=0)

    state = env.get_state("test-episode-id")
    assert isinstance(state, StateResult)
    assert state.episode_id == "test-episode-id"
    assert state.task_id == TaskId.BUG_DETECTION
    assert state.step == 0
    assert state.max_steps == 10
    assert state.noise_budget == 5
    assert state.cumulative_score == 0.0
    assert state.done == False
    assert state.issues_found == []


def test_get_state_updates_after_step():
    env = CodeReviewEnv()
    env.reset(TaskId.BUG_DETECTION, seed=1)

    action = Action(
        action_type=ActionType.FLAG_ISSUE,
        body="None dereference null check guard clause",
        filename="auth.py",
        line_number=16,
        category=Category.BUG,
        severity=Severity.HIGH
    )
    env.step(action)

    state = env.get_state("ep-123")
    assert state.step == 1
    assert state.cumulative_score > 0
    assert len(state.issues_found) > 0


def test_get_state_before_reset_raises():
    env = CodeReviewEnv()
    with pytest.raises(RuntimeError):
        env.get_state("no-episode")


# ─────────────────────────────────────────────────────────────────────────────
# Multi-task smoke tests
# ─────────────────────────────────────────────────────────────────────────────

def test_security_task_runs_to_completion():
    env = CodeReviewEnv()
    # seed=1 selects sec_003: JWT verification disabled in tokens.py
    env.reset(TaskId.SECURITY_AUDIT, seed=1)

    action = Action(
        action_type=ActionType.FLAG_ISSUE,
        body="JWT decoded without signature verification bypass authentication none algorithm",
        filename="tokens.py",
        line_number=10,
        category=Category.SECURITY,
        severity=Severity.CRITICAL
    )
    step_res = env.step(action)
    assert step_res.reward >= 0, f"Correct security flag should give non-negative reward, got {step_res.reward}"

    env.step(Action(
        action_type=ActionType.REQUEST_CHANGES,
        body="JWT verification must never be disabled. Must be fixed before merge.",
        verdict=Verdict.REQUEST_CHANGES
    ))
    final = env.get_final_result()
    assert final.final_score > 0


def test_arch_task_runs_to_completion():
    env = CodeReviewEnv()
    env.reset(TaskId.ARCHITECTURAL_REVIEW, seed=0)

    action = Action(
        action_type=ActionType.FLAG_ISSUE,
        body="Direct DB access from dashboard bypasses API layer separation of concerns architectural violation",
        filename="services/dashboard.py",
        line_number=5,
        category=Category.ARCHITECTURE,
        severity=Severity.CRITICAL
    )
    env.step(action)

    env.step(Action(
        action_type=ActionType.REQUEST_CHANGES,
        body="Must go through API layer.",
        verdict=Verdict.REQUEST_CHANGES
    ))
    final = env.get_final_result()
    assert final.final_score > 0
    assert final.verdict_correct == True
