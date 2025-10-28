# Ensure package-local .env is loaded as soon as the package is imported
from pathlib import Path
from dotenv import load_dotenv

_pkg_root = Path(__file__).resolve().parent
load_dotenv(_pkg_root / ".env")
