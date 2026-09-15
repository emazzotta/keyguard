import Foundation

/// Linux stand-in for the macOS termios reader. The CLI tests drive `set` and
/// `import-key` non-interactively, so a plain line read is enough.
func readSecret() -> String? {
    guard let line = readLine(strippingNewline: true), !line.isEmpty else { return nil }
    return line
}
