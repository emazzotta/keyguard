import Foundation
import KeyguardCore

func commandGet(_ arguments: [String]) {
    let parsed = parseArgs(arguments)
    let keys = parsed.positional
    guard !keys.isEmpty else {
        fail("Usage: keyguard get <KEY> [KEY...] [--cache-duration N]")
    }

    let reason = buildReason(base: "Reveal \(keys.joined(separator: ", "))",
                             cacheDuration: parsed.cacheDuration)

    let session = attempt { try Session.make() }
    let unlocked = attempt { try session.unlock(reason: reason) }
    let values = attempt {
        try session.store.values(of: keys, index: unlocked.index, identity: unlocked.identity)
    }

    if keys.count == 1 {
        print(values[keys[0]]!, terminator: "")
    } else {
        keys.forEach { emit(name: $0, value: values[$0]!) }
    }
}

func commandSet(name: String, value: String) {
    let session = attempt { try Session.make() }
    let unlocked = attempt { try session.unlock(reason: "Save \(name)", requireService: true) }

    if unlocked.index.entries[name] != nil, isInteractive, !confirmOverwrite(name) {
        fail("Cancelled")
    }
    _ = attempt { try session.store.put(name: name, value: value, tier: .high, index: unlocked.index) }
    attempt { try session.pushAfterWrite() }
    print("Set '\(name)'")
}

func commandDelete(name: String) {
    let session = attempt { try Session.make() }
    let unlocked = attempt { try session.unlock(reason: "Delete \(name)", requireService: true) }
    _ = attempt { try session.store.remove(name: name, index: unlocked.index) }
    attempt { try session.pushAfterWrite() }
    print("Deleted '\(name)'")
}

func commandRename(from old: String, to new: String, force: Bool) {
    guard old != new else { fail("Source and destination must differ") }

    let session = attempt { try Session.make() }
    let unlocked = attempt { try session.unlock(reason: "Rename \(old) to \(new)", requireService: true) }

    guard let entry = unlocked.index.entries[old] else { fail("Key '\(old)' not found") }
    if unlocked.index.entries[new] != nil, !force {
        fail("Key '\(new)' already exists; use --force to overwrite")
    }

    let value = attempt {
        try session.store.values(of: [old], index: unlocked.index, identity: unlocked.identity)
    }[old]!
    let afterPut = attempt {
        try session.store.put(name: new, value: value, tier: entry.tier, index: unlocked.index)
    }
    _ = attempt { try session.store.remove(name: old, index: afterPut) }
    attempt { try session.pushAfterWrite() }
    print("Renamed '\(old)' to '\(new)'")
}

func parseRecipientCommand(_ arguments: [String], command: String) -> (recipient: String, tiers: [Tier]) {
    var tierArg = "high,low"
    var positional: [String] = []
    var cursor = 0
    while cursor < arguments.count {
        if arguments[cursor] == "--tier", cursor + 1 < arguments.count {
            tierArg = arguments[cursor + 1]
            cursor += 2
        } else {
            positional.append(arguments[cursor])
            cursor += 1
        }
    }
    guard positional.count == 1 else {
        fail("Usage: keyguard \(command) <age1-recipient> [--tier high,low]")
    }
    let tiers = tierArg.split(separator: ",").map { Tier(rawValue: String($0)) }
    guard !tiers.contains(nil) else { fail("Invalid --tier: use high, low, or high,low") }
    guard positional[0].hasPrefix("age1") else { fail("Invalid recipient: expected an age1... public key") }
    return (positional[0], tiers.compactMap { $0 })
}

private func tierLabel(_ tiers: [Tier]) -> String {
    tiers.map(\.rawValue).sorted().joined(separator: ", ")
}

private func applyRecipients(_ updated: RecipientSet, session: Session, identity: String) {
    _ = attempt { try session.store.reseal(to: updated, identity: identity) }
    attempt { try writePinnedRecipients(updated, to: session.recipientsFile) }
    attempt { try session.pushAfterWrite() }
}

func commandAddRecipient(recipient: String, tiers: [Tier]) {
    let session = attempt { try Session.make() }
    let unlocked = attempt { try session.unlock(reason: "Add a store recipient", requireService: true) }

    let current = unlocked.index.recipients
    guard !tiers.allSatisfy({ current.recipients(for: $0).contains(recipient) }) else {
        fail("That recipient is already in tier(s) \(tierLabel(tiers))")
    }

    let updated = current.adding(recipient, to: tiers)
    applyRecipients(updated, session: session, identity: unlocked.identity)
    print("Added the recipient to tier(s) \(tierLabel(tiers)); recipient set is now version \(updated.version)")
    print("Update the pinned recipient set on every other device that reads this store.")
}

