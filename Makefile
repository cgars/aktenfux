.PHONY: verify

PYTHON ?= python

verify:
	$(PYTHON) scripts/verify.py
