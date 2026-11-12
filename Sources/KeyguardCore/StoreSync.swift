import Foundation

public enum SyncOutcome: Equatable, Sendable {
    case upToDate
    case remoteEmpty
    case pulled(version: Int)
    case pushed(version: Int)
    case localAhead(unpushed: [String])
}

public struct StoreDivergence: Error, Equatable {
    public let unpushed: [String]
    public let serviceVersion: Int
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

    public func pull(discardingLocalChanges: Bool = false) throws -> SyncOutcome {
        let meta = try remote.meta()
        if meta.version == 0 { return .remoteEmpty }

        let unpushed = try discardingLocalChanges ? [] : unpushedPaths(service: meta)
        if meta.version == syncedVersion, !discardingLocalChanges {
            try remember(meta)
            return unpushed.isEmpty ? .upToDate : .localAhead(unpushed: unpushed)
        }
        guard unpushed.isEmpty else {
            throw StoreDivergence(unpushed: unpushed, serviceVersion: meta.version)
        }
        try reconcile(to: meta)
        try store.rebuildMeta()
        try remember(meta)
        return .pulled(version: meta.version)
    }

    public func push() throws -> SyncOutcome {
        let meta = try remote.meta()
        let base = max(syncedVersion, 0)
        guard meta.version == base else { throw RemoteStoreError.conflict(current: meta.version) }

        let local = try store.remoteFiles()
        let hashes = local.mapValues { hex(digest($0)) }
        let changed = local.filter { meta.files[$0.key] != hashes[$0.key] }
        let deletes = meta.files.keys.filter { local[$0] == nil }.sorted()

        if changed.isEmpty, deletes.isEmpty {
            try remember(meta)
            return .upToDate
        }
        let newVersion = try remote.commit(files: changed, deletes: deletes, ifMatch: meta.version)
        try remember(RemoteMeta(version: newVersion, files: hashes))
        return .pushed(version: newVersion)
    }

    private func unpushedPaths(service meta: RemoteMeta) throws -> [String] {
        let synced = meta.version == syncedVersion ? meta.files : syncedFiles ?? meta.files
        return try store.remoteFiles()
            .filter { isWrittenByKeyguard($0.key) && synced[$0.key] != hex(digest($0.value)) }
            .keys.sorted()
    }

    private func isWrittenByKeyguard(_ path: String) -> Bool {
        path == storeIndexFile || (path.hasPrefix("\(storeVariableDirectory)/") && path.hasSuffix(".age"))
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

    private var syncedFilesURL: URL { versionURL.appendingPathExtension("files") }

    private var syncedFiles: [String: String]? {
        guard let data = try? Data(contentsOf: syncedFilesURL),
              let state = try? JSONDecoder().decode(SyncedState.self, from: data),
              state.version == syncedVersion else {
            return nil
        }
        return state.files
    }

    private func remember(_ meta: RemoteMeta) throws {
        if meta.version == syncedVersion, meta.files == syncedFiles { return }
        try FileManager.default.createDirectory(
            at: versionURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(SyncedState(version: meta.version, files: meta.files))
            .write(to: syncedFilesURL, options: .atomic)
        try String(meta.version).write(to: versionURL, atomically: true, encoding: .utf8)
    }
}

private struct SyncedState: Codable {
    let version: Int
    let files: [String: String]
}
