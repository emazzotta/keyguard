import Foundation
import LocalAuthentication

/// The gate in front of every identity load. It is an application-level check,
/// not an OS-enforced one - see the passkey-secrets design, stage 1b. Removing
/// it before the Secure Enclave identity exists leaves a binary that reads
/// every secret with no prompt at all, which is exactly what shipped once.
func authenticate(reason: String) {
    let context = LAContext()
    var error: NSError?

    guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) else {
        fputs("Biometrics unavailable: \(error?.localizedDescription ?? "unknown")\n", stderr)
        exit(1)
    }

    let semaphore = DispatchSemaphore(value: 0)
    var succeeded = false

    context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) { success, err in
        succeeded = success
        if !success, let err = err {
            fputs("Authentication failed: \(err.localizedDescription)\n", stderr)
        }
        semaphore.signal()
    }
    semaphore.wait()

    guard succeeded else { exit(2) }
}
