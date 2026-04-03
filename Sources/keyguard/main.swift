import CryptoKit
import Darwin
import Foundation
import KeyguardCore
import LocalAuthentication
import Security

let KEYCHAIN_SERVICE = "keyguard"
let KEYCHAIN_ACCOUNT = "encryption-key"
let SECRETS_FILE: URL = {
    if let custom = ProcessInfo.processInfo.environment["KEYGUARD_SECRETS_FILE"] {
        return URL(fileURLWithPath: custom)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".keyguard/secrets.enc")
}()

func authenticate(reason: String) {
    let context = LAContext()
    var error: NSError?

    guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) else {
        fputs("Biometrics unavailable: \(error?.localizedDescription ?? "unknown")\n", stderr)
        exit(1)
    }

    let semaphore = DispatchSemaphore(value: 0)
    var succeeded = false

    context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) { success, err in
        succeeded = success
        if !success, let err = err {
            fputs("Authentication failed: \(err.localizedDescription)\n", stderr)
        }
        semaphore.signal()
    }
    semaphore.wait()

    guard succeeded else { exit(2) }
}

func storeKey(_ key: SymmetricKey) {
    let keyData = key.withUnsafeBytes { Data($0) }

    let deleteQuery: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: KEYCHAIN_SERVICE,
        kSecAttrAccount as String: KEYCHAIN_ACCOUNT
    ]
    SecItemDelete(deleteQuery as CFDictionary)

    let addQuery: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: KEYCHAIN_SERVICE,
        kSecAttrAccount as String: KEYCHAIN_ACCOUNT,
        kSecValueData as String: keyData,
        kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlocked
    ]

    let status = SecItemAdd(addQuery as CFDictionary, nil)
    guard status == errSecSuccess else {
        fputs("Failed to store key: \(status)\n", stderr)
        exit(1)
    }
}

func loadKey() -> SymmetricKey? {
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: KEYCHAIN_SERVICE,
        kSecAttrAccount as String: KEYCHAIN_ACCOUNT,
        kSecReturnData as String: true
    ]

    var item: AnyObject?
    let status = SecItemCopyMatching(query as CFDictionary, &item)

    guard status == errSecSuccess, let keyData = item as? Data else { return nil }
    return SymmetricKey(data: keyData)
}

func decryptWithKey(reason: String) -> (SymmetricKey, String) {
    authenticate(reason: reason)

    guard let combined = try? Data(contentsOf: SECRETS_FILE) else {
        fputs("No secrets file found. Use 'keyguard set KEY' or 'keyguard import <path>' to create one.\n", stderr)
        exit(1)
    }

    guard let key = loadKey() else {
        fputs("No encryption key found in Keychain. Was it deleted or created on another machine?\n", stderr)
        fputs("If starting fresh, run 'keyguard clear' first, then re-import your secrets.\n", stderr)
        exit(1)
    }

    guard let sealed = try? AES.GCM.SealedBox(combined: combined),
          let decrypted = try? AES.GCM.open(sealed, using: key),
          let content = String(data: decrypted, encoding: .utf8) else {
        fputs("Decryption failed - secrets file may be corrupted or encrypted with a different key\n", stderr)
        exit(1)
    }

    return (key, content)
}

func decrypt(reason: String) -> String {
    decryptWithKey(reason: reason).1
}

func encrypt(_ content: String, using key: SymmetricKey) {
    guard let data = content.data(using: .utf8),
          let sealed = try? AES.GCM.seal(data, using: key),
          let combined = sealed.combined else {
        fputs("Encryption failed\n", stderr)
        exit(1)
    }

    let dir = SECRETS_FILE.deletingLastPathComponent()
    try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

    guard (try? combined.write(to: SECRETS_FILE)) != nil else {
        fputs("Failed to write \(SECRETS_FILE.path)\n", stderr)
        exit(1)
    }
}

func readSecret() -> String? {
    var tty = termios()
    tcgetattr(STDIN_FILENO, &tty)
    var raw = tty
    raw.c_lflag &= ~(UInt(ECHO) | UInt(ICANON))
    withUnsafeMutablePointer(to: &raw.c_cc) {
        $0.withMemoryRebound(to: cc_t.self, capacity: Int(NCCS)) {
            $0[Int(VMIN)] = 1
            $0[Int(VTIME)] = 0
        }
    }
    tcsetattr(STDIN_FILENO, TCSANOW, &raw)
    defer {
        tcsetattr(STDIN_FILENO, TCSANOW, &tty)
        fputs("\n", stderr)
    }
    var secret = ""
    var byte: UInt8 = 0
    while read(STDIN_FILENO, &byte, 1) == 1, byte != UInt8(ascii: "\n"), byte != UInt8(ascii: "\r") {
        secret.append(Character(UnicodeScalar(byte)))
    }
    return secret.isEmpty ? nil : secret
}

