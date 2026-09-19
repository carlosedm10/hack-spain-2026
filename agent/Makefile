.PHONY: test

test:
	@echo ":: test: agent"
	uv run --frozen pytest $(if $(TEST),$(TEST),tests)
