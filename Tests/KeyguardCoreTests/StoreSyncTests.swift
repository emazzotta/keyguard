import Foundation

private func rollingDigest(_ data: Data) -> Data {
    var hash: UInt64 = 0xcbf2_9ce4_8422_2325
    for byte in data { hash = (hash ^ UInt64(byte)) &* 0x100_0000_01b3 }
    hash = (hash ^ UInt64(data.count)) &* 0x100_0000_01b3
    var bigEndian = hash.bigEndian
    var out = Data()
    withUnsafeBytes(of: &bigEndian) { out.append(contentsOf: $0) }
    return out
}

private func json(_ object: [String: Any]) -> Data {
    (try? JSONSerialization.data(withJSONObject: object)) ?? Data()
}

private final class FakeService: StoreTransport {
    private(set) var version: Int
    private(set) var files: [String: Data]
    private let digest: DigestFunction
    var reachable = true

    init(digest: @escaping DigestFunction, version: Int = 0, files: [String: Data] = [:]) {
        self.digest = digest
        self.version = version
        self.files = files
    }

    func send(_ request: HTTPRequest) throws -> HTTPResponse {
        guard reachable else { throw NSError(domain: "offline", code: -1) }
        switch (request.method, request.path) {
        case (.get, "store/meta"):
            return HTTPResponse(status: 200, body: json(["version": version,
                                                         "files": files.mapValues { hex(digest($0)) }]))
        case (.post, "store/commit"):
            return commit(request)
        case (.get, let path) where path.hasPrefix("store/"):
            let rel = String(path.dropFirst("store/".count))
            guard let data = files[rel] else { return HTTPResponse(status: 404) }
            return HTTPResponse(status: 200, body: data)
        default:
            return HTTPResponse(status: 404)
        }
    }

    private func commit(_ request: HTTPRequest) -> HTTPResponse {
        if let ifMatch = request.headers["If-Match"].flatMap({ Int($0) }), ifMatch != version {
            return HTTPResponse(status: 412, body: json(["error": "version is \(version)"]))
        }
        let payload = (try? JSONSerialization.jsonObject(with: request.body ?? Data())) as? [String: Any] ?? [:]
        for (path, base64) in (payload["files"] as? [String: String]) ?? [:] {
            files[path] = Data(base64Encoded: base64) ?? Data()
        }
        for path in (payload["delete"] as? [String]) ?? [] { files.removeValue(forKey: path) }
        version += 1
        return HTTPResponse(status: 200, body: json(["version": version]))
    }
}

@main
struct StoreSyncTestRunner {
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

        guard let ageBinary = try? AgeRunner.locate(candidates: AgeCommand.searchPaths + ["/usr/bin/age"]),
              let keygenBinary = try? AgeRunner.locate(candidates: AgeCommand.keygenSearchPaths + ["/usr/bin/age-keygen"]) else {
            print("age binary not present - skipping store sync tests")
            return
        }
        let runner = AgeRunner(binary: ageBinary)
        guard let owner = try? runner.keygen(binary: keygenBinary) else {
            fputs("could not generate an identity\n", stderr); exit(1)
        }
        let recipients = RecipientSet(version: 1, tiers: ["high": [owner.recipient], "low": [owner.recipient]])

        var roots: [URL] = []
        func tempURL(_ tag: String) -> URL {
            let url = URL(fileURLWithPath: NSTemporaryDirectory())
                .appendingPathComponent("keyguard-\(tag)-\(UUID().uuidString)")
            roots.append(url)
            return url
        }
        func newStore() -> Store {
            Store(root: tempURL("sync-store"), runner: runner, digest: rollingDigest)
        }
        func seededStore(_ values: [String: String]) -> Store {
            let store = newStore()
            try! store.create(salt: Data([9, 8, 7, 6]), recipients: recipients, identity: owner.secret)
            var index = try! store.loadIndex(identity: owner.secret)
            for name in values.keys.sorted() {
                index = try! store.put(name: name, value: values[name]!, tier: .high, index: index)
            }
            return store
        }
        func syncFor(_ store: Store, _ service: FakeService) -> StoreSync {
            StoreSync(store: store, remote: RemoteStore(transport: service),
                      digest: rollingDigest, versionURL: tempURL("version").appendingPathComponent("v"))
        }

