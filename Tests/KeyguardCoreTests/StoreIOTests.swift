import Foundation

private func rollingDigest(_ data: Data) -> Data {
    var lanes = [UInt64](repeating: 0xcbf2_9ce4_8422_2325, count: 4)
    for (offset, byte) in data.enumerated() {
        let lane = offset % lanes.count
        lanes[lane] = (lanes[lane] ^ UInt64(byte)) &* 0x100_0000_01b3
    }
    lanes[0] = (lanes[0] ^ UInt64(data.count)) &* 0x100_0000_01b3
    for round in 0..<(lanes.count - 1) {
        for lane in 0..<lanes.count {
            lanes[lane] = (lanes[lane] ^ lanes[(lane + round + 1) % lanes.count]) &* 0x100_0000_01b3
        }
    }
    var out = Data()
    for lane in lanes {
        var bigEndian = lane.bigEndian
        withUnsafeBytes(of: &bigEndian) { out.append(contentsOf: $0) }
    }
    return out
}

private func encryptedSample(_ runner: AgeRunner, to recipient: String, body: String) throws -> Data {
    let path = NSTemporaryDirectory() + "/keyguard-sample-\(UUID().uuidString).age"
    defer { try? FileManager.default.removeItem(atPath: path) }
    try runner.encrypt(Data(body.utf8), to: [recipient], at: path)
    return try Data(contentsOf: URL(fileURLWithPath: path))
}

@main
struct StoreIOTestRunner {
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

        guard let ageBinary = try? AgeRunner.locate(candidates: AgeCommand.searchPaths + ["/usr/bin/age"]),
              let keygenBinary = try? AgeRunner.locate(candidates: AgeCommand.keygenSearchPaths + ["/usr/bin/age-keygen"]) else {
            print("age binary not present - skipping store I/O tests")
            return
        }

        let runner = AgeRunner(binary: ageBinary)
        guard let macbook = try? runner.keygen(binary: keygenBinary),
              let devbox = try? runner.keygen(binary: keygenBinary),
              let stranger = try? runner.keygen(binary: keygenBinary) else {
            fputs("could not generate identities\n", stderr)
            exit(1)
        }

        let recipients = RecipientSet(version: 1, tiers: [
            "high": [macbook.recipient],
            "low": [macbook.recipient, devbox.recipient]
        ])

        func freshStore() -> Store {
            let root = URL(fileURLWithPath: NSTemporaryDirectory())
                .appendingPathComponent("keyguard-store-tests-\(UUID().uuidString)")
            let store = Store(root: root, runner: runner, digest: rollingDigest)
            try! store.create(salt: Data([9, 8, 7, 6]), recipients: recipients, identity: macbook.secret)
            return store
        }

        func stanzaCount(_ url: URL) -> Int {
            let bytes = (try? Data(contentsOf: url)) ?? Data()
            return recipientStanzaCount(inHeader: String(decoding: bytes.prefix(512), as: UTF8.self))
        }

        print("digest helper")
        let sampleA = (try? encryptedSample(runner, to: macbook.recipient, body: "aaaa")) ?? Data()
        let sampleB = (try? encryptedSample(runner, to: macbook.recipient, body: "bbbb")) ?? Data()
        check("should distinguish two age files, or every integrity test below is vacuous",
              !sampleA.isEmpty && rollingDigest(sampleA) != rollingDigest(sampleB))
        checkEqual("should be stable for identical input", rollingDigest(sampleA), rollingDigest(sampleA))

        print("\ncreate")
        let empty = freshStore()
        check("should report the store as existing", empty.exists)
        check("should write an index", FileManager.default.fileExists(atPath: empty.indexURL.path))
        check("should write meta", FileManager.default.fileExists(atPath: empty.metaURL.path))
        check("should start with no variables",
              ((try? empty.loadIndex(identity: macbook.secret))?.entries.isEmpty) ?? false)
        check("should start with clean integrity", ((try? empty.integrity())?.isClean) ?? false)
        check("should not report a store that was never created",
              !Store(root: URL(fileURLWithPath: "/nonexistent/keyguard"), runner: runner, digest: rollingDigest).exists)

