# Updating

A release is a tag `vX.Y.Z` on this repository. `keyguard-passkey version` prints the release that runs.

The client never fetches or replaces itself. An update is a step that you, your package manager or your organisation's repository starts.

## Update Channel per Install

| Install | Update |
|---|---|
| A checkout, run as `python3 keyguard-passkey/keyguard-passkey` | `git pull` |
| `uv tool install <checkout>` or `pipx install <checkout>` | `git pull`, then `uv tool upgrade keyguard-passkey` or `pipx upgrade keyguard-passkey` |
| `uv tool install git+https://github.com/emazzotta/keyguard-passkey@vX.Y.Z` | The same command with the new tag. `uv tool upgrade` keeps the tag |
| A vendored copy in another repository | That repository's own update, for example `git pull` or a plugin update |
| One file, `keyguard-passkey.pyz` | Pack the new tag ([One File](#one-file)) and replace the file |

Measured on 2026-11-12 with uv and pipx on Linux.

## What an Update Keeps

- **The passkey.** It is bound to the name `localhost`, not to a port, a folder or a release.
- **The store.** A release reads every store format up to its own.
- **The pages.** The client serves them from its own folder or zip file, so a page and a client of different releases never meet.

Values held from earlier approvals are in memory only. `keyguard-passkey lock` drops them, and a new release holds them again after the next approval.

## Store Formats

The encrypted index holds the format number in `version`. An index without one is format 1, which is the only format today.

| Case | Result |
|---|---|
| The store's format is the client's or older | The client reads and writes it |
| The store's format is newer | The client stops before any write, exits 1 and names the release that reads it |

Rules for a release that changes the format:

- The index of format 2 or later holds `reader`, the first release that reads it. An older client prints it.
- The migration runs on the first write. It keeps the previous files until the new ones are read back.
- A test migrates a store of each older format.

## Vendoring Into Another Repository

A repository that must not depend on this one at run time keeps a copy:

```bash
python3 tools/vendor.py v0.6.0 ../other-repository/bin
```

The command takes the files of the tag, not of the working tree. It replaces `bin/keyguard-passkey` and `bin/keyguard_passkey/`, and writes `bin/keyguard_passkey/VENDORED.json` with the tag, the commit and a SHA-256 per file. It refuses a tag whose files hold another version, and a `keyguard_passkey/` folder that has no `VENDORED.json`.

A vendor update is a commit in the other repository, so that repository's review sees every changed line.

```bash
keyguard-passkey verify-install
```

| Exit code | Meaning |
|---|---|
| 0 | Every file matches the manifest |
| 1 | A file is changed, missing or not in the manifest, or the copy has no manifest |

The check compares files with their line endings normalised, so a Windows checkout with CRLF passes. It proves that the copy is the tagged release. It does not prove that the release is safe: anyone who can change the files can change the manifest.

## One File

A machine that keeps one file instead of a folder runs the client as a zip application:

```bash
python3 tools/vendor.py --pyz v0.6.0 ../other-repository/bin
../other-repository/bin/keyguard-passkey.pyz version
```

The command packs `keyguard_passkey/` of the tag into `keyguard-passkey.pyz` and prints its SHA-256. The file holds no dates, so the same tag gives the same bytes: Python 3.9 and 3.15 packed identical files on 2026-11-16. Anyone can pack the tag again and compare the hash. The file has no manifest, so `verify-install` exits 1 for it: compare the hash instead.

From v0.3.1, each GitHub release carries this file, packed by the tag's pipeline, with its SHA-256 in the notes. A repository that keeps it can let Renovate bump the tag it pins (datasource `github-releases`, package `emazzotta/keyguard-passkey`) and download the release's file in the same pull request, as a `postUpgradeTasks` command:

```bash
curl -fsSL -o bin/keyguard-passkey.pyz https://github.com/emazzotta/keyguard-passkey/releases/download/v0.6.0/keyguard-passkey.pyz
```

A check in that repository's CI packs the pinned tag again and compares the bytes, so a pull request whose file is not the tag's fails.
