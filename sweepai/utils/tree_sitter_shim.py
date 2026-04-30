"""
Compatibility shim: replaces `tree_sitter_languages.get_language / get_parser`
with the modern tree-sitter 0.23.x individual-package API.

Usage — in any sweepai file that previously had:
    from tree_sitter_languages import get_language, get_parser

Change it to:
    from sweepai.utils.tree_sitter_shim import get_language, get_parser
"""
from __future__ import annotations

import importlib
from functools import lru_cache
from tree_sitter import Language, Parser

# Map language name strings (as used by tree_sitter_languages) to their
# modern individual package module name + exported function.
_LANG_MAP: dict[str, tuple[str, str]] = {
    "python":        ("tree_sitter_python",     "language"),
    "javascript":    ("tree_sitter_javascript", "language"),
    "typescript":    ("tree_sitter_typescript", "language_typescript"),
    "tsx":           ("tree_sitter_typescript", "language_tsx"),
    "go":            ("tree_sitter_go",         "language"),
    "java":          ("tree_sitter_java",       "language"),
    "cpp":           ("tree_sitter_cpp",        "language"),
    "c":             ("tree_sitter_c",          "language"),
    "ruby":          ("tree_sitter_ruby",       "language"),
    "rust":          ("tree_sitter_rust",       "language"),
}


@lru_cache(maxsize=None)
def get_language(lang: str) -> Language:
    """Return a tree_sitter.Language for *lang* (e.g. 'python', 'javascript')."""
    entry = _LANG_MAP.get(lang)
    if entry is None:
        raise ValueError(
            f"Language '{lang}' is not registered in tree_sitter_shim._LANG_MAP. "
            f"Install tree-sitter-{lang} and add it to the map."
        )
    module_name, fn_name = entry
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            f"Could not import '{module_name}'. "
            f"Run: pip install {module_name.replace('_', '-')}"
        ) from exc
    lang_fn = getattr(mod, fn_name)
    return Language(lang_fn())


@lru_cache(maxsize=None)
def get_parser(lang: str) -> Parser:
    """Return a configured tree_sitter.Parser for *lang*."""
    p = Parser()
    p.language = get_language(lang)
    return p