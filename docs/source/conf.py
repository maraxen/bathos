# Configuration file for the Sphinx documentation builder.
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/config.html

import sys
from pathlib import Path

# Add source directory to path for autodoc
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

# Project information
project = "bathos"
copyright = "2026, Marielle Russo"
author = "Marielle Russo"
release = "0.1.0"

# General configuration
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# Options for autodoc
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
}

# Suppress harmless warnings
suppress_warnings = [
    "ref.class",  # pathlib.Path and similar stdlib types
    "autodoc",  # Suppress autodoc import errors for modules still under development
]

# Options for HTML output
html_theme = "alabaster"
html_static_path = ["_static"]
html_theme_options = {
    "github_user": "marielle-russo",
    "github_repo": "bathos",
}
