import Darwin
import Foundation

func readSecret() -> String? {
    var tty = termios()
    tcgetattr(STDIN_FILENO, &tty)
    var raw = tty
    raw.c_lflag &= ~(UInt(ECHO) | UInt(ICANON))
    withUnsafeMutablePointer(to: &raw.c_cc) {
        $0.withMemoryRebound(to: cc_t.self, capacity: Int(NCCS)) {
            $0[Int(VMIN)] = 1
            $0[Int(VTIME)] = 0
        }
    }
    tcsetattr(STDIN_FILENO, TCSANOW, &raw)
    defer {
        tcsetattr(STDIN_FILENO, TCSANOW, &tty)
        fputs("\n", stderr)
    }
    var secret = ""
    var byte: UInt8 = 0
    while read(STDIN_FILENO, &byte, 1) == 1, byte != UInt8(ascii: "\n"), byte != UInt8(ascii: "\r") {
        secret.append(Character(UnicodeScalar(byte)))
    }
    return secret.isEmpty ? nil : secret
}
