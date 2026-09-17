import CryptoKit
import Foundation
import KeyguardCore

enum LegacyStore {
    static func exists(at url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path)
    }

    static func read(at url: URL) throws -> [String: String] {
        guard let combined = try? Data(contentsOf: url) else {
            throw KeyguardError.message("No secrets file at \(url.path)")
        }
        guard let key = Keychain.loadLegacyKey() else {
            throw KeyguardError.message(
                "No encryption key in the Keychain. The secrets file at \(url.path) cannot be read.")
        }
        guard let sealed = try? AES.GCM.SealedBox(combined: combined),
              let decrypted = try? AES.GCM.open(sealed, using: key),
              let content = String(data: decrypted, encoding: .utf8) else {
            throw KeyguardError.message(
                "Decryption failed - the secrets file is corrupt or was encrypted with a different key")
        }
        return parseEnv(content)
    }
}