func commandRemoveRecipient(recipient: String, tiers: [Tier]) {
    let session = attempt { try Session.make() }
    let unlocked = attempt { try session.unlock(reason: "Remove a store recipient", requireService: true) }
    let owner = attempt { try session.runner.recipient(forIdentity: unlocked.identity, keygenBinary: session.keygenBinary) }
    guard recipient != owner else {
        fail("That is this Mac's own key; removing it would lock keyguard out of its own store")
    }

    let current = unlocked.index.recipients
    guard tiers.contains(where: { current.recipients(for: $0).contains(recipient) }) else {
        fail("That recipient is not in tier(s) \(tierLabel(tiers))")
    }

    let updated = current.removing(recipient, from: tiers)
    applyRecipients(updated, session: session, identity: unlocked.identity)
    print("Removed the recipient from tier(s) \(tierLabel(tiers)); recipient set is now version \(updated.version)")
    print("Secrets it could read before may survive in copies it made; rotate the ones that matter.")
    print("Update the pinned recipient set on every other device that reads this store.")
}

func commandList(_ arguments: [String]) {
    let parsed = parseArgs(arguments)
    let session = attempt { try Session.make() }
    let unlocked = attempt {
        try session.unlock(reason: buildReason(base: "List secrets", cacheDuration: parsed.cacheDuration))
    }
    unlocked.index.entries.keys.sorted().forEach { print($0) }
}

func commandExport() {
    let session = attempt { try Session.make() }
    let unlocked = attempt { try session.unlock(reason: "Export all secrets") }
    let names = unlocked.index.entries.keys.sorted()
    let values = attempt {
        try session.store.values(of: names, index: unlocked.index, identity: unlocked.identity)
    }
    print(serializeEnv(values), terminator: "")
}

func commandImport(path: String, force: Bool) {
    let url = URL(fileURLWithPath: path)
    guard let contents = try? String(contentsOf: url, encoding: .utf8) else {
        fail("Cannot read file: \(path)")
    }
    let incoming = parseEnv(contents)
    guard !incoming.isEmpty else { fail("No KEY=VALUE pairs found in \(path)") }

    let session = attempt { try Session.make() }
    var index = attempt { try session.unlock(reason: "Import \(url.lastPathComponent)", requireService: true) }.index

    var added = 0, overwritten = 0, skipped = 0
    for name in incoming.keys.sorted() {
        if index.entries[name] != nil {
            let overwrite = force || (isInteractive && confirmOverwrite(name))
            guard overwrite else {
                if !isInteractive {
                    fputs("  Skipped \(name) (already exists, use --force to overwrite)\n", stderr)
                }
                skipped += 1
                continue
            }
            overwritten += 1
        } else {
            added += 1
        }
        index = attempt { try session.store.put(name: name, value: incoming[name]!, tier: .high, index: index) }
    }
    attempt { try session.pushAfterWrite() }

    var summary = "Imported from \(url.lastPathComponent): \(added) added"
    if overwritten > 0 { summary += ", \(overwritten) overwritten" }
    if skipped > 0 { summary += ", \(skipped) skipped" }
    print(summary)
    print("You can now delete the plaintext file: rm \(url.path)")
}

func commandVerify() {
    let session = attempt { try Session.make() }
    guard session.store.exists else { fail(session.notInitialisedMessage) }

    let report = attempt { try session.store.integrity() }
    guard !report.isClean else {
        print("Store at \(session.store.root.path) is intact")
        return
    }
    report.missing.forEach { fputs("missing:    \($0)\n", stderr) }
    report.modified.forEach { fputs("modified:   \($0)\n", stderr) }
    report.unexpected.forEach { fputs("unexpected: \($0)\n", stderr) }
    fail("Store at \(session.store.root.path) does not match its manifest")
}

func establishIdentity(_ session: Session) -> AgeIdentity {
    if let secret = Keychain.loadIdentity() {
        let recipient = attempt {
            try session.runner.recipient(forIdentity: secret, keygenBinary: session.keygenBinary)
        }
        return AgeIdentity(secret: secret, recipient: recipient)
    }
    let generated = attempt { try session.runner.keygen(binary: session.keygenBinary) }
    attempt { try Keychain.store(identity: generated.secret) }
    return generated
}

func establishStore(_ session: Session, identity: AgeIdentity) {
    let recipients = RecipientSet(version: 1, tiers: ["high": [identity.recipient],
                                                     "low": [identity.recipient]])
    attempt { try writePinnedRecipients(recipients, to: session.recipientsFile) }
    attempt { try session.store.create(salt: randomSalt(), recipients: recipients, identity: identity.secret) }
}

func commandInit() {
    let session = attempt { try Session.make() }
    guard !session.store.exists else { fail("A store already exists at \(session.store.root.path)") }
    if LegacyStore.exists(at: session.legacyFile) {
        fail("""
        A pre-age secrets file exists at \(session.legacyFile.path). Run 'keyguard migrate' instead,
        so its contents come with you.
        """)
    }
    authenticate(reason: "Create a keyguard store")
    establishStore(session, identity: establishIdentity(session))
    print("Created a store at \(session.store.root.path)")
    print("Pinned the recipient set at \(session.recipientsFile.path)")
    seedService(session)
}

func commandMigrate(force: Bool) {
    let session = attempt { try Session.make() }

    if session.store.exists, !force {
        fail("A store already exists at \(session.store.root.path). Use --force to replace it.")
    }
    guard LegacyStore.exists(at: session.legacyFile) else {
        fail("No pre-age secrets file at \(session.legacyFile.path) - nothing to migrate.")
    }

    authenticate(reason: "Migrate secrets into the age store")
    let entries = attempt { try LegacyStore.read(at: session.legacyFile) }
    guard !entries.isEmpty else { fail("The secrets file at \(session.legacyFile.path) holds no entries.") }

    if session.store.exists {
        let aside = session.store.root.appendingPathExtension("replaced-\(Int(Date().timeIntervalSince1970))")
        attempt { try FileManager.default.moveItem(at: session.store.root, to: aside) }
        fputs("Moved the previous store to \(aside.path)\n", stderr)
    }

    let identity = establishIdentity(session)
    establishStore(session, identity: identity)

    var index = attempt { try session.store.loadIndex(identity: identity.secret) }
    for name in entries.keys.sorted() {
        index = attempt { try session.store.put(name: name, value: entries[name]!, tier: .high, index: index) }
    }

    let reloaded = attempt { try session.store.loadIndex(identity: identity.secret) }
    let readBack = attempt {
        try session.store.values(of: Array(entries.keys), index: reloaded, identity: identity.secret)
    }
    let report = attempt { try session.store.integrity() }

    guard readBack == entries, report.isClean else {
        let quarantine = session.store.root.appendingPathExtension("failed-\(Int(Date().timeIntervalSince1970))")
        try? FileManager.default.moveItem(at: session.store.root, to: quarantine)
        fail("""
        Verification failed - the new store did not read back identically. It has been moved to
        \(quarantine.path) and nothing else was touched. The secrets file at \(session.legacyFile.path)
        and its Keychain key are exactly as they were.
        """)
    }

    print("Migrated \(entries.count) secrets to \(session.store.root.path)")
    print("Verified: every value read back identically and the manifest matches.")
    seedService(session)
    print("")
    print("The old file and its Keychain key are untouched, so reinstalling the previous keyguard")
    print("is a complete rollback. Delete this only once you are satisfied:")
    print("  \(session.legacyFile.path)")
}

func commandExportKey() {
    authenticate(reason: "Export the store identity")
    guard let identity = Keychain.loadIdentity() else { fail("No age identity in the Keychain") }
    fputs("Warning: treat this like a master password - it decrypts every secret\n", stderr)
    print(identity, terminator: "")
}

func commandImportKey(_ identity: String) {
    guard identity.hasPrefix("AGE-SECRET-KEY-1") else {
        if Data(base64Encoded: identity)?.count == 32 {
            fail("""
            That is a pre-age encryption key. This keyguard holds an age identity instead.
            Reinstall the previous keyguard to import it, then run 'keyguard migrate'.
            """)
        }
        fail("Invalid identity: expected a string starting with AGE-SECRET-KEY-1")
    }
    if Keychain.loadIdentity() != nil {
        fail("The Keychain already holds an age identity. Run 'keyguard clear' first to replace it.")
    }
    authenticate(reason: "Import a store identity")
    attempt { try Keychain.store(identity: identity) }
    print("Identity imported into the Keychain")
}

func commandClear() {
    let session = attempt { try Session.make() }
    authenticate(reason: "Delete the store and its identity")
    try? FileManager.default.removeItem(at: session.store.root)
    Keychain.deleteIdentity()
    print("Cleared the store at \(session.store.root.path) and its identity")
    if LegacyStore.exists(at: session.legacyFile) {
        print("Left the pre-age secrets file at \(session.legacyFile.path) alone")
    }
}

func commandPush() {
    let session = attempt { try Session.make() }
    guard let sync = session.sync else { fail("No store service configured. Set \(Locations.storeURLVariable).") }
    guard session.store.exists else { fail(session.notInitialisedMessage) }
    switch attempt({ try sync.push() }) {
    case .pushed(let version): print("Pushed to the store service; now at version \(version)")
    case .upToDate: print("The store service is already up to date")
    case .remoteEmpty, .pulled: break
    }
}

func commandPull() {
    let session = attempt { try Session.make() }
    guard let sync = session.sync else { fail("No store service configured. Set \(Locations.storeURLVariable).") }
    switch attempt({ try sync.pull() }) {
    case .pulled(let version): print("Pulled from the store service; local cache now at version \(version)")
    case .upToDate: print("Already up to date with the store service")
    case .remoteEmpty: print("The store service has no store yet. Run 'keyguard push' to seed it")
    case .pushed: break
    }
}

func seedService(_ session: Session) {
    guard let sync = session.sync else { return }
    do {
        if case .pushed(let version) = try sync.push() {
            print("Seeded the store service at version \(version)")
        }
    } catch {
        fputs("Could not reach the store service to seed it. Run 'keyguard push' when it is reachable.\n", stderr)
    }
}
