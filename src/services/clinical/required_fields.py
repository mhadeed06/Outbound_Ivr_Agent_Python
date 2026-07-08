"""
Per-insurance list of visit_data fields that MUST be present (non-null,
non-empty) before a call can start. If any are missing, orchestrate
refuses to place the call.
"""
from typing import Dict, List

# Keys match the prompt placeholders in src/core/prompts/templates/*.
REQUIRED_FIELDS_BY_INSURANCE: Dict[str, List[str]] = {
    "CIGNA":        ["tax_id", "npi", "member_id", "dob", "member_name", "dos"],
    "HUMANA":       ["tax_id", "npi", "member_id", "dob", "member_name", "dos"],
    "BAYLOR_SCOTT": ["tax_id", "npi", "member_id", "dob", "member_name", "dos"],
    "OSCAR":        ["tax_id", "npi", "member_id", "dos"],
    "UHC":          ["npi", "member_id", "dob", "member_name", "dos"],
}


def missing_fields_for(insurance_name: str, visit_data: dict) -> List[str]:
    """Return the list of required fields that are missing/empty for this insurance."""
    required = REQUIRED_FIELDS_BY_INSURANCE.get(insurance_name, [])
    missing = []
    for key in required:
        value = visit_data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing
