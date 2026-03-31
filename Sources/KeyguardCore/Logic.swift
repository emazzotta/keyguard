import Foundation

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
        entries[key] = value
    }
    return entries
}

public func serializeEnv(_ entries: [String: String]) -> String {
    entries.keys.sorted().map { "\($0)=\(entries[$0]!)" }.joined(separator: "\n")
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
