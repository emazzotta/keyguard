import Foundation

public enum RebaseRefusal: Error, Equatable {
    case noSyncRecord
    case recipientsChanged
    case changedOnBothSides([String])
}

public struct RebasePlan: Equatable, Sendable {
    public let entries: [String: IndexEntry]
    public let fetch: [String]
    public let remove: [String]
    public let conflicts: [String]
}

public func planRebase(base: [String: String],
                       local: [String: String],
                       service: [String: String],
                       localEntries: [String: IndexEntry],
                       serviceEntries: [String: IndexEntry]) -> RebasePlan {
    let localNames = namesByFile(localEntries)
    let serviceNames = namesByFile(serviceEntries)
    var entries = serviceEntries
    var fetch: [String] = []
    var remove: [String] = []
    var conflicts: [String] = []

    for path in Set(local.keys).union(service.keys).sorted()
        where path != storeIndexFile && local[path] != service[path] {
        let editedHere = local[path] != nil && local[path] != base[path] && localNames[path] != nil
        let deletedHere = local[path] == nil && base[path] != nil
            && serviceNames[path].map { localEntries[$0] == nil } == true
        let changedThere = service[path] != base[path]

        if editedHere, changedThere, service[path] != nil, let name = localNames[path] {
            conflicts.append(name)
        } else if editedHere, let name = localNames[path] {
            entries[name] = localEntries[name]
        } else if deletedHere, !changedThere, let name = serviceNames[path] {
            entries.removeValue(forKey: name)
        } else if service[path] == nil {
            remove.append(path)
        } else {
            fetch.append(path)
        }
    }
    return RebasePlan(entries: entries, fetch: fetch, remove: remove, conflicts: conflicts.sorted())
}

private func namesByFile(_ entries: [String: IndexEntry]) -> [String: String] {
    Dictionary(entries.map { ($0.value.file, $0.key) }, uniquingKeysWith: { first, _ in first })
}
