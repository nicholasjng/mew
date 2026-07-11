"""Sphinx configuration for the mew documentation."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

# -- Project information -----------------------------------------------------

project = "mew"
author = "Nicholas Junge"
copyright = f"{datetime.now(tz=UTC):%Y}, {author}"

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx.ext.doctest",
    "sphinx_autodoc_typehints",
    "sphinx_copybutton",
    "sphinx_design",
]

source_suffix = {".md": "markdown", ".rst": "restructuredtext"}
master_doc = "index"
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- MyST --------------------------------------------------------------------

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "tasklist",
    "attrs_inline",
    "substitution",
    "fieldlist",
]
myst_heading_anchors = 3

# -- Autodoc -----------------------------------------------------------------

autosummary_generate = True
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_typehints = "description"
autodoc_typehints_format = "short"
# NumPy-style docstrings (matches the in-tree convention in mew.api etc.).
napoleon_numpy_docstring = True
napoleon_google_docstring = False
napoleon_use_rtype = False

# Only run doctests in explicit ``.. doctest::`` directives. The NumPy-style
# `>>>` example blocks in docstrings are illustrative, not executable.
doctest_test_doctest_blocks = ""
doctest_global_setup = "import mew"

# -- Intersphinx -------------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pyarrow": ("https://arrow.apache.org/docs/", None),
}

# -- HTML output -------------------------------------------------------------

html_theme = "furo"
html_static_path = ["_static"]
html_title = "mew documentation"
html_theme_options = {
    "source_repository": "https://github.com/nicholasjng/mew/",
    "source_branch": "master",
    "source_directory": "docs/",
    "sidebar_hide_name": False,
}

# -- CLI reference generator -------------------------------------------------


def _render_cli_help() -> None:
    """Capture argparse ``--help`` output for the CLI reference page.

    Imports mew.cli lazily so a missing extension build at conf-load time
    surfaces as a Sphinx warning rather than an import crash. ``main`` is the
    argparse entrypoint; ``--help`` prints to stdout and raises ``SystemExit``,
    which the capture below suppresses.
    """
    out_dir = os.path.join(os.path.dirname(__file__), "_generated")
    os.makedirs(out_dir, exist_ok=True)
    try:
        from mew.cli import main
    except ImportError as exc:
        with open(os.path.join(out_dir, "cli-help.txt"), "w") as fh:
            fh.write(f"<<failed to render CLI help: {exc!r}>>\n")
        return

    from contextlib import redirect_stdout, suppress
    from io import StringIO

    def _capture(argv: list[str]) -> str:
        buf = StringIO()
        with redirect_stdout(buf), suppress(SystemExit):
            main(argv)
        return buf.getvalue()

    blocks = {
        "root": (["--help"], "mew --help"),
        "run": (["run", "--help"], "mew run --help"),
        "ls": (["list", "--help"], "mew list --help"),
        "profile": (["profile", "--help"], "mew profile --help"),
        "compare": (["compare", "--help"], "mew compare --help"),
        "completions": (["completions", "--help"], "mew completions --help"),
    }
    with open(os.path.join(out_dir, "cli-help.txt"), "w") as fh:
        fh.writelines(f"$ {title}\n{_capture(argv)}\n" for argv, title in blocks.values())

    for slug, (argv, title) in blocks.items():
        with open(os.path.join(out_dir, f"cli-help-{slug}.txt"), "w") as fh:
            fh.write(f"$ {title}\n{_capture(argv)}")


_render_cli_help()

# -- Suppress warnings for known third-party intersphinx gaps ----------------

nitpicky = False
suppress_warnings = ["myst.header"]

# -- linkcheck ---------------------------------------------------------------
# GitHub's HTML commit pages aggressively rate-limit unauthenticated linkcheck
# requests, which stalls CI.
linkcheck_ignore = [r"https://github\.com/.*/commits/.*"]

# Add docs/ to sys.path so any local helpers are importable.
sys.path.insert(0, os.path.dirname(__file__))
