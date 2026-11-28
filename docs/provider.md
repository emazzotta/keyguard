# The Provider Contract

A provider is one command that gives secrets to a caller. A caller that speaks this contract works with `keyguard-passkey` or with any other secret manager behind a short shim.

## The Command

| Verb | Standard input | Standard output | Required |
|---|---|---|---|
| `get NAME...` | not read | one `NAME=value` line per name, in the asked order | yes |
| `list` | not read | one name per line | no |
| `set NAME` | the value | nothing | no |
| `rm NAME` | not read | nothing | no |

| Exit code | Meaning |
|---|---|
| 0 | Done |
| 1 | Another error |
| 2 | The person refused |
| 3 | The provider cannot answer now: locked, no store, no answer from the person |
| 4 | A name is not in the store |
| 64 | The provider does not support the verb, or the usage is wrong |

Rules for a provider:

- A name matches `[A-Za-z_][A-Za-z0-9_]*`. The provider refuses any other name with exit 64.
- `get` prints every asked value or none. On a code other than 0, standard output is empty.
- Values go to standard output only: never into arguments, files or standard error. Messages for the person go to standard error.
- A value that holds a line break, or that starts with `base64:`, is printed as `NAME=base64:<base64 of its UTF-8 bytes>`. Every other value is printed as it is.
- `set` stores the bytes of standard input without one line break at the end, and replaces a value that the store holds.
- A provider that cannot tell a missing name from a refusal answers 3.

## The Caller

| Variable | Meaning |
|---|---|
| `KEYGUARD_PROVIDER` | The provider as a command line. Not set: the caller's own default, which is `keyguard-passkey` for a caller that ships it |
| `KEYGUARD_SESSION`, `KEYGUARD_TITLE` | The asking session. The caller sets them, and the provider shows them to the person. They decide nothing |

Rules for a caller:

- Decode every value that starts with `base64:`.
- Treat every code other than 0 as "no value". Do not ask again in a loop after 2: the person said no.
- Keep the values in the environment of the process that needs them. Do not write them to a file.

A caller in POSIX shell, tested with `dash` and `bash`:

```sh
load_secrets() {
    lines=$(${KEYGUARD_PROVIDER:-keyguard-passkey} get "$@") || return
    while IFS= read -r line; do
        name="${line%%=*}"
        value="${line#*=}"
        case "$value" in
            base64:*) value=$(printf '%s' "${value#base64:}" | base64 -d) ;;
        esac
        export "$name=$value"
    done <<LINES
$lines
LINES
}

load_secrets API_TOKEN DB_PASSWORD || exit
```

With `keyguard-passkey` itself, `keyguard-passkey exec NAME... -- COMMAND` is shorter and gives the values to `COMMAND` only. `exec` is not part of the contract.

## Shims

Each shim is one file with no dependency on this repository. Copy it, make it executable and name it in `KEYGUARD_PROVIDER`.

### pass

[providers/pass](../providers/pass) maps each name to one entry of [pass](https://www.passwordstore.org).

| Verb | Runs |
|---|---|
| `get` | `pass show <entry>`: the whole entry is the value |
| `list` | the `.gpg` files of the folder, without `pass` |
| `set` | `pass insert --multiline --force <entry>` |
| `rm` | `pass rm --force <entry>` |

| Variable | Meaning | Default |
|---|---|---|
| `KEYGUARD_PASS_PREFIX` | Folder in the password store that holds the entries | the store's root |
| `PASSWORD_STORE_DIR` | The password store, as `pass` reads it | `~/.password-store` |

A cancelled passphrase prompt gives 3, not 2: `pass` does not report why GnuPG failed.

### 1Password

[providers/1password](../providers/1password) reads each name as an item of one vault with the [1Password CLI](https://developer.1password.com/docs/cli/): `op read --no-newline op://<vault>/<NAME>/<field>`. It supports `get` only.

| Variable | Meaning | Default |
|---|---|---|
| `KEYGUARD_OP_VAULT` | The vault that holds the items | none, required |
| `KEYGUARD_OP_FIELD` | The field of each item that holds the value | `password` |

A missing item and a refused prompt both give 3: `op read` does not tell them apart in its exit code.

Both shims are tested against stand-in binaries, not against a real `pass` or `op`.
