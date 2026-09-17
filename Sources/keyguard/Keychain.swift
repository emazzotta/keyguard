import CryptoKit
import Foundation
import Security

enum Keychain {
    static let service = "keyguard"
    static let identityAccount = "age-identity"
    static let legacyKeyAccount = "encryption-key"

    static func store(identity: String) throws {
        try store(Data(identity.utf8), account: identityAccount)
    }

    static func loadIdentity() -> String? {
        guard let data = load(account: identityAccount) else { return nil }
        return String(decoding: data, as: UTF8.self)
    }

    static func deleteIdentity() {
        SecItemDelete(query(account: identityAccount) as CFDictionary)
    }

    static func loadLegacyKey() -> SymmetricKey? {
        guard let data = load(account: legacyKeyAccount) else { return nil }
        return SymmetricKey(data: data)
    }

    private static func query(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account
        ]
    }

    private static func store(_ data: Data, account: String) throws {
        SecItemDelete(query(account: account) as CFDictionary)

        var attributes = query(account: account)
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlocked

        let status = SecItemAdd(attributes as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeyguardError.message("Failed to store \(account) in the Keychain: \(status)")
        }
    }

    private static func load(account: String) -> Data? {
        var attributes = query(account: account)
        attributes[kSecReturnData as String] = true

        var item: AnyObject?
        guard SecItemCopyMatching(attributes as CFDictionary, &item) == errSecSuccess else { return nil }
        return item as? Data
    }
}

struct KeyguardError: Error {
    let text: String
    static func message(_ text: String) -> KeyguardError { KeyguardError(text: text) }
}
