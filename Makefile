# ── Colours ───────────────────────────────────────────────────────────────────
BLUE   := \033[36m
YELLOW := \033[33m
GREEN  := \033[32m
RED    := \033[31m
RESET  := \033[0m

.DEFAULT_GOAL := help

# ── Config ────────────────────────────────────────────────────────────────────
IMAGE       ?= sweep
TAG         ?= latest
PORT        ?= 8080
PYTHON      ?= 3.10
REGISTRY    ?= ghcr.io/sweepai

DEV_COMPOSE := docker-compose.dev.yml

# uv platform triples — must match Debian glibc (python:3.10-slim base)
UV_PLATFORM_AMD64 := x86_64-manylinux_2_28
UV_PLATFORM_ARM64 := aarch64-manylinux_2_28

# Detect host arch; used to choose the right lockfile
HOST_ARCH   := $(shell uname -m)
ifeq ($(HOST_ARCH),arm64)
  LOCKFILE  := requirements.arm64.txt
  PLATFORM  := linux/arm64
else
  LOCKFILE  := requirements.txt
  PLATFORM  := linux/amd64
endif

# ── Help ──────────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Display this help
	@awk 'BEGIN {FS = ":.*##"; printf "\n$(BLUE)Usage:$(RESET)\n  make $(YELLOW)<target>$(RESET)\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  $(YELLOW)%-25s$(RESET) %s\n", $$1, $$2 } \
		/^##@/ { printf "\n$(GREEN)%s$(RESET)\n", substr($$0, 5) }' $(MAKEFILE_LIST)

# ── Info ──────────────────────────────────────────────────────────────────────
.PHONY: info
info: ## Show resolved build config
	@printf "\n$(BLUE)Build config$(RESET)\n"
	@printf "  $(YELLOW)%-18s$(RESET) %s\n" "Host arch"   "$(HOST_ARCH)"
	@printf "  $(YELLOW)%-18s$(RESET) %s\n" "Platform"    "$(PLATFORM)"
	@printf "  $(YELLOW)%-18s$(RESET) %s\n" "Lockfile"    "$(LOCKFILE)"
	@printf "  $(YELLOW)%-18s$(RESET) %s\n" "Image"       "$(IMAGE):$(TAG)"
	@printf "  $(YELLOW)%-18s$(RESET) %s\n" "Port"        "$(PORT)"
	@printf "  $(YELLOW)%-18s$(RESET) %s\n" "Dev compose" "$(DEV_COMPOSE)"
	@echo ""

##@ Dependencies
.PHONY: install
install: ## Install dependencies locally via uv
	uv sync

.PHONY: lock
lock: ## Compile lockfiles for BOTH platforms (requires uv)
	@echo "$(BLUE)Compiling amd64 lockfile...$(RESET)"
	uv pip compile pyproject.toml \
		--python-version $(PYTHON) \
		--python-platform $(UV_PLATFORM_AMD64) \
		-o requirements.txt
	@echo "$(BLUE)Compiling arm64 lockfile...$(RESET)"
	uv pip compile pyproject.toml \
		--python-version $(PYTHON) \
		--python-platform $(UV_PLATFORM_ARM64) \
		-o requirements.arm64.txt
	@echo "$(GREEN)✓ Both lockfiles updated$(RESET)"

.PHONY: lock-amd64
lock-amd64: ## Compile amd64 lockfile only
	uv pip compile pyproject.toml \
		--python-version $(PYTHON) \
		--python-platform $(UV_PLATFORM_AMD64) \
		-o requirements.txt

.PHONY: lock-arm64
lock-arm64: ## Compile arm64 lockfile only (for M-series Macs)
	uv pip compile pyproject.toml \
		--python-version $(PYTHON) \
		--python-platform $(UV_PLATFORM_ARM64) \
		-o requirements.arm64.txt

.PHONY: lock-check
lock-check: ## Verify lockfiles are in sync with pyproject.toml (CI use)
	@echo "$(BLUE)Checking amd64 lockfile...$(RESET)"
	uv pip compile pyproject.toml \
		--python-version $(PYTHON) \
		--python-platform $(UV_PLATFORM_AMD64) \
		-o /tmp/req.check.txt
	@diff requirements.txt /tmp/req.check.txt > /dev/null || \
		(echo "$(RED)✗ requirements.txt is out of sync — run: make lock$(RESET)" && exit 1)
	@echo "$(BLUE)Checking arm64 lockfile...$(RESET)"
	uv pip compile pyproject.toml \
		--python-version $(PYTHON) \
		--python-platform $(UV_PLATFORM_ARM64) \
		-o /tmp/req.arm64.check.txt
	@diff requirements.arm64.txt /tmp/req.arm64.check.txt > /dev/null || \
		(echo "$(RED)✗ requirements.arm64.txt is out of sync — run: make lock$(RESET)" && exit 1)
	@echo "$(GREEN)✓ Lockfiles are up to date$(RESET)"

##@ Docker — development (live-reload, mounted source)
.PHONY: dev-build
dev-build: ## Build the dev image (arm64, no cache bust)
	@echo "$(BLUE)Building dev image for $(PLATFORM)...$(RESET)"
	docker compose -f $(DEV_COMPOSE) build \
		--build-arg LOCKFILE=$(LOCKFILE)
	@echo "$(GREEN)✓ Dev image ready$(RESET)"

.PHONY: dev-rebuild
dev-rebuild: ## Force-rebuild the dev image from scratch (no layer cache)
	@echo "$(BLUE)Rebuilding dev image (no cache)...$(RESET)"
	docker compose -f $(DEV_COMPOSE) build \
		--no-cache \
		--build-arg LOCKFILE=$(LOCKFILE)
	@echo "$(GREEN)✓ Dev image rebuilt$(RESET)"

.PHONY: dev-up
dev-up: ## Start dev container (builds if image missing), tail logs
	@echo "$(BLUE)Starting dev stack...$(RESET)"
	docker compose -f $(DEV_COMPOSE) up --build

.PHONY: dev-up-d
dev-up-d: ## Start dev container in the background (detached)
	@echo "$(BLUE)Starting dev stack (detached)...$(RESET)"
	docker compose -f $(DEV_COMPOSE) up --build -d
	@echo "$(GREEN)✓ Running at http://localhost:$(PORT)$(RESET)"
	@echo "  Logs: make dev-logs   Shell: make dev-shell"

.PHONY: dev-down
dev-down: ## Stop and remove dev container + network (keeps named volumes)
	docker compose -f $(DEV_COMPOSE) down
	@echo "$(GREEN)✓ Dev stack stopped$(RESET)"

.PHONY: dev-clean
dev-clean: ## Stop dev stack AND remove named volumes (fresh-slate reset)
	docker compose -f $(DEV_COMPOSE) down -v
	@echo "$(GREEN)✓ Dev stack and volumes removed$(RESET)"

.PHONY: dev-logs
dev-logs: ## Tail logs from the dev container
	docker compose -f $(DEV_COMPOSE) logs -f

.PHONY: dev-shell
dev-shell: ## Open an interactive bash shell in the running dev container
	docker compose -f $(DEV_COMPOSE) exec hosted bash

.PHONY: dev-restart
dev-restart: ## Restart the dev container without rebuilding
	docker compose -f $(DEV_COMPOSE) restart hosted
	@echo "$(GREEN)✓ Dev container restarted$(RESET)"

##@ Docker — local (native arch)
.PHONY: build
build: ## Build image for your current machine arch (auto-detected)
	@echo "$(BLUE)Building $(IMAGE):$(TAG) for $(PLATFORM)...$(RESET)"
	docker build \
		--platform $(PLATFORM) \
		--build-arg LOCKFILE=$(LOCKFILE) \
		-t $(IMAGE):$(TAG) .
	@echo "$(GREEN)✓ Built $(IMAGE):$(TAG)$(RESET)"

.PHONY: run
run: ## Run the container locally (detached)
	.venv/bin/uvicorn sweepai.api:app --reload --port 8080 --env-file .en

##@ Docker — explicit arch
.PHONY: build-arm64
build-arm64: ## Build image explicitly for arm64 (M-series Mac / Graviton)
	docker build \
		--platform linux/arm64 \
		--build-arg LOCKFILE=requirements.arm64.txt \
		-t $(IMAGE):$(TAG)-arm64 .
	@echo "$(GREEN)✓ Built $(IMAGE):$(TAG)-arm64$(RESET)"

.PHONY: build-amd64
build-amd64: ## Build image explicitly for amd64
	docker build \
		--platform linux/amd64 \
		--build-arg LOCKFILE=requirements.txt \
		-t $(IMAGE):$(TAG)-amd64 .
	@echo "$(GREEN)✓ Built $(IMAGE):$(TAG)-amd64$(RESET)"

##@ Quality
.PHONY: test-cov-simple
test-cov-simple: ## Run ALL tests with simple console coverage report (shows missing lines)
	@echo "$(GREEN)Running All Tests with Simple Coverage Report...$(RESET)"
	uv run pytest \
		--cov=mychat_reflex \
		--cov-report=term-missing \
		--cov-fail-under=0

.PHONY: test-unit
test-unit: ## Run only unit tests (fast, no coverage)
	@echo "$(GREEN)Running Unit Tests...$(RESET)"
	uv run pytest -m "unit"

.PHONY: test-integration
test-integration: ## Run only integration tests
	@echo "$(GREEN)Running Integration Tests...$(RESET)"
	uv run pytest -m "integration"

.PHONY: lint
lint: ## Run ruff + pylint locally
	uv run ruff check sweepai/
	uv run pylint sweepai/

.PHONY: ts-check
ts-check: ## Verify tree-sitter aarch64 wheels resolve correctly (dry-run)
	uv pip compile pyproject.toml \
		--python-version $(PYTHON) \
		--python-platform $(UV_PLATFORM_ARM64) \
		--dry-run 2>&1 | grep tree-sitter

##@ Housekeeping
.PHONY: clean
clean: stop ## Remove local image and stopped containers
	docker rmi $(IMAGE):$(TAG) 2>/dev/null || true
	docker rmi $(IMAGE):$(TAG)-arm64 2>/dev/null || true
	docker rmi $(IMAGE):$(TAG)-amd64 2>/dev/null || true
	@echo "$(GREEN)✓ Cleaned$(RESET)"

.PHONY: prune
prune: ## Docker system prune (removes ALL unused images/layers — be careful)
	@printf "$(RED)This will prune ALL unused Docker resources. Continue? [y/N] $(RESET)" && \
		read ans && [ $${ans:-N} = y ]
	docker system prune -f