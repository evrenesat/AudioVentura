import Foundation

public struct HTTPResponse: Sendable {
    public let statusCode: Int
    public let body: Data

    public init(statusCode: Int, body: Data) {
        self.statusCode = statusCode
        self.body = body
    }
}

public protocol HTTPRequesting: AnyObject, Sendable {
    func request(_ request: URLRequest) async throws -> HTTPResponse
}

public enum WorkerClientError: Error, Equatable, LocalizedError, Sendable {
    case invalidEndpoint
    case unauthorized
    case unexpectedStatus(Int)
    case responseTooLarge
    case invalidResponse
    case transport

    public var errorDescription: String? {
        switch self {
        case .invalidEndpoint:
            "worker_endpoint_invalid"
        case .unauthorized:
            "worker_unauthorized"
        case .unexpectedStatus:
            "worker_unexpected_status"
        case .responseTooLarge:
            "worker_response_too_large"
        case .invalidResponse:
            "worker_response_invalid"
        case .transport:
            "worker_transport_failed"
        }
    }
}

public final class URLSessionRequester: HTTPRequesting, @unchecked Sendable {
    private let session: URLSession
    private let responseLimit: Int

    public init(responseLimit: Int = 65_536) {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 10
        configuration.timeoutIntervalForResource = 15
        configuration.waitsForConnectivity = false
        self.session = URLSession(configuration: configuration)
        self.responseLimit = responseLimit
    }

    public func request(_ request: URLRequest) async throws -> HTTPResponse {
        do {
            let (data, response) = try await session.data(for: request)
            guard data.count <= responseLimit else {
                throw WorkerClientError.responseTooLarge
            }
            guard let http = response as? HTTPURLResponse else {
                throw WorkerClientError.invalidResponse
            }
            return HTTPResponse(statusCode: http.statusCode, body: data)
        } catch let error as WorkerClientError {
            throw error
        } catch {
            throw WorkerClientError.transport
        }
    }
}

@MainActor
public protocol WorkerClienting: AnyObject {
    func health() async throws -> WorkerHealth
    func drain() async throws -> DrainResponse
}

@MainActor
public final class WorkerClient: WorkerClienting {
    public static let responseLimit = 65_536

    public let endpoint: URL
    private let token: String
    private let supervisorToken: String?
    private let requester: HTTPRequesting
    private let decoder = JSONDecoder()

    public init(
        endpoint: URL,
        token: String,
        supervisorToken: String? = nil,
        requester: HTTPRequesting = URLSessionRequester()
    ) throws {
        guard endpoint.scheme == "http" || endpoint.scheme == "https", endpoint.host != nil,
            endpoint.user == nil, endpoint.password == nil, endpoint.query == nil,
            endpoint.fragment == nil
        else {
            throw WorkerClientError.invalidEndpoint
        }
        self.endpoint = endpoint
        self.token = token
        self.supervisorToken = supervisorToken
        self.requester = requester
    }

    public func health() async throws -> WorkerHealth {
        let response = try await request(path: "healthz", method: "GET")
        guard response.statusCode == 200 else {
            throw error(for: response.statusCode)
        }
        guard !JSONDuplicateKeyDetector.containsDuplicateTopLevelKey(response.body) else {
            throw WorkerModelError.healthContractMismatch
        }
        do {
            return try decoder.decode(WorkerHealth.self, from: response.body)
        } catch let error as WorkerModelError {
            throw error
        } catch {
            throw WorkerClientError.invalidResponse
        }
    }

    public func drain() async throws -> DrainResponse {
        let response = try await request(
            path: "v1/supervisor/drain",
            method: "POST",
            bearerToken: supervisorToken ?? token
        )
        guard response.statusCode == 200 else {
            throw error(for: response.statusCode)
        }
        guard !JSONDuplicateKeyDetector.containsDuplicateTopLevelKey(response.body) else {
            throw WorkerClientError.invalidResponse
        }
        do {
            return try decoder.decode(DrainResponse.self, from: response.body)
        } catch {
            throw WorkerClientError.invalidResponse
        }
    }

    private func request(
        path: String,
        method: String,
        bearerToken: String? = nil
    ) async throws -> HTTPResponse {
        var request = URLRequest(url: endpoint.appendingPathComponent(path))
        request.httpMethod = method
        request.setValue("Bearer \(bearerToken ?? token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 10
        if method == "POST" {
            request.httpBody = Data()
        }
        let response = try await requester.request(request)
        guard response.body.count <= Self.responseLimit else {
            throw WorkerClientError.responseTooLarge
        }
        return response
    }

    private func error(for status: Int) -> Error {
        switch status {
        case 401:
            WorkerClientError.unauthorized
        default:
            WorkerClientError.unexpectedStatus(status)
        }
    }
}
