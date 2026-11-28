import Foundation

private final class RecordingTransport: StoreTransport {
    private(set) var requests: [HTTPRequest] = []
    private let handler: (HTTPRequest) throws -> HTTPResponse

    init(_ handler: @escaping (HTTPRequest) throws -> HTTPResponse) {
        self.handler = handler
    }

    func send(_ request: HTTPRequest) throws -> HTTPResponse {
        requests.append(request)
        return try handler(request)
    }
}

private struct Offline: Error {}

private func json(_ object: [String: Any]) -> Data {
    (try? JSONSerialization.data(withJSONObject: object)) ?? Data()
}

@main
struct RemoteStoreTestRunner {
    static func main() {
        var failures = 0

        func check(_ desc: String, _ passed: Bool, _ detail: @autoclosure () -> String = "") {
            if passed {
                print("  ✓ \(desc)")
            } else {
                print("  ✗ \(desc)\(detail().isEmpty ? "" : ": \(detail())")")
                failures += 1
            }
        }

        func checkEqual<T: Equatable>(_ desc: String, _ actual: T, _ expected: T) {
            check(desc, actual == expected, "got \(actual), want \(expected)")
        }

        print("meta")
        let metaTransport = RecordingTransport { _ in
            HTTPResponse(status: 200, body: json(["version": 7,
                                                  "files": ["index.age": "aa", "vars/bb.age": "cc"],
                                                  "meta_version": 1]))
        }
        let meta = try? RemoteStore(transport: metaTransport).meta()
        checkEqual("should parse the version", meta?.version, 7)
        checkEqual("should parse the file manifest", meta?.files, ["index.age": "aa", "vars/bb.age": "cc"])
        checkEqual("should GET the meta endpoint", metaTransport.requests.first?.path, "store/meta")

        check("should surface a forbidden meta as .forbidden",
              throwsRemote { try RemoteStore(transport: RecordingTransport { _ in
                  HTTPResponse(status: 403, body: json(["error": "no tailnet identity"]))
              }).meta() } == .forbidden)

        check("should reject a meta body it cannot decode",
              throwsRemote { try RemoteStore(transport: RecordingTransport { _ in
                  HTTPResponse(status: 200, body: Data("not json".utf8))
              }).meta() } == .malformedResponse("meta"))

        print("\ntransport failure")
        check("should turn a transport error into .unreachable",
              isUnreachable(throwsRemote {
                  try RemoteStore(transport: RecordingTransport { _ in throw Offline() }).meta()
              }))

        print("\nfetch")
        let fetchTransport = RecordingTransport { request in
            request.path == "store/vars/dead.age"
                ? HTTPResponse(status: 200, body: Data([0x01, 0x02, 0x03]))
                : HTTPResponse(status: 404, body: Data())
        }
        let store = RemoteStore(transport: fetchTransport)
        checkEqual("should return the file bytes", try? store.fetch("vars/dead.age"), Data([0x01, 0x02, 0x03]))
        check("should map a missing file to .notFound",
              throwsRemote { try store.fetch("vars/gone.age") } == .notFound("vars/gone.age"))

        print("\ncommit")
        let payload: [String: Data] = ["index.age": Data("INDEX".utf8), "vars/aa.age": Data("VAR".utf8)]
        let commitTransport = RecordingTransport { _ in
            HTTPResponse(status: 200, body: json(["version": 9]))
        }
        let newVersion = try? RemoteStore(transport: commitTransport)
            .commit(files: payload, deletes: ["vars/old.age"], ifMatch: 8)
        checkEqual("should return the new version", newVersion, 9)

        let sent = commitTransport.requests.first
        checkEqual("should POST the commit endpoint", sent?.path, "store/commit")
        checkEqual("should send the base-version as If-Match", sent?.headers["If-Match"], "8")
        let body = (try? JSONSerialization.jsonObject(with: sent?.body ?? Data())) as? [String: Any]
        let files = body?["files"] as? [String: String]
        checkEqual("should base64-encode each file", files?["index.age"], Data("INDEX".utf8).base64EncodedString())
        checkEqual("should carry deletes under the 'delete' key", body?["delete"] as? [String], ["vars/old.age"])

        check("should read the current version out of a 412 conflict",
              throwsRemote { try RemoteStore(transport: RecordingTransport { _ in
                  HTTPResponse(status: 412, body: json(["error": "version is 12"]))
              }).commit(files: [:], deletes: [], ifMatch: 5) } == .conflict(current: 12))

        if failures > 0 {
            fputs("\n\(failures) failure(s)\n", stderr)
            exit(1)
        }
        print("\nAll tests passed")
    }

    private static func throwsRemote(_ body: () throws -> Any) -> RemoteStoreError? {
        do { _ = try body(); return nil } catch let error as RemoteStoreError { return error } catch { return nil }
    }

    private static func isUnreachable(_ error: RemoteStoreError?) -> Bool {
        if case .unreachable = error { return true }
        return false
    }
}