        print("\nput and read back")
        let store = freshStore()
        var index = try! store.loadIndex(identity: macbook.secret)
        index = try! store.put(name: "JIRA_TOKEN", value: "s3cret", tier: .high, index: index)
        index = try! store.put(name: "GITHUB_TOKEN", value: "gh-value", tier: .low, index: index)

        let readBack = (try? store.values(of: ["JIRA_TOKEN", "GITHUB_TOKEN"], index: index, identity: macbook.secret)) ?? [:]
        checkEqual("should return every requested value", readBack,
                   ["JIRA_TOKEN": "s3cret", "GITHUB_TOKEN": "gh-value"])
        checkEqual("should give each variable its own file",
                   Set(index.entries.values.map { $0.file }).count, 2)
        check("should keep integrity clean after writes", ((try? store.integrity())?.isClean) ?? false)
        check("should leave no staging files behind",
              !FileManager.default.fileExists(atPath: store.indexURL.path + ".tmp"))

        let reopened = Store(root: store.root, runner: runner, digest: rollingDigest)
        checkEqual("should survive being reopened",
                   (try? reopened.values(of: ["JIRA_TOKEN"],
                                         index: try! reopened.loadIndex(identity: macbook.secret),
                                         identity: macbook.secret)) ?? [:],
                   ["JIRA_TOKEN": "s3cret"])

        print("\ntiers decide recipients")
        checkEqual("should seal a high-tier variable to the high recipients only",
                   stanzaCount(store.url(for: index.entries["JIRA_TOKEN"]!.file)), 1)
        checkEqual("should seal a low-tier variable to every low recipient",
                   stanzaCount(store.url(for: index.entries["GITHUB_TOKEN"]!.file)), 2)
        check("should let a low-tier recipient read a low-tier variable",
              (try? store.values(of: ["GITHUB_TOKEN"], index: index, identity: devbox.secret)) != nil)
        check("should keep a high-tier variable away from a low-tier recipient",
              (try? store.values(of: ["JIRA_TOKEN"], index: index, identity: devbox.secret)) == nil)

        print("\nindex secrecy")
        let indexBytes = (try? Data(contentsOf: store.indexURL)) ?? Data()
        check("should not leak a value into the index",
              !String(decoding: indexBytes, as: UTF8.self).contains("s3cret"))
        check("should not leak a variable name into the index",
              !String(decoding: indexBytes, as: UTF8.self).contains("JIRA_TOKEN"))
        check("should not leak a variable name into a filename",
              !index.entries["JIRA_TOKEN"]!.file.contains("JIRA"))
        check("should refuse an identity that is on no file",
              (try? store.loadIndex(identity: stranger.secret)) == nil)

        print("\nindex reachability")
        check("should let a low-tier recipient open the index",
              (try? store.loadIndex(identity: devbox.secret)) != nil)
        checkEqual("should show a low-tier recipient the same entries",
                   Set((try? store.loadIndex(identity: devbox.secret))?.entries.keys ?? [:].keys),
                   Set(index.entries.keys))
        check("should keep the index away from an identity in no tier",
              (try? store.loadIndex(identity: stranger.secret)) == nil)

        print("\nstore version")
        let futureRoot = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("keyguard-future-\(UUID().uuidString)")
        let future = Store(root: futureRoot, runner: runner, digest: rollingDigest)
        try! future.create(salt: Data([1]), recipients: recipients, identity: macbook.secret)
        let ahead = StoreIndex(version: StoreIndex.currentVersion + 1,
                               salt: Data([1]), recipients: recipients, entries: [:])
        try! runner.encrypt(Padding.pad(try! storeEncoder().encode(ahead)),
                            to: [macbook.recipient], at: future.indexURL.path)
        check("should refuse a store written by a newer keyguard rather than guessing at it",
              (try? future.loadIndex(identity: macbook.secret)) == nil)
        try? FileManager.default.removeItem(at: futureRoot)

        print("\noverwrite and remove")
        index = try! store.put(name: "JIRA_TOKEN", value: "rotated", tier: .high, index: index)
        checkEqual("should overwrite in place",
                   (try? store.values(of: ["JIRA_TOKEN"], index: index, identity: macbook.secret)) ?? [:],
                   ["JIRA_TOKEN": "rotated"])
        checkEqual("should not grow the index on overwrite", index.entries.count, 2)

