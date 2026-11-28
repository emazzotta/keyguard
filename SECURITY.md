# Security

## Report a Vulnerability

Report it in private: open the [Security tab](https://github.com/emazzotta/keyguard-passkey/security/advisories/new) of this repository and choose "Report a vulnerability". Do not open a public issue for a fault that can expose a secret.

A useful report holds:

- the release (`keyguard-passkey version`), the system, the browser and the authenticator
- the steps that show the fault
- what an attacker gets, and what they need before they start

Fixes go into the newest release. Older releases get none.

## What the Store Protects

- **A copied or stolen disk, and a backup.** Every secret is an age file, and so is the index of names. A file name is a salted hash, so it does not show which secret it holds.
- **Each read.** A command gets values only after an approval with the passkey, on a page that shows the names and, when the caller gives one, the session that asks.
- **Other commands.** `exec` gives the values to one command. Released values stay in the memory of a process of the same user for 5 minutes at most, and `lock` drops them.

## What It Does Not Protect

These limits are known. They are not vulnerabilities of this project:

- Code that already runs as you. It can serve a look-alike page on `localhost` and ask for the same passkey. It can also read what an approved command prints and the values that the hold keeps.
- A command after you approved it. It holds the values and can do with them what it wants.
- A passkey that its provider syncs. The store is then as safe as that account.

[README, Limits](README.md#limits) has the full list.

## The Code You Run

- The client imports the Python standard library only and never fetches or replaces itself.
- The one bundled dependency is `keyguard_passkey/pages/typage.js`. `python3 tools/build_typage.py --check` proves that it is the build of the packages that `tools/typage/bun.lock` pins.
- A copy in another repository carries `VENDORED.json`, and `keyguard-passkey verify-install` compares each file with it.
