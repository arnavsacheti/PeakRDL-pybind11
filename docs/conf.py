# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

from datetime import datetime
import os
import sys

# Add the project root directory to the path
sys.path.insert(0, os.path.abspath('..'))

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = 'PeakRDL-pybind11'
copyright = f'{datetime.now().year}, Arnav Sacheti'
author = 'Arnav Sacheti'
# Version intentionally omitted — the rendered docs follow the latest
# tagged release on PyPI and the inline number drifts from reality as
# soon as it's hard-coded. Leaving ``release`` empty drops the version
# line from the Sphinx header.
release = ''

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx.ext.viewcode',
    'sphinx.ext.intersphinx',
    'sphinx_autodoc_typehints',
]

templates_path = ['_templates']

# RST-only setup. We intentionally do NOT enable myst_parser; the design
# sketch is exposed via a `:download:` link in index.rst rather than rendered
# inline. Keep ``IDEAL_API_SKETCH.md`` out of source discovery so Sphinx does
# not warn about an orphaned non-RST document.
#
# The ``concepts/*`` toctree entries in ``index.rst`` are populated by
# sibling documentation work units. Until those land, Sphinx will warn about
# missing documents -- that is expected and acceptable.
exclude_patterns = [
    '_build',
    'Thumbs.db',
    '.DS_Store',
    'IDEAL_API_SKETCH.md',
]

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'sphinx_book_theme'
html_static_path = ['_static']

# sphinx_book_theme picks up the repository URL from these options and
# adds a GitHub button to the rendered header so users can jump back to
# the source without scrolling for a link in the body.
html_theme_options = {
    'repository_url': 'https://github.com/arnavsacheti/PeakRDL-pybind11',
    'repository_branch': 'main',
    'use_repository_button': True,
    'use_issues_button': True,
    'use_source_button': True,
}

# -- Options for autodoc -----------------------------------------------------
autodoc_member_order = 'bysource'
autodoc_typehints = 'description'

# -- Options for intersphinx -------------------------------------------------
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'systemrdl': ('https://systemrdl-compiler.readthedocs.io/en/stable/', None),
}
