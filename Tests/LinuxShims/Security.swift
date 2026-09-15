// Linux stand-in for the Security framework. Exists only so the CLI can be
// typechecked and exercised off macOS; never compiled into the shipped binary,
// which resolves `import Security` to the real SDK module.
import Foundation

// Linux Foundation ships none of the CoreFoundation types, so they are defined
// here as the Swift types the calls actually carry. `x as CFDictionary` in the
// CLI then becomes a no-op cast rather than a bridge.
public typealias OSStatus = Int32
public typealias CFDictionary = [String: Any]
public typealias CFString = String
public typealias CFTypeRef = AnyObject

public let errSecSuccess: OSStatus = 0
public let errSecItemNotFound: OSStatus = -25300

public let kSecClass: CFString = "class"
public let kSecClassGenericPassword: CFString = "genp"
public let kSecAttrService: CFString = "svce"
public let kSecAttrAccount: CFString = "acct"
public let kSecValueData: CFString = "v_Data"
public let kSecAttrAccessible: CFString = "pdmn"
public let kSecAttrAccessibleWhenUnlocked: CFString = "ak"
public let kSecReturnData: CFString = "r_Data"

/// Backed by a file so a test can drive several `keyguard` invocations and have
/// them agree about what is stored, the way a real Keychain does.
public enum FakeKeychain {
    public static var path: String {
        ProcessInfo.processInfo.environment["KEYGUARD_FAKE_KEYCHAIN"] ?? "/tmp/keyguard-fake-keychain.json"
    }

    static func load() -> [String: String] {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let items = try? JSONDecoder().decode([String: String].self, from: data) else { return [:] }
        return items
    }

    static func save(_ items: [String: String]) {
        try? JSONEncoder().encode(items).write(to: URL(fileURLWithPath: path))
    }

    static func key(_ query: [String: Any]) -> String {
        let service = query[kSecAttrService as String] as? String ?? ""
        let account = query[kSecAttrAccount as String] as? String ?? ""
        return "\(service)/\(account)"
    }
}

public func SecItemAdd(_ attributes: CFDictionary, _ result: UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus {
    let query = attributes
    guard let data = query[kSecValueData as String] as? Data else { return -50 }
    var items = FakeKeychain.load()
    items[FakeKeychain.key(query)] = data.base64EncodedString()
    FakeKeychain.save(items)
    return errSecSuccess
}

public func SecItemCopyMatching(_ query: CFDictionary, _ result: UnsafeMutablePointer<CFTypeRef?>?) -> OSStatus {
    guard let encoded = FakeKeychain.load()[FakeKeychain.key(query)],
          let data = Data(base64Encoded: encoded) else { return errSecItemNotFound }
    result?.pointee = data as NSData
    return errSecSuccess
}

@discardableResult
public func SecItemDelete(_ query: CFDictionary) -> OSStatus {
    var items = FakeKeychain.load()
    guard items.removeValue(forKey: FakeKeychain.key(query)) != nil else { return errSecItemNotFound }
    FakeKeychain.save(items)
    return errSecSuccess
}

public let kSecRandomDefault: UnsafeRawPointer? = nil

public func SecRandomCopyBytes(_ rnd: UnsafeRawPointer?, _ count: Int,
                               _ bytes: UnsafeMutableRawPointer) -> OSStatus {
    let buffer = bytes.assumingMemoryBound(to: UInt8.self)
    for offset in 0..<count { buffer[offset] = UInt8.random(in: 0...255) }
    return errSecSuccess
}
