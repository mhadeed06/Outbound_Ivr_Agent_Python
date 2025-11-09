from src.config.insurance_config import config_manager
from .loader import load_prompt

# Map your config names to the actual files under templates/<carrier>/
_MAIN_PROMPT_FILES = {
    "CIGNA_PROMPT_TEMPLATE":        "cigna/cigna_prompt_template",
    "HUMANA_PROMPT_TEMPLATE":       "humana/humana_prompt_template",
    "BAYLOR_SCOTT_PROMPT_TEMPLATE": "baylor_scott/baylor_scott_prompt_template",
    "OSCAR_PROMPT_TEMPLATE": "oscar/oscar_prompt_template",
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

# Back-compat
def get_prompt_template() -> str:
    return get_main_prompt_template()
