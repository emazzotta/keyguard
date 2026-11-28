.PHONY: all build client test test-swift test-cli test-python clean install uninstall start stop restart status help
.DEFAULT_GOAL := all

PREFIX             ?= /usr/local
LAUNCH_AGENTS      := $(HOME)/Library/LaunchAgents
LOGS_DIR           := $(HOME)/Library/Logs
SECRETS_FILE       ?= $(or $(KEYGUARD_SECRETS_FILE),$(HOME)/.keyguard/secrets.enc)
STORE_URL          ?= $(KEYGUARD_STORE_URL)
STORE              ?= $(KEYGUARD_STORE)
RECIPIENTS_FILE    ?= $(KEYGUARD_RECIPIENTS_FILE)
BRIDGE_CONFIG_FILE ?= $(or $(KEYGUARD_BRIDGE_CONFIG_FILE),$(HOME)/.mac-bridge-endpoints.yaml)
LOG_FILE           ?= $(or $(KEYGUARD_LOG_FILE),$(HOME)/.keyguard/access.log)
PYTEST_WITH        ?=
VERSION            := $(shell sed -n 's/^__version__ = "\(.*\)"$$/\1/p' ../keyguard_passkey/__init__.py)
PACKED_CLIENT      := .build/client/keyguard-passkey.pyz
PHONE_CLIENT_PYZ   ?= $(or $(KEYGUARD_PHONE_CLIENT_PYZ),$(PACKED_CLIENT))
PHONE_SIGNING_KEY  ?= $(or $(KEYGUARD_PHONE_SIGNING_KEY),$(HOME)/.keyguard/mac-sign.key)

