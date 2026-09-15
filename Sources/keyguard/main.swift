import Darwin
import Foundation
import KeyguardCore
import Security

// Mirrors keyguard_server.config.BRIDGE_CONFIG_PATH - the CLI confirms a claimed
// endpoint against the same file the server dispatches from, so the prompt can
// only ever name a command that is actually configured.
let BRIDGE_CONFIG_FILE: URL = {
    if let custom = ProcessInfo.processInfo.environment["KEYGUARD_BRIDGE_CONFIG_FILE"] {
        return URL(fileURLWithPath: NSString(string: custom).expandingTildeInPath)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".mac-bridge-endpoints.yaml")
}()

func configuredBridgeEndpointNames() -> Set<String> {
    guard let contents = try? String(contentsOf: BRIDGE_CONFIG_FILE, encoding: .utf8) else { return [] }
    return parseBridgeEndpointNames(contents)
}

func randomSalt() -> Data {
    var bytes = [UInt8](repeating: 0, count: 32)
    guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
        fail("Could not gather random bytes for the store salt")
    }
    return Data(bytes)
}

var isInteractive: Bool { isatty(STDIN_FILENO) != 0 }

func confirmOverwrite(_ name: String) -> Bool {
    fputs("  \(name) already exists. Overwrite? [y/N] ", stderr)
    guard let answer = readLine(strippingNewline: true) else { return false }
    return answer.lowercased() == "y"
}

func emit(name: String, value: String) {
    if value.contains("\n") {
        print("\(name)=base64:\(Data(value.utf8).base64EncodedString())")
    } else {
        print("\(name)=\(value)")
    }
}

func printUsage() {
    let text = """
    keyguard - local secret manager with Touch ID authentication

    Usage: keyguard <command> [options]

    Commands:
      get <KEY> [KEY...]           Retrieve one or more secrets
          [--cache-duration N]       Cache the decryption for N seconds
      set <KEY> [VALUE]            Store a secret (prompts for value if omitted)
      delete <KEY>                 Remove a secret (alias: rm)
      mv <OLD> <NEW> [--force]     Rename a secret (alias: rename)
                                     --force overwrites NEW if it already exists
      list [--cache-duration N]    List all secret names
      import <path> [--force]      Import secrets from a .env file
                                     --force overwrites existing keys without prompting
      export                       Print all secrets in KEY=VALUE format
      verify                       Check the store against its manifest
      init                         Create an empty store
      migrate [--force]            Convert a pre-age secrets file into the store
      import-key [IDENTITY]        Import an age identity into the Keychain
      export-key                   Print the age identity
      clear                        Delete the store and its identity
      help                         Show this help message

    Environment:
      KEYGUARD_STORE               Store directory. Defaults to a 'keyguard-store'
                                   directory beside KEYGUARD_SECRETS_FILE, otherwise
                                   ~/.keyguard/store
      KEYGUARD_SECRETS_FILE        Pre-age secrets file, read only by 'migrate'
      KEYGUARD_RECIPIENTS_FILE     Pinned recipient set (~/.keyguard/recipients)
      KEYGUARD_AGE_BIN             Full path to the age binary

    Examples:
      keyguard migrate
      keyguard set API_TOKEN
      keyguard get API_TOKEN
      keyguard get TOKEN_A TOKEN_B --cache-duration 120
      keyguard import ~/secrets.env --force
      keyguard mv HETZNER_USER HETZNER_ACCOUNT_USER
    """
    fputs(text + "\n", stderr)
}

// MARK: - Dispatch

let args = CommandLine.arguments
guard args.count >= 2 else {
    printUsage()
    exit(1)
}

// Answered before anything else and kept out of --help: the shell completion
// calls this on every TAB, so it must never prompt, never touch the store and
// never appear as a user-facing option.
if args[1] == "--complete" {
    Completion.values(field: args.count > 2 ? args[2] : "",
                      argument: args.count > 3 ? args[3] : nil).forEach { print($0) }
    exit(0)
}

switch args[1] {
case "help", "--help", "-h":
    printUsage()

case "get":
    guard args.count >= 3 else {
        fail("Usage: keyguard get <KEY> [KEY...] [--cache-duration N] [--bridge-endpoint NAME]")
    }
    commandGet(Array(args[2...]))

case "set":
    guard args.count >= 3 else { fail("Usage: keyguard set <KEY> [value]") }
    let value: String
    if args.count == 4 {
        fputs("Warning: inline values are saved in shell history\n", stderr)
        value = args[3]
    } else if isInteractive {
        fputs("Value for \(args[2]): ", stderr)
        guard let input = readSecret(), !input.isEmpty else { fail("No value provided") }
        value = input
    } else {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        guard let input = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines), !input.isEmpty else {
            fail("No value provided via stdin")
        }
        value = input
    }
    commandSet(name: args[2], value: value)

case "delete", "rm":
    guard args.count == 3 else { fail("Usage: keyguard delete <KEY>") }
    commandDelete(name: args[2])

case "mv", "rename":
    let rest = Array(args.dropFirst(2))
    let force = rest.contains("--force")
    let positional = rest.filter { $0 != "--force" }
    guard positional.count == 2 else { fail("Usage: keyguard mv <OLD> <NEW> [--force]") }
    commandRename(from: positional[0], to: positional[1], force: force)

case "list":
    commandList(Array(args.dropFirst(2)))

case "export":
    commandExport()

case "import":
    guard args.count >= 3 else { fail("Usage: keyguard import <path-to-.env> [--force]") }
    commandImport(path: args[2], force: args.contains("--force"))

case "verify":
    commandVerify()

case "init":
    commandInit()

case "migrate":
    commandMigrate(force: args.contains("--force"))

case "export-key":
    commandExportKey()

case "import-key":
    let identity: String
    if args.count == 3 {
        identity = args[2]
    } else if isInteractive {
        fputs("Paste the age identity: ", stderr)
        guard let input = readLine(strippingNewline: true), !input.isEmpty else { fail("No identity provided") }
        identity = input
    } else {
        guard let input = readLine(strippingNewline: true), !input.isEmpty else {
            fail("No identity provided via stdin")
        }
        identity = input
    }
    commandImportKey(identity)

case "clear":
    commandClear()

default:
    fputs("Unknown command '\(args[1])'\n\n", stderr)
    printUsage()
    exit(1)
}
