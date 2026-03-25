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
keyguard import path/to/.env
rm path/to/.env  # delete the plaintext source
```

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
keyguard list                  # list key names
keyguard export                # print all key=value pairs
keyguard delete MY_API_TOKEN   # remove a key
keyguard clear                 # wipe everything (secrets file + encryption key)
```

## Using from Docker

From inside any Docker container on the same machine:

```bash
curl http://host.docker.internal:7777/MY_API_TOKEN          # single value
curl http://host.docker.internal:7777/MY_API_TOKEN,PASSWORD  # KEY=value lines
curl http://host.docker.internal:7777/_keys                  # list all key names
```

Every GET request triggers a Touch ID prompt on the host showing the exact key names being revealed. A macOS notification is displayed for every secret read, showing the key names, source IP with resolved names in parentheses (reverse DNS hostname and/or Docker container name), and whether it was served from cache. Examples: `TOKEN read by 172.17.0.2 (my-container)`, `TOKEN read by 192.168.1.50 (macbook.local)`, `TOKEN read by 127.0.0.1 (localhost) (cached)`.

**Caching with `?timeout=N` (skip repeated Touch ID prompts):**
```bash
curl http://host.docker.internal:7777/TOKEN?timeout=60
curl http://host.docker.internal:7777/TOKEN,PASSWORD?timeout=30
```

After Touch ID succeeds, the decrypted values are cached in the server's memory for `N` seconds (max 300). Subsequent requests for the same keys within that window are served without Touch ID — but still trigger a macOS notification marked `(cached)`. The Touch ID prompt shows the cache duration for informed consent: `"Reveal TOKEN (cached for 60s)"`.

- Default: no caching (omit `?timeout` for current behavior)
- Max timeout: 300 seconds (values above are clamped)
- Cache is in-memory only — lost on server restart
- Only GET is cacheable — POST always requires Touch ID
- Flush manually: `curl -X DELETE http://host.docker.internal:7777/_cache`

**Storing a secret from a container (POST):**
```bash
curl -s -X POST http://host.docker.internal:7777/MY_API_TOKEN -d 'the-value'
```

`POST /<name>` stores or updates a secret. The value is read from the request body. Triggers Touch ID on the host (`"Update MY_API_TOKEN"`). Responds with `Set 'MY_API_TOKEN'` on success.

The server only accepts connections from localhost and Docker's internal networks — other devices on the local network are rejected.

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
| Unauthenticated HTTP request | Touch ID required for every decryption; biometrics only (no password fallback). When caching is used, a macOS notification is sent for every read (cached or not) |
| Cached secret read without Touch ID | Cache is opt-in per-request (`?timeout=N`), capped at 300s, in-memory only, and every cached read triggers a visible macOS notification |
| Request from another device on the network | Server rejects all IPs outside localhost and Docker subnets |
| Inline `keyguard set KEY value` | Warning printed to stderr — use the interactive prompt instead |
| `POST /<name>` from container | Value piped to `keyguard` via stdin — never appears in process args or `ps` |

### Encryption details

- **Algorithm**: AES-256-GCM (authenticated encryption)
- **Key**: 256-bit, randomly generated, stored in macOS Keychain
- **Nonce**: 96-bit random nonce, freshly generated on every write
- **On-disk format**: `nonce (12 bytes) || ciphertext || auth tag (16 bytes)`
- **Auth tag**: any tampering with the file is detected on decryption

### Known limitations

- The encryption key in Keychain can be extracted with the macOS login password (no Touch ID required for that path)
- Decrypted values are held in memory briefly and not explicitly zeroed
- When `?timeout=N` is used, decrypted values remain in the server process memory for the specified duration
- All Docker containers on the machine have equal access to all secrets
