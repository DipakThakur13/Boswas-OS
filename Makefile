# Boswas OS - convenience targets. The scripts are the source of truth.
.DEFAULT_GOAL := help
.PHONY: help build iso packages test test-quick test-boot clean distclean

help: ## Show this help
	@echo "Boswas OS v1 build targets:"
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{ printf "  make %-11s %s\n", $$1, $$2 }'

build: ## Build the ISO (container mode automatically on non-Debian-13 hosts)
	./build.sh

iso: build ## Alias for build

packages: ## Build and check only the Boswas .deb packages (static + lintian + unit tests)
	./test.sh --packages-only

test: ## Run all tests that the environment supports (incl. QEMU boot test)
	./test.sh

test-quick: ## Static/unit/package tests and ISO inspection, no VM boot
	./test.sh --no-boot

test-boot: ## Only the QEMU boot test of the built ISO
	./test.sh --boot-only

clean: ## Remove outputs, logs, manifests and work trees
	./clean.sh

distclean: ## clean + package cache + builder image
	./clean.sh --all
