import XCTest

@testable import ACEWorkerMenu

final class KeychainTests: XCTestCase {
    func testKeychainCreateReadAndDelete() throws {
        let service = "io.evren.audioventura.ace-node.test-" + UUID().uuidString
        let store = KeychainStore(service: service)
        defer { try? store.delete(account: "test-token") }

        XCTAssertNil(try store.read(account: "test-token"))
        try store.write("secret-value", account: "test-token")
        XCTAssertEqual(try store.read(account: "test-token"), "secret-value")
        try store.delete(account: "test-token")
        XCTAssertNil(try store.read(account: "test-token"))
    }
}
