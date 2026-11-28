import Foundation
import LocalAuthentication

/// How a prompt that did not succeed ends the process. keyguard-server reads these: the owner's own cancel is a no and
/// ends a read, while a prompt the system ended, as a locked screen does, sends the read to the phone instead.
enum PromptExit {
    static let ownerCancelled: Int32 = 2
    static let systemEnded: Int32 = 3
}

func authenticate(reason: String) {
    let context = LAContext()
    var error: NSError?

    guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) else {
        fputs("Biometrics unavailable: \(error?.localizedDescription ?? "unknown")\n", stderr)
        exit(1)
    }

    let semaphore = DispatchSemaphore(value: 0)
    var succeeded = false
    var failure: Error?

    context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) { success, err in
        succeeded = success
        failure = err
        if !success, let err = err {
            fputs("Authentication failed: \(err.localizedDescription)\n", stderr)
        }
        semaphore.signal()
    }
    semaphore.wait()

    guard succeeded else { exit(promptExit(for: failure)) }
}

/// Only an answer from the owner at the Mac is a no; anything else ended the prompt without one.
func promptExit(for error: Error?) -> Int32 {
    switch (error as? LAError)?.code {
    case .userCancel?, .userFallback?:
        return PromptExit.ownerCancelled
    default:
        return PromptExit.systemEnded
    }
}
