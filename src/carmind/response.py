"""Small, interface-independent projection of a validated assessment.

This layer turns trusted internal fields into ordinary-driver language.  It does
not interpret raw evidence, choose actions, or author a safety disposition.
"""

from dataclasses import dataclass

from carmind.assessment import ValidatedAssessment


@dataclass(frozen=True)
class UserFacingResponse:
    text: str


def render_user_response(validated: ValidatedAssessment) -> UserFacingResponse:
    """Render only validated claim text, approved wording and deterministic safety."""
    assessment = validated.assessment
    paragraphs: list[str] = []
    if assessment.observations:
        paragraphs.append("Based on the available information:\n" + "\n".join(
            f"- {claim}" for claim in assessment.observations))
    if assessment.hypotheses:
        paragraphs.append("Possible explanations (not confirmed):\n" + "\n".join(
            f"- {hypothesis}" for hypothesis in assessment.hypotheses))

    limitations = [item for item in assessment.limitations
                   if item not in validated.safety.limitations and item != "This is a development proof of concept."]
    # The approved safety message carries the authoritative warning. The
    # simulation-only audit phrase is useful internally but is not ordinary
    # driver language.
    limitations.extend(item for item in validated.safety.user_limitations
                       if not item.startswith("Simulation policy only"))
    if limitations:
        paragraphs.append("Limits of this assessment:\n" + "\n".join(
            f"- {item}" for item in dict.fromkeys(limitations)))

    paragraphs.append(validated.safety.approved_text)
    if validated.action_wording:
        paragraphs.append("Suggested next steps:\n" + "\n".join(
            f"- {wording}" for wording in validated.action_wording))
    return UserFacingResponse("\n\n".join(paragraphs))
