from functools import lru_cache
from importlib import resources

PKG = "OUTBOUND_AZURE_TELNYX.prompts"

@lru_cache(maxsize=64)
def load_prompt(name: str) -> str:
    filename = name if name.endswith(".txt") else f"{name}.txt"
    try:
        return resources.files(PKG).joinpath(filename).read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Prompt '{filename}' not found in {PKG}") from e

def render_prompt(name: str, **vars) -> str:
    return load_prompt(name).format(**vars) if vars else load_prompt(name)
