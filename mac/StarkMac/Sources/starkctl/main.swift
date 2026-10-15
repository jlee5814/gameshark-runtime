import Foundation

// Demo driver for a StarkMac launched with STARK_DEMO_CONTROL=1. It posts one distributed
// notification; the app runs the same panel and model code its own UI runs.
//
//   starkctl move X Y          panel origin, screen points, bottom left
//   starkctl chat open|close
//   starkctl say TEXT...       type TEXT into the chat field and send it
//   starkctl clickthrough on|off
//   starkctl reconnect
//   starkctl jackin            Jack In Here: pull Stark back from a paired device
//   starkctl snapshot PATH
let args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else {
    FileHandle.standardError.write(Data("usage: starkctl move|chat|say|clickthrough|reconnect|jackin|snapshot ...\n".utf8))
    exit(2)
}
DistributedNotificationCenter.default().postNotificationName(
    Notification.Name("dev.gameshark.starkmac.control"), object: nil,
    userInfo: ["cmd": cmd, "args": Array(args.dropFirst())], deliverImmediately: true)
