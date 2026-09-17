import Foundation

private func fakeDigest(_ data: Data) -> Data {
    var rolling: UInt8 = 7
    var out = Data()
    for byte in data {
        rolling = (rolling &* 31) &+ byte
        out.append(rolling)
    }
    while out.count < 32 { out.append(rolling &+ UInt8(out.count)) }
    return out.prefix(32)
}

private let macbook = "age1macbookmacbookmacbookmacbookmacbookmacbookmacbookmacbo"
private let devbox = "age1devboxdevboxdevboxdevboxdevboxdevboxdevboxdevboxdevbox"

private func sampleRecipients(version: Int = 1) -> RecipientSet {
    RecipientSet(version: version, tiers: ["high": [macbook], "low": [macbook, devbox]])
}

@main
struct StoreTestRunner {
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

        func checkThrows(_ desc: String, _ body: () throws -> Void, _ expected: StoreError) {
            do {
                try body()
                check(desc, false, "did not throw, wanted \(expected)")
            } catch let error as StoreError {
                check(desc, error == expected, "threw \(error), wanted \(expected)")
            } catch {
                check(desc, false, "threw \(error), wanted \(expected)")
            }
        }

        print("Padding")
        for size in [0, 1, 100, 251, 252, 253, 512, 1000] {
            let body = Data((0..<size).map { UInt8($0 % 251) })
            let padded = Padding.pad(body)
            check("should round-trip \(size) bytes", (try? Padding.unpad(padded)) == body)
            check("should pad \(size) bytes to a block boundary", padded.count % Padding.blockSize == 0)
            check("should never emit an empty block for \(size) bytes", padded.count >= Padding.blockSize)
        }
        checkEqual("should hide length within a block", Padding.pad(Data(repeating: 1, count: 10)).count,
                   Padding.pad(Data(repeating: 1, count: 200)).count)
        checkThrows("should reject padding shorter than its length prefix",
                    { _ = try Padding.unpad(Data([0, 0, 1])) }, .corruptPadding)
        checkThrows("should reject a length that overruns the block",
                    { _ = try Padding.unpad(Data([0, 0, 0, 255]) + Data(repeating: 0, count: 4)) }, .corruptPadding)

        print("\nvariableFile")
        let salt = Data([1, 2, 3, 4])
        let path = variableFile(salt: salt, name: "JIRA_TOKEN", digest: fakeDigest)
        check("should live under the vars directory", path.hasPrefix("vars/"))
        check("should carry the .age suffix", path.hasSuffix(".age"))
        checkEqual("should be 64 hex characters plus vars/ and .age", path.count, 5 + 64 + 4)
        check("should be lowercase hex", path.dropFirst(5).dropLast(4).allSatisfy { "0123456789abcdef".contains($0) })
        checkEqual("should be stable for the same salt and name", path,
                   variableFile(salt: salt, name: "JIRA_TOKEN", digest: fakeDigest))
        check("should differ when the salt differs",
              path != variableFile(salt: Data([9, 9, 9, 9]), name: "JIRA_TOKEN", digest: fakeDigest))
        check("should differ when the name differs",
              path != variableFile(salt: salt, name: "JIRA_EMAIL", digest: fakeDigest))
        checkEqual("should hash the salt before the name, pinned by known answer", path,
                   "vars/da689bc9a1c88af7480cc3e85d919fa0a1a2a3a4a5a6a7a8a9aaabacadaeafb0.age")

        print("\ncheckIntegrity")
        let meta = StoreMeta(files: ["index.age": "aa", "vars/x.age": "bb"])
        check("should pass when every file matches",
              checkIntegrity(meta: meta, actual: ["index.age": "aa", "vars/x.age": "bb"]).isClean)
        checkEqual("should name a missing file",
                   checkIntegrity(meta: meta, actual: ["index.age": "aa"]).missing, ["vars/x.age"])
        checkEqual("should name a modified file",
                   checkIntegrity(meta: meta, actual: ["index.age": "aa", "vars/x.age": "ZZ"]).modified, ["vars/x.age"])
        checkEqual("should name an unexpected file, which is what a Drive conflict copy looks like",
                   checkIntegrity(meta: meta, actual: ["index.age": "aa", "vars/x.age": "bb", "vars/x (1).age": "cc"]).unexpected,
                   ["vars/x (1).age"])
        check("should not report a modified file as missing",
              checkIntegrity(meta: meta, actual: ["index.age": "aa", "vars/x.age": "ZZ"]).missing.isEmpty)

