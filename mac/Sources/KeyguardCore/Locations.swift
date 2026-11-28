import Foundation

public enum Locations {
    public static let storeVariable = "KEYGUARD_STORE"
    public static let legacySecretsVariable = "KEYGUARD_SECRETS_FILE"
    public static let recipientsVariable = "KEYGUARD_RECIPIENTS_FILE"
    public static let storeURLVariable = "KEYGUARD_STORE_URL"
    public static let identityVariable = "KEYGUARD_STORE_IDENTITY"

    public static let storeDirectoryName = "keyguard-store"

    public static func storeURL(environment: [String: String]) -> URL? {
        guard let raw = environment[storeURLVariable], !raw.isEmpty else { return nil }
        return URL(string: raw)
    }

    public static func remoteVersionFile(environment: [String: String], home: String) -> URL {
        recipientsFile(environment: environment, home: home)
            .deletingLastPathComponent()
            .appendingPathComponent("remote-version")
    }

    public static func storeRoot(environment: [String: String], home: String) -> URL {
        if let explicit = environment[storeVariable], !explicit.isEmpty {
            return URL(fileURLWithPath: expandTilde(explicit, home: home))
        }
        if let legacy = environment[legacySecretsVariable], !legacy.isEmpty {
            return URL(fileURLWithPath: expandTilde(legacy, home: home))
                .deletingLastPathComponent()
                .appendingPathComponent(storeDirectoryName)
        }
        return URL(fileURLWithPath: home).appendingPathComponent(".keyguard/store")
    }

    public static func recipientsFile(environment: [String: String], home: String) -> URL {
        if let explicit = environment[recipientsVariable], !explicit.isEmpty {
            return URL(fileURLWithPath: expandTilde(explicit, home: home))
        }
        return URL(fileURLWithPath: home).appendingPathComponent(".keyguard/recipients")
    }

    public static func legacySecretsFile(environment: [String: String], home: String) -> URL {
        if let explicit = environment[legacySecretsVariable], !explicit.isEmpty {
            return URL(fileURLWithPath: expandTilde(explicit, home: home))
        }
        return URL(fileURLWithPath: home).appendingPathComponent(".keyguard/secrets.enc")
    }

    private static func expandTilde(_ path: String, home: String) -> String {
        guard path == "~" || path.hasPrefix("~/") else { return path }
        return home + path.dropFirst(1)
    }
}

public func loadPinnedRecipients(at url: URL) throws -> RecipientSet {
    try storeDecoder().decode(RecipientSet.self, from: try Data(contentsOf: url))
}

public func writePinnedRecipients(_ set: RecipientSet, to url: URL) throws {
    try FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try storeEncoder().encode(set).write(to: url, options: .atomic)
}
