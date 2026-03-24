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
