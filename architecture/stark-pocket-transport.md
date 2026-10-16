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
4. Every later session: the server sends `challenge {nonce, server_fp, instance}`; the phone answers
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

## Draft continuity

Unsent composer text follows Stark. The source sends it in `ready_out {draft}`; the commit
transaction stores it in the handoff row with `draft_state = pending`. Delivery and receipt:

- Only the jack's target, after `ack_in`, at the generation the jack granted and while it
  still holds presence, can read the draft: in its `ack_in` result and in its own
  `snapshot.draft {handoff_id, text}`. The source, other surfaces, and a target that has
  since lost presence see nothing.
- The target restores the text into an empty composer and never sends it. When the
  composer already holds different text, the user chooses Use It or Keep Mine; nothing is
  overwritten silently. Either way the target then sends `draft_ack {handoff_id,
  presence_gen}`, fenced like `ack_in`, which sets `draft_state = delivered`. A confirmed
  draft is never offered again; `draft_ack` replays are idempotent.
- Until receipt the row redelivers on every snapshot, so a dropped connection, an app
  restart, or a SIGKILLed runtime between `ack_in` and `draft_ack` loses nothing. The client
  resends an unanswered `draft_ack` after reconnecting and drops it on `stale_presence`.
- The source clears its composer only on the runtime's receipt: `ready_out` answers
  `draft_carried`, true only when the handoff row holds exactly the text sent and the jack
  is committed or acked, never reverted (a reverted row keeps its draft only for
  recovery). The source takes the receipt only from an acked answer; on a committed one it
  asks again once the jack closes. Sending is not enough. A request lost on the way, refused, or beaten by the prepare
  deadline (which commits without a draft) answers false or nothing, and the text stays.
  A lost reply is reconciled after reconnecting by replaying the same `ready_out`, which the
  runtime answers from the durable row, across restarts and SIGKILL, without changing it.
- With that receipt, the source clears only once Stark has left with exactly that text (the
  jack resolved away from it) and the composer still holds it; a revert or newer typing
  keeps the text where it was typed.
- A draft no target confirmed (the jack reverted, or the target moved on first) stays in
  the journal; `stark.py drafts` lists it for the operator.

`draft_ack` is the one command this adds to the closed table; `draft_carried` is a result
field, not a command.

## Tailscale (Phase 3.3)

Away from home the phone reaches the same runtime through Tailscale. Nothing about trust
changes; only the path does.

```
iPhone on cellular, Tailscale on            Mac on home Wi-Fi (stark.py run --lan --tailscale)
  NWConnection to 100.x:47821  ──WireGuard──▶  tailnet LanListener, bound to 100.x only
                                                 same pin, instance, cap, failure limiter
                                                 └─▶ GatewayServer.requests (unchanged)
```

