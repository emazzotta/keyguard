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

        func checkArgs(_ desc: String, _ actual: ParsedArgs, positional: [String], cacheDuration: Int?) {
            let posOk = actual.positional == positional
            let cacheOk = actual.cacheDuration == cacheDuration
            if posOk && cacheOk {
                print("  ✓ \(desc)")
            } else {
                print("  ✗ \(desc): got positional=\(actual.positional) cache=\(String(describing: actual.cacheDuration)), want positional=\(positional) cache=\(String(describing: cacheDuration))")
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

        print("\nbuildReason")
        checkStr("without cache",  buildReason(base: "List secrets", cacheDuration: nil),  "List secrets")
        checkStr("with cache",     buildReason(base: "List secrets", cacheDuration: 120),  "List secrets (cached for 120s)")
        checkStr("with zero",      buildReason(base: "Reveal TOKEN", cacheDuration: 0),    "Reveal TOKEN (cached for 0s)")

        func checkRename(_ desc: String, _ block: () throws -> [String: String], _ expected: [String: String]) {
            do {
                let actual = try block()
                if actual == expected {
                    print("  ✓ \(desc)")
                } else {
                    print("  ✗ \(desc): got \(actual), want \(expected)")
                    failures += 1
                }
            } catch {
                print("  ✗ \(desc): unexpected error \(error)")
                failures += 1
            }
        }

        func checkRenameThrows(_ desc: String, _ block: () throws -> [String: String], _ expected: RenameError) {
            do {
                let actual = try block()
                print("  ✗ \(desc): expected error \(expected), got \(actual)")
                failures += 1
            } catch let e as RenameError where e == expected {
                print("  ✓ \(desc)")
            } catch {
                print("  ✗ \(desc): got error \(error), want \(expected)")
                failures += 1
            }
        }

        print("\nrenameEntry")
        checkRename("basic rename moves value",
                    { try renameEntry(in: ["A": "1"], from: "A", to: "B") },
                    ["B": "1"])
        checkRename("other entries preserved",
                    { try renameEntry(in: ["A": "1", "C": "3"], from: "A", to: "B") },
                    ["B": "1", "C": "3"])
        checkRename("multiline value preserved",
                    { try renameEntry(in: ["A": multiLineJson, "X": "y"], from: "A", to: "B") },
                    ["B": multiLineJson, "X": "y"])
        checkRename("value with equals signs preserved",
                    { try renameEntry(in: ["A": "x=y=z"], from: "A", to: "B") },
                    ["B": "x=y=z"])
        checkRename("overwrite=true replaces existing destination",
                    { try renameEntry(in: ["A": "1", "B": "2"], from: "A", to: "B", overwrite: true) },
                    ["B": "1"])
        checkRename("overwrite=false ok when destination is free",
                    { try renameEntry(in: ["A": "1"], from: "A", to: "B", overwrite: false) },
                    ["B": "1"])
        checkRenameThrows("source missing throws sourceNotFound",
                          { try renameEntry(in: [:], from: "X", to: "Y") },
                          .sourceNotFound("X"))
        checkRenameThrows("source missing in non-empty dict",
                          { try renameEntry(in: ["A": "1"], from: "X", to: "Y") },
                          .sourceNotFound("X"))
        checkRenameThrows("same key throws sameKey",
                          { try renameEntry(in: ["A": "1"], from: "A", to: "A") },
                          .sameKey)
        checkRenameThrows("same key wins over missing source check",
                          { try renameEntry(in: [:], from: "X", to: "X") },
                          .sameKey)
        checkRenameThrows("destination exists without force",
                          { try renameEntry(in: ["A": "1", "B": "2"], from: "A", to: "B") },
                          .destinationExists("B"))
        checkRenameThrows("destination exists explicit overwrite=false",
                          { try renameEntry(in: ["A": "1", "B": "2"], from: "A", to: "B", overwrite: false) },
                          .destinationExists("B"))

        if failures > 0 {
            fputs("\n\(failures) failure(s)\n", stderr)
            exit(1)
        }
        print("\nAll tests passed")
    }
}
