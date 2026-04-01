from codereview_env.models import (
    TaskId, Action, Observation, StepResult, ResetResult,
    ActionType, ActionRecord, EpisodeResult, StateResult
)
from codereview_env.scenario_bank import get_scenario
from codereview_env.graders.grader_utils import find_best_match
from codereview_env.graders.bug_grader import grade_bug_detection
from codereview_env.graders.security_grader import grade_security_audit
from codereview_env.graders.arch_grader import grade_architectural_review


class CodeReviewEnv:
    TASK_MAX_STEPS = {
        TaskId.BUG_DETECTION:        10,
        TaskId.SECURITY_AUDIT:       15,
        TaskId.ARCHITECTURAL_REVIEW: 20,
    }

    def __init__(self):
        self._state = None

    def reset(self, task_id: TaskId, seed: int = 42) -> ResetResult:
        scenario = get_scenario(task_id, seed)
        self._state = {
            "task_id":      task_id,
            "seed":         seed,
            "scenario":     scenario,
            "step_count":   0,
            "noise_budget": 5,
            "max_steps":    self.TASK_MAX_STEPS[task_id],
            "history":      [],
            "running_score": 0.0,
            "done":         False,
            "issues_found": set(),   # set of matched ground-truth issue IDs
            "false_positives": []    # list of FP action bodies
        }
        return ResetResult(
            observation=self._build_obs(),
            task_id=task_id,
            seed=seed,
            scenario_hash=scenario.hash
        )

    def step(self, action: Action) -> StepResult:
        if self._state is None or self._state["done"]:
            raise RuntimeError("Episode is done or not initialized. Call reset().")

        s = self._state
        s["step_count"] += 1

        # Record action in history
        s["history"].append(ActionRecord(
            action_type=action.action_type,
            body=action.body,
            filename=action.filename,
            line_number=action.line_number,
            severity=action.severity,
            category=action.category,
            verdict=action.verdict
        ))

        # Apply action logic and compute incremental reward delta
        prev_score    = s["running_score"]
        reward_delta  = self._apply_action(action)
        s["running_score"] = prev_score + reward_delta

        # Check termination
        s["done"] = (
            action.action_type in (ActionType.APPROVE, ActionType.REQUEST_CHANGES)
            or s["step_count"] >= s["max_steps"]
            or s["noise_budget"] <= 0
        )

        return StepResult(
            observation=self._build_obs(),
            reward=round(reward_delta, 4),   # ← incremental delta, NOT cumulative
            done=s["done"],
            info={
                "step":               s["step_count"],
                "cumulative_score":   round(s["running_score"], 4),
                "noise_budget":       s["noise_budget"],
                "issues_found_count": len(s["issues_found"]),
            }
        )

    def get_state(self, episode_id: str) -> StateResult:
        """Return a snapshot of current episode state (required by /state endpoint)."""
        if self._state is None:
            raise RuntimeError("Episode not initialized. Call reset() first.")
        s  = self._state
        sc = s["scenario"]
        return StateResult(
            episode_id=episode_id,
            task_id=s["task_id"],
            step=s["step_count"],
            max_steps=s["max_steps"],
            scenario_hash=sc.hash,
            cumulative_score=round(s["running_score"], 4),
            noise_budget=s["noise_budget"],
            issues_found=list(s["issues_found"]),
            done=s["done"],
        )

    def _build_obs(self) -> Observation:
        s  = self._state
        sc = s["scenario"]
        return Observation(
            task_id=s["task_id"],
            pr_title=sc.pr_title,
            pr_description=sc.pr_description,
            diff="\n".join([f.patch for f in sc.files_changed]),
            files_changed=sc.files_changed,
            step_count=s["step_count"],
            max_steps=s["max_steps"],
            history=s["history"],
            noise_budget=s["noise_budget"],
            # Blast radius / service context from scenario metadata
            affected_users=sc.affected_users,
            service_criticality=sc.service_criticality,
            blast_radius=sc.blast_radius,
            service_name=sc.service_name,
        )

    def _apply_action(self, action: Action) -> float:
        """
        Compute the incremental reward delta for this single action.

        Reward shaping:
          - FLAG_ISSUE that matches ground truth: delta = new_score - old_score (always >= 0)
          - FLAG_ISSUE that is a false positive:  delta = -0.05 per FP (noise penalty)
          - Terminal action (approve/request_changes): grader recalculates full score
          - Any other action: delta = 0
        """
        s  = self._state
        sc = s["scenario"]

        if action.action_type == ActionType.FLAG_ISSUE:
            matched_gt = find_best_match(action, sc.ground_truth_issues, s["issues_found"])
            if matched_gt:
                s["issues_found"].add(matched_gt.id)
                # Recalculate full grader score and return delta
                new_score    = self._grade(sc, s)
                reward_delta = new_score - s["running_score"]
                return max(0.0, reward_delta)   # finding a real issue is always non-negative
            else:
                # False positive: consume noise budget and penalize
                s["noise_budget"]     -= 1
                s["false_positives"].append(action.body)
                return -0.05

        elif action.action_type in (ActionType.APPROVE, ActionType.REQUEST_CHANGES):
            # Terminal: compute full final score delta
            new_score    = self._grade(sc, s)
            reward_delta = new_score - s["running_score"]
            return reward_delta

        # comment / ask_question — no reward signal
        return 0.0

    def _grade(self, sc, s) -> float:
        """Route to the right grader based on task_id."""
        if s["task_id"] == TaskId.BUG_DETECTION:
            return grade_bug_detection(sc, s["history"])
        elif s["task_id"] == TaskId.SECURITY_AUDIT:
            return grade_security_audit(sc, s["history"])
        else:
            return grade_architectural_review(sc, s["history"])

    def get_final_result(self) -> EpisodeResult:
        s  = self._state
        sc = s["scenario"]

        all_gt_ids = {gt.id for gt in sc.ground_truth_issues}
        missed_ids = list(all_gt_ids - s["issues_found"])
        final_score = self._grade(sc, s)

        verdict_correct = None
        if s["task_id"] == TaskId.ARCHITECTURAL_REVIEW:
            final_action = s["history"][-1] if s["history"] else None
            if final_action and final_action.action_type in (ActionType.APPROVE, ActionType.REQUEST_CHANGES):
                required_verdicts = [gt.required_verdict for gt in sc.ground_truth_issues if gt.required_verdict]
                if required_verdicts:
                    verdict_correct = final_action.verdict == required_verdicts[0]

        return EpisodeResult(
            task_id=s["task_id"],
            seed=s["seed"],
            total_steps=s["step_count"],
            final_score=round(final_score, 4),
            issues_found=list(s["issues_found"]),
            issues_missed=missed_ids,
            false_positives=s["false_positives"],
            verdict_correct=verdict_correct
        )
