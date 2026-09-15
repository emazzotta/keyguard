// Linux stand-in for the slice of CryptoKit keyguard uses. SHA256 is real
// enough to be a stable content hash; AES.GCM is a toy that only has to
// round-trip, because the only thing that still reads it is the migration
// path being tested.
import Foundation

public struct SymmetricKey: Equatable {
    public let bytes: Data
    public init(data: Data) { self.bytes = data }
    public init<D: DataProtocol>(data: D) { self.bytes = Data(data) }
    public func withUnsafeBytes<R>(_ body: (UnsafeRawBufferPointer) throws -> R) rethrows -> R {
        try bytes.withUnsafeBytes(body)
    }
}

public struct SHA256Digest: Sequence {
    let bytes: [UInt8]
    public func makeIterator() -> Array<UInt8>.Iterator { bytes.makeIterator() }
}

public enum SHA256 {
    /// FNV-1a over four lanes, then cross-mixed. Not SHA-256, but deterministic
    /// and sensitive to every byte, which is all the manifest needs of it here.
    public static func hash(data: Data) -> SHA256Digest {
        var lanes = [UInt64](repeating: 0xcbf2_9ce4_8422_2325, count: 4)
        for (offset, byte) in data.enumerated() {
            let lane = offset % lanes.count
            lanes[lane] = (lanes[lane] ^ UInt64(byte)) &* 0x100_0000_01b3
        }
        lanes[0] = (lanes[0] ^ UInt64(data.count)) &* 0x100_0000_01b3
        for round in 0..<(lanes.count - 1) {
            for lane in 0..<lanes.count {
                lanes[lane] = (lanes[lane] ^ lanes[(lane + round + 1) % lanes.count]) &* 0x100_0000_01b3
            }
        }
        var out: [UInt8] = []
        for lane in lanes {
            var bigEndian = lane.bigEndian
            withUnsafeBytes(of: &bigEndian) { out.append(contentsOf: $0) }
        }
        return SHA256Digest(bytes: out)
    }
}

public enum AES {
    public enum GCM {
        public struct SealedBox {
            public let combined: Data?
            public init(combined: Data) throws {
                guard combined.count > 4, combined.prefix(4) == Data("FAKE".utf8) else {
                    throw NSError(domain: "AES", code: 1)
                }
                self.combined = combined
            }
        }

        public static func seal(_ data: Data, using key: SymmetricKey) throws -> SealedBox {
            try SealedBox(combined: Data("FAKE".utf8) + xor(data, key))
        }

        public static func open(_ box: SealedBox, using key: SymmetricKey) throws -> Data {
            guard let combined = box.combined else { throw NSError(domain: "AES", code: 2) }
            return xor(combined.dropFirst(4), key)
        }

        private static func xor<D: DataProtocol>(_ data: D, _ key: SymmetricKey) -> Data {
            let material = [UInt8](key.bytes)
            guard !material.isEmpty else { return Data(data) }
            return Data(Data(data).enumerated().map { $0.element ^ material[$0.offset % material.count] })
        }
    }
}
