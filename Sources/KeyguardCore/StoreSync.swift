import Foundation

public enum SyncOutcome: Equatable, Sendable {
    case upToDate
    case remoteEmpty
    case pulled(version: Int)
    case pushed(version: Int)
}

public struct StoreSync {
    private let store: Store
    private let remote: RemoteStore
    private let digest: DigestFunction
    private let versionURL: URL

    public init(store: Store, remote: RemoteStore, digest: @escaping DigestFunction, versionURL: URL) {
        self.store = store
        self.remote = remote
        self.digest = digest
        self.versionURL = versionURL
    }

    public func pull() throws -> SyncOutcome {
        let meta = try remote.meta()
        if meta.version == syncedVersion { return .upToDate }
        if meta.version == 0 { return .remoteEmpty }

        try reconcile(to: meta)
        try store.rebuildMeta()
        try writeSyncedVersion(meta.version)
        return .pulled(version: meta.version)
    }

    public func push() throws -> SyncOutcome {
        let meta = try remote.meta()
        let base = max(syncedVersion, 0)
        guard meta.version == base else { throw RemoteStoreError.conflict(current: meta.version) }

        let local = try store.remoteFiles()
        var changed: [String: Data] = [:]
        for (path, data) in local where meta.files[path] != hex(digest(data)) {
            changed[path] = data
        }
        let deletes = meta.files.keys.filter { local[$0] == nil }.sorted()

        if changed.isEmpty, deletes.isEmpty {
            try writeSyncedVersion(meta.version)
            return .upToDate
        }
        let newVersion = try remote.commit(files: changed, deletes: deletes, ifMatch: meta.version)
        try writeSyncedVersion(newVersion)
        return .pushed(version: newVersion)
    }

    private func reconcile(to meta: RemoteMeta) throws {
        let local = try store.remoteFiles()
        for (path, remoteHash) in meta.files where hex(digest(local[path] ?? Data())) != remoteHash {
            let data = try remote.fetch(path)
            guard hex(digest(data)) == remoteHash else {
                throw RemoteStoreError.malformedResponse("hash mismatch for \(path)")
            }
            let destination = store.url(for: path)
            try FileManager.default.createDirectory(
                at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: destination, options: .atomic)
        }
        for path in local.keys where meta.files[path] == nil {
            try? FileManager.default.removeItem(at: store.url(for: path))
        }
    }

    private var syncedVersion: Int {
        guard let text = try? String(contentsOf: versionURL, encoding: .utf8),
              let value = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            return -1
        }
        return value
    }

    private func writeSyncedVersion(_ version: Int) throws {
        try FileManager.default.createDirectory(
            at: versionURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try String(version).write(to: versionURL, atomically: true, encoding: .utf8)
    }
}
