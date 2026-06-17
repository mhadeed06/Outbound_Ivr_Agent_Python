from src.config.insurance_config import config_manager
from .loader import load_prompt

# Map your config names to the actual files under templates/<carrier>/
_MAIN_PROMPT_FILES = {
    "CIGNA_PROMPT_TEMPLATE":        "cigna/cigna_prompt_template",
    "HUMANA_PROMPT_TEMPLATE":       "humana/humana_prompt_template",
    "BAYLOR_SCOTT_PROMPT_TEMPLATE": "baylor_scott/baylor_scott_prompt_template",
    "OSCAR_PROMPT_TEMPLATE": "oscar/oscar_prompt_template",
    "HEALTH_FIRST_PROMPT_TEMPLATE": "health_first/health_first_prompt_template",
}

# Denial-inquiry templates — keyed by (insurance_name, phase).
# Each insurance that supports denial_inquiry needs TWO templates:
#   - ivr            → menu navigation, same response-format style as claim-status
#   - representative → free-form conversation with a human advocate
_DENIAL_PROMPT_FILES = {
    ("HUMANA", "ivr"):            "humana/humana_denial_ivr_template",
    ("HUMANA", "representative"): "humana/humana_denial_rep_template",
}

def get_main_prompt_template() -> str:
    """
    Return the raw main prompt text (unformatted).
    Callers do: get_main_prompt_template().format(**vars)
    """
    prompt_name = config_manager.get_config().prompt_template
    try:
        file_stem = _MAIN_PROMPT_FILES[prompt_name]
    except KeyError:
        raise ValueError(f"Unknown main prompt: {prompt_name}")
    return load_prompt(file_stem)


def get_denial_prompt_template(phase: str) -> str:
    """Return the denial-inquiry template for the active insurance + phase.

    `phase` is one of: "ivr", "representative". Raises if no template exists
    for the (insurance, phase) pair — meaning the insurance was wired into
    the denial-inquiry endpoint without the required prompt files.
    """
    insurance_name = config_manager.get_insurance_name()
    key = (insurance_name, phase)
    try:
        file_stem = _DENIAL_PROMPT_FILES[key]
    except KeyError:
        raise ValueError(
            f"No denial-inquiry template for insurance={insurance_name!r} phase={phase!r}"
        )
    return load_prompt(file_stem)


# Back-compat
def get_prompt_template() -> str:
    return get_main_prompt_template()