        print("\nvalidateRecipients")
        check("should accept identical sets at the same version",
              (try? validateRecipients(pinned: sampleRecipients(), canonical: sampleRecipients(), highestSeenVersion: 1)) != nil)
        checkThrows("should reject a canonical set the pinned file does not match",
                    { try validateRecipients(pinned: sampleRecipients(),
                                             canonical: RecipientSet(version: 1, tiers: ["high": [macbook, devbox], "low": [macbook, devbox]]),
                                             highestSeenVersion: 1) },
                    .recipientSetDivergence)
        checkThrows("should reject a version older than the highest seen",
                    { try validateRecipients(pinned: sampleRecipients(version: 3),
                                             canonical: sampleRecipients(version: 2),
                                             highestSeenVersion: 3) },
                    .recipientSetRollback(pinned: 3, canonical: 2))
        checkThrows("should reject a bumped canonical version the pinned file has not adopted",
                    { try validateRecipients(pinned: sampleRecipients(version: 1),
                                             canonical: sampleRecipients(version: 2),
                                             highestSeenVersion: 1) },
                    .recipientSetDivergence)

        print("\nRecipientSet")
        checkEqual("should return the high tier sorted", sampleRecipients().recipients(for: .high), [macbook])
        checkEqual("should return the low tier sorted", sampleRecipients().recipients(for: .low), [devbox, macbook].sorted())
        checkEqual("should return nothing for a tier it does not define",
                   RecipientSet(version: 1, tiers: ["low": [macbook]]).recipients(for: .high), [])

        print("\nverify(payload:)")
        let payload = VariablePayload(name: "JIRA_TOKEN", value: "s3cret")
        checkEqual("should return the value when the embedded name matches",
                   (try? verify(payload: payload, expecting: "JIRA_TOKEN")) ?? "", "s3cret")
        checkThrows("should reject a payload whose embedded name differs, which is a swapped file",
                    { _ = try verify(payload: payload, expecting: "JIRA_EMAIL") },
                    .payloadNameMismatch(expected: "JIRA_EMAIL", found: "JIRA_TOKEN"))

        print("\nindex and meta encoding")
        let index = StoreIndex(salt: salt,
                               recipients: sampleRecipients(),
                               entries: ["JIRA_TOKEN": IndexEntry(file: "vars/ab.age", tier: .high)])
        if let encoded = try? storeEncoder().encode(index),
           let decoded = try? storeDecoder().decode(StoreIndex.self, from: encoded) {
            checkEqual("should round-trip the index", decoded, index)
            let json = String(data: encoded, encoding: .utf8) ?? ""
            check("should hold no secret value", !json.contains("s3cret"))
            check("should encode the salt as base64", json.contains("\"salt\":\"AQIDBA==\""))
        } else {
            check("should round-trip the index", false, "encode or decode failed")
        }
        if let encoded = try? storeEncoder().encode(meta),
           let decoded = try? storeDecoder().decode(StoreMeta.self, from: encoded) {
            checkEqual("should round-trip meta", decoded, meta)
        } else {
            check("should round-trip meta", false, "encode or decode failed")
        }
        check("should reject an unknown tier rather than defaulting it",
              (try? storeDecoder().decode(IndexEntry.self,
                                          from: Data(#"{"file":"vars/a.age","tier":"medium"}"#.utf8))) == nil)

        print("\nmigrationPlan")
        let plan = migrationPlan(entries: ["B_TOKEN": "two", "A_TOKEN": "one"],
                                 salt: salt,
                                 recipients: sampleRecipients(),
                                 tierFor: { $0 == "A_TOKEN" ? .low : .high },
                                 digest: fakeDigest)
        checkEqual("should plan one file per variable", plan.variables.count, 2)
        checkEqual("should plan variables in a stable order",
                   plan.variables.map { $0.payload.name }, ["A_TOKEN", "B_TOKEN"])
        checkEqual("should carry each value into its payload",
                   plan.variables.map { $0.payload.value }, ["one", "two"])
        checkEqual("should apply the tier decision per variable",
                   plan.variables.map { $0.tier }, [.low, .high])
        checkEqual("should index every planned variable", Set(plan.index.entries.keys), ["A_TOKEN", "B_TOKEN"])
        checkEqual("should point the index at the planned path",
                   plan.index.entries["A_TOKEN"]?.file, plan.variables[0].path)
        checkEqual("should record the tier in the index", plan.index.entries["A_TOKEN"]?.tier, .low)
        checkEqual("should stamp the current store version", plan.index.version, StoreIndex.currentVersion)
        checkEqual("should give every variable a distinct file",
                   Set(plan.variables.map { $0.path }).count, 2)
        check("should produce an empty plan for an empty store",
              migrationPlan(entries: [:], salt: salt, recipients: sampleRecipients(),
                            tierFor: { _ in .high }, digest: fakeDigest).variables.isEmpty)

        if failures > 0 {
            fputs("\n\(failures) failure(s)\n", stderr)
            exit(1)
        }
        print("\nAll tests passed")
    }
}
