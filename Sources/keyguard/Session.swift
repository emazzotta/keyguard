import CryptoKit
import Foundation
import KeyguardCore

/// Resolves where everything lives and opens the store. One instance per
/// invocation, and `unlock` prompts exactly once however many variables the
/// command goes on to read - `source envify A B C D` must stay one prompt.
struct Session {
    let store: Store
    let recipientsFile: URL
    let legacyFile: URL
    let runner: AgeRunner
    let keygenBinary: String

    static let sha256: DigestFunction = { Data(SHA256.hash(data: $0)) }

    static func make() throws -> Session {
        let environment = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let override = environment[AgeCommand.binaryOverrideVariable]

        let age: String
        let keygen: String
        do {
            age = try AgeRunner.locate(override: override)
            keygen = try AgeRunner.locate(candidates: AgeCommand.keygenSearchPaths)
        } catch {
            throw KeyguardError.message("""
            Cannot find the age binary. Install it with 'brew install age', or set \
            \(AgeCommand.binaryOverrideVariable) to its full path.
            """)
        }

        let runner = AgeRunner(binary: age)
        return Session(
            store: Store(root: Locations.storeRoot(environment: environment, home: home),
                         runner: runner,
                         digest: sha256),
            recipientsFile: Locations.recipientsFile(environment: environment, home: home),
            legacyFile: Locations.legacySecretsFile(environment: environment, home: home),
            runner: runner,
            keygenBinary: keygen
        )
    }

    /// The single Touch ID prompt for the whole invocation.
    func unlock(reason: String) throws -> (identity: String, index: StoreIndex) {
        guard store.exists else { throw KeyguardError.message(notInitialisedMessage) }

        authenticate(reason: reason)

        guard let identity = Keychain.loadIdentity() else {
            throw KeyguardError.message("""
            No age identity in the Keychain. The store at \(store.root.path) cannot be read.
            Restore one with 'keyguard import-key', or start over with 'keyguard clear'.
            """)
        }

        let index = try store.loadIndex(identity: identity)
        let pinned = try loadPinned()
        try validateRecipients(pinned: pinned,
                               canonical: index.recipients,
                               highestSeenVersion: pinned.version)
        return (identity, index)
    }

    func loadPinned() throws -> RecipientSet {
        do {
            return try loadPinnedRecipients(at: recipientsFile)
        } catch {
            throw KeyguardError.message("""
            Cannot read the pinned recipient set at \(recipientsFile.path). It is compared against \
            the copy inside the store on every operation, so keyguard will not run without it.
            """)
        }
    }

    var notInitialisedMessage: String {
        if LegacyStore.exists(at: legacyFile) {
            return """
            No store at \(store.root.path), but a pre-age secrets file exists at \(legacyFile.path).
            Run 'keyguard migrate' to convert it. The old file and its key are left untouched.
            """
        }
        return "No store at \(store.root.path). Run 'keyguard init' to create one."
    }
}
