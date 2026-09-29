import Foundation

public enum Completion {
    public static let commands = [
        "add-recipient", "clear", "confirm", "delete", "export", "export-key", "get", "help", "import",
        "import-key", "init", "list", "migrate", "mv", "pull", "push", "remove-recipient", "rename", "rm", "set", "verify"
    ]

    public static func flags(forCommand command: String) -> [String] {
        switch command {
        case "get": return ["--bridge-endpoint", "--cache-duration"]
        case "list": return ["--cache-duration"]
        case "import", "migrate", "mv", "rename": return ["--force"]
        case "add-recipient", "remove-recipient": return ["--tier"]
        default: return []
        }
    }

    public static func values(field: String, argument: String?) -> [String] {
        switch field {
        case "commands": return commands
        case "flags": return flags(forCommand: argument ?? "")
        default: return []
        }
    }
}
