import Foundation

public enum Tier: String, Codable, CaseIterable, Sendable {
    case high
    case low
}

public struct RecipientSet: Codable, Equatable, Sendable {
    public let version: Int
    public let tiers: [String: [String]]

    public init(version: Int, tiers: [String: [String]]) {
        self.version = version
        self.tiers = tiers
    }

    public func recipients(for tier: Tier) -> [String] {
        (tiers[tier.rawValue] ?? []).sorted()
    }
}

public struct IndexEntry: Codable, Equatable, Sendable {
    public let file: String
    public let tier: Tier

    public init(file: String, tier: Tier) {
        self.file = file
        self.tier = tier
    }
}

public struct StoreIndex: Codable, Equatable, Sendable {
    public static let currentVersion = 1

    public let version: Int
    public let salt: Data
    public let recipients: RecipientSet
    public let entries: [String: IndexEntry]

    public init(version: Int = StoreIndex.currentVersion,
                salt: Data,
                recipients: RecipientSet,
                entries: [String: IndexEntry]) {
        self.version = version
        self.salt = salt
        self.recipients = recipients
        self.entries = entries
    }
}

public struct StoreMeta: Codable, Equatable, Sendable {
    public static let currentVersion = 1

    public let version: Int
    public let files: [String: String]

    public init(version: Int = StoreMeta.currentVersion, files: [String: String]) {
        self.version = version
        self.files = files
    }
}

public struct VariablePayload: Codable, Equatable, Sendable {
    public let name: String
    public let value: String

    public init(name: String, value: String) {
        self.name = name
        self.value = value
    }
}

public struct IntegrityReport: Equatable, Sendable {
    public let missing: [String]
    public let modified: [String]
    public let unexpected: [String]

    public var isClean: Bool { missing.isEmpty && modified.isEmpty && unexpected.isEmpty }

    public init(missing: [String], modified: [String], unexpected: [String]) {
        self.missing = missing
        self.modified = modified
        self.unexpected = unexpected
    }
}

public enum StoreError: Error, Equatable {
    case recipientSetDivergence
    case recipientSetRollback(pinned: Int, canonical: Int)
    case payloadNameMismatch(expected: String, found: String)
    case corruptPadding
    case unsupportedStoreVersion(Int)
    case unknownVariables([String])
    case writeFailed(String)
}

public let storeIndexFile = "index.age"
public let storeMetaFile = "meta.json"
public let storeVariableDirectory = "vars"

public typealias DigestFunction = (Data) -> Data

public func hex(_ data: Data) -> String {
    data.map { String(format: "%02x", $0) }.joined()
}

public func variableFile(salt: Data, name: String, digest: DigestFunction) -> String {
    "\(storeVariableDirectory)/\(hex(digest(salt + Data(name.utf8)))).age"
}

public enum Padding {
    public static let blockSize = 256
    private static let lengthBytes = 4

    public static func pad(_ data: Data, blockSize: Int = Padding.blockSize) -> Data {
        var out = Data()
        var length = UInt32(data.count).bigEndian
        withUnsafeBytes(of: &length) { out.append(contentsOf: $0) }
        out.append(data)
        let remainder = out.count % blockSize
        if remainder != 0 {
            out.append(Data(repeating: 0, count: blockSize - remainder))
        }
        return out
    }

    public static func unpad(_ data: Data) throws -> Data {
        guard data.count >= lengthBytes else { throw StoreError.corruptPadding }
        let bytes = [UInt8](data.prefix(lengthBytes))
        let length = Int(UInt32(bytes[0]) << 24 | UInt32(bytes[1]) << 16 | UInt32(bytes[2]) << 8 | UInt32(bytes[3]))
        let end = lengthBytes + length
        guard length >= 0, end <= data.count else { throw StoreError.corruptPadding }
        return data.subdata(in: (data.startIndex + lengthBytes)..<(data.startIndex + end))
    }
}

public func checkIntegrity(meta: StoreMeta, actual: [String: String]) -> IntegrityReport {
    let expected = Set(meta.files.keys)
    let present = Set(actual.keys)
    return IntegrityReport(
        missing: expected.subtracting(present).sorted(),
        modified: expected.intersection(present).filter { meta.files[$0] != actual[$0] }.sorted(),
        unexpected: present.subtracting(expected).sorted()
    )
}

public func validateRecipients(pinned: RecipientSet,
                               canonical: RecipientSet,
                               highestSeenVersion: Int) throws {
    guard canonical.version >= highestSeenVersion else {
        throw StoreError.recipientSetRollback(pinned: highestSeenVersion, canonical: canonical.version)
    }
    guard pinned == canonical else { throw StoreError.recipientSetDivergence }
}

public func verify(payload: VariablePayload, expecting name: String) throws -> String {
    guard payload.name == name else {
        throw StoreError.payloadNameMismatch(expected: name, found: payload.name)
    }
    return payload.value
}

public func storeEncoder() -> JSONEncoder {
    let encoder = JSONEncoder()
    encoder.keyEncodingStrategy = .convertToSnakeCase
    encoder.outputFormatting = [.sortedKeys]
    return encoder
}

public func storeDecoder() -> JSONDecoder {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return decoder
}

public struct PlannedVariable: Equatable, Sendable {
    public let path: String
    public let payload: VariablePayload
    public let tier: Tier

    public init(path: String, payload: VariablePayload, tier: Tier) {
        self.path = path
        self.payload = payload
        self.tier = tier
    }
}

public func migrationPlan(entries: [String: String],
                          salt: Data,
                          recipients: RecipientSet,
                          tierFor: (String) -> Tier,
                          digest: DigestFunction) -> (variables: [PlannedVariable], index: StoreIndex) {
    var planned: [PlannedVariable] = []
    var indexEntries: [String: IndexEntry] = [:]

    for name in entries.keys.sorted() {
        let path = variableFile(salt: salt, name: name, digest: digest)
        let tier = tierFor(name)
        planned.append(PlannedVariable(path: path,
                                       payload: VariablePayload(name: name, value: entries[name]!),
                                       tier: tier))
        indexEntries[name] = IndexEntry(file: path, tier: tier)
    }

    return (planned, StoreIndex(salt: salt, recipients: recipients, entries: indexEntries))
}
