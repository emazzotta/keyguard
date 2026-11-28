import Foundation

func readSecret() -> String? {
    guard let line = readLine(strippingNewline: true), !line.isEmpty else { return nil }
    return line
}
