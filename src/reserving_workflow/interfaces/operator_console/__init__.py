"""Packaged Operator Console resource loader."""

from importlib.resources import files


def load_operator_console_html() -> str:
    """Load the Operator Console document from its packaged resource."""

    return files(__package__).joinpath("console.html").read_text(encoding="utf-8")