func loadOrInitSecrets(reason: String) -> (SymmetricKey, [String: String]) {
    guard FileManager.default.fileExists(atPath: SECRETS_FILE.path) else {
        if loadKey() != nil {
            fputs("Keychain already contains an encryption key but no secrets file at \(SECRETS_FILE.path)\n", stderr)
            fputs("Generating a new key would make any existing .enc file permanently undecryptable.\n", stderr)
            fputs("If you want to start fresh, run 'keyguard clear' first.\n", stderr)
            exit(1)
        }
        let key = SymmetricKey(size: .bits256)
        storeKey(key)
        return (key, [:])
    }
    authenticate(reason: reason)
    guard let existingKey = loadKey() else {
        fputs("No encryption key found in Keychain. Secrets file exists at \(SECRETS_FILE.path) but cannot be decrypted.\n", stderr)
        fputs("If starting fresh, run 'keyguard clear' first, then re-import your secrets.\n", stderr)
        exit(1)
    }
    guard let combined = try? Data(contentsOf: SECRETS_FILE),
          let sealed = try? AES.GCM.SealedBox(combined: combined),
          let decrypted = try? AES.GCM.open(sealed, using: existingKey),
          let content = String(data: decrypted, encoding: .utf8) else {
        fputs("Failed to read existing secrets - file may be corrupted or encrypted with a different key\n", stderr)
        exit(1)
    }
    return (existingKey, parseEnv(content))
}

func setSecret(name: String, value: String) {
    var (key, entries) = loadOrInitSecrets(reason: "Update \(name)")
    entries[name] = value
    encrypt(serializeEnv(entries), using: key)
    print("Set '\(name)'")
}

func deleteSecret(name: String) {
    let (key, content) = decryptWithKey(reason: "Delete \(name)")
    var entries = parseEnv(content)
    guard entries[name] != nil else {
        fputs("Key '\(name)' not found\n", stderr)
        exit(1)
    }
    entries.removeValue(forKey: name)
    encrypt(serializeEnv(entries), using: key)
    print("Deleted '\(name)'")
}

func clearSecrets() {
    authenticate(reason: "Clear all secrets")
    try? FileManager.default.removeItem(at: SECRETS_FILE)

    let deleteQuery: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: KEYCHAIN_SERVICE,
        kSecAttrAccount as String: KEYCHAIN_ACCOUNT
    ]
    SecItemDelete(deleteQuery as CFDictionary)
    print("Cleared all secrets")
}

func promptConflictResolution(_ name: String) -> Bool {
    fputs("  \(name) already exists. Overwrite? [y/N] ", stderr)
    guard let answer = readLine(strippingNewline: true) else { return false }
    return answer.lowercased() == "y"
}

func importEnv(path: String, forceOverwrite: Bool) {
    let url = URL(fileURLWithPath: path)
    guard let incoming = try? String(contentsOf: url, encoding: .utf8) else {
        fputs("Cannot read file: \(path)\n", stderr)
        exit(1)
    }

    var (key, entries) = loadOrInitSecrets(reason: "Import \(url.lastPathComponent)")
    let incomingEntries = parseEnv(incoming)
    let interactive = isatty(STDIN_FILENO) != 0

    var added = 0
    var overwritten = 0
    var skipped = 0

    for (name, value) in incomingEntries.sorted(by: { $0.key < $1.key }) {
        if entries[name] != nil {
            if forceOverwrite {
                entries[name] = value
                overwritten += 1
            } else if interactive {
                if promptConflictResolution(name) {
                    entries[name] = value
                    overwritten += 1
                } else {
                    skipped += 1
                }
            } else {
                fputs("  Skipped \(name) (already exists, use --force to overwrite)\n", stderr)
                skipped += 1
            }
        } else {
            entries[name] = value
            added += 1
        }
    }

    encrypt(serializeEnv(entries), using: key)

    var summary = "Imported from \(url.lastPathComponent): \(added) added"
    if overwritten > 0 { summary += ", \(overwritten) overwritten" }
    if skipped > 0 { summary += ", \(skipped) skipped" }
    print(summary)
    print("You can now delete the plaintext file: rm \(url.path)")
}

func exportKey() {
    authenticate(reason: "Export encryption key")
    guard let key = loadKey() else {
        fputs("No encryption key found in Keychain\n", stderr)
        exit(1)
    }
    let encoded = key.withUnsafeBytes { Data($0).base64EncodedString() }
    fputs("Warning: treat this key like a master password - it decrypts all your secrets\n", stderr)
    print(encoded, terminator: "")
}

