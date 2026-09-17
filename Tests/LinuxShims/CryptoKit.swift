import Foundation

public struct SymmetricKey: Equatable {
    public let bytes: Data
    public init(data: Data) { self.bytes = data }
    public init<D: DataProtocol>(data: D) { self.bytes = Data(data) }
    public func withUnsafeBytes<R>(_ body: (UnsafeRawBufferPointer) throws -> R) rethrows -> R {
        try bytes.withUnsafeBytes(body)
    }
}

public protocol Digest: Sequence where Element == UInt8 {}
public protocol HashFunction {}
public protocol MessageAuthenticationCode {}
public enum HMAC {}
public enum HKDF {}
public enum ChaChaPoly {}
public enum Curve25519 {}
public enum P256 {}
public enum P384 {}
public enum P521 {}
public enum Insecure {}
public enum SecureEnclave {}
public enum SharedSecret {}
public enum SymmetricKeySize {}
public enum CryptoKitError: Error { case incorrectParameterSize }

public struct SHA256Digest: Digest {
    let bytes: [UInt8]
    public func makeIterator() -> Array<UInt8>.Iterator { bytes.makeIterator() }
}

public enum SHA256 {
    public static func hash(data: Data) -> SHA256Digest {
        var state: [UInt32] = [0x6a09_e667, 0xbb67_ae85, 0x3c6e_f372, 0xa54f_f53a,
                               0x510e_527f, 0x9b05_688c, 0x1f83_d9ab, 0x5be0_cd19]
        let k: [UInt32] = [
            0x428a_2f98, 0x7137_4491, 0xb5c0_fbcf, 0xe9b5_dba5, 0x3956_c25b, 0x59f1_11f1, 0x923f_82a4, 0xab1c_5ed5,
            0xd807_aa98, 0x1283_5b01, 0x2431_85be, 0x550c_7dc3, 0x72be_5d74, 0x80de_b1fe, 0x9bdc_06a7, 0xc19b_f174,
            0xe49b_69c1, 0xefbe_4786, 0x0fc1_9dc6, 0x240c_a1cc, 0x2de9_2c6f, 0x4a74_84aa, 0x5cb0_a9dc, 0x76f9_88da,
            0x983e_5152, 0xa831_c66d, 0xb003_27c8, 0xbf59_7fc7, 0xc6e0_0bf3, 0xd5a7_9147, 0x06ca_6351, 0x1429_2967,
            0x27b7_0a85, 0x2e1b_2138, 0x4d2c_6dfc, 0x5338_0d13, 0x650a_7354, 0x766a_0abb, 0x81c2_c92e, 0x9272_2c85,
            0xa2bf_e8a1, 0xa81a_664b, 0xc24b_8b70, 0xc76c_51a3, 0xd192_e819, 0xd699_0624, 0xf40e_3585, 0x106a_a070,
            0x19a4_c116, 0x1e37_6c08, 0x2748_774c, 0x34b0_bcb5, 0x391c_0cb3, 0x4ed8_aa4a, 0x5b9c_ca4f, 0x682e_6ff3,
            0x748f_82ee, 0x78a5_636f, 0x84c8_7814, 0x8cc7_0208, 0x90be_fffa, 0xa450_6ceb, 0xbef9_a3f7, 0xc671_78f2]

        func rotr(_ x: UInt32, _ n: UInt32) -> UInt32 { (x >> n) | (x << (32 - n)) }

        var message = [UInt8](data)
        let bitLength = UInt64(message.count) * 8
        message.append(0x80)
        while message.count % 64 != 56 { message.append(0) }
        for shift in stride(from: 56, through: 0, by: -8) {
            message.append(UInt8((bitLength >> UInt64(shift)) & 0xff))
        }

        var offset = 0
        while offset < message.count {
            var w = [UInt32](repeating: 0, count: 64)
            for i in 0..<16 {
                let base = offset + i * 4
                w[i] = (UInt32(message[base]) << 24) | (UInt32(message[base + 1]) << 16)
                     | (UInt32(message[base + 2]) << 8) | UInt32(message[base + 3])
            }
            for i in 16..<64 {
                let s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
                let s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
                w[i] = w[i - 16] &+ s0 &+ w[i - 7] &+ s1
            }

            var a = state[0], b = state[1], c = state[2], d = state[3]
            var e = state[4], f = state[5], g = state[6], h = state[7]
            for i in 0..<64 {
                let s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)
                let ch = (e & f) ^ (~e & g)
                let t1 = h &+ s1 &+ ch &+ k[i] &+ w[i]
                let s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)
                let maj = (a & b) ^ (a & c) ^ (b & c)
                let t2 = s0 &+ maj
                h = g; g = f; f = e; e = d &+ t1; d = c; c = b; b = a; a = t1 &+ t2
            }
            state[0] &+= a; state[1] &+= b; state[2] &+= c; state[3] &+= d
            state[4] &+= e; state[5] &+= f; state[6] &+= g; state[7] &+= h
            offset += 64
        }

        var out: [UInt8] = []
        for value in state {
            var bigEndian = value.bigEndian
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
