"""The export-gating decision (task 15.3; Req 7.4, 8.6, 8.7, 22.4).

Export is the single irreversible step that ships a plugin to a tenant, so the
tool guards it behind one explicit, auditable decision that composes the two
independent pre-export checks:

* the :class:`~icplugin_builder.core.spec_validator.ValidationReport` produced by
  the ``Spec_Validator`` (structural + semantic-version validation, Req 7), and
* the :class:`~icplugin_builder.integrations.code_validator.PipelineReport`
  produced by the ``Code_Validator`` (the four-stage lint/build/test/validate
  pipeline, Req 8).

The rule is a strict conjunction (design "Property 17: Export gating equals
validation conjunction"): **export is permitted if and only if the spec is valid
and all four code stages passed**. Any other combination blocks export (Req 7.4,
8.6) while surfacing exactly what remains -- the outstanding spec-validation
errors and the code stages that did not pass -- so the operator knows what to fix
before retrying (feeds Req 22.4, "re-run Spec_Validator and Code_Validator before
permitting export").

This module only *reads* the two reports and renders a decision; it never mutates
the draft, spec, or generated code, so a blocked export leaves everything
unchanged (Req 8.6). Either report may be ``None`` to model a check that has not
run yet (e.g. code was never built): a missing report is treated as "not passed"
and blocks export with an explanatory reason rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..core.spec_validator import SpecValidationError, ValidationReport
from .code_validator import PipelineReport, StageName, StageResult

__all__ = [
    "ExportDecision",
    "decide_export",
    "ExportGate",
]

#: Surfaced when export is requested before the spec has been validated.
SPEC_NOT_VALIDATED_MESSAGE = "Plugin spec has not been validated; run Spec_Validator before export."

#: Surfaced when export is requested before the code pipeline has run.
CODE_NOT_VALIDATED_MESSAGE = "Code validation has not been run; run the four-stage Code_Validator before export."


@dataclass(frozen=True)
class ExportDecision:
    """The outcome of the export-gating decision.

    Attributes:
        permitted: ``True`` iff the spec is valid **and** all four code stages
            passed (design "Property 17"). ``False`` blocks export (Req 7.4, 8.6).
        spec_valid: Whether the spec-validation report was present and clean.
        code_passed: Whether the pipeline report was present and every one of the
            four stages passed.
        spec_errors: The outstanding spec-validation errors, empty when the spec
            is valid. Copied from the report so the decision is self-contained.
        failed_stages: The code stages that did not pass, in pipeline order,
            empty when all passed (Req 8.5 feeds this).
        reasons: Human-readable lines explaining why export is blocked, empty
            when export is permitted.
    """

    permitted: bool
    spec_valid: bool
    code_passed: bool
    spec_errors: Tuple[SpecValidationError, ...] = ()
    failed_stages: Tuple[StageResult, ...] = ()
    reasons: Tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        """Return ``True`` iff export is blocked (the inverse of :attr:`permitted`)."""
        return not self.permitted

    def summary(self) -> str:
        """Return a single human-readable line describing the decision.

        On a permit this is a short confirmation; on a block it names the number
        of unresolved spec errors and failed stages so the operator sees the
        remaining work at a glance (Req 7.4, 8.6).
        """
        if self.permitted:
            return "Export permitted: plugin spec is valid and all four code stages passed."

        parts: List[str] = []
        if not self.spec_valid:
            count = len(self.spec_errors)
            if count:
                noun = "error" if count == 1 else "errors"
                parts.append(f"{count} unresolved spec validation {noun}")
            else:
                parts.append("spec not validated")
        if not self.code_passed:
            names = ", ".join(stage.name for stage in self.failed_stages)
            if names:
                parts.append(f"failed code stages: {names}")
            else:
                parts.append("code not validated")
        return "Export blocked: " + "; ".join(parts) + "."


def decide_export(
    validation_report: Optional[ValidationReport],
    pipeline_report: Optional[PipelineReport],
) -> ExportDecision:
    """Decide whether export is permitted from the two pre-export reports.

    Permit iff the spec is valid **and** all four code stages passed; otherwise
    block and record the remaining spec errors and failed stages (design
    "Property 17"; Req 7.4, 8.6, 8.7, 22.4).

    Args:
        validation_report: The ``Spec_Validator`` report, or ``None`` if the spec
            has not been validated yet (treated as invalid).
        pipeline_report: The ``Code_Validator`` report, or ``None`` if the
            pipeline has not run yet (treated as not passed).

    Returns:
        An :class:`ExportDecision` capturing the permit/block outcome together
        with the outstanding spec errors, failed stages, and block reasons.
    """
    spec_valid = validation_report is not None and validation_report.is_valid
    code_passed = pipeline_report is not None and pipeline_report.passed

    spec_errors: Tuple[SpecValidationError, ...] = (
        tuple(validation_report.errors) if validation_report is not None else ()
    )
    failed_stages = _failed_stages(pipeline_report)

    reasons = _block_reasons(
        validation_report=validation_report,
        spec_valid=spec_valid,
        spec_errors=spec_errors,
        pipeline_report=pipeline_report,
        code_passed=code_passed,
        failed_stages=failed_stages,
    )

    return ExportDecision(
        permitted=spec_valid and code_passed,
        spec_valid=spec_valid,
        code_passed=code_passed,
        spec_errors=spec_errors,
        failed_stages=failed_stages,
        reasons=reasons,
    )


def _failed_stages(pipeline_report: Optional[PipelineReport]) -> Tuple[StageResult, ...]:
    """Return the not-passed stages, or an empty tuple when no report exists."""
    if pipeline_report is None:
        return ()
    return pipeline_report.failed_stages


def _block_reasons(
    *,
    validation_report: Optional[ValidationReport],
    spec_valid: bool,
    spec_errors: Tuple[SpecValidationError, ...],
    pipeline_report: Optional[PipelineReport],
    code_passed: bool,
    failed_stages: Tuple[StageResult, ...],
) -> Tuple[str, ...]:
    """Build the ordered, human-readable reasons export is blocked (empty if permitted)."""
    reasons: List[str] = []

    if not spec_valid:
        if validation_report is None:
            reasons.append(SPEC_NOT_VALIDATED_MESSAGE)
        else:
            for error in spec_errors:
                reasons.append(f"Spec error at {error.path}: {error.message}")

    if not code_passed:
        if pipeline_report is None:
            reasons.append(CODE_NOT_VALIDATED_MESSAGE)
        elif failed_stages:
            for stage in failed_stages:
                detail = stage.message or f"{stage.name} stage did not pass"
                reasons.append(f"Code stage '{stage.name}' did not pass: {detail}")
        else:
            # The pipeline exists but did not run every stage (e.g. Docker absent
            # aborted the run early); name the missing stages so the gap is clear.
            reported = {stage.name for stage in pipeline_report.stages}
            missing = [name for name in StageName.ORDER if name not in reported]
            joined = ", ".join(missing) if missing else "one or more stages"
            reasons.append(f"Code validation incomplete; stages did not all run: {joined}.")

    return tuple(reasons)


class ExportGate:
    """A thin, reusable façade over the export-gating decision.

    The decision itself is pure (:func:`decide_export`); this class exists so
    callers that carry the two reports around (e.g. the Orchestrator) can express
    the gate as a small object and query :meth:`decide` without re-plumbing the
    arguments. It holds no mutable state and never touches the working tree.
    """

    def decide(
        self,
        validation_report: Optional[ValidationReport],
        pipeline_report: Optional[PipelineReport],
    ) -> ExportDecision:
        """Return the :class:`ExportDecision` for the given pair of reports."""
        return decide_export(validation_report, pipeline_report)
