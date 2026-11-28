import Foundation

@main
struct LocationsTestRunner {
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

        let home = "/Users/someone"

        print("storeRoot")
        checkEqual("should honour an explicit store path",
                   Locations.storeRoot(environment: ["KEYGUARD_STORE": "/elsewhere/store"], home: home).path,
                   "/elsewhere/store")
        checkEqual("should expand a tilde in an explicit store path",
                   Locations.storeRoot(environment: ["KEYGUARD_STORE": "~/somewhere/store"], home: home).path,
                   "/Users/someone/somewhere/store")
        checkEqual("should sit beside the legacy secrets file so the store keeps its backup",
                   Locations.storeRoot(environment: ["KEYGUARD_SECRETS_FILE": "/Users/someone/Drive/Keepass/keyguard.enc"], home: home).path,
                   "/Users/someone/Drive/Keepass/keyguard-store")
        checkEqual("should prefer the explicit store path over the legacy one",
                   Locations.storeRoot(environment: ["KEYGUARD_STORE": "/a", "KEYGUARD_SECRETS_FILE": "/b/c.enc"], home: home).path,
                   "/a")
        checkEqual("should fall back under home when nothing is set",
                   Locations.storeRoot(environment: [:], home: home).path,
                   "/Users/someone/.keyguard/store")
        checkEqual("should ignore an empty store variable",
                   Locations.storeRoot(environment: ["KEYGUARD_STORE": ""], home: home).path,
                   "/Users/someone/.keyguard/store")

        print("\nrecipientsFile")
        checkEqual("should default under home", Locations.recipientsFile(environment: [:], home: home).path,
                   "/Users/someone/.keyguard/recipients")
        check("should never follow the secrets file, which is synced and so untrusted",
              Locations.recipientsFile(environment: ["KEYGUARD_SECRETS_FILE": "/Users/someone/Drive/Keepass/keyguard.enc"],
                                       home: home).path == "/Users/someone/.keyguard/recipients")
        checkEqual("should honour an explicit override",
                   Locations.recipientsFile(environment: ["KEYGUARD_RECIPIENTS_FILE": "/custom/recipients"], home: home).path,
                   "/custom/recipients")

        print("\nlegacySecretsFile")
        checkEqual("should default under home", Locations.legacySecretsFile(environment: [:], home: home).path,
                   "/Users/someone/.keyguard/secrets.enc")
        checkEqual("should honour the existing variable",
                   Locations.legacySecretsFile(environment: ["KEYGUARD_SECRETS_FILE": "~/Drive/k.enc"], home: home).path,
                   "/Users/someone/Drive/k.enc")

        print("\npinned recipients file")
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("keyguard-locations-\(UUID().uuidString)")
        let file = dir.appendingPathComponent("recipients")
        let set = RecipientSet(version: 2, tiers: ["high": ["age1a"], "low": ["age1a", "age1b"]])
        if (try? writePinnedRecipients(set, to: file)) != nil {
            checkEqual("should round-trip through disk", (try? loadPinnedRecipients(at: file)), set)
            check("should create the directory it needs",
                  FileManager.default.fileExists(atPath: dir.path))
            let raw = (try? String(contentsOf: file, encoding: .utf8)) ?? ""
            check("should be readable as plain JSON", raw.contains("\"version\":2"))
        } else {
            check("should round-trip through disk", false, "write failed")
        }
        check("should fail rather than invent a set when the file is absent",
              (try? loadPinnedRecipients(at: dir.appendingPathComponent("nope"))) == nil)
        try? Data("{ not json".utf8).write(to: file)
        check("should fail on a corrupt pinned file", (try? loadPinnedRecipients(at: file)) == nil)
        try? FileManager.default.removeItem(at: dir)

        if failures > 0 { fputs("\n\(failures) failure(s)\n", stderr); exit(1) }
        print("\nAll tests passed")
    }
}
