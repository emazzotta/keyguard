import Foundation

@main
struct RebaseTestRunner {
    static func main() {
        var failures = 0

        func checkEqual<T: Equatable>(_ desc: String, _ actual: T, _ expected: T) {
            if actual == expected { print("  ✓ \(desc)") } else {
                print("  ✗ \(desc): got \(actual), want \(expected)")
                failures += 1
            }
        }
        func entry(_ file: String) -> IndexEntry { IndexEntry(file: file, tier: .high) }

        let index = "index.age"
        let a = "vars/a.age"
        let b = "vars/b.age"
        let c = "vars/c.age"
        let none: [String] = []

        print("changes on different secrets")
        let added = planRebase(base: [index: "i1", a: "a1"],
                               local: [index: "i2", a: "a1", b: "b1"],
                               service: [index: "i3", a: "a1", c: "c1"],
                               localEntries: ["A": entry(a), "B": entry(b)],
                               serviceEntries: ["A": entry(a), "C": entry(c)])
        checkEqual("should keep a secret added here beside one added there",
                   added.entries, ["A": entry(a), "B": entry(b), "C": entry(c)])
        checkEqual("should fetch the file added there", added.fetch, [c])
        checkEqual("should remove nothing when nothing was deleted there", added.remove, none)
        checkEqual("should see no conflict between different secrets", added.conflicts, none)

        let edited = planRebase(base: [index: "i1", a: "a1"],
                                local: [index: "i2", a: "a2"],
                                service: [index: "i3", a: "a1", c: "c1"],
                                localEntries: ["A": entry(a)],
                                serviceEntries: ["A": entry(a), "C": entry(c)])
        checkEqual("should keep a file edited here that is unchanged there", edited.fetch, [c])
        checkEqual("should keep the entry of a secret edited here", edited.entries, ["A": entry(a), "C": entry(c)])

        print("\ndeletions")
        let deletedHere = planRebase(base: [index: "i1", a: "a1", b: "b1"],
                                     local: [index: "i2", a: "a1"],
                                     service: [index: "i3", a: "a1", b: "b1", c: "c1"],
                                     localEntries: ["A": entry(a)],
                                     serviceEntries: ["A": entry(a), "B": entry(b), "C": entry(c)])
        checkEqual("should drop the entry of a secret deleted here", deletedHere.entries, ["A": entry(a), "C": entry(c)])
        checkEqual("should not fetch back a secret deleted here", deletedHere.fetch, [c])

        let deletedThere = planRebase(base: [index: "i1", a: "a1", b: "b1"],
                                      local: [index: "i2", a: "a1", b: "b1", c: "c1"],
                                      service: [index: "i3", a: "a1"],
                                      localEntries: ["A": entry(a), "B": entry(b), "C": entry(c)],
                                      serviceEntries: ["A": entry(a)])
        checkEqual("should remove a file deleted there that is unchanged here", deletedThere.remove, [b])
        checkEqual("should drop its entry and keep the one added here",
                   deletedThere.entries, ["A": entry(a), "C": entry(c)])

        let deletedHereEditedThere = planRebase(base: [index: "i1", a: "a1"],
                                                local: [index: "i2"],
                                                service: [index: "i3", a: "a2"],
                                                localEntries: [:],
                                                serviceEntries: ["A": entry(a)])
        checkEqual("should take the service's edit over a deletion here", deletedHereEditedThere.fetch, [a])
        checkEqual("should keep the entry of a secret edited there",
                   deletedHereEditedThere.entries, ["A": entry(a)])

        let editedHereDeletedThere = planRebase(base: [index: "i1", a: "a1"],
                                                local: [index: "i2", a: "a2"],
                                                service: [index: "i3"],
                                                localEntries: ["A": entry(a)],
                                                serviceEntries: [:])
        checkEqual("should keep an edit here over a deletion there", editedHereDeletedThere.entries, ["A": entry(a)])
        checkEqual("should not remove the file edited here", editedHereDeletedThere.remove, none)

        print("\nconflicts")
        let bothEdited = planRebase(base: [index: "i1", a: "a1"],
                                    local: [index: "i2", a: "a2"],
                                    service: [index: "i3", a: "a3"],
                                    localEntries: ["A": entry(a)],
                                    serviceEntries: ["A": entry(a)])
        checkEqual("should name a secret edited on both sides", bothEdited.conflicts, ["A"])
        let bothAdded = planRebase(base: [index: "i1"],
                                   local: [index: "i2", b: "b1"],
                                   service: [index: "i3", b: "b2"],
                                   localEntries: ["B": entry(b)],
                                   serviceEntries: ["B": entry(b)])
        checkEqual("should name a secret added on both sides", bothAdded.conflicts, ["B"])

        print("\nfiles keyguard did not change")
        let repaired = planRebase(base: [index: "i1", a: "a1"],
                                  local: [index: "i2", ".DS_Store": "x"],
                                  service: [index: "i3", a: "a1", "identity-remote.phone.passkey": "p1"],
                                  localEntries: ["A": entry(a)],
                                  serviceEntries: ["A": entry(a)])
        checkEqual("should fetch a missing file its own index still lists, and the service's other files",
                   repaired.fetch, ["identity-remote.phone.passkey", a])
        checkEqual("should remove a stray local file", repaired.remove, [".DS_Store"])
        checkEqual("should keep the entry of the repaired secret", repaired.entries, ["A": entry(a)])

        if failures > 0 { fputs("\n\(failures) failure(s)\n", stderr); exit(1) }
        print("\nAll tests passed")
    }
}
