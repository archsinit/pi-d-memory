PYTHON ?= python3
NODE ?= node

.PHONY: test test-history test-controller test-reviewer

test: test-history test-controller test-reviewer

test-history:
	$(PYTHON) -B -m unittest discover -s helpers/tests -v

test-controller:
	$(PYTHON) -B -m unittest discover -s automation/tests -p 'test_*.py' -v

test-reviewer:
	$(NODE) --test automation/tests/run_review.test.mjs
