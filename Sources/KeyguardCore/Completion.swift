import Foundation

/// The command and flag vocabulary, owned here rather than copied into the
/// shell completion. `keyguard --complete <field>` prints it, so adding a
/// command cannot leave the completion silently out of date.
public enum Completion {
    public static let commands = [
        "clear", "delete", "export", "export-key", "get", "help", "import",
        "import-key", "init", "list", "migrate", "mv", "rename", "rm", "set", "verify"
    ]

    public static func flags(forCommand command: String) -> [String] {
        switch command {
        case "get": return ["--bridge-endpoint", "--cache-duration"]
        case "list": return ["--cache-duration"]
        case "import", "migrate", "mv", "rename": return ["--force"]
        default: return []
        }
    }

    /// Answers `--complete <field> [argument]`. An unknown field prints
    /// nothing, because a completion that guesses is worse than one that is
    /// quiet.
    public static func values(field: String, argument: String?) -> [String] {
        switch field {
        case "commands": return commands
        case "flags": return flags(forCommand: argument ?? "")
        default: return []
        }
    }
}
