import Foundation

/// Argument construction for the `age` binary. Kept separate from the process
/// plumbing so the part that can be wrong - flag order, recipient repetition,
/// which stream carries the secret - is testable without macOS.
public enum AgeCommand {
    public static let binaryOverrideVariable = "KEYGUARD_AGE_BIN"

    /// Homebrew first: launchd hands the server a minimal PATH, so `age` is
    /// resolved by absolute path rather than left to a lookup that works in an
    /// interactive shell and nowhere else.
    public static let searchPaths = [
        "/opt/homebrew/bin/age",
        "/usr/local/bin/age",
        "/usr/bin/age"
    ]

    public static let keygenSearchPaths = [
        "/opt/homebrew/bin/age-keygen",
        "/usr/local/bin/age-keygen",
        "/usr/bin/age-keygen"
    ]

    /// Plaintext arrives on stdin, never as an argument, so it stays out of the
    /// process list.
    public static func encryptArguments(recipients: [String], output: String) -> [String] {
        recipients.flatMap { ["-r", $0] } + ["-o", output]
    }

    /// The identity arrives on stdin for the same reason; `-` is age's spelling
    /// for that. The ciphertext is a path because stdin is already taken.
    public static func decryptArguments(file: String) -> [String] {
        ["-d", "-i", "-", file]
    }
}

public enum AgeError: Error, Equatable {
    case binaryNotFound(String)
    case failed(command: String, status: Int32, message: String)
    case malformedKeygenOutput
}

public struct AgeIdentity: Equatable, Sendable {
    public let secret: String
    public let recipient: String

    public init(secret: String, recipient: String) {
        self.secret = secret
        self.recipient = recipient
    }
}

/// `age-keygen` prints a comment block and then the key. Parsing it here keeps
/// the brittle bit in reach of a test.
public func parseKeygenOutput(_ output: String) throws -> AgeIdentity {
    var secret: String?
    var recipient: String?

    for line in output.components(separatedBy: .newlines) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("AGE-SECRET-KEY-") {
            secret = trimmed
        } else if trimmed.hasPrefix("#"), let range = trimmed.range(of: "public key:") {
            recipient = String(trimmed[range.upperBound...]).trimmingCharacters(in: .whitespaces)
        }
    }

    guard let secret, let recipient, recipient.hasPrefix("age1") else {
        throw AgeError.malformedKeygenOutput
    }
    return AgeIdentity(secret: secret, recipient: recipient)
}

/// Counts `-> ` stanzas in an age header. A file carrying more recipients than
/// its tier calls for was re-encrypted to someone else.
public func recipientStanzaCount(inHeader header: String) -> Int {
    header.components(separatedBy: .newlines)
        .prefix { !$0.hasPrefix("---") }
        .filter { $0.hasPrefix("-> ") }
        .count
}

/// Runs the `age` binary. Foundation-only on purpose: keeping this out of the
/// macOS-framework half of the tool is what lets the encrypt/decrypt path be
/// tested end to end against a real `age` rather than reasoned about.
public struct AgeRunner {
    public let binary: String

    public init(binary: String) {
        self.binary = binary
    }

    public static func locate(candidates: [String] = AgeCommand.searchPaths,
                              override: String? = nil,
                              isExecutable: (String) -> Bool = {
                                  FileManager.default.isExecutableFile(atPath: $0)
                              }) throws -> String {
        if let override, !override.isEmpty {
            guard isExecutable(override) else { throw AgeError.binaryNotFound(override) }
            return override
        }
        guard let found = candidates.first(where: isExecutable) else {
            throw AgeError.binaryNotFound(candidates.joined(separator: ", "))
        }
        return found
    }

    public func encrypt(_ plaintext: Data, to recipients: [String], at path: String) throws {
        _ = try run(arguments: AgeCommand.encryptArguments(recipients: recipients, output: path),
                    stdin: plaintext)
    }

    public func decrypt(at path: String, identity: String) throws -> Data {
        try run(arguments: AgeCommand.decryptArguments(file: path),
                stdin: Data(identity.utf8))
    }

    public func keygen(binary keygenBinary: String) throws -> AgeIdentity {
        let output = try AgeRunner(binary: keygenBinary).run(arguments: [], stdin: Data())
        return try parseKeygenOutput(String(decoding: output, as: UTF8.self))
    }

    /// Secrets travel on stdin, so they never reach the process list. stdin is
    /// closed before stdout is drained, which is what keeps a large payload
    /// from deadlocking against a full pipe.
    private func run(arguments: [String], stdin: Data) throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: binary)
        process.arguments = arguments

        let input = Pipe(), output = Pipe(), errors = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors

        try process.run()
        input.fileHandleForWriting.write(stdin)
        input.fileHandleForWriting.closeFile()

        let stdout = output.fileHandleForReading.readDataToEndOfFile()
        let stderr = errors.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        guard process.terminationStatus == 0 else {
            throw AgeError.failed(
                command: "\(binary) \(arguments.joined(separator: " "))",
                status: process.terminationStatus,
                message: String(decoding: stderr, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
        return stdout
    }
}