- Opt in: `--tailscale` requires `--lan`. At boot the runtime reads `tailscale status
  --json` (backend Running, this node's IPv4 in 100.64.0.0/10) and `tailscale serve status
  --json`, then binds a second listener to that address alone on `--tailscale-port` (47821).
  It never runs `up`, `serve`, or `funnel`. Tailscale stopped or unreadable: the LAN serves
  alone and the log says why. Any serve or Funnel handler forwarding a runtime port: no
  network listener starts at all (the Unix socket still serves StarkMac).
- Admission: the tailnet listener admits only Tailscale node addresses (100.64.0.0/10
  except 100.100.100.100, and fd7a:115c:a1e0::/48). The LAN listener now refuses
  fd7a:115c:a1e0::/48, which sits inside the fc00::/7 it admits, so LAN mode never served a
  tailnet peer unasked. One `SessionSlots` (4) and one `FailureLimiter` cover both.
- Advertisement: `<socket>.lan.json` gains `tailscale {address, port, dns}` only while that
  listener serves. `pair` proves it current before naming it: Tailscale still Running with
  the same address, no serve forward to a runtime port, and a pinned probe on
  `address:port` meeting this runtime's instance id. Any mismatch refuses the link (stale or
  ambiguous), never silently drops the tailnet.
- Link: `&t=<tailnet ip>&tp=<port>&tn=<MagicDNS name>`, still `v=1`; an older app ignores
  the keys. The phone refuses a `t` outside the tailnet ranges and a `tp` without a `t`.
- Routes: the credential keeps `routes [{host, port, via}]`; one stored before 3.3 decodes
  to its LAN hosts on its LAN port. Wi-Fi or wired dials LAN first, cellular or other
  dials the tailnet first, and a reconnect redials the route that last worked. A change of
  network class redials at once; pending messages resend under their ids, rows resync by
  seq, open handoffs and drafts reconcile as above. The first dial waits for
  NWPathMonitor's first report, at most 1 s, then dials in the link's order.
- Sends: frames go out on a per connection writer queue, in order, never on the client's
  state queue. A TLS send the peer never drains fails after the dial timeout (5 s) and
  cancels the connection. A network change, reconnect, or stop() closes the transport,
  which frees a stuck send; its late failure names an old connection and is ignored.
- One Stark: a device proving itself on a new session supersedes its older ones
  (`bye {reason: superseded}`). The new one is live first, so the old one leaving is not
  the surface leaving, and presence stays put: no revert, no new generation, no second
  device row. A welcome naming another agent uuid than the pairing's is refused before
  anything is mirrored. Accepted sockets use TCP keepalive (about a minute to notice a
  vanished peer).

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
| Internet host via Funnel or `tailscale serve` | reach a loopback forward the operator set up | reach Stark: the runtime starts no network listener while any serve handler forwards a runtime port, and `pair` refuses then too |
| Other node in the tailnet | connect to the tailnet port | get past the handshake: same pin, device proof, and failure limiter as the LAN; Tailscale ACLs can narrow who reaches it |
| Tailnet peer on the LAN port | send from its 100.x or fd7a address | be admitted: the LAN listener refuses both tailnet ranges |
| Stale tailnet address in a link | point the phone at a reassigned 100.x | be trusted: the pin decides; `pair` refuses an address Tailscale no longer reports |
| Stolen or lost phone | act as Stark's body until revoked | outlast `stark.py devices revoke`, which takes effect at the next pump |
| Revoked device | keep a socket open briefly | send or receive after the next pump; reconnect fails authentication |
| Stale surface (Mac or phone) | send under an old generation | write: `stale_presence` from the same fenced transaction as before |
| Database leak | learn device public keys and secret hashes | impersonate a phone: no private key or secret is stored |
| Other users on the Mac | read world readable files | read the TLS key, certificate, or LAN advertisement: 0600 in the 0700 gateway directory |

Trusted, as before: processes running as the same user on the Mac (they can read the
database and the token), the operator, and the phone's OS.

## Connection requirements

- `stark.py run --lan` (off by default). Port 47820 unless `--lan-port`; the runtime records
  its endpoint for `stark.py pair` in an advertisement named after its socket
  (`gateway/stark.sock.lan.json` by default) holding the port, pin, agent uuid, and a random
  per boot instance id. Runtimes with different sockets in one directory never touch each
  other's file: a boot drops only its own socket's stale advertisement (unless another live
  database owns it), and a clean stop deletes it only when the instance id is still its own.
  `pair` issues no link unless the database's daemon is running, the advertisement names
  that database's agent uuid, and the process answering a pinned TLS probe on that port
  presents the same instance id in its challenge. Runtimes sharing a directory share its
  TLS key, so the pin alone cannot tell them apart. A legacy `gateway/lan.json` from an
  older runtime is ignored and left in place.
- Same LAN or a link local path, or Tailscale with `--tailscale` (WireGuard, peer to peer
  or through Tailscale's DERP relays, which see only ciphertext). No port forwarding, no
  UPnP, no Funnel.
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
- Tailscale: the tailnet listener serves IPv4 only. The address and serve config are read at
  boot and at `pair`, not watched while running; a changed address costs the phone a
  fallback, and the daemon needs a restart to serve the new one. Path classes come from
  NWPath; with a VPN up, iOS may report a class change late or not at all, in which case a
  dead LAN socket is caught by TCP keepalive instead of an immediate redial.
- Physical device evidence is one run by hand (ios/StarkPocket/README.md): an iPhone on
  the PR #32 build passed cellular Jack In, Jack Out, and reply, Wi-Fi and cellular
  switches, and Tailscale off and on with the draft kept and no observed duplicate. Not yet
  proven on hardware: cold launch route order, a stalled send reaching the 5 s deadline, a
  background during the first 1 s path wait, every fault and revocation case, and composer
  text surviving app termination.

## Measured (loopback, M series Mac, in process runtime)

| Path | p50 | p99 |
|---|---|---|
| ping over Unix socket | 0.10 ms | 0.20 ms |
| ping over TLS | 0.16 ms | 0.28 ms |
| TLS connect and device proof | 1.30 ms | 1.99 ms |
| Jack In, request to ack, scripted surfaces | 0.72 ms | 0.81 ms |

Wi-Fi adds its own round trips; these isolate the runtime's cost.