CORE_SOURCES      := $(wildcard Sources/KeyguardCore/*.swift)
CLI_SOURCES       := $(wildcard Sources/keyguard/*.swift)
SWIFT_TEST_SUITES := $(basename $(notdir $(wildcard Tests/KeyguardCoreTests/*.swift)))

BINARY     := $(PREFIX)/bin/keyguard
SERVER_DIR := $(PREFIX)/lib/keyguard
SERVER     := $(SERVER_DIR)/keyguard-server.py
UNLOCK_DIR := $(SERVER_DIR)/unlock
PLIST      := $(LAUNCH_AGENTS)/com.keyguard.server.plist

all: clean build install restart ## Build, install, and restart the server

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'
	@echo ""
	@echo "  Usage from Docker container:"
	@echo "    curl http://host.docker.internal:7777/<secret-name>"
	@echo ""
	@echo "  Manage secrets (run on host):"
	@echo "    keyguard clear                 # wipe all secrets and encryption key"
	@echo "    keyguard import path/to/.env   # merge from plaintext (additive, then delete source)"
	@echo "    keyguard set MY_API_TOKEN      # set a value (prompts for input)"
	@echo "    keyguard delete MY_API_TOKEN   # remove a key (requires Touch ID)"
	@echo "    keyguard mv OLD NEW            # rename a key (requires Touch ID)"
	@echo "    keyguard list                  # list key names (requires Touch ID)"
	@echo "    keyguard export                # print all values (requires Touch ID)"
	@echo ""
	@echo "  Custom secrets file path:"
	@echo "    export KEYGUARD_SECRETS_FILE=/path/to/secrets.enc"
	@echo ""
	@echo "  Custom store folder and pinned recipients:"
	@echo "    export KEYGUARD_STORE=/path/to/store"
	@echo "    export KEYGUARD_RECIPIENTS_FILE=/path/to/recipients"
	@echo ""
	@echo "  Store service the server syncs with:"
	@echo "    export KEYGUARD_STORE_URL=https://unlock.example.ts.net"
	@echo ""
	@echo "  Custom bridge config path:"
	@echo "    export KEYGUARD_BRIDGE_CONFIG_FILE=/path/to/bridge.yaml"
	@echo ""
	@echo "  Custom access log path:"
	@echo "    export KEYGUARD_LOG_FILE=/path/to/access.log"

build: bin/keyguard ## Compile the Swift binary

client: ## Pack the phone client of release v$(VERSION) from this repository's tag
	@rm -f "$(PACKED_CLIENT)"
	@mkdir -p "$(dir $(PACKED_CLIENT))"
	@python3 ../tools/vendor.py --pyz "v$(VERSION)" "$(dir $(PACKED_CLIENT))" || \
		echo "Could not pack v$(VERSION); 'git fetch --tags' and retry, or set KEYGUARD_PHONE_CLIENT_PYZ."

bin/keyguard: $(CLI_SOURCES) $(CORE_SOURCES)
	swift build -c release 2>&1
	mkdir -p bin
	cp .build/release/keyguard bin/keyguard
	codesign --sign - bin/keyguard

test: test-swift test-cli test-python ## Run all tests

test-swift: ## Run Swift unit tests
	@mkdir -p bin
	@for suite in $(SWIFT_TEST_SUITES); do \
		echo "== $$suite"; \
		swiftc -parse-as-library $(CORE_SOURCES) Tests/KeyguardCoreTests/$$suite.swift -o "bin/test-$$suite" || exit 1; \
		"bin/test-$$suite" || exit 1; \
	done

test-cli: ## Drive the whole CLI end to end (Linux only; skips elsewhere)
	@bash Tests/linux-cli-test.sh

test-python: ## Run Python tests
	uv run --locked $(PYTEST_WITH) python3 -m pytest Tests/ -v

install: build $(if $(filter $(PACKED_CLIENT),$(PHONE_CLIENT_PYZ)),client) ## Install binary, server, and register launchd agent
	sudo install -d "$(PREFIX)/bin" "$(SERVER_DIR)" "$(SERVER_DIR)/keyguard_server"
	sudo install -m 755 bin/keyguard "$(BINARY)"
	sudo install -m 755 src/keyguard-server.py "$(SERVER)"
	sudo install -m 644 src/keyguard_server/*.py "$(SERVER_DIR)/keyguard_server/"
	@if [ -f "$(PHONE_CLIENT_PYZ)" ]; then \
		sudo install -d "$(UNLOCK_DIR)" && \
		sudo install -m 755 "$(PHONE_CLIENT_PYZ)" "$(UNLOCK_DIR)/keyguard-passkey.pyz" && \
		sudo install -m 755 src/pending.py "$(UNLOCK_DIR)/pending.py" && \
		sudo rm -f "$(UNLOCK_DIR)/unlock.py" "$(UNLOCK_DIR)/unlock_client.py" "$(UNLOCK_DIR)/hold.py"; \
	else \
		echo "No keyguard-passkey client at $(PHONE_CLIENT_PYZ): reads stay Touch ID only."; \
	fi
	mkdir -p "$(LAUNCH_AGENTS)" "$(LOGS_DIR)"
	sed 's|__PREFIX__|$(PREFIX)|g; s|__HOME__|$(HOME)|g; s|__SECRETS_FILE__|$(SECRETS_FILE)|g; s|__STORE_URL__|$(STORE_URL)|g; s|__STORE__|$(STORE)|g; s|__RECIPIENTS_FILE__|$(RECIPIENTS_FILE)|g; s|__BRIDGE_CONFIG_FILE__|$(BRIDGE_CONFIG_FILE)|g; s|__LOG_FILE__|$(LOG_FILE)|g; s|__PHONE_CLIENT__|$(UNLOCK_DIR)/keyguard-passkey.pyz|g; s|__PHONE_SIGNING_KEY__|$(PHONE_SIGNING_KEY)|g' \
		com.keyguard.server.plist > "$(PLIST)"
	@echo "Installed. Run 'make start' to start the server."

clean: ## Remove compiled binary
	rm -f bin/keyguard

uninstall: stop ## Remove all installed files
	sudo rm -f "$(BINARY)"
	sudo rm -rf "$(SERVER_DIR)" "$(SERVER_DIR)/keyguard_server"
	rm -f "$(PLIST)"

start: ## Start the keyguard server via launchd
	launchctl load "$(PLIST)"

stop: ## Stop the keyguard server
	-launchctl unload "$(PLIST)" 2>/dev/null

restart: stop start ## Restart the keyguard server

status: ## Show server status
	@launchctl list | grep keyguard || echo "keyguard not running"
