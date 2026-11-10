import Foundation

public typealias NSErrorPointer = UnsafeMutablePointer<NSError?>?

public enum LABiometryType { case none, touchID, faceID }
public enum LACredentialType { case applicationPassword }
/// Mirrors the LAError macOS bridges an evaluatePolicy failure into: its code tells the owner's cancel from the system's.
public struct LAError: Error {
    public enum Code { case authenticationFailed, userCancel, userFallback, systemCancel }

    public let code: Code

    public init(_ code: Code) { self.code = code }
}

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

    /// The prompt ends with no answer from the owner, as on a locked screen.
    public static var systemCancels: Bool {
        ProcessInfo.processInfo.environment["KEYGUARD_FAKE_SYSTEM_CANCEL"] == "1"
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
        if FakePrompts.systemCancels {
            reply(false, LAError(.systemCancel))
        } else {
            reply(!FakePrompts.denies, FakePrompts.denies ? LAError(.userCancel) : nil)
        }
    }
}
