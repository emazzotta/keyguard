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

        let multiLineJson = "{\n  \"type\": \"service_account\",\n  \"key\": \"value\"\n}"
        checkDict("base64-encoded multiline value",
                  parseEnv("KEY=base64:\(Data(multiLineJson.utf8).base64EncodedString())"),
                  ["KEY": multiLineJson])
        checkDict("plain base64 padding not decoded",
                  parseEnv("TOKEN=abc123XYZ+/def456=="),
                  ["TOKEN": "abc123XYZ+/def456=="])

        print("\nserializeEnv")
        checkStr("sorted alphabetically",   serializeEnv(["B": "2", "A": "1"]),     "A=1\nB=2")
        checkStr("empty dict",              serializeEnv([:]),                       "")
        checkStr("single entry",            serializeEnv(["ONLY": "one"]),          "ONLY=one")
        checkStr("multiline value base64-encoded",
                 serializeEnv(["KEY": multiLineJson]),
                 "KEY=base64:\(Data(multiLineJson.utf8).base64EncodedString())")
        checkDict("round-trip",             parseEnv(serializeEnv(["FOO": "bar", "TOKEN": "abc==", "KEY": "val=ue"])),
                                            ["FOO": "bar", "TOKEN": "abc==", "KEY": "val=ue"])
        checkDict("round-trip single",     parseEnv(serializeEnv(["X": "y"])),    ["X": "y"])
        checkDict("round-trip empty",      parseEnv(serializeEnv([:])),            [:])
        checkDict("round-trip multiline",  parseEnv(serializeEnv(["JSON": multiLineJson, "PLAIN": "hello"])),
                                           ["JSON": multiLineJson, "PLAIN": "hello"])

        func checkArgs(_ desc: String, _ actual: ParsedArgs, positional: [String], cacheDuration: Int?,
                       caller: String? = nil) {
            if actual.positional == positional && actual.cacheDuration == cacheDuration && actual.caller == caller {
                print("  ✓ \(desc)")
            } else {
                print("  ✗ \(desc): got positional=\(actual.positional) cache=\(String(describing: actual.cacheDuration)) caller=\(String(describing: actual.caller)), want positional=\(positional) cache=\(String(describing: cacheDuration)) caller=\(String(describing: caller))")
                failures += 1
            }
        }

        print("\nparseArgs")
        checkArgs("no args",                    parseArgs([]),                              positional: [],              cacheDuration: nil)
        checkArgs("positional only",            parseArgs(["KEY1", "KEY2"]),                positional: ["KEY1", "KEY2"], cacheDuration: nil)
        checkArgs("cache-duration at end",      parseArgs(["KEY", "--cache-duration", "60"]), positional: ["KEY"],       cacheDuration: 60)
        checkArgs("cache-duration at start",    parseArgs(["--cache-duration", "120", "KEY"]), positional: ["KEY"],      cacheDuration: 120)
        checkArgs("cache-duration between keys", parseArgs(["A", "--cache-duration", "30", "B"]), positional: ["A", "B"], cacheDuration: 30)
        checkArgs("invalid duration kept as positional", parseArgs(["--cache-duration", "abc"]), positional: ["--cache-duration", "abc"], cacheDuration: nil)
        checkArgs("missing duration value",     parseArgs(["--cache-duration"]),            positional: ["--cache-duration"], cacheDuration: nil)
        checkArgs("zero duration",              parseArgs(["--cache-duration", "0"]),       positional: [],              cacheDuration: 0)
        checkArgs("negative duration",          parseArgs(["--cache-duration", "-5"]),      positional: [],              cacheDuration: -5)
        checkArgs("should read the caller label", parseArgs(["KEY", "--caller", "\"Fix CI\" (48f92bcb)"]),
                  positional: ["KEY"], cacheDuration: nil, caller: "\"Fix CI\" (48f92bcb)")
        checkArgs("should read the caller next to a cache duration",
                  parseArgs(["--caller", "session 48f92bcb", "A", "--cache-duration", "60", "B"]),
                  positional: ["A", "B"], cacheDuration: 60, caller: "session 48f92bcb")
        checkArgs("should keep a caller flag with no value as positional", parseArgs(["KEY", "--caller"]),
                  positional: ["KEY", "--caller"], cacheDuration: nil)

        print("\nbuildReason")
        checkStr("without cache",  buildReason(base: "List secrets", cacheDuration: nil),  "List secrets")
        checkStr("with cache",     buildReason(base: "List secrets", cacheDuration: 120),  "List secrets, cached for 120s")
        checkStr("with zero",      buildReason(base: "Reveal TOKEN", cacheDuration: 0),    "Reveal TOKEN, cached for 0s")
        checkStr("should name the caller",
                 buildReason(base: "Reveal TOKEN", cacheDuration: nil, caller: "\"Fix CI\" (48f92bcb)"),
                 "Reveal TOKEN for \"Fix CI\" (48f92bcb)")
        checkStr("should name the caller before the cache duration",
                 buildReason(base: "Reveal GITLAB_TOKEN", cacheDuration: 120,
                             caller: "\"Keyguard approval session names\" (48f92bcb)"),
                 "Reveal GITLAB_TOKEN for \"Keyguard approval session names\" (48f92bcb), cached for 120s")
        checkStr("should leave out an empty caller",
                 buildReason(base: "List secrets", cacheDuration: 60, caller: ""), "List secrets, cached for 60s")

        if failures > 0 {
            fputs("\n\(failures) failure(s)\n", stderr)
            exit(1)
        }
        print("\nAll tests passed")
    }
}
