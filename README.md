# keyguard

[![Pipeline](https://github.com/emazzotta/keyguard/actions/workflows/pipeline.yml/badge.svg)](https://github.com/emazzotta/keyguard/actions/workflows/pipeline.yml)

A lightweight, local secret manager for macOS. Encrypts secrets on disk with AES-256-GCM and gates every decryption behind Touch ID. Exposes secrets over a local HTTP server so Docker containers or local scripts can fetch them at runtime without credentials ever being baked into images or environment variables.

## How it works

```
Docker container
  └── curl http://host.docker.internal:7777/TOKEN,PASSWORD
        └── keyguard-server (host, port 7777)
              └── keyguard get TOKEN PASSWORD
                    ├── Touch ID prompt: "Reveal TOKEN, PASSWORD"
                    ├── AES-256-GCM decrypt ~/.keyguard/secrets.enc
                    └── return TOKEN=value\nPASSWORD=value
```

Secrets never exist in plaintext on disk. The encrypted file is the source of truth. Every read requires a fingerprint.

## Requirements

- macOS with Touch ID
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
keyguard clear                                       # wipe everything (secrets file + encryption key)
```

**Backup and restore the encryption key:**
```bash
keyguard export-key            # print base64-encoded encryption key to stdout
keyguard import-key            # import key interactively (prompts for paste)
keyguard import-key <base64>   # import key from argument
echo "<base64>" | keyguard import-key  # import key from stdin
```

`export-key` outputs the raw 256-bit AES key as base64 (44 characters). Store it somewhere safe - with this key and the `secrets.enc` file you can restore your secrets on any Mac. `import-key` refuses to overwrite an existing key - run `keyguard clear` first if replacing.

## Using from Docker

From inside any Docker container on the same machine:

```bash
curl http://host.docker.internal:7777/MY_API_TOKEN          # single value
curl http://host.docker.internal:7777/MY_API_TOKEN,PASSWORD  # KEY=value lines
curl http://host.docker.internal:7777/_keys                  # list all key names
```

Every GET request triggers a Touch ID prompt on the host showing the exact key names being revealed. A macOS notification is displayed for every secret read, showing an ISO timestamp, key names, and source IP with resolved names (reverse DNS hostname and/or Docker container name). Clients can send an `X-Keyguard-Source` header (e.g., the container hostname) to identify themselves — the server resolves it to a container name via `docker inspect` when possible.

Optionally, append `?timeout=N` to cache decrypted values in the server's process memory for up to `N` seconds (max 300), reducing repeated Touch ID prompts. Cache entries are scoped to the requesting IP — a different container cannot read another's cache unless explicitly allowed with `?share=all` or `?share=172.17.0.2,172.17.0.3`. Cached reads still trigger a notification marked `(cached)`, and the Touch ID prompt shows the duration for informed consent (`"Reveal TOKEN (cached for 60s)"`). By default there is no caching. Flush with `DELETE /_cache`.

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

Two layers protect the Touch ID prompt from abuse:

1. Requests without a well-formed `Authorization: Bearer …` header are rejected **before** keyguard is invoked. Unauthenticated callers cannot trigger a prompt at all.
2. Failed resolutions (Touch ID denied, key missing) are rate-limited to one attempt per 60 seconds. A misconfigured client cannot spam prompts.

### Reloading config

```bash
kill -HUP $(launchctl list | awk '/com.keyguard.server/{print $1}')
```

This re-reads the YAML file, clears the cached token, and clears the rate-limit state.

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
```

**Command rules:**
- Must be a YAML list — no shell string, no glob expansion, no interpolation.
- The executable path must be absolute, or resolvable via the server's `$PATH`.
- No user-controlled values are ever passed into command arguments — the only caller input that reaches the command is the POST body via `stdin: true`.

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
```

Every successful bridge call sends a macOS notification with the endpoint name and caller IP.

### Security

| Concern | How it is handled |
|---|---|
| Unauthenticated call | `Authorization: Bearer <token>` required; 401 otherwise |
| Token interception on the network | Only localhost and Docker internal subnets are accepted (same as keyguard secrets) |
| Token at rest | The token lives inside the AES-256-GCM-encrypted keyguard store under `MAC_BRIDGE_TOKEN` — never on disk in plaintext |
| Touch ID prompt spam from unauthenticated callers | Requests without a `Bearer …` header are rejected *before* keyguard is invoked — no prompt fires for malformed/missing auth |
| Touch ID prompt spam from misconfigured callers | Failed token resolutions are rate-limited to 1 per 60 seconds; SIGHUP clears the limit |
| Command injection via request body | Body can only reach `stdin` — never command args. Commands are fixed lists, no shell involved |
| Arbitrary command execution | Only commands declared in the gitignored config file run; no dynamic dispatch |
| Endpoint enumeration | 404 for unknown endpoints regardless of auth; timing is identical to auth failure |
| Token brute force | `hmac.compare_digest` (constant-time) prevents timing attacks; IP allowlist limits the attack surface to Docker networks |
| Config file leaks to git | `.mac-bridge-endpoints.yaml` is in `.gitignore` |

The config file itself is the trust boundary: only what you write into it can be called.

## Custom secrets file path

By default secrets are stored at `~/.keyguard/secrets.enc`. Override with an environment variable:

```bash
export KEYGUARD_SECRETS_FILE=~/Dropbox/keyguard/secrets.enc
```

Set this in your shell profile before running `make install` — the value is baked into the launchd plist automatically so the server always uses the correct path.

## Makefile targets

| Target | Description |
|---|---|
| `make` | Build, install, and restart (default) |
| `make build` | Compile the Swift binary |
| `make install` | Install binary, server, and launchd agent |
| `make test` | Run all tests (Swift + Python) |
| `make test-swift` | Run Swift unit tests only |
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
| Process on host reads `secrets.enc` | AES-256-GCM encrypted — unreadable without the key |
| Process on host reads the encryption key | macOS Keychain ACL — other apps are challenged with a system password prompt |
| Unauthenticated HTTP request | Touch ID required for every decryption; biometrics only (no password fallback) |
| Optional cached read without Touch ID | Opt-in per-request (`?timeout=N`), capped at 300s, in-memory only, every read still triggers a macOS notification |
| Request from another device on the network | Server rejects all IPs outside localhost and Docker subnets |
| Inline `keyguard set KEY value` | Warning printed to stderr — use the interactive prompt instead |
| `POST /<name>` from container | Value piped to `keyguard` via stdin — never appears in process args or `ps` |
| Exported encryption key leaked | Touch ID required to export; not available over HTTP; stderr warning reminds user to store safely |

### Encryption details

- **Algorithm**: AES-256-GCM (authenticated encryption)
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
