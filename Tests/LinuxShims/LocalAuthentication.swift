// Linux stand-in for LocalAuthentication. Records every prompt so a test can
// assert that the biometric gate was actually reached - the check that was
// missing when a binary shipped reading every secret without one.
import Foundation

public typealias NSErrorPointer = UnsafeMutablePointer<NSError?>?

// Present only to occupy the SDK's names so a collision surfaces here.
public enum LABiometryType { case none, touchID, faceID }
public enum LACredentialType { case applicationPassword }
public enum LAError: Error { case authenticationFailed }

public enum LAPolicy {
    case deviceOwnerAuthenticationWithBiometrics
}

public enum FakePrompts {
    public static var path: String {
        ProcessInfo.processInfo.environment["KEYGUARD_FAKE_PROMPTS"] ?? "/tmp/keyguard-fake-prompts.log"
    }

    public static var denies: Bool {
        ProcessInfo.processInfo.environment["KEYGUARD_FAKE_DENY"] == "1"
    }

    static func record(_ reason: String) {
        let handle = FileHandle(forWritingAtPath: path) ?? {
            FileManager.default.createFile(atPath: path, contents: nil)
            return FileHandle(forWritingAtPath: path)!
        }()
        handle.seekToEndOfFile()
        handle.write(Data((reason + "\n").utf8))
        handle.closeFile()
    }
}

public final class LAContext {
    public init() {}

    public func canEvaluatePolicy(_ policy: LAPolicy, error: NSErrorPointer) -> Bool { true }

    public func evaluatePolicy(_ policy: LAPolicy,
                               localizedReason: String,
                               reply: @escaping (Bool, Error?) -> Void) {
        FakePrompts.record(localizedReason)
        reply(!FakePrompts.denies, FakePrompts.denies ? NSError(domain: "fake", code: 1) : nil)
    }
}