        print("push seeds an empty service")
        let service = FakeService(digest: rollingDigest)
        let source = seededStore(["A": "alpha", "B": "beta"])
        let pushSync = syncFor(source, service)
        checkEqual("should report the seeded version", try? pushSync.push(), .pushed(version: 1))
        checkEqual("should leave the service holding index.age plus one file per variable",
                   Set(service.files.keys), Set(["index.age"] + (try! source.remoteFiles()).keys.filter { $0 != "index.age" }))
        check("should send exactly index.age and two vars", service.files.count == 3)
        check("should never send the local integrity manifest", service.files["meta.json"] == nil)
        checkEqual("should no-op a second push", try? pushSync.push(), .upToDate)

        print("\npull rebuilds a working store on a fresh machine")
        let fresh = newStore()
        let pullSync = syncFor(fresh, service)
        checkEqual("should report the pulled version", try? pullSync.pull(), .pulled(version: 1))
        let index = try? fresh.loadIndex(identity: owner.secret)
        let values = index.flatMap { try? fresh.values(of: ["A", "B"], index: $0, identity: owner.secret) }
        checkEqual("should decrypt every pulled secret", values, ["A": "alpha", "B": "beta"])
        check("should rebuild a clean integrity manifest after a pull", ((try? fresh.integrity())?.isClean) ?? false)
        checkEqual("should no-op a pull at the same version", try? pullSync.pull(), .upToDate)

        print("\nan unseeded service is left alone")
        let empty = FakeService(digest: rollingDigest)
        let localOnly = seededStore(["KEEP": "value"])
        checkEqual("should report the service as empty rather than wiping local",
                   try? syncFor(localOnly, empty).pull(), .remoteEmpty)
        check("should not delete the local store when the service is empty",
              ((try? localOnly.values(of: ["KEEP"],
                                      index: try! localOnly.loadIndex(identity: owner.secret),
                                      identity: owner.secret)))?["KEEP"] == "value")

        print("\na change made elsewhere is a conflict, not a lost update")
        let shared = FakeService(digest: rollingDigest)
        let deviceA = seededStore(["X": "one"])
        let syncA = syncFor(deviceA, shared)
        _ = try! syncA.push()
        _ = try! RemoteStore(transport: shared)
            .commit(files: ["vars/\(String(repeating: "a", count: 64)).age": Data("x".utf8)], deletes: [], ifMatch: 1)
        var indexA = try! deviceA.loadIndex(identity: owner.secret)
        indexA = try! deviceA.put(name: "Y", value: "two", tier: .high, index: indexA)
        check("should refuse to push over a version it has not seen",
              throwsConflict { _ = try syncA.push() })

        print("\na deletion propagates through push and pull")
        let repl = FakeService(digest: rollingDigest)
        let writer = seededStore(["A": "alpha", "B": "beta"])
        let writerSync = syncFor(writer, repl)
        _ = try! writerSync.push()
        let reader = newStore()
        let readerSync = syncFor(reader, repl)
        _ = try! readerSync.pull()
        let removedFile = try! writer.loadIndex(identity: owner.secret).entries["B"]!.file
        var writerIndex = try! writer.loadIndex(identity: owner.secret)
        writerIndex = try! writer.remove(name: "B", index: writerIndex)
        _ = try! writerSync.push()
        _ = try! readerSync.pull()
        check("should delete a remotely-removed file from the local cache",
              !FileManager.default.fileExists(atPath: reader.url(for: removedFile).path))
        checkEqual("should leave the reader holding only what remains",
                   Set((try? reader.loadIndex(identity: owner.secret))?.entries.keys ?? [:].keys), Set(["A"]))
        check("should keep the reader's integrity clean after a delete pull",
              ((try? reader.integrity())?.isClean) ?? false)

        for root in roots { try? FileManager.default.removeItem(at: root) }

        if failures > 0 { fputs("\n\(failures) failure(s)\n", stderr); exit(1) }
        print("\nAll tests passed")
    }

    private static func throwsConflict(_ body: () throws -> Void) -> Bool {
        do { try body(); return false } catch RemoteStoreError.conflict { return true } catch { return false }
    }
}
