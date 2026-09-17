import Foundation

public struct HTTPRequest: Equatable, Sendable {
    public enum Method: String, Sendable { case get = "GET", post = "POST" }

    public let method: Method
    public let path: String
    public let headers: [String: String]
    public let body: Data?

    public init(method: Method, path: String, headers: [String: String] = [:], body: Data? = nil) {
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body
    }
}

public struct HTTPResponse: Equatable, Sendable {
    public let status: Int
    public let headers: [String: String]
    public let body: Data

    public init(status: Int, headers: [String: String] = [:], body: Data = Data()) {
        self.status = status
        self.headers = headers
        self.body = body
    }
}

public protocol StoreTransport {
    func send(_ request: HTTPRequest) throws -> HTTPResponse
}

public struct RemoteMeta: Equatable, Sendable {
    public let version: Int
    public let files: [String: String]

    public init(version: Int, files: [String: String]) {
        self.version = version
        self.files = files
    }
}

public enum RemoteStoreError: Error, Equatable {
    case unreachable(String)
    case forbidden
    case conflict(current: Int)
    case notFound(String)
    case badResponse(status: Int)
    case malformedResponse(String)
}

public struct RemoteStore {
    private let transport: StoreTransport

    public init(transport: StoreTransport) {
        self.transport = transport
    }

    public func meta() throws -> RemoteMeta {
        let response = try perform(HTTPRequest(method: .get, path: "store/meta"))
        switch response.status {
        case 200:
            let decoded = try decode(MetaResponse.self, from: response.body, context: "meta")
            return RemoteMeta(version: decoded.version, files: decoded.files)
        case 403: throw RemoteStoreError.forbidden
        default: throw RemoteStoreError.badResponse(status: response.status)
        }
    }

    public func fetch(_ path: String) throws -> Data {
        let response = try perform(HTTPRequest(method: .get, path: "store/\(path)"))
        switch response.status {
        case 200: return response.body
        case 403: throw RemoteStoreError.forbidden
        case 404: throw RemoteStoreError.notFound(path)
        default: throw RemoteStoreError.badResponse(status: response.status)
        }
    }

    public func commit(files: [String: Data], deletes: [String], ifMatch: Int) throws -> Int {
        let body = CommitBody(files: files.mapValues { $0.base64EncodedString() }, delete: deletes.sorted())
        let response = try perform(HTTPRequest(
            method: .post,
            path: "store/commit",
            headers: ["If-Match": String(ifMatch), "Content-Type": "application/json"],
            body: try JSONEncoder().encode(body)))

        switch response.status {
        case 200: return try decode(VersionResponse.self, from: response.body, context: "commit").version
        case 403: throw RemoteStoreError.forbidden
        case 412: throw RemoteStoreError.conflict(current: currentVersion(inConflict: response.body) ?? -1)
        default: throw RemoteStoreError.badResponse(status: response.status)
        }
    }

    private func perform(_ request: HTTPRequest) throws -> HTTPResponse {
        do {
            return try transport.send(request)
        } catch let error as RemoteStoreError {
            throw error
        } catch {
            throw RemoteStoreError.unreachable(String(describing: error))
        }
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data, context: String) throws -> T {
        guard let value = try? JSONDecoder().decode(type, from: data) else {
            throw RemoteStoreError.malformedResponse(context)
        }
        return value
    }

    private func currentVersion(inConflict body: Data) -> Int? {
        guard let decoded = try? JSONDecoder().decode(ErrorResponse.self, from: body) else { return nil }
        return decoded.error.split(separator: " ").last.flatMap { Int($0) }
    }
}

private struct MetaResponse: Decodable {
    let version: Int
    let files: [String: String]
}

private struct VersionResponse: Decodable {
    let version: Int
}

private struct ErrorResponse: Decodable {
    let error: String
}

private struct CommitBody: Encodable {
    let files: [String: String]
    let delete: [String]
}
