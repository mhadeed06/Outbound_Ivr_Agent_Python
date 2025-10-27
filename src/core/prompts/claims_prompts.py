from .loader import render_prompt, load_prompt


_CLAIMS_PROMPT_FILES = {
    "CIGNA_CLAIMS_CONTROLLER_TEMPLATE": "cigna/claims_controller_template",
    "HUMANA_CLAIMS_CONTROLLER_TEMPLATE": "humana/claims_controller_template",
    "BAYLOR_SCOTT_CLAIMS_CONTROLLER_TEMPLATE": "baylor_scott/claims_controller_template",
}

def get_claims_prompt(prompt_name: str) -> str:
    """
    Return raw claims prompt text (unformatted).
    Callers will do: get_claims_prompt(name).format(**vars)
    """
    try:
        file_stem = _CLAIMS_PROMPT_FILES[prompt_name]
    except KeyError:
        raise ValueError(f"Unknown claims prompt: {prompt_name}")
    return load_prompt(file_stem)  # <-- raw text
