.PHONY: all build clean install uninstall start stop restart status help
.DEFAULT_GOAL := all

PREFIX        ?= /usr/local
LAUNCH_AGENTS := $(HOME)/Library/LaunchAgents
LOGS_DIR      := $(HOME)/Library/Logs

BINARY     := $(PREFIX)/bin/keyguard
SERVER_DIR := $(PREFIX)/lib/keyguard
SERVER     := $(SERVER_DIR)/keyguard-server.py
PLIST      := $(LAUNCH_AGENTS)/com.keyguard.server.plist

all: install restart ## Build, install, and restart the server

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
	@echo "    keyguard set MY_API_TOKEN val  # set a value inline"
	@echo "    keyguard delete MY_API_TOKEN   # remove a key (requires Touch ID)"
	@echo "    keyguard list                  # list key names (requires Touch ID)"
	@echo "    keyguard export                # print all values (requires Touch ID)"
	@echo ""
	@echo "  Custom secrets file path:"
	@echo "    export KEYGUARD_SECRETS_FILE=/path/to/secrets.enc"

build: bin/keyguard ## Compile the Swift binary

bin/keyguard: src/keyguard.swift
	mkdir -p bin
	swiftc src/keyguard.swift -o bin/keyguard -framework Security -framework LocalAuthentication -framework CryptoKit
	codesign --sign - bin/keyguard

install: build ## Install binary, server, and register launchd agent
	sudo install -d "$(PREFIX)/bin" "$(SERVER_DIR)"
	sudo install -m 755 bin/keyguard "$(BINARY)"
	sudo install -m 644 src/keyguard-server.py "$(SERVER)"
	mkdir -p "$(LAUNCH_AGENTS)" "$(LOGS_DIR)"
	sed 's|__PREFIX__|$(PREFIX)|g; s|__HOME__|$(HOME)|g' \
		com.keyguard.server.plist > "$(PLIST)"
	@echo "Installed. Run 'make start' to start the server."

clean: ## Remove compiled binary
	rm -f bin/keyguard

uninstall: stop ## Remove all installed files
	sudo rm -f "$(BINARY)"
	sudo rm -rf "$(SERVER_DIR)"
	rm -f "$(PLIST)"

start: ## Start the keyguard server via launchd
	launchctl load "$(PLIST)"

stop: ## Stop the keyguard server
	-launchctl unload "$(PLIST)" 2>/dev/null

restart: stop start ## Restart the keyguard server

status: ## Show server status
	@launchctl list | grep keyguard || echo "keyguard not running"
