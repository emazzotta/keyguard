// Linux stand-in for `import Darwin`. Everything keyguard uses from it - the
// termios calls, the standard descriptors, isatty, read - is in Glibc under
// the same names, so this only has to re-export.
@_exported import Glibc
@_exported import Foundation
