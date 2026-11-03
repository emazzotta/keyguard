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

    static func loadIdentity() throws -> String? {
        try load(account: identityAccount).map { String(decoding: $0, as: UTF8.self) }
    }

    static func deleteIdentity() throws {
        let status = SecItemDelete(query(account: identityAccount) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw refusal(status, action: "delete \(identityAccount)")
        }
    }

    static func loadLegacyKey() throws -> SymmetricKey? {
        try load(account: legacyKeyAccount).map { SymmetricKey(data: $0) }
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

    private static func load(account: String) throws -> Data? {
        var attributes = query(account: account)
        attributes[kSecReturnData as String] = true

        var item: AnyObject?
        let status = SecItemCopyMatching(attributes as CFDictionary, &item)
        switch status {
        case errSecSuccess: return item as? Data
        case errSecItemNotFound: return nil
        default: throw refusal(status, action: "read \(account)")
        }
    }

    private static func refusal(_ status: OSStatus, action: String) -> KeyguardError {
        guard status == errSecInteractionNotAllowed else {
            return .message("The Keychain refused to \(action): OSStatus \(status).")
        }
        return .message("""
        The Keychain cannot ask to \(action) from this shell (errSecInteractionNotAllowed): \
        it runs outside the GUI login session, for example in a tmux server started over SSH.
        This says nothing about what the Keychain holds, so do not run 'keyguard clear' or \
        'keyguard import-key' over it. Run keyguard from a Terminal window instead.
        """)
    }
}

struct KeyguardError: Error {
    let text: String
    static func message(_ text: String) -> KeyguardError { KeyguardError(text: text) }
}
