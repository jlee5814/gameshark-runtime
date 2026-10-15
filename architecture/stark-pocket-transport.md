# Stark Pocket transport: paired devices on the LAN

Phase 3A. A paired iPhone reaches the runtime on the Mac as one more surface. The runtime
stays the only host of identity, duties, conversation, and inference; the phone holds a
device key and presentation preferences, never agent state and never SQLite access.

## Shape

```
iPhone (StarkKit TLSTransport)          Mac (stark.py run --lan)
  NWConnection, TLS 1.3, pin  ──TCP──▶  LanListener (net_gateway.py)
                                          TLSSession thread: TLS, frames, filter
                                          └─▶ GatewayServer.requests (shared with Unix)
                                                └─▶ SurfaceAPI on the coordinator
                                                      device proof, pairing, revocation,
                                                      the same closed command table
```

The LAN listener feeds the queue, session table, and session cap the Unix gateway already
uses, so presence fencing, dedup by `client_msg_id`, resync by seq, and the push order
apply unchanged. The Unix socket and its token are untouched and mean nothing over TLS.

## Pairing

1. The operator runs `stark.py pair --name "My iPhone"` on the Mac. It writes a pending
   pairing holding only SHA256 of a fresh 128 bit secret (5 minute lifetime) and prints
   `starkpocket://pair?v=1&h=<host>.local&a=<lan ip>&p=<port>&fp=<pin>&id=<pairing>&s=<secret>`,
   plus a terminal QR code when `qrcode` is installed.
2. The phone opens the link (camera scan, paste, or tap), generates a NIST P256 key
   (Secure Enclave on a real iPhone), and dials the address with the pin.
3. Over pinned TLS it sends `pair {pairing_id, secret, public_key, name}`. The coordinator
   checks the secret in constant time, expiry, and single use, then creates the device in
   one fenced transaction and answers `paired {device_id, surface}`. The session ends.
4. Every later session: the server sends `challenge {nonce, server_fp}`; the phone answers
   `hello {device_id, sig}` where `sig` is ECDSA P256 SHA256 over
   `"stark-device" 0 nonce 0 device_id 0 proto 0 server_fp`. The surface (`pocket-xxxxxx`)
   comes from the device row, never from the frame.

Revocation: `stark.py devices revoke <id>` marks the row and pings the daemon. The next pump
closes the device's sessions, drops its queued requests, and evicts it from presence (the
holder returns home under generation + 1; an open jack it is part of aborts or reverts
home, never back to the revoked device, even when it was the jack's source), all before
answering anything else. The daemon applies revocations with or without `--lan`.

## Surface initiated jack

One command joins the closed table: `jack {handoff_id, to: "self" | "home", presence_gen}`.
`self` pulls presence to the caller and requires the caller to have seen the current
generation; `home` sends Stark home and only the holder at its generation may ask. Both
checks run inside the transaction that opens the handoff
(`PresenceManager.request_jack_by`). Everything after is the existing two phase jack:
the source plays `ready_out`, the target `ack_in`, the deadlines and revert rules hold.

## Threat model

| Adversary | Can | Cannot, because |
|---|---|---|
| Passive LAN observer | see that a phone talks to the Mac, and traffic sizes | read frames: TLS 1.3 with forward secrecy |
| Active LAN attacker (ARP or DNS spoofing, rogue AP) | redirect the phone's connection | impersonate the runtime: the phone pins the server key from the pairing link, so the handshake fails before any frame |
| Attacker who records a hello | replay it | log in: the signature covers a fresh 32 byte nonce |
| Attacker relaying to another runtime | forward a device's signature | use it elsewhere: the signature binds the server key hash |
| Holder of a used or expired pairing link | try it | pair: single use, 5 minute expiry, checked in the consuming transaction |
| Unpaired host on the LAN | connect and guess | get past the handshake: no device key; 5 failures in 60 s block the address |
| Internet host | reach the port only if the operator forwards it | be admitted: peers outside loopback, RFC 1918, link local, and ULA are dropped at accept |
| Stolen or lost phone | act as Stark's body until revoked | outlast `stark.py devices revoke`, which takes effect at the next pump |
| Revoked device | keep a socket open briefly | send or receive after the next pump; reconnect fails authentication |
| Stale surface (Mac or phone) | send under an old generation | write: `stale_presence` from the same fenced transaction as before |
| Database leak | learn device public keys and secret hashes | impersonate a phone: no private key or secret is stored |
| Other users on the Mac | read world readable files | read the TLS key, certificate, or `lan.json`: 0600 in the 0700 gateway directory |

Trusted, as before: processes running as the same user on the Mac (they can read the
database and the token), the operator, and the phone's OS.

## Connection requirements

- `stark.py run --lan` (off by default). Port 47820 unless `--lan-port`; the runtime records
  the port and pin in `gateway/lan.json` for `stark.py pair`, drops it at every boot and
  clean stop, and `pair` issues no link unless the running daemon answers a pinned TLS
  probe on that port with that key.
- Same LAN or a link local path. No relay, no cloud, no port forwarding, no UPnP.
- macOS may ask once to allow incoming connections for Python.
- iOS asks for local network permission on first connect (`NSLocalNetworkUsageDescription`).
- Addresses in the link are hints; trust rests on the pin, so a new DHCP address costs a
  retry or a fresh pairing link, never trust.

## Known limitations

- No channel binding to the TLS session: Python's stdlib exposes no TLS 1.3 exporter. The
  signature binds the pinned server key instead, which defeats relaying to another runtime
  but not an attacker who already holds the runtime's private key (same user on the Mac).
- Pairing secrecy rests on the out of band link. Anyone who reads it within 5 minutes,
  before the phone uses it, can pair instead; the operator sees an unexpected device in
  `stark.py devices` and can revoke it.
- Revocation latency is one pump; `devices revoke` pings the daemon so it pumps at once.
  Without a running daemon there is nothing to revoke from.
- LAN denial of service (connection floods, Wi-Fi jamming) is out of scope. The LAN
  listener holds at most 4 of the 8 shared session slots, so a flood cannot lock StarkMac
  out, but it can keep the phone from connecting. Repeated handshake failures from the
  phone's own address (a port scanner on the same host) can block it for 60 s.
- Hardware without a Secure Enclave falls back to a software key in the Keychain. The iOS 27
  simulator on this Apple silicon Mac reports an enclave and uses it.
- Bonjour discovery is not built; the link carries a host name and address.

## Measured (loopback, M series Mac, in process runtime)

| Path | p50 | p99 |
|---|---|---|
| ping over Unix socket | 0.10 ms | 0.20 ms |
| ping over TLS | 0.16 ms | 0.28 ms |
| TLS connect and device proof | 1.30 ms | 1.99 ms |
| Jack In, request to ack, scripted surfaces | 0.72 ms | 0.81 ms |

Wi-Fi adds its own round trips; these isolate the runtime's cost.
