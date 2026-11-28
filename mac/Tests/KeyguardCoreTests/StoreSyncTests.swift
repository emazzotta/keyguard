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

        print("\na pull keeps local changes the service never received")
        let hub = FakeService(digest: rollingDigest)
        let mac = seededStore(["A": "alpha"])
        let macVersionURL = tempURL("version").appendingPathComponent("v")
        let macSync = StoreSync(store: mac, remote: RemoteStore(transport: hub),
                                digest: rollingDigest, versionURL: macVersionURL)
        _ = try! macSync.push()
        let macIndex = try! mac.put(name: "LOCAL_ONLY", value: "kept", tier: .high,
                                    index: try! mac.loadIndex(identity: owner.secret))
        let unsent = ["index.age", macIndex.entries["LOCAL_ONLY"]!.file].sorted()
        checkEqual("should report an unsent secret while the service is unchanged",
                   try? macSync.pull(), .localAhead(unpushed: unsent))
        _ = try! RemoteStore(transport: hub)
            .commit(files: ["vars/\(String(repeating: "b", count: 64)).age": Data("y".utf8)], deletes: [], ifMatch: 1)
        checkEqual("should refuse to replace the store once the service has moved on",
                   divergence { _ = try macSync.pull() }, StoreDivergence(unpushed: unsent, serviceVersion: 2))
        checkEqual("should still decrypt the unsent secret after the refused pull",
                   try? mac.values(of: ["LOCAL_ONLY"], index: macIndex, identity: owner.secret),
                   ["LOCAL_ONLY": "kept"])
        try? FileManager.default.removeItem(at: macVersionURL.appendingPathExtension("files"))
        check("should refuse the same pull when no record of the last sync survives",
              divergence { _ = try macSync.pull() } != nil)
        try? Data("finder".utf8).write(to: mac.url(for: ".DS_Store"))
        checkEqual("should not count a stray file as an unsent change",
                   divergence { _ = try macSync.pull() }?.unpushed, unsent)
        checkEqual("should take the service's copy when told to discard local changes",
                   try? macSync.pull(discardingLocalChanges: true), .pulled(version: 2))
        checkEqual("should drop the unsent secret on a discarding pull",
                   Set((try? mac.loadIndex(identity: owner.secret))?.entries.keys ?? [:].keys), Set(["A"]))
        checkEqual("should be in step with the service after a discarding pull", try? macSync.pull(), .upToDate)

        print("\nan unsent change reaches the service on the next push")
        let relay = FakeService(digest: rollingDigest)
        let laptop = seededStore(["A": "alpha"])
        let laptopSync = syncFor(laptop, relay)
        _ = try! laptopSync.push()
        _ = try! laptop.put(name: "LATE", value: "sent", tier: .high,
                            index: try! laptop.loadIndex(identity: owner.secret))
        checkEqual("should push the unsent secret", try? laptopSync.push(), .pushed(version: 2))
        checkEqual("should be in step with the service after the push", try? laptopSync.pull(), .upToDate)
        let second = newStore()
        _ = try? syncFor(second, relay).pull()
        checkEqual("should hand the late secret to another machine",
                   (try? second.loadIndex(identity: owner.secret))
                       .flatMap { try? second.values(of: ["LATE"], index: $0, identity: owner.secret) },
                   ["LATE": "sent"])

        print("\na store that never synced is not replaced by the service's")
        let stranger = seededStore(["MINE": "local"])
        check("should refuse to overwrite a never-synced store",
              divergence { _ = try syncFor(stranger, relay).pull() } != nil)
        checkEqual("should keep the never-synced store readable",
                   (try? stranger.loadIndex(identity: owner.secret))
                       .flatMap { try? stranger.values(of: ["MINE"], index: $0, identity: owner.secret) },
                   ["MINE": "local"])
        checkEqual("should refuse to rebase without a record of the last sync",
                   refusal { _ = try syncFor(stranger, relay).rebase(identity: owner.secret) }, .noSyncRecord)

        print("\nunsent changes are rebased onto a service that moved on")
        let cloud = FakeService(digest: rollingDigest)
        let desk = seededStore(["A": "alpha"])
        let deskSync = syncFor(desk, cloud)
        _ = try! deskSync.push()
        let phone = newStore()
        let phoneSync = syncFor(phone, cloud)
        _ = try! phoneSync.pull()
        _ = try! phone.put(name: "FROM_PHONE", value: "p", tier: .high,
                           index: try! phone.loadIndex(identity: owner.secret))
        _ = try! phoneSync.push()
        _ = try! desk.put(name: "FROM_DESK", value: "d", tier: .high,
                          index: try! desk.loadIndex(identity: owner.secret))
        check("should refuse a plain pull before the rebase", divergence { _ = try deskSync.pull() } != nil)
        checkEqual("should leave only the desk's own index and secret unsent after the rebase",
                   (try? deskSync.rebase(identity: owner.secret)).map { $0.count }, 2)
        checkEqual("should hold both sides' secrets after the rebase",
                   (try? desk.loadIndex(identity: owner.secret)).flatMap {
                       try? desk.values(of: ["A", "FROM_DESK", "FROM_PHONE"], index: $0, identity: owner.secret)
                   },
                   ["A": "alpha", "FROM_DESK": "d", "FROM_PHONE": "p"])
        check("should keep the integrity manifest clean after the rebase", ((try? desk.integrity())?.isClean) ?? false)
        checkEqual("should push the rebased store as the next version", try? deskSync.push(), .pushed(version: 3))
        _ = try? phoneSync.pull()
        checkEqual("should hand the merged store to the other device",
                   Set((try? phone.loadIndex(identity: owner.secret))?.entries.keys ?? [:].keys),
                   Set(["A", "FROM_DESK", "FROM_PHONE"]))

        print("\na secret changed on both sides is not merged")
        _ = try! desk.put(name: "A", value: "desk-edit", tier: .high,
                          index: try! desk.loadIndex(identity: owner.secret))
        _ = try! phone.put(name: "A", value: "phone-edit", tier: .high,
                           index: try! phone.loadIndex(identity: owner.secret))
        _ = try! phoneSync.push()
        checkEqual("should name the secret both sides changed",
                   refusal { _ = try deskSync.rebase(identity: owner.secret) }, .changedOnBothSides(["A"]))
        checkEqual("should leave the desk's edit in place after the refusal",
                   (try? desk.loadIndex(identity: owner.secret)).flatMap {
                       try? desk.values(of: ["A"], index: $0, identity: owner.secret)
                   },
                   ["A": "desk-edit"])

        print("\na secret deleted here stays deleted through a rebase")
        let vault = FakeService(digest: rollingDigest)
        let first = seededStore(["KEEP": "k", "DROP": "x"])
        let firstSync = syncFor(first, vault)
        _ = try! firstSync.push()
        let peer = newStore()
        let peerSync = syncFor(peer, vault)
        _ = try! peerSync.pull()
        _ = try! peer.put(name: "NEW", value: "n", tier: .high, index: try! peer.loadIndex(identity: owner.secret))
        _ = try! peerSync.push()
        _ = try! first.remove(name: "DROP", index: try! first.loadIndex(identity: owner.secret))
        _ = try? firstSync.rebase(identity: owner.secret)
        checkEqual("should push the deletion on top of the other side's addition",
                   try? firstSync.push(), .pushed(version: 3))
        _ = try? peerSync.pull()
        checkEqual("should leave the other device without the deleted secret and with its own addition",
                   Set((try? peer.loadIndex(identity: owner.secret))?.entries.keys ?? [:].keys), Set(["KEEP", "NEW"]))

        for root in roots { try? FileManager.default.removeItem(at: root) }

        if failures > 0 { fputs("\n\(failures) failure(s)\n", stderr); exit(1) }
        print("\nAll tests passed")
    }

    private static func throwsConflict(_ body: () throws -> Void) -> Bool {
        do { try body(); return false } catch RemoteStoreError.conflict { return true } catch { return false }
    }

    private static func divergence(_ body: () throws -> Void) -> StoreDivergence? {
        do { try body(); return nil } catch let error as StoreDivergence { return error } catch { return nil }
    }

    private static func refusal(_ body: () throws -> Void) -> RebaseRefusal? {
        do { try body(); return nil } catch let error as RebaseRefusal { return error } catch { return nil }
    }
}
