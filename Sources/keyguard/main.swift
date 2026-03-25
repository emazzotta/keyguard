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

func decrypt(reason: String) -> String {
    authenticate(reason: reason)

    guard let combined = try? Data(contentsOf: SECRETS_FILE) else {
        fputs("No secrets file found. Use 'keyguard set KEY' or 'keyguard import <path>' to create one.\n", stderr)
        exit(1)
    }

    guard let key = loadKey(),
          let sealed = try? AES.GCM.SealedBox(combined: combined),
          let decrypted = try? AES.GCM.open(sealed, using: key),
          let content = String(data: decrypted, encoding: .utf8) else {
        fputs("Decryption failed\n", stderr)
        exit(1)
    }

    return content
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
        let key = SymmetricKey(size: .bits256)
        storeKey(key)
        return (key, [:])
    }
    authenticate(reason: reason)
    guard let existingKey = loadKey(),
          let combined = try? Data(contentsOf: SECRETS_FILE),
          let sealed = try? AES.GCM.SealedBox(combined: combined),
          let decrypted = try? AES.GCM.open(sealed, using: existingKey),
          let content = String(data: decrypted, encoding: .utf8) else {
        fputs("Failed to read existing secrets\n", stderr)
        exit(1)
    }
    return (existingKey, parseEnv(content))
}

func setKey(name: String, value: String) {
    var (key, entries) = loadOrInitSecrets(reason: "Update \(name)")
    entries[name] = value
    encrypt(serializeEnv(entries), using: key)
    print("Set '\(name)'")
}

func deleteKey(name: String) {
    let content = decrypt(reason: "Delete \(name)")
    guard let key = loadKey() else {
        fputs("No encryption key found\n", stderr)
        exit(1)
    }

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

func importEnv(path: String) {
    let url = URL(fileURLWithPath: path)
    guard let incoming = try? String(contentsOf: url, encoding: .utf8) else {
        fputs("Cannot read file: \(path)\n", stderr)
        exit(1)
    }

    var (key, entries) = loadOrInitSecrets(reason: "Import \(url.lastPathComponent)")
    let incomingEntries = parseEnv(incoming)
    entries.merge(incomingEntries) { _, new in new }
    encrypt(serializeEnv(entries), using: key)

    print("Imported \(incomingEntries.count) keys from \(url.lastPathComponent) → \(SECRETS_FILE.path)")
    print("You can now delete the plaintext file: rm \(url.path)")
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("Usage: keyguard <clear|import|set|delete|get|list|export> [KEY] [VALUE]\n", stderr)
    exit(1)
}

switch args[1] {
case "clear":
    clearSecrets()

case "import":
    guard args.count == 3 else { fputs("Usage: keyguard import <path-to-.env>\n", stderr); exit(1) }
    importEnv(path: args[2])

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
        guard let input = readLine(strippingNewline: true), !input.isEmpty else {
            fputs("No value provided via stdin\n", stderr); exit(1)
        }
        value = input
    }
    setKey(name: args[2], value: value)

case "delete", "rm":
    guard args.count == 3 else { fputs("Usage: keyguard delete <KEY>\n", stderr); exit(1) }
    deleteKey(name: args[2])

case "get":
    guard args.count >= 3 else { fputs("Usage: keyguard get <KEY> [KEY...] [--cache-duration N]\n", stderr); exit(1) }
    var keys: [String] = []
    var cacheDuration: Int? = nil
    var i = 2
    while i < args.count {
        if args[i] == "--cache-duration", i + 1 < args.count, let duration = Int(args[i + 1]) {
            cacheDuration = duration
            i += 2
        } else {
            keys.append(args[i])
            i += 1
        }
    }
    guard !keys.isEmpty else { fputs("Usage: keyguard get <KEY> [KEY...] [--cache-duration N]\n", stderr); exit(1) }
    var reason = "Reveal \(keys.joined(separator: ", "))"
    if let duration = cacheDuration {
        reason += " (cached for \(duration)s)"
    }
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
    parseEnv(decrypt(reason: "List secrets")).keys.sorted().forEach { print($0) }

case "export":
    print(decrypt(reason: "Export all secrets"), terminator: "")

default:
    fputs("Unknown command '\(args[1])'\nUsage: keyguard <clear|import|set|delete|get|list|export> [KEY] [VALUE]\n", stderr)
    exit(1)
}
