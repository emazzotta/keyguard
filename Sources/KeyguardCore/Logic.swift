import Foundation

private let base64Prefix = "base64:"

public func parseEnv(_ content: String) -> [String: String] {
    var entries: [String: String] = [:]
    for line in content.components(separatedBy: .newlines) {
        var trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, !trimmed.hasPrefix("#") else { continue }
        if trimmed.hasPrefix("export ") {
            trimmed = String(trimmed.dropFirst(7)).trimmingCharacters(in: .whitespaces)
        }
        let parts = trimmed.split(separator: "=", maxSplits: 1)
        guard parts.count == 2 else { continue }
        let key = String(parts[0])
        var value = String(parts[1])
        if (value.hasPrefix("\"") && value.hasSuffix("\"")) ||
           (value.hasPrefix("'") && value.hasSuffix("'")) {
            value = String(value.dropFirst().dropLast())
        } else if let range = value.range(of: #"\s+#.*$"#, options: .regularExpression) {
            value = String(value[value.startIndex..<range.lowerBound])
        }
        if value.hasPrefix(base64Prefix),
           let data = Data(base64Encoded: String(value.dropFirst(base64Prefix.count))),
           let decoded = String(data: data, encoding: .utf8) {
            value = decoded
        }
        entries[key] = value
    }
    return entries
}

public func serializeEnv(_ entries: [String: String]) -> String {
    entries.keys.sorted().map { key in
        let value = entries[key]!
        if value.contains("\n") {
            let encoded = Data(value.utf8).base64EncodedString()
            return "\(key)=\(base64Prefix)\(encoded)"
        }
        return "\(key)=\(value)"
    }.joined(separator: "\n")
}

public struct ParsedArgs {
    public let positional: [String]
    public let cacheDuration: Int?

    public init(positional: [String], cacheDuration: Int?) {
        self.positional = positional
        self.cacheDuration = cacheDuration
    }
}

public func parseArgs(_ args: [String]) -> ParsedArgs {
    var positional: [String] = []
    var cacheDuration: Int?
    var i = 0
    while i < args.count {
        if args[i] == "--cache-duration", i + 1 < args.count, let duration = Int(args[i + 1]) {
            cacheDuration = duration
            i += 2
        } else {
            positional.append(args[i])
            i += 1
        }
    }
    return ParsedArgs(positional: positional, cacheDuration: cacheDuration)
}

public func buildReason(base: String, cacheDuration: Int?) -> String {
    guard let duration = cacheDuration else { return base }
    return "\(base) (cached for \(duration)s)"
}

public enum RenameError: Error, Equatable {
    case sourceNotFound(String)
    case sameKey
    case destinationExists(String)
}

public func renameEntry(
    in entries: [String: String],
    from oldKey: String,
    to newKey: String,
    overwrite: Bool = false
) throws -> [String: String] {
    guard oldKey != newKey else { throw RenameError.sameKey }
    guard let value = entries[oldKey] else { throw RenameError.sourceNotFound(oldKey) }
    if entries[newKey] != nil && !overwrite {
        throw RenameError.destinationExists(newKey)
    }
    var result = entries
    result.removeValue(forKey: oldKey)
    result[newKey] = value
    return result
}
