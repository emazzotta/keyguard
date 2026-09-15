import Foundation
import KeyguardCore

/// Turns the typed errors from KeyguardCore into something a person can act
/// on. Every message says what was *not* done, because a half-applied write
/// to a secret store is the thing a reader needs ruled out first.
func fail(_ message: String) -> Never {
    fputs(message.hasSuffix("\n") ? message : message + "\n", stderr)
    exit(1)
}

func attempt<T>(_ body: () throws -> T) -> T {
    do {
        return try body()
    } catch let error as KeyguardError {
        fail(error.text)
    } catch let error as StoreError {
        fail(describe(error))
    } catch let error as AgeError {
        fail(describe(error))
    } catch {
        fail("\(error)")
    }
}

func describe(_ error: StoreError) -> String {
    switch error {
    case .recipientSetDivergence:
        return """
        The recipient set inside the store does not match the pinned copy on this machine.
        Nothing was read or written. One of the two has changed - compare them before going
        further, and do not resolve it by copying one over the other.
        """
    case .recipientSetRollback(let pinned, let canonical):
        return """
        The store advertises recipient set version \(canonical), older than the pinned \(pinned).
        Refusing to move backwards.
        """
    case .payloadNameMismatch(let expected, let found):
        return """
        The file holding '\(expected)' decrypted to '\(found)'. The store has been tampered with,
        or a write went wrong. Run 'keyguard verify'.
        """
    case .corruptPadding:
        return "A secret decrypted to a malformed payload. Run 'keyguard verify'."
    case .unsupportedStoreVersion(let version):
        return "The store is version \(version); this keyguard understands \(StoreIndex.currentVersion)."
    case .unknownVariables(let names):
        return "Keys not found: \(names.joined(separator: ", "))"
    case .writeFailed(let path):
        return "Failed to write \(path)"
    }
}

func describe(_ error: AgeError) -> String {
    switch error {
    case .binaryNotFound(let searched):
        return "Cannot find the age binary (looked at \(searched)). Install it with 'brew install age'."
    case .failed(_, let status, let message):
        return "age exited \(status)\(message.isEmpty ? "" : ": \(message)")"
    case .malformedKeygenOutput:
        return "age-keygen produced output keyguard could not read."
    }
}
