import Foundation

@main
struct TestRunner {
    static func main() {
        var failures = 0

        func checkDict(_ desc: String, _ actual: [String: String], _ expected: [String: String]) {
            if actual == expected {
                print("  ✓ \(desc)")
            } else {
                print("  ✗ \(desc): got \(actual), want \(expected)")
                failures += 1
            }
        }

        func checkStr(_ desc: String, _ actual: String, _ expected: String) {
            if actual == expected {
                print("  ✓ \(desc)")
            } else {
                print("  ✗ \(desc): got \(actual.debugDescription), want \(expected.debugDescription)")
                failures += 1
            }
        }

        print("parseEnv")
        checkDict("simple key=value",        parseEnv("KEY=value"),                  ["KEY": "value"])
        checkDict("export prefix",           parseEnv("export KEY=value"),           ["KEY": "value"])
        checkDict("double-quoted value",     parseEnv(#"KEY="hello world""#),        ["KEY": "hello world"])
        checkDict("single-quoted value",     parseEnv("KEY='hello world'"),          ["KEY": "hello world"])
        checkDict("inline comment stripped", parseEnv("KEY=value  # comment"),       ["KEY": "value"])
        checkDict("hash inside double quotes kept", parseEnv(#"KEY="val # not a comment""#), ["KEY": "val # not a comment"])
        checkDict("hash inside single quotes kept", parseEnv("KEY='val # not a comment'"),   ["KEY": "val # not a comment"])
        checkDict("comment line skipped",    parseEnv("# comment\nKEY=value"),       ["KEY": "value"])
        checkDict("empty lines skipped",     parseEnv("\n\nKEY=value\n\n"),          ["KEY": "value"])
        checkDict("equals sign in value",    parseEnv("KEY=a=b=c"),                  ["KEY": "a=b=c"])
        checkDict("base64 padding (==)",     parseEnv("TOKEN=abc123XYZ+/def456=="),  ["TOKEN": "abc123XYZ+/def456=="])
        checkDict("multiple entries",        parseEnv("A=1\nB=2\nC=3"),              ["A": "1", "B": "2", "C": "3"])
        checkDict("empty content",           parseEnv(""),                           [:])
        checkDict("whitespace-only lines",  parseEnv("  \n  \n"),                   [:])
        checkDict("key with empty value",   parseEnv("KEY="),                       [:])
        checkDict("no equals sign",         parseEnv("INVALID_LINE"),               [:])
        checkDict("export with extra space", parseEnv("export   KEY=value"),         ["KEY": "value"])
        checkDict("tabs around key",        parseEnv("\tKEY=value\t"),               ["KEY": "value"])
        checkDict("value with inner quotes", parseEnv(#"KEY=he said "hi""#),         ["KEY": #"he said "hi""#])
        checkDict("single char value",      parseEnv("K=v"),                        ["K": "v"])
        checkDict("numeric value",          parseEnv("PORT=8080"),                  ["PORT": "8080"])

        print("\nserializeEnv")
        checkStr("sorted alphabetically",   serializeEnv(["B": "2", "A": "1"]),     "A=1\nB=2")
        checkStr("empty dict",              serializeEnv([:]),                       "")
        checkStr("single entry",            serializeEnv(["ONLY": "one"]),          "ONLY=one")
        checkDict("round-trip",             parseEnv(serializeEnv(["FOO": "bar", "TOKEN": "abc==", "KEY": "val=ue"])),
                                            ["FOO": "bar", "TOKEN": "abc==", "KEY": "val=ue"])
        checkDict("round-trip single",     parseEnv(serializeEnv(["X": "y"])),    ["X": "y"])
        checkDict("round-trip empty",      parseEnv(serializeEnv([:])),            [:])

        if failures > 0 {
            fputs("\n\(failures) failure(s)\n", stderr)
            exit(1)
        }
        print("\nAll tests passed")
    }
}
