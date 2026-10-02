# Local and remote room access

[Documents](../README.md)

How a browser reaches a lab's live room: the room service (`room_service/`, port
8891 in the lab VM) with its viewer, its HTTP API and its Server-Sent Events. The same
for both labs, but for the gateway: the RDK lab has an optional one, the prplMesh lab
none.

## Existing transport

The viewer uses HTTP requests for state/control and Server-Sent Events for
updates. The browser receives positions, metrics and events, not raw hwsim
radio frames. Containers, wmediumd, controller and optimizer remain together
on the lab radio host. Internet latency affects controls/observation; it must
not be mistaken for native steering time.

On a trusted LAN, open the host IP and configured room port. The default root
URL selects the live view; no `?mode=` is needed. Keep viewer assets, HTTP APIs
and SSE at the same origin. LXD proxy devices persist across VM restarts.

## Private remote use

In the RDK lab, for browser access to all three views, use the optional
the Tailscale gateway (the RDK lab's `gen/remote-access`, in [meta-cmf-bananapi-vcpe](https://vcpe.dev/meta-cmf-bananapi-vcpe/)). Its setup scripts provide private Serve access or authenticated public
Funnel access, with one shared timed reservation, visible ownership and
direct-port protection.

Prefer a VPN or SSH tunnel rather than forwarding unauthenticated management
ports to the Internet. For example, run on your workstation, replacing the
host and port with the deployment's values:

```sh
ssh -N -L 127.0.0.1:8891:127.0.0.1:8891 rev@RADIO_HOST
```

That example targets a radio host's guest-local listener. When SSH terminates
on the **outer** LXD host instead, use its reachable forwarded LAN address/port
as the destination, or use SSH through to the guest. An outer host's
`127.0.0.1:8891` is not automatically the guest's listener.
Open `http://127.0.0.1:8891/` locally and keep the tunnel open.

Do not bind an SSH tunnel to all interfaces. Verify SSH host keys; never share
LXD admin credentials just to let someone watch a demonstration.

## Shared Internet service

The RDK lab's Tailscale gateway is optional; installing repository code does not
enable a public endpoint. It places authentication and a reservation gate in front
of the same-origin viewer/API/SSE services. The prplMesh lab has no gateway: put an
authenticated HTTPS gateway in front of the same services, or reach them through a
private VPN. Keep full LXD, Prometheus, native control and
wmediumd sockets private. If the lab is behind CGNAT, use an outbound private
tunnel to the gateway; no Wi-Fi simulation changes are required.

The gateway must support long-lived SSE without buffering, appropriate idle
timeouts/keepalives, the full API path set and control methods, body limits,
authentication on both streams and requests, and reconnection/resync.
Use a trusted certificate; do not teach users to bypass Internet TLS errors.
An operator lease is concurrency control, not Internet authentication.
The RDK lab's gateway grants exclusive operator access, not public spectator
access. Separate observer/operator authorization would require an extension.

Before publication, test login/logout, denied unauthenticated control, SSE
reconnect after a network break, lease behavior, world load/play/drag,
stale indicators and safe recovery. Stop/restart the gateway without restarting
the native lab to confirm isolation. Retain real measurement timestamps.

## GitHub Pages boundary

GitHub Pages serves static assets; it cannot proxy a private lab's APIs.
The published explorer is a standalone illustration, not a live connection.
An HTTPS page also cannot simply fetch an arbitrary HTTP LAN address because
of mixed-content, origin and private-network restrictions.

The lowest-complexity future connection chooser should open the selected lab's
own viewer URL, defaulting to a configured local IP/port. Keeping a static
viewer on Pages while selecting a remote API requires explicit HTTPS, origin,
authentication and SSE support. That chooser is **proposed**, not implemented
by this documentation change.