        let removedFile = index.entries["GITHUB_TOKEN"]!.file
        index = try! store.remove(name: "GITHUB_TOKEN", index: index)
        check("should drop the entry from the index", index.entries["GITHUB_TOKEN"] == nil)
        check("should delete the ciphertext", !FileManager.default.fileExists(atPath: store.url(for: removedFile).path))
        check("should keep integrity clean after a removal", ((try? store.integrity())?.isClean) ?? false)
        check("should refuse to remove something that is not there",
              (try? store.remove(name: "NOT_THERE", index: index)) == nil)
        check("should refuse to read something that is not there",
              (try? store.values(of: ["JIRA_TOKEN", "NOT_THERE"], index: index, identity: macbook.secret)) == nil)

        print("\ntamper detection")
        let tampered = freshStore()
        var tamperedIndex = try! tampered.loadIndex(identity: macbook.secret)
        tamperedIndex = try! tampered.put(name: "ALPHA", value: "alpha-value", tier: .high, index: tamperedIndex)
        tamperedIndex = try! tampered.put(name: "BETA", value: "beta-value", tier: .high, index: tamperedIndex)

        let alpha = tampered.url(for: tamperedIndex.entries["ALPHA"]!.file)
        let beta = tampered.url(for: tamperedIndex.entries["BETA"]!.file)
        let alphaBytes = try! Data(contentsOf: alpha)
        try! Data(contentsOf: beta).write(to: alpha)
        check("should reject a swapped ciphertext by its embedded name",
              (try? tampered.values(of: ["ALPHA"], index: tamperedIndex, identity: macbook.secret)) == nil)
        checkEqual("should report the swapped file as modified",
                   ((try? tampered.integrity())?.modified) ?? [], [tamperedIndex.entries["ALPHA"]!.file])
        try! alphaBytes.write(to: alpha)
        check("should be clean again once the file is restored", ((try? tampered.integrity())?.isClean) ?? false)

        try! Data("not an age file".utf8).write(to: tampered.url(for: "vars/deadbeef.age"))
        checkEqual("should report an unexpected file, which is what a Drive conflict copy looks like",
                   ((try? tampered.integrity())?.unexpected) ?? [], ["vars/deadbeef.age"])
        try! FileManager.default.removeItem(at: tampered.url(for: "vars/deadbeef.age"))

        try! FileManager.default.removeItem(at: beta)
        checkEqual("should report a deleted ciphertext as missing",
                   ((try? tampered.integrity())?.missing) ?? [], [tamperedIndex.entries["BETA"]!.file])

        print("\nawkward values")
        let awkward = freshStore()
        var awkwardIndex = try! awkward.loadIndex(identity: macbook.secret)
        let cases = [
            "MULTILINE": "-----BEGIN KEY-----\nline two\nline three\n-----END KEY-----",
            "UNICODE": "pässwörd-with-ümlauts-\u{1F510}",
            "EQUALS": "a=b=c==",
            "HASH": "value # not a comment",
            "LONG": String(repeating: "x", count: 50_000),
            "SPACES": "  leading and trailing  "
        ]
        for (name, value) in cases.sorted(by: { $0.key < $1.key }) {
            awkwardIndex = try! awkward.put(name: name, value: value, tier: .high, index: awkwardIndex)
        }
        let awkwardBack = (try? awkward.values(of: Array(cases.keys), index: awkwardIndex, identity: macbook.secret)) ?? [:]
        for (name, value) in cases.sorted(by: { $0.key < $1.key }) {
            checkEqual("should round-trip \(name) exactly", awkwardBack[name], value)
        }
        check("should keep integrity clean across awkward values", ((try? awkward.integrity())?.isClean) ?? false)

        for store in [empty, store, tampered, awkward] {
            try? FileManager.default.removeItem(at: store.root)
        }

        if failures > 0 {
            fputs("\n\(failures) failure(s)\n", stderr)
            exit(1)
        }
        print("\nAll tests passed")
    }
}