func importKey(base64: String) {
    guard let data = Data(base64Encoded: base64), data.count == 32 else {
        fputs("Invalid key: expected 44-character base64 encoding of a 256-bit key\n", stderr)
        exit(1)
    }
    if loadKey() != nil {
        fputs("Keychain already contains an encryption key. Run 'keyguard clear' first to replace it.\n", stderr)
        exit(1)
    }
    authenticate(reason: "Import encryption key")
    storeKey(SymmetricKey(data: data))
    print("Encryption key imported into Keychain")
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
      list [--cache-duration N]    List all secret names
      import <path> [--force]      Import secrets from a .env file
                                     --force overwrites existing keys without prompting
      export                       Print all secrets in KEY=VALUE format
      import-key [BASE64]          Import an encryption key into Keychain
                                     (prompts for key if omitted)
      export-key                   Print the encryption key as base64
      clear                        Delete all secrets and the encryption key
      help                         Show this help message

    Environment:
      KEYGUARD_SECRETS_FILE        Override default secrets path (~/.keyguard/secrets.enc)

    Examples:
      keyguard set API_TOKEN
      keyguard get API_TOKEN
      keyguard get TOKEN_A TOKEN_B --cache-duration 120
      keyguard import ~/secrets.env --force
      keyguard delete OLD_KEY
    """
    fputs(text + "\n", stderr)
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    printUsage()
    exit(1)
}

switch args[1] {
case "help", "--help", "-h":
    printUsage()

case "clear":
    clearSecrets()

case "import":
    guard args.count >= 3 else { fputs("Usage: keyguard import <path-to-.env> [--force]\n", stderr); exit(1) }
    let forceOverwrite = args.contains("--force")
    let importPath = args[2]
    importEnv(path: importPath, forceOverwrite: forceOverwrite)

case "set":
    guard args.count >= 3 else { fputs("Usage: keyguard set <KEY> [value]\n", stderr); exit(1) }
    let value: String
    if args.count == 4 {
        fputs("Warning: inline values are saved in shell history\n", stderr)
        value = args[3]
    } else if isatty(STDIN_FILENO) != 0 {
        fputs("Value for \(args[2]): ", stderr)
        guard let input = readSecret(), !input.isEmpty else {
            fputs("No value provided\n", stderr); exit(1)
        }
        value = input
    } else {
        let data = FileHandle.standardInput.readDataToEndOfFile()
        guard let input = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !input.isEmpty else {
            fputs("No value provided via stdin\n", stderr); exit(1)
        }
        value = input
    }
    setSecret(name: args[2], value: value)

case "delete", "rm":
    guard args.count == 3 else { fputs("Usage: keyguard delete <KEY>\n", stderr); exit(1) }
    deleteSecret(name: args[2])

case "get":
    guard args.count >= 3 else { fputs("Usage: keyguard get <KEY> [KEY...] [--cache-duration N]\n", stderr); exit(1) }
    let parsed = parseArgs(Array(args[2...]))
    let keys = parsed.positional
    guard !keys.isEmpty else { fputs("Usage: keyguard get <KEY> [KEY...] [--cache-duration N]\n", stderr); exit(1) }
    let reason = buildReason(base: "Reveal \(keys.joined(separator: ", "))", cacheDuration: parsed.cacheDuration)
    let env = parseEnv(decrypt(reason: reason))
    let missing = keys.filter { env[$0] == nil }
    if !missing.isEmpty {
        fputs("Keys not found: \(missing.joined(separator: ", "))\n", stderr)
        exit(1)
    }
    if keys.count == 1 {
        print(env[keys[0]]!, terminator: "")
    } else {
        keys.forEach { print("\($0)=\(env[$0]!)") }
    }

case "list":
    let listParsed = parseArgs(Array(args[2...]))
    let listReason = buildReason(base: "List secrets", cacheDuration: listParsed.cacheDuration)
    parseEnv(decrypt(reason: listReason)).keys.sorted().forEach { print($0) }

case "export":
    print(decrypt(reason: "Export all secrets"), terminator: "")

case "export-key":
    exportKey()

case "import-key":
    let base64: String
    if args.count == 3 {
        base64 = args[2]
    } else if isatty(STDIN_FILENO) != 0 {
        fputs("Paste base64 key: ", stderr)
        guard let input = readLine(strippingNewline: true), !input.isEmpty else {
            fputs("No key provided\n", stderr); exit(1)
        }
        base64 = input
    } else {
        guard let input = readLine(strippingNewline: true), !input.isEmpty else {
            fputs("No key provided via stdin\n", stderr); exit(1)
        }
        base64 = input
    }
    importKey(base64: base64)

default:
    fputs("Unknown command '\(args[1])'\n\n", stderr)
    printUsage()
    exit(1)
}
