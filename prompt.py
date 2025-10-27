from .insurance_config import config_manager  # keep as-is
from .prompts.loader import render_prompt, load_prompt


# Dictionary to map prompt names to templates
_MAIN_PROMPT_FILES = {
    "CIGNA_PROMPT_TEMPLATE": "cigna_prompt_template",
    "HUMANA_PROMPT_TEMPLATE": "humana_prompt_template",
    "BAYLOR_SCOTT_PROMPT_TEMPLATE": "baylor_scott_prompt_template",
}


def get_main_prompt_template() -> str:
    """
    Return the raw prompt text (unformatted).
    Callers will do: get_main_prompt_template().format(**vars)
    """
    config = config_manager.get_config()
    prompt_name = config.prompt_template
    try:
        file_stem = _MAIN_PROMPT_FILES[prompt_name]
    except KeyError:
        raise ValueError(f"Unknown main prompt: {prompt_name}")
    return load_prompt(file_stem)  # <-- raw text, no formatting

# Back-compat alias
def get_prompt_template() -> str:
    return get_main_prompt_template()
