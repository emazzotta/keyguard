import Foundation
import LocalAuthentication

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
