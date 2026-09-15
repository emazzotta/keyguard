# keyguard

[![Pipeline](https://github.com/emazzotta/keyguard/actions/workflows/pipeline.yml/badge.svg)](https://github.com/emazzotta/keyguard/actions/workflows/pipeline.yml)

A lightweight, local secret manager for macOS. Encrypts each secret on disk as its own [age](https://age-encryption.org) file and gates every decryption behind Touch ID. Exposes secrets over a local HTTP server so Docker containers or local scripts can fetch them at runtime without credentials ever being baked into images or environment variables.

## How it works

```
Docker container
  └── curl http://host.docker.internal:7777/TOKEN,PASSWORD
        └── keyguard-server (host, port 7777)
              └── keyguard get TOKEN PASSWORD
                    ├── Touch ID prompt: "Reveal TOKEN, PASSWORD"  (once, not once per key)
                    ├── age-decrypt the index, then only the two files it names
                    └── return TOKEN=value\nPASSWORD=value
```

Secrets never exist in plaintext on disk, and every read requires a fingerprint.

### The store

```
keyguard-store/
├── meta.json       plaintext: a SHA-256 per file, so damage is detectable
├── index.age       the salt, and the name -> file mapping
└── vars/
    └── <sha256(salt || NAME)>.age     one secret, holding {name, value}
```

One file per secret rather than one blob, which buys three things: a reader
decrypts only what it was asked for, two writers adding different secrets stop
colliding, and `list` never has to decrypt any values at all.

Filenames are salted hashes and the salt lives inside `index.age`, so a copy of
the directory reveals how many secrets exist and nothing about which. Each
secret's name is stored *inside* its own ciphertext, so a swapped file is
detected on read rather than silently returning the wrong value. Values are
padded to a 256-byte boundary so file sizes do not leak secret lengths.

The recipient set is held in two places - `~/.keyguard/recipients` on the
machine, and canonically inside `index.age` - and compared on every operation.
They diverging is the one failure that would otherwise be silent and total, so
it hard-fails rather than picking a winner.

## Requirements

- macOS with Touch ID
- [`age`](https://age-encryption.org) and `age-keygen` (`brew install age`)
- Python 3 (pre-installed on macOS)
- Xcode Command Line Tools (`xcode-select --install`)

## Installation

```bash
git clone https://github.com/emazzotta/keyguard.git
cd keyguard
make
```

This compiles the Swift binary, installs everything to `/usr/local`, and registers a launchd agent that starts automatically at login.

## Managing secrets

**Import from an existing `.env` file (additive — merges with existing secrets):**
```bash
keyguard import path/to/.env           # prompts for each conflicting key
keyguard import path/to/.env --force   # overwrites all conflicts without asking
rm path/to/.env                        # delete the plaintext source
```

When a key already exists, the interactive prompt asks whether to overwrite or keep the original. In non-interactive mode (piped input), conflicts are skipped by default - use `--force` to overwrite.

Supported `.env` formats:
```bash
TOKEN=abc123
export TOKEN=abc123
export TOKEN="abc123"
export TOKEN='abc123'
export TOKEN="abc123" # inline comments are stripped
```

**Set a single secret (prompts for value, nothing saved to shell history):**
```bash
keyguard set MY_API_TOKEN
# Value for MY_API_TOKEN: ▌
```

**Get one or multiple secrets:**
```bash
keyguard get MY_API_TOKEN                    # returns raw value
keyguard get MY_API_TOKEN PASSWORD DB_URL    # returns KEY=value lines
```

Touch ID prompt shows exactly what is being revealed: `"Reveal MY_API_TOKEN, PASSWORD, DB_URL"`.

**Other commands (all require Touch ID):**
```bash
keyguard list                                        # list key names
keyguard export                                      # print all key=value pairs
keyguard delete MY_API_TOKEN                         # remove a key
keyguard mv HETZNER_USER HETZNER_ACCOUNT_USER        # rename a key (alias: rename)
keyguard mv OLD NEW --force                          # overwrite NEW if it already exists
keyguard verify                                      # check the store against its manifest
keyguard clear                                       # wipe the store and its identity
```

`verify` is the only command that needs no Touch ID: it hashes what is on disk
and compares it with `meta.json`, which catches a half-applied write or a
sync-conflict copy without decrypting anything.

**Backup and restore the identity:**
```bash
keyguard export-key                       # print the age identity to stdout
keyguard import-key                       # import interactively (prompts for paste)
keyguard import-key AGE-SECRET-KEY-1...   # import from argument
echo "AGE-SECRET-KEY-1..." | keyguard import-key
```

`export-key` prints the age identity that opens every file in the store. Keep it
somewhere safe: with it and a copy of the store directory you can restore on any
Mac. `import-key` refuses to overwrite an existing identity - run `keyguard
clear` first if you mean to replace it.

## Migrating from the pre-age store

Earlier keyguard kept one AES-256-GCM blob at `~/.keyguard/secrets.enc`. To convert:

```bash
keyguard migrate
```

It reads every secret back out of the new store and compares it with the source
before reporting success; if anything differs, the new store is moved aside and
nothing else is touched. **The old file and its Keychain key are left exactly as
they were**, so reinstalling the previous keyguard is a complete rollback. Delete
them yourself once you are satisfied.

## Using from Docker

From inside any Docker container on the same machine:

```bash
curl http://host.docker.internal:7777/MY_API_TOKEN          # single value
curl http://host.docker.internal:7777/MY_API_TOKEN,PASSWORD  # KEY=value lines
curl http://host.docker.internal:7777/_keys                  # list all key names
```

Every GET request triggers a Touch ID prompt on the host showing the exact key names being revealed. Each secret read appends a line to `~/.keyguard/access.log` with an ISO timestamp, the source IP with resolved names (reverse DNS hostname and/or Docker container name), and the keys read. No GUI notifications - tail the log to watch reads in real time. Clients can send an `X-Keyguard-Source` header (e.g., the container hostname) to identify themselves - the server resolves it to a container name via `docker inspect` when possible.

Optionally, append `?timeout=N` to cache decrypted values in the server's process memory for up to `N` seconds (max 300), reducing repeated Touch ID prompts. Cache entries are scoped to the requesting IP - a different container cannot read another's cache unless explicitly allowed with `?share=all` or `?share=172.17.0.2,172.17.0.3`. Cached reads still write a log line marked `(cached)`, and the Touch ID prompt shows the duration for informed consent (`"Reveal TOKEN (cached for 60s)"`). By default there is no caching. Flush with `DELETE /_cache`.

**Storing a secret from a container (POST):**
```bash
curl -s -X POST http://host.docker.internal:7777/MY_API_TOKEN -d 'the-value'
```

`POST /<name>` stores or updates a secret. The value is read from the request body. Triggers Touch ID on the host (`"Update MY_API_TOKEN"`). Responds with `Set 'MY_API_TOKEN'` on success.

The server only accepts connections from localhost and Docker's internal networks — other devices on the local network are rejected.

## Bridge endpoints

The bridge lets you expose whitelisted Mac commands over HTTP. A Docker container can trigger a Spotify pause, send a system notification, or call any script you pre-approve — without SSH, without scripting on the host, and without exposing arbitrary command execution.

### Setup

Install PyYAML (one-time):

```bash
pip3 install pyyaml
```

Copy the example config to your home directory and customise it:

```bash
cp .mac-bridge-endpoints.yaml.example ~/.mac-bridge-endpoints.yaml
chmod 600 ~/.mac-bridge-endpoints.yaml
$EDITOR ~/.mac-bridge-endpoints.yaml
```

The file is gitignored — it never enters the repo. To use a different path, set `KEYGUARD_BRIDGE_CONFIG_FILE` in your shell profile **before** running `make install` — the value is baked into the launchd plist (same pattern as `KEYGUARD_SECRETS_FILE`):

```bash
export KEYGUARD_BRIDGE_CONFIG_FILE=~/Dropbox/keyguard/bridge.yaml
make install
make restart
```

### Generating and storing the token

The bridge token always lives inside keyguard itself — no plaintext-on-disk option. Generate a random value and store it under the fixed name `MAC_BRIDGE_TOKEN`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" \
  | xargs -0 keyguard set MAC_BRIDGE_TOKEN
```

That's the entire token setup. The YAML config only contains endpoint definitions.

The server reads `MAC_BRIDGE_TOKEN` from keyguard on the **first authenticated bridge request** (lazy load) — one Touch ID prompt per server lifetime, then cached in process memory. Send `SIGHUP` to force a re-resolve.

That prompt names the endpoint that triggered it — `"Reveal MAC_BRIDGE_TOKEN for bridge endpoint resolve-mcp-start"` — so you approve a specific command rather than an anonymous token read.

The name is **verified, not asserted**. The server passes only a bare key via `keyguard get MAC_BRIDGE_TOKEN --bridge-endpoint <name>`, and the CLI itself looks that name up in `~/.mac-bridge-endpoints.yaml` before it will show it. A name that is not a configured endpoint, or that is anything other than an identifier (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`), is silently dropped and you get the bare `"Reveal MAC_BRIDGE_TOKEN"` instead. So the flag cannot be used to dress a token read up as something reassuring — nothing calling the CLI can put its own words in the dialog, and the prompt never claims an endpoint it has not confirmed. Reads triggered by `_bridge/list` dispatch no endpoint and therefore name none.

Two layers protect the Touch ID prompt from abuse:

1. Requests without a well-formed `Authorization: Bearer …` header are rejected **before** keyguard is invoked. Unauthenticated callers cannot trigger a prompt at all.
2. Failed resolutions (Touch ID denied, key missing) are rate-limited to one attempt per 60 seconds. A misconfigured client cannot spam prompts.

### Reloading config

Saving the YAML file is enough - the server checks the file's mtime on every bridge request and reloads automatically.

To force a reload that also clears the cached token and rate-limit state, send `SIGHUP`:

```bash
kill -HUP $(launchctl list | awk '/com.keyguard.server/{print $1}')
```

### Config format

```yaml
endpoints:
  spotify-play:
    command: [osascript, -e, 'tell application "Spotify" to play']
    method: POST          # GET | POST | [GET, POST]  (default: POST)

  system-notify:
    command: [osascript, -e, 'display notification "bridge triggered" with title "keyguard"']
    method: POST

  uptime:
    command: [/usr/bin/uptime]
    method: GET           # read-only by convention

  say:
    command: [/usr/bin/say]
    method: POST
    stdin: true           # pipe POST body to the command's stdin
    timeout: 15           # seconds before the process is killed (default: 60)

  ping:
    command: [/usr/bin/true]
    method: GET
    public: true          # callable without Authorization - see warning below
```

**Command rules:**
- Must be a YAML list — no shell string, no glob expansion, no interpolation.
- The executable path must be absolute, or resolvable via the server's `$PATH`.
- No user-controlled values are ever passed into command arguments — the only caller input that reaches the command is the POST body via `stdin: true`.

**The `public` flag (auth bypass — exception, never the default):**

By default every endpoint requires `Authorization: Bearer <MAC_BRIDGE_TOKEN>`. Adding `public: true` to a single endpoint disables that check **for that endpoint only** — no token, no Touch ID, just the IP allowlist as the gate. Use it sparingly:

- Suitable for: side-effect-light, non-secret-returning commands you'd be comfortable with anything on the local Docker network triggering — a status ping, a non-confidential notification, a "play/pause" toggle.
- Unsuitable for: anything that mutates persistent state, reveals secrets, runs external network calls, or that you'd be unhappy seeing called by a compromised container on the same machine.
- Strict parsing: only the literal YAML boolean `true` opens the gate. `public: "true"` (quoted), `public: 1`, `public: yes-but-no` all stay protected, with a warning logged. Default and missing values are protected.
- Method whitelist, stdin handling, timeout, and access logging all still apply to public endpoints - `public: true` only relaxes authentication, nothing else.

The `_bridge/list` endpoint is privilege-aware:

- **Without a bearer header (or with a wrong one)**: returns only endpoints marked `public: true`. Protected endpoint names never leak to anonymous callers. No Touch ID is triggered when the bearer header is absent.
- **With a valid bearer**: returns every endpoint, with the `public` field distinguishing them. The first authenticated call resolves the bridge token from keyguard (one Touch ID prompt per server lifetime, then cached).
- **With a bearer but token resolution fails** (Touch ID denied, rate-limited, key missing): the listing falls back to the public-only view rather than returning 503 - so listing remains useful for discovery even when the server cannot verify the caller's identity.

```bash
# Anonymous discovery - public endpoints only
curl -s http://host.docker.internal:7777/_bridge/list | jq
# [{"name":"ping","methods":["GET"],"timeout":60,"public":true}]

# Authenticated discovery - full list, mixed public + protected
curl -s -H "Authorization: Bearer $TOKEN" \
  http://host.docker.internal:7777/_bridge/list | jq
# [{"name":"ping","methods":["GET"],"timeout":60,"public":true},
#  {"name":"spotify-play","methods":["POST"],"timeout":60,"public":false}, ...]
```

### Usage from Docker

```bash
TOKEN="your-random-token-here"

# Trigger a command (POST)
curl -s -X POST http://host.docker.internal:7777/_bridge/spotify-play \
  -H "Authorization: Bearer $TOKEN"

# Read output (GET)
curl -s http://host.docker.internal:7777/_bridge/uptime \
  -H "Authorization: Bearer $TOKEN"

# Pass data as stdin (POST + stdin)
curl -s -X POST http://host.docker.internal:7777/_bridge/say \
  -H "Authorization: Bearer $TOKEN" \
  -d "hello from your container"

# Public endpoint - no Authorization header needed
curl -s http://host.docker.internal:7777/_bridge/ping

# Anonymous discovery - lists only public endpoints
curl -s http://host.docker.internal:7777/_bridge/list

# Authenticated discovery - lists every endpoint, public + protected
curl -s -H "Authorization: Bearer $TOKEN" http://host.docker.internal:7777/_bridge/list
```

Every successful bridge call appends a line to `~/.keyguard/access.log` with the endpoint name and caller IP. Failed calls are not logged.

### Security

| Concern | How it is handled |
|---|---|
| Unauthenticated call to a protected endpoint | `Authorization: Bearer <token>` required; 401 otherwise |
| Public endpoints (`public: true`) | Auth check is skipped by design — the IP allowlist is the only gate. Use only for side-effect-light, non-secret-returning commands you would be comfortable seeing called by anything on the local Docker network |
| Accidental opt-in to public | Strict parser: only the literal YAML boolean `true` opens the gate. `public: "true"`, `public: 1`, `public: maybe` all stay protected, with a warning logged |
| Token interception on the network | Only localhost and Docker internal subnets are accepted (same as keyguard secrets) |
| Token at rest | The token lives inside the age-encrypted keyguard store under `MAC_BRIDGE_TOKEN` — never on disk in plaintext |
| Touch ID prompt spam from unauthenticated callers | Requests without a `Bearer …` header are rejected *before* keyguard is invoked — no prompt fires for malformed/missing auth |
| Touch ID prompt spam from misconfigured callers | Failed token resolutions are rate-limited to 1 per 60 seconds; SIGHUP clears the limit |
| Touch ID prompt from public endpoint calls | Public endpoints never invoke keyguard — a million unauthenticated calls to a public endpoint cannot fire a single prompt |
| Command injection via request body | Body can only reach `stdin` — never command args. Commands are fixed lists, no shell involved |
| Arbitrary command execution | Only commands declared in the gitignored config file run; no dynamic dispatch |
| Endpoint enumeration | `_bridge/list` is privilege-aware: anonymous callers see only `public: true` endpoints, authenticated callers see the full list. Protected endpoint names do not leak to unauthenticated traffic. The list never reveals the underlying command |
| Token brute force | `hmac.compare_digest` (constant-time) prevents timing attacks; IP allowlist limits the attack surface to Docker networks |
| Config file leaks to git | `.mac-bridge-endpoints.yaml` is in `.gitignore` |

The config file itself is the trust boundary: only what you write into it can be called.

## Custom secrets file path

By default the store sits in a `keyguard-store` directory beside `KEYGUARD_SECRETS_FILE` if that is set, and at `~/.keyguard/store` otherwise. Sitting beside the old file is deliberate: that location is already wherever the secrets are backed up, and quietly relocating the store out of it would drop the off-machine copy. Override either with an environment variable:

```bash
export KEYGUARD_STORE=~/Dropbox/keyguard/keyguard-store
export KEYGUARD_RECIPIENTS_FILE=~/.keyguard/recipients   # keep this one local, never synced
```

Set this in your shell profile before running `make install` — the value is baked into the launchd plist automatically so the server always uses the correct path.

## Access log

Every successful secret read and successful bridge call writes one line to `~/.keyguard/access.log`:

```
2026-05-03T11:45:23Z 172.17.0.5 (my-container) MY_TOKEN
2026-05-03T11:45:24Z 172.17.0.5 (my-container) MY_TOKEN (cached)
2026-05-03T11:45:25Z 192.168.65.2 bridge:spotify-play
```

Format: ISO-8601 UTC timestamp, source (IP plus resolved hostname or container name), comma-separated key list (or `bridge:<name>`), optional `(cached)` marker. Failed reads, list operations, POSTs, and failed bridge calls are not logged.

Tail to watch reads live:

```bash
tail -f ~/.keyguard/access.log
```

Rotate, prune, or delete it whenever you want - the server recreates the file on the next write. To override the path:

```bash
export KEYGUARD_LOG_FILE=~/Library/Logs/keyguard/access.log
```

Set it before `make install` so the value is baked into the launchd plist.

## Testing the CLI off macOS

The CLI is macOS-only in production - Keychain, LocalAuthentication, CryptoKit -
which historically meant it could only be exercised by installing it. That is a
poor place to discover that a change removed the Touch ID prompt.

`Tests/LinuxShims/` supplies stand-in modules under those exact framework names:
a file-backed Keychain, an `LAContext` that records every prompt it was asked
for, and enough CryptoKit to hash and to open the pre-age blob. `make test-cli`
builds the real `Sources/keyguard/` against them and drives it end to end -
migrate, read, write, rename, delete, a denied fingerprint, a poisoned recipient
set. The shims are never part of the shipped binary; `make build` resolves those
imports to the real SDK.

Only `TerminalInput.swift` is substituted, because `tcflag_t` differs between the
platforms. Everything else under test is the code that ships.

## Makefile targets

| Target | Description |
|---|---|
| `make` | Build, install, and restart (default) |
| `make build` | Compile the Swift binary |
| `make install` | Install binary, server, and launchd agent |
| `make test` | Run all tests (Swift + CLI + Python) |
| `make test-swift` | Run Swift unit tests only |
| `make test-cli` | Drive the whole CLI end to end (Linux only, see below) |
| `make test-python` | Run Python server tests only |
| `make start` | Start the server |
| `make stop` | Stop the server |
| `make restart` | Restart the server |
| `make status` | Show whether the server is running |
| `make clean` | Remove compiled binary |
| `make uninstall` | Remove all installed files |

## Security model

| Threat | Protection |
|---|---|
| Docker container reads secrets directly | Containers have no access to the host Keychain or filesystem |
| Process on host reads the store | age-encrypted per file — unreadable without the identity |
| Process on host reads the identity | macOS Keychain ACL — other apps are challenged with a system password prompt |
| Unauthenticated HTTP request | Touch ID required for every decryption; biometrics only (no password fallback) |
| Optional cached read without Touch ID | Opt-in per-request (`?timeout=N`), capped at 300s, in-memory only, every read still appends to the access log |
| Request from another device on the network | Server rejects all IPs outside localhost and Docker subnets |
| Inline `keyguard set KEY value` | Warning printed to stderr — use the interactive prompt instead |
| `POST /<name>` from container | Value piped to `keyguard` via stdin — never appears in process args or `ps` |
| Exported identity leaked | Touch ID required to export; not available over HTTP; stderr warning reminds user to store safely |

### Encryption details

- **Algorithm**: age (X25519 + ChaCha20-Poly1305), via the `age` binary rather than a reimplementation of the format
- **Key**: 256-bit, randomly generated, stored in macOS Keychain
- **Nonce**: 96-bit random nonce, freshly generated on every write
- **On-disk format**: `nonce (12 bytes) || ciphertext || auth tag (16 bytes)`
- **Auth tag**: any tampering with the file is detected on decryption

### Known limitations

- The encryption key in Keychain can be extracted with the macOS login password (no Touch ID required for that path)
- Decrypted values are held in memory briefly and not explicitly zeroed
- When the optional `?timeout=N` is used, decrypted values remain in the server process memory for the specified duration
- All Docker containers on the machine have equal access to all secrets and bridge endpoints — there is no per-container scoping
- The bridge command list lives in `~/.mac-bridge-endpoints.yaml`; protect the file with `chmod 600` so only your user can edit which commands the bridge will run
