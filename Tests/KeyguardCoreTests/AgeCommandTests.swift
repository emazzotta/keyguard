import Foundation

private func temporaryDirectory() -> URL {
    let url = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("keyguard-age-tests-\(UUID().uuidString)")
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

@main
struct AgeCommandTestRunner {
    static func main() {
        var failures = 0

        func check(_ desc: String, _ passed: Bool, _ detail: @autoclosure () -> String = "") {
            if passed {
                print("  ✓ \(desc)")
            } else {
                print("  ✗ \(desc)\(detail().isEmpty ? "" : ": \(detail())")")
                failures += 1
            }
        }

        func checkEqual<T: Equatable>(_ desc: String, _ actual: T, _ expected: T) {
            check(desc, actual == expected, "got \(actual), want \(expected)")
        }

        print("encryptArguments")
        checkEqual("should repeat -r once per recipient",
                   AgeCommand.encryptArguments(recipients: ["age1a", "age1b"], output: "/tmp/x.age"),
                   ["-r", "age1a", "-r", "age1b", "-o", "/tmp/x.age"])
        checkEqual("should still name an output with no recipients",
                   AgeCommand.encryptArguments(recipients: [], output: "/tmp/x.age"),
                   ["-o", "/tmp/x.age"])
        check("should never place a recipient after the output path",
              AgeCommand.encryptArguments(recipients: ["age1a"], output: "/tmp/x.age").last == "/tmp/x.age")

        print("\ndecryptArguments")
        checkEqual("should read the identity from stdin and the ciphertext from a path",
                   AgeCommand.decryptArguments(file: "/tmp/x.age"), ["-d", "-i", "-", "/tmp/x.age"])

        print("\nparseKeygenOutput")
        let sample = """
        # created: 2026-09-15T02:00:00Z
        # public key: age1ug9pxf0jr8a45wnnvy5xkh0hucksamzhphspvf25xlaslu9jtp7s8608x0
        AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ
        """
        if let identity = try? parseKeygenOutput(sample) {
            checkEqual("should read the recipient from the comment block",
                       identity.recipient, "age1ug9pxf0jr8a45wnnvy5xkh0hucksamzhphspvf25xlaslu9jtp7s8608x0")
            check("should read the secret key", identity.secret.hasPrefix("AGE-SECRET-KEY-1"))
        } else {
            check("should parse well-formed keygen output", false)
        }
        check("should reject output with no secret key",
              (try? parseKeygenOutput("# public key: age1abc")) == nil)
        check("should reject output with no public key",
              (try? parseKeygenOutput("AGE-SECRET-KEY-1ABC")) == nil)
        check("should reject a public key that is not an age recipient",
              (try? parseKeygenOutput("# public key: ssh-ed25519 AAAA\nAGE-SECRET-KEY-1ABC")) == nil)

        print("\nrecipientStanzaCount")
        let twoStanza = """
        age-encryption.org/v1
        -> X25519 aaaa
        bbbb
        -> X25519 cccc
        dddd
        --- eeee
        """
        checkEqual("should count one stanza per recipient", recipientStanzaCount(inHeader: twoStanza), 2)
        checkEqual("should count nothing in a headerless blob", recipientStanzaCount(inHeader: "garbage"), 0)
        checkEqual("should stop counting at the header MAC",
                   recipientStanzaCount(inHeader: twoStanza + "\n-> X25519 ffff"), 2)

        print("\nlocate")
        checkEqual("should prefer the first candidate that exists",
                   (try? AgeRunner.locate(candidates: ["/a", "/b", "/c"], isExecutable: { $0 != "/a" })) ?? "",
                   "/b")
        checkEqual("should honour an explicit override",
                   (try? AgeRunner.locate(candidates: ["/a"], override: "/custom", isExecutable: { _ in true })) ?? "",
                   "/custom")
        check("should reject an override that is not executable",
              (try? AgeRunner.locate(candidates: ["/a"], override: "/missing", isExecutable: { $0 == "/a" })) == nil)
        check("should fail when no candidate exists",
              (try? AgeRunner.locate(candidates: ["/a", "/b"], isExecutable: { _ in false })) == nil)

        guard let age = try? AgeRunner.locate(candidates: AgeCommand.searchPaths + ["/usr/bin/age"]),
              let keygen = try? AgeRunner.locate(candidates: AgeCommand.keygenSearchPaths + ["/usr/bin/age-keygen"]) else {
            print("\nage binary not present - skipping round-trip")
            if failures > 0 { fputs("\n\(failures) failure(s)\n", stderr); exit(1) }
            print("\nAll tests passed")
            return
        }

        print("\nround-trip against the real age binary")
        let runner = AgeRunner(binary: age)
        let dir = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: dir) }

        guard let alice = try? runner.keygen(binary: keygen),
              let bob = try? runner.keygen(binary: keygen) else {
            check("should generate identities", false)
            fputs("\n\(failures + 1) failure(s)\n", stderr)
            exit(1)
        }
        check("should generate distinct identities", alice.secret != bob.secret)
        checkEqual("should derive the recipient from an identity we already hold",
                   (try? runner.recipient(forIdentity: alice.secret, keygenBinary: keygen)) ?? "",
                   alice.recipient)
        check("should refuse to derive a recipient from something that is not an identity",
              (try? runner.recipient(forIdentity: "not-a-key", keygenBinary: keygen)) == nil)

        let file = dir.appendingPathComponent("v.age").path
        let secret = Padding.pad(Data(#"{"name":"JIRA_TOKEN","value":"s3cret"}"#.utf8))

        do {
            try runner.encrypt(secret, to: [alice.recipient, bob.recipient], at: file)
            // An age file is an ASCII header followed by a binary payload, so it
            // is never decodable as a whole string.
            let bytes = (try? Data(contentsOf: URL(fileURLWithPath: file))) ?? Data()
            let header = String(decoding: bytes.prefix(512), as: UTF8.self)
            check("should write a file age recognises", header.hasPrefix("age-encryption.org/v1"))
            checkEqual("should carry one stanza per recipient", recipientStanzaCount(inHeader: header), 2)

            let roundTripped = try runner.decrypt(at: file, identity: alice.secret)
            checkEqual("should decrypt to the padded plaintext", roundTripped, secret)
            checkEqual("should unpad back to the payload",
                       String(decoding: (try? Padding.unpad(roundTripped)) ?? Data(), as: UTF8.self),
                       #"{"name":"JIRA_TOKEN","value":"s3cret"}"#)

            let bySecondRecipient = try runner.decrypt(at: file, identity: bob.secret)
            checkEqual("should decrypt for every recipient on the file", bySecondRecipient, secret)
        } catch {
            check("should round-trip through age", false, "\(error)")
        }

        let stranger = (try? runner.keygen(binary: keygen))?.secret ?? ""
        check("should refuse an identity that is not a recipient",
              (try? runner.decrypt(at: file, identity: stranger)) == nil)
        check("should fail loudly on a ciphertext that does not exist",
              (try? runner.decrypt(at: dir.appendingPathComponent("nope.age").path, identity: alice.secret)) == nil)

        let big = Padding.pad(Data(String(repeating: "x", count: 400_000).utf8))
        let bigFile = dir.appendingPathComponent("big.age").path
        if (try? runner.encrypt(big, to: [alice.recipient], at: bigFile)) != nil,
           let back = try? runner.decrypt(at: bigFile, identity: alice.secret) {
            checkEqual("should survive a payload larger than a pipe buffer", back, big)
        } else {
            check("should survive a payload larger than a pipe buffer", false, "round-trip failed")
        }

        if failures > 0 {
            fputs("\n\(failures) failure(s)\n", stderr)
            exit(1)
        }
        print("\nAll tests passed")
    }
}
