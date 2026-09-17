import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import KeyguardCore

enum TransportError: Error { case noResponse, badURL(String) }

struct URLSessionTransport: StoreTransport {
    let baseURL: URL
    let identity: String?
    let timeout: TimeInterval

    init(baseURL: URL, identity: String?, timeout: TimeInterval = 15) {
        self.baseURL = baseURL
        self.identity = identity
        self.timeout = timeout
    }

    func send(_ request: HTTPRequest) throws -> HTTPResponse {
        let base = baseURL.absoluteString.hasSuffix("/")
            ? String(baseURL.absoluteString.dropLast())
            : baseURL.absoluteString
        guard let url = URL(string: "\(base)/\(request.path)") else {
            throw TransportError.badURL(request.path)
        }
        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = request.method.rawValue
        urlRequest.httpBody = request.body
        urlRequest.timeoutInterval = timeout
        request.headers.forEach { urlRequest.setValue($1, forHTTPHeaderField: $0) }
        if let identity { urlRequest.setValue(identity, forHTTPHeaderField: "Tailscale-User-Login") }

        let semaphore = DispatchSemaphore(value: 0)
        var outcome: Result<HTTPResponse, Error> = .failure(TransportError.noResponse)
        URLSession.shared.dataTask(with: urlRequest) { data, response, error in
            defer { semaphore.signal() }
            if let error {
                outcome = .failure(error)
            } else if let http = response as? HTTPURLResponse {
                var headers: [String: String] = [:]
                for (key, value) in http.allHeaderFields {
                    if let key = key as? String, let value = value as? String {
                        headers[key.lowercased()] = value
                    }
                }
                outcome = .success(HTTPResponse(status: http.statusCode, headers: headers, body: data ?? Data()))
            }
        }.resume()
        semaphore.wait()
        return try outcome.get()
    }
}
