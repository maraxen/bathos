# Makefile for Sphinx documentation at docs/
#
# You can set these variables from the command line.

SPHINXBUILD   = sphinx-build
SOURCEDIR     = source
BUILDDIR      = build

# Default target
.PHONY: help Makefile clean html

help:
	@echo "Sphinx documentation help:"
	@echo "  make html      Build HTML documentation"
	@echo "  make clean     Remove build directory"
	@echo "  make distclean Remove both build and all temporary files"

clean:
	rm -rf $(BUILDDIR)

distclean: clean
	find $(SOURCEDIR) -name "*.pyc" -delete
	find $(SOURCEDIR) -type d -name "__pycache__" -delete

html:
	$(SPHINXBUILD) -b html $(SOURCEDIR) $(BUILDDIR)/html

.DEFAULT_GOAL := help
