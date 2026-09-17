import CryptoKit
import Foundation
import KeyguardCore

struct Session {
    let store: Store
    let recipientsFile: URL
    let legacyFile: URL
    let runner: AgeRunner
    let keygenBinary: String
    let sync: StoreSync?

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
        let store = Store(root: Locations.storeRoot(environment: environment, home: home),
                          runner: runner, digest: sha256)

        var sync: StoreSync?
        if let url = Locations.storeURL(environment: environment) {
            let transport = URLSessionTransport(baseURL: url, identity: environment[Locations.identityVariable])
            sync = StoreSync(store: store,
                             remote: RemoteStore(transport: transport),
                             digest: sha256,
                             versionURL: Locations.remoteVersionFile(environment: environment, home: home))
        }

        return Session(
            store: store,
            recipientsFile: Locations.recipientsFile(environment: environment, home: home),
            legacyFile: Locations.legacySecretsFile(environment: environment, home: home),
            runner: runner,
            keygenBinary: keygen,
            sync: sync
        )
    }

    func unlock(reason: String, requireService: Bool = false) throws -> (identity: String, index: StoreIndex) {
        try refresh(requireService: requireService)
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

    func pushAfterWrite() throws {
        guard let sync else { return }
        do {
            _ = try sync.push()
        } catch RemoteStoreError.conflict(let current) {
            _ = try? sync.pull()
            throw KeyguardError.message("""
            The store changed on the service (now version \(current)) while you were writing, so the \
            change was not applied. The local cache has been restored; re-run the command.
            """)
        } catch let error as RemoteStoreError {
            throw KeyguardError.message("Could not save to the store service (\(error)). Re-run when it is reachable.")
        }
    }

    private func refresh(requireService: Bool) throws {
        guard let sync else { return }
        do {
            if case .remoteEmpty = try sync.pull() {
                fputs("The store service has no store yet; run 'keyguard push' to seed it.\n", stderr)
            }
        } catch let error as RemoteStoreError {
            guard case .unreachable = error else {
                throw KeyguardError.message("Store service error: \(error)")
            }
            if requireService {
                throw KeyguardError.message(
                    "The store service is unreachable, so nothing was written. Try again on the tailnet.")
            }
            fputs("Store service unreachable; reading the local cache.\n", stderr)
        }
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
