import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// Reads and writes the on-disk store. Foundation-only so the layout, the
/// atomic-rename discipline and the meta bookkeeping are exercised against
/// real files and a real `age` rather than reasoned about.
public struct Store {
    public let root: URL
    private let runner: AgeRunner
    private let digest: DigestFunction

    public init(root: URL, runner: AgeRunner, digest: @escaping DigestFunction) {
        self.root = root
        self.runner = runner
        self.digest = digest
    }

    public var indexURL: URL { root.appendingPathComponent(storeIndexFile) }
    public var metaURL: URL { root.appendingPathComponent(storeMetaFile) }

    public func url(for path: String) -> URL { root.appendingPathComponent(path) }

    public var exists: Bool {
        FileManager.default.fileExists(atPath: indexURL.path)
    }

    // MARK: - Reading

    public func loadIndex(identity: String) throws -> StoreIndex {
        let plaintext = try Padding.unpad(try runner.decrypt(at: indexURL.path, identity: identity))
        let index = try storeDecoder().decode(StoreIndex.self, from: plaintext)
        guard index.version == StoreIndex.currentVersion else {
            throw StoreError.unsupportedStoreVersion(index.version)
        }
        return index
    }

    /// Resolves every name before decrypting any of them, so a typo fails
    /// before a prompt rather than after several successful reads.
    public func values(of names: [String], index: StoreIndex, identity: String) throws -> [String: String] {
        let missing = names.filter { index.entries[$0] == nil }
        guard missing.isEmpty else { throw StoreError.unknownVariables(missing.sorted()) }

        var found: [String: String] = [:]
        for name in names {
            let entry = index.entries[name]!
            let plaintext = try Padding.unpad(try runner.decrypt(at: url(for: entry.file).path, identity: identity))
            let payload = try storeDecoder().decode(VariablePayload.self, from: plaintext)
            found[name] = try verify(payload: payload, expecting: name)
        }
        return found
    }

    public func loadMeta() throws -> StoreMeta {
        try storeDecoder().decode(StoreMeta.self, from: try Data(contentsOf: metaURL))
    }

    /// Hashes what is on disk and compares it with `meta.json`. Cheap because
    /// the files are small, and it is the only thing that notices a Drive
    /// conflict copy or a half-applied write.
    public func integrity() throws -> IntegrityReport {
        let meta = try loadMeta()
        var actual: [String: String] = [:]
        for path in try relativeFilePaths() where path != storeMetaFile {
            actual[path] = hex(digest(try Data(contentsOf: url(for: path))))
        }
        return checkIntegrity(meta: meta, actual: actual)
    }

    // MARK: - Writing

    public func create(salt: Data, recipients: RecipientSet, identity: String) throws {
        try FileManager.default.createDirectory(
            at: root.appendingPathComponent(storeVariableDirectory), withIntermediateDirectories: true)
        let index = StoreIndex(salt: salt, recipients: recipients, entries: [:])
        try writeIndex(index, recipients: recipients)
        try writeMeta(files: [storeIndexFile: try hashOf(indexURL)])
    }

    /// Returns the index the caller should keep; the one passed in is stale the
    /// moment this succeeds.
    public func put(name: String,
                    value: String,
                    tier: Tier,
                    index: StoreIndex) throws -> StoreIndex {
        let path = variableFile(salt: index.salt, name: name, digest: digest)
        let payload = Padding.pad(try storeEncoder().encode(VariablePayload(name: name, value: value)))
        try encrypt(payload, to: index.recipients.recipients(for: tier), at: url(for: path))

        var entries = index.entries
        entries[name] = IndexEntry(file: path, tier: tier)
        let updated = StoreIndex(salt: index.salt, recipients: index.recipients, entries: entries)
        try writeIndex(updated, recipients: index.recipients)

        var meta = (try? loadMeta().files) ?? [:]
        meta[path] = try hashOf(url(for: path))
        meta[storeIndexFile] = try hashOf(indexURL)
        try writeMeta(files: meta)
        return updated
    }

    public func remove(name: String, index: StoreIndex) throws -> StoreIndex {
        guard let entry = index.entries[name] else {
            throw StoreError.unknownVariables([name])
        }
        var entries = index.entries
        entries.removeValue(forKey: name)
        let updated = StoreIndex(salt: index.salt, recipients: index.recipients, entries: entries)

        try writeIndex(updated, recipients: index.recipients)
        try? FileManager.default.removeItem(at: url(for: entry.file))

        var meta = (try? loadMeta().files) ?? [:]
        meta.removeValue(forKey: entry.file)
        meta[storeIndexFile] = try hashOf(indexURL)
        try writeMeta(files: meta)
        return updated
    }

    // MARK: - Internals

    private func writeIndex(_ index: StoreIndex, recipients: RecipientSet) throws {
        let everyRecipient = Set(recipients.tiers.values.flatMap { $0 }).sorted()
        try encrypt(Padding.pad(try storeEncoder().encode(index)), to: everyRecipient, at: indexURL)
    }

    private func writeMeta(files: [String: String]) throws {
        try storeEncoder().encode(StoreMeta(files: files)).write(to: metaURL, options: .atomic)
    }

    private func hashOf(_ url: URL) throws -> String {
        hex(digest(try Data(contentsOf: url)))
    }

    /// age writes straight to its `-o` path, so it lands beside the target and
    /// is renamed into place: a crash mid-encrypt must not truncate the file
    /// that currently holds the only copy of a secret.
    private func encrypt(_ plaintext: Data, to recipients: [String], at destination: URL) throws {
        let staged = destination.appendingPathExtension("tmp")
        try FileManager.default.createDirectory(
            at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        try runner.encrypt(plaintext, to: recipients, at: staged.path)
        guard rename(staged.path, destination.path) == 0 else {
            try? FileManager.default.removeItem(at: staged)
            throw StoreError.writeFailed(destination.path)
        }
    }

    private func relativeFilePaths() throws -> [String] {
        let prefix = root.standardizedFileURL.path + "/"
        guard let walker = FileManager.default.enumerator(
            at: root, includingPropertiesForKeys: [.isRegularFileKey]) else { return [] }

        var paths: [String] = []
        for case let url as URL in walker {
            guard (try? url.resourceValues(forKeys: [.isRegularFileKey]))?.isRegularFile == true else { continue }
            let path = url.standardizedFileURL.path
            guard path.hasPrefix(prefix) else { continue }
            paths.append(String(path.dropFirst(prefix.count)))
        }
        return paths.sorted()
    }
}
