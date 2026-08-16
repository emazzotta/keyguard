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
    public let bridgeEndpoint: String?

    public init(positional: [String], cacheDuration: Int?, bridgeEndpoint: String? = nil) {
        self.positional = positional
        self.cacheDuration = cacheDuration
        self.bridgeEndpoint = bridgeEndpoint
    }
}

public func parseArgs(_ args: [String]) -> ParsedArgs {
    var positional: [String] = []
    var cacheDuration: Int?
    var bridgeEndpoint: String?
    var i = 0
    while i < args.count {
        if args[i] == "--cache-duration", i + 1 < args.count, let duration = Int(args[i + 1]) {
            cacheDuration = duration
            i += 2
        } else if args[i] == "--bridge-endpoint", i + 1 < args.count {
            bridgeEndpoint = args[i + 1]
            i += 2
        } else {
            positional.append(args[i])
            i += 1
        }
    }
    return ParsedArgs(positional: positional, cacheDuration: cacheDuration, bridgeEndpoint: bridgeEndpoint)
}

private let bridgeEndpointNameCharacters = CharacterSet(charactersIn:
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
private let maxBridgeEndpointNameLength = 64

/// Endpoint names are identifiers, never prose - so a caller cannot dress a
/// token read up as reassuring text in the Touch ID prompt.
public func isValidBridgeEndpointName(_ name: String) -> Bool {
    guard !name.isEmpty, name.count <= maxBridgeEndpointNameLength else { return false }
    guard name.unicodeScalars.allSatisfy(bridgeEndpointNameCharacters.contains) else { return false }
    guard let first = name.first else { return false }
    return first.isLetter || first.isNumber
}

private func endpointKey(fromTrimmed line: String) -> String? {
    guard let colon = line.firstIndex(of: ":") else { return nil }
    var key = String(line[line.startIndex..<colon]).trimmingCharacters(in: .whitespaces)
    let quoted = (key.hasPrefix("\"") && key.hasSuffix("\"")) || (key.hasPrefix("'") && key.hasSuffix("'"))
    if key.count >= 2, quoted {
        key = String(key.dropFirst().dropLast())
    }
    return isValidBridgeEndpointName(key) ? key : nil
}

/// Top-level keys of the bridge config's `endpoints:` mapping, scanned without a
/// YAML library. Deliberately conservative: anything it cannot read confidently
/// yields an empty set, which downgrades the prompt to its bare form rather than
/// asserting an endpoint it has not actually confirmed.
public func parseBridgeEndpointNames(_ yaml: String) -> Set<String> {
    var names: Set<String> = []
    var childIndent: Int?
    var inEndpoints = false

    for line in yaml.components(separatedBy: .newlines) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty || trimmed.hasPrefix("#") { continue }
        if line.contains("\t") { return [] }

        let indent = line.prefix(while: { $0 == " " }).count
        if !inEndpoints {
            if indent == 0, trimmed == "endpoints:" { inEndpoints = true }
            continue
        }
        if indent == 0 { break }

        if let expected = childIndent, indent != expected { continue }
        childIndent = indent
        if let name = endpointKey(fromTrimmed: trimmed) {
            names.insert(name)
        }
    }
    return names
}

/// The prompt qualifier for a bridge-triggered token read, or nil when the name
/// is not a confirmed endpoint - never a caller-supplied string.
public func bridgePurpose(endpoint: String, configuredNames: Set<String>) -> String? {
    guard isValidBridgeEndpointName(endpoint), configuredNames.contains(endpoint) else { return nil }
    return "bridge endpoint \(endpoint)"
}

public func buildReason(base: String, cacheDuration: Int?, purpose: String? = nil) -> String {
    var reason = base
    if let purpose, !purpose.isEmpty {
        reason += " for \(purpose)"
    }
    guard let duration = cacheDuration else { return reason }
    return "\(reason) (cached for \(duration)s)"
}

public func setSecretReason(name: String, exists: Bool) -> String {
    exists ? "Update \(name)" : "Add \(name)"
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
