import Foundation

/// Reads the dispatch switch out of main.swift. The point of this suite is to
/// catch the completion drifting away from the CLI, which is the failure that
/// left `migrate`, `verify`, `init`, `delete` and six others untabbable.
private func dispatchedCommands(at path: String) -> Set<String>? {
    guard let source = try? String(contentsOfFile: path, encoding: .utf8),
          let switchStart = source.range(of: "switch args[1] {") else { return nil }

    var found: Set<String> = []
    for line in source[switchStart.upperBound...].components(separatedBy: .newlines) {
        // Only a closing brace in column zero ends the switch; the indented ones
        // belong to blocks inside a case body.
        if line == "}" { break }
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.hasPrefix("case "), trimmed.hasSuffix(":") else { continue }
        for piece in trimmed.dropFirst(5).dropLast().components(separatedBy: ",") {
            let label = piece.trimmingCharacters(in: CharacterSet(charactersIn: " \"'"))
            // `--help` and `-h` are flags, not words anyone tabs for.
            if !label.isEmpty, !label.hasPrefix("-"), label != "default" { found.insert(label) }
        }
    }
    return found
}

@main
struct CompletionTestRunner {
    static func main() {
        var failures = 0

        func check(_ desc: String, _ passed: Bool, _ detail: @autoclosure () -> String = "") {
            if passed { print("  ✓ \(desc)") } else {
                print("  ✗ \(desc)\(detail().isEmpty ? "" : ": \(detail())")")
                failures += 1
            }
        }

        func checkEqual<T: Equatable>(_ desc: String, _ actual: T, _ expected: T) {
            check(desc, actual == expected, "got \(actual), want \(expected)")
        }

        print("commands")
        check("should be sorted, so the completion list reads predictably",
              Completion.commands == Completion.commands.sorted())
        check("should hold no duplicates", Set(Completion.commands).count == Completion.commands.count)
        check("should never offer a flag as a command",
              Completion.commands.allSatisfy { !$0.hasPrefix("-") })

        if let dispatched = dispatchedCommands(at: "Sources/keyguard/main.swift") {
            let advertised = Set(Completion.commands)
            checkEqual("should offer every command the CLI dispatches",
                       dispatched.subtracting(advertised).sorted(), [])
            checkEqual("should offer nothing the CLI cannot run",
                       advertised.subtracting(dispatched).sorted(), [])
        } else {
            print("  - main.swift not readable from here, skipping the drift check")
        }

        print("\nflags")
        checkEqual("should offer both flags get accepts",
                   Completion.flags(forCommand: "get"), ["--bridge-endpoint", "--cache-duration"])
        checkEqual("should offer --cache-duration to list", Completion.flags(forCommand: "list"), ["--cache-duration"])
        checkEqual("should offer --force to migrate", Completion.flags(forCommand: "migrate"), ["--force"])
        checkEqual("should offer --force to both spellings of rename",
                   Completion.flags(forCommand: "mv"), Completion.flags(forCommand: "rename"))
        checkEqual("should offer nothing to a command that takes no flags",
                   Completion.flags(forCommand: "verify"), [])
        checkEqual("should offer nothing for a command that does not exist",
                   Completion.flags(forCommand: "nonsense"), [])
        check("should only ever offer flag-shaped strings",
              Completion.commands.allSatisfy { Completion.flags(forCommand: $0).allSatisfy { $0.hasPrefix("--") } })

        print("\nvalues(field:)")
        checkEqual("should answer commands", Completion.values(field: "commands", argument: nil), Completion.commands)
        checkEqual("should answer flags for the named command",
                   Completion.values(field: "flags", argument: "get"), ["--bridge-endpoint", "--cache-duration"])
        checkEqual("should stay silent on an unknown field rather than guess",
                   Completion.values(field: "nonsense", argument: "get"), [])
        checkEqual("should stay silent when flags is asked without a command",
                   Completion.values(field: "flags", argument: nil), [])

        if failures > 0 { fputs("\n\(failures) failure(s)\n", stderr); exit(1) }
        print("\nAll tests passed")
    }
}
