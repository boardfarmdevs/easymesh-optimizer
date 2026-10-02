# The opt-in native load policy

[Documents](../README.md)

Default operation is signal-only: the optimizer steers a client off a weak link and
nothing else. Two opt-in policies add load, from the controller's own reports:

- `configs/load-aware-policy.yaml` (`configs/load-aware-policy-prplmesh.yaml` as
  qualified in the prplMesh lab) balances a strong link from sustained high load to a
  fresh, quieter channel;
- `configs/load-counter-guard-policy.yaml` additionally requires fresh native retry,
  TX-failure and RX-drop rates before balancing: missing counters abstain, and rates
  over explicit limits veto balancing. Signal rescue is unchanged.

The RF coverage reference (in [easymesh-medium](https://vcpe.dev/easymesh-medium/))
has the limits, the decision reasons, the demonstration rooms and their validation.

## What the policy reads

Schema-2 snapshots carry native AP utilization (0 to 255), station count,
packet-counter activity, receipt timestamps and the provider epoch. The survey bridge
supplies only provenance and liveness, not load values or SNR inputs. Packets per second
are activity, not offered demand or calibrated capacity; backhaul hops are a conservative
cost guard, not a bandwidth estimate.

Transport provenance is `ieee1905-ethernet` on RDK. The shared collector uses prplMesh's
native broker for its colocated and remote APs, preserving native publication timestamps
instead of re-timestamping cached data. Disconnected, stale or unsupported receivers fail
closed. Live mode requires root in the VM and `--candidate-provider controller`; its own
native IEEE 1905 receiver closes on exit. Default operation starts no additional
collector.

## How it decides

Strong links balance from sustained high load to a fresh quieter channel with viable RF
and no extra wireless hop. RDK's independent five-second native reports use a ten-second
hold, with five-second freshness and skew bounds; both reports must advance. The
prplMesh profile uses a one-second skew and a five-second hold. These opt-in parameters
change neither native reporting nor the default room gates.

One active client moves at a time, then settles with cooldown; weak links keep signal
protection. Missing, stale, skewed, synthetic or epoch-mismatched observations never
become zero load. Decisions record explicit reasons and evidence. Native BTM may still be
rejected by the client. Reception-backed candidates, demand and capacity estimation and
general channel selection are separate work.

## Running it in a room

1. Stop the existing room service first: never run two actuating optimizers.
2. Copy the room manifest and change only `policy` to the load-aware policy (in the RDK
   lab `gen/optimizer/configs/load-aware-policy.yaml`).
3. Start the room with the lab's launcher and `--manifest /absolute/temporary-manifest.json`
   added to the normal interactive command.
4. Restart the unchanged service to return to signal-only operation.

The counter guard has a ready manifest in each lab, `native-counter-guard-room-profile.json`
(next to the room manifests: `gen/rooms/manifests` in the RDK lab, `rooms/manifests` in
the prplMesh lab). It plays `rf-asymmetric-ack` with the guard:

```sh
gen/rooms/room-service interactive --mode recommend --profiling \
  --manifest gen/rooms/manifests/native-counter-guard-room-profile.json
```

The bounded native counter acceptance can replay real report windows through the shared
guard without inventing a load target. Clear, pressure and recovery qualify only that
veto, not a load move.

## Preparing the radios

The policy reconfigures neither radios nor client capabilities. For a load move:

- Configure a same-band, different-channel fronthaul AP first. Do not retune the active
  backhaul radio.
- Band-directed profiles can restrict `freq_list`: explicitly permit the new channel
  before BTM; a scan alone does not grant eligibility.
- Verify the kernel **and the native controller** channel state. RDK local retunes need
  an operating-channel report; an agent refresh is test preconditioning, never a
  timed-steer repair. After a refresh, replay the native metrics-reporting policy and
  verify fresh reports: agent timer state is volatile.
- The global `Device.WiFi.ApplyRadioSettings` can also apply stale channel settings on
  unrelated radios. Snapshot all three **live** channels, preserve them during the test
  and verify their exact restoration, including 6 GHz.

## Checking the native load reports

A bounded read-only check, inside the RDK lab VM from the lab's checkout:

```sh
PYTHONPATH=gen/optimizer python3 gen/optimizer/acceptance/native-load-acceptance.py \
  --stack rdk --output /tmp/native-load-new
```

It checks every BSS's load, the station counts, client activity, advancing timestamps and
the receiver's cleanup.
