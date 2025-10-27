from functools import lru_cache
import os
from pathlib import Path

# Get the templates directory path
TEMPLATES_DIR = Path(__file__).parent / "templates"

@lru_cache(maxsize=64)
def load_prompt(template_path: str) -> str:
    """
    Load a prompt template file.
    template_path format: "insurance_name/template_name" (without .txt extension)
    """
    filename = template_path if template_path.endswith(".txt") else f"{template_path}.txt"
    file_path = TEMPLATES_DIR / filename
    
    try:
        return file_path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise RuntimeError(f"Prompt template '{filename}' not found in {TEMPLATES_DIR}") from e

def render_prompt(template_path: str, **vars) -> str:
    """Render a prompt template with variables"""
    template_content = load_prompt(template_path)
    return template_content.format(**vars) if vars else template_content
