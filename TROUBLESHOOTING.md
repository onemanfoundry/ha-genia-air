# Troubleshooting

## Telemetry is frozen: every entity shows a plausible value, none of them changes

This is the nastiest failure mode of the whole stack, because **nothing looks
broken**. The add-on is running, Home Assistant shows temperatures, setpoints and
compressor states — and every one of them is hours or days old.

### How to tell in ten seconds

Pick a value that *must* move, such as the outside temperature broadcast, and
look at **`last_reported`**, not at the state:

```jinja
{{ states.sensor.ebusd_broadcast_outsidetemp_temp2.last_reported }}
```

> ⚠️ **Do not use `last_changed` or `last_updated` for this.** Home Assistant only
> moves those when the *value* changes. With the heat pump idle, half the entities
> legitimately hold the same number for hours, so those fields will convince you
> everything is stale when it is fine — or that things are fine when they are not.
> `last_reported` updates on every received message, which is exactly the question
> you are asking.

Then confirm at the source. ebusd publishes its own health:

```bash
mosquitto_sub -h <broker> -t 'ebusd/global/#' -v -C 8
```

- `ebusd/global/signal false` → **ebusd cannot see the bus.** Everything it
  publishes from now on is a stale cache. This is the smoking gun.
- `ebusd/global/signal true` + `scan "finished"` → the bus side is healthy; the
  problem is elsewhere.

### The usual cause: the adapter's TCP slot is wedged

The eBUS Adapter Shield accepts **exactly one TCP client**. If ebusd's process is
killed while that connection is open — an add-on update, a container restart, an
OOM kill — the adapter can be left holding a zombie socket. It then stops
accepting connections while still serving its web UI perfectly happily.

Ask the adapter directly:

```bash
curl -s http://<adapter-ip>/api/v1/status
```

The diagnosis is in the `ebus` object:

| `signal` | `connected` | Meaning |
|---|---|---|
| `true` | `true` | Healthy. Look elsewhere. |
| `true` | **`false`** | **This bug.** The bus is fine, nobody is reading it. |
| `false` | any | The adapter itself cannot see the bus — check wiring/power at the appliance. |

`signal: true` with `connected: false` means the appliance and the bus are fine
(its own control panel will work normally) and the adapter simply is not letting
ebusd in. Its REPL will say so out loud:

```bash
curl -X PUT --data-binary "ebus" http://<adapter-ip>/api/v1/repl
# -> "already switched to enhanced eBUS mode on TCP port, restart needed"
```

### The fix: reboot the adapter, remotely

```bash
curl -s http://<adapter-ip>/api/v1/config > /tmp/ebus-cfg.json
curl -X PUT -H "Content-Type: application/json" \
     --data-binary @/tmp/ebus-cfg.json \
     "http://<adapter-ip>/api/v1/config?reboot=1"
```

Three things that are easy to get wrong:

1. **The parameter is `?reboot=1`.** Plain `?reboot` returns `HTTP 200` and does
   nothing at all, which is a very convincing way to waste an hour.
2. **Send back the body exactly as `GET /config` returned it.** The Wi-Fi password
   comes back masked (`*+#*+#*+#*+#`); the firmware treats that mask as "unchanged".
   Do not try to be clever and strip it.
3. **The firmware only reboots when something changes** — `?reboot=1` is what makes
   the request count as a change. Re-sending an identical config without it is a no-op.

Afterwards, restart the add-on so ebusd reconnects and rescans the bus. Expect
`ebusd/global/scan` to go `"running"` → `"finished"`.

### Restarting the add-on alone will not fix it

If the adapter is the wedged party, a fresh ebusd still has nowhere to connect.
These do **not** help either, all verified the hard way:

- Kicking the adapter off Wi-Fi. It reconnects with its uptime intact; the wedge
  is in the application, not the network stack.
- Power-cycling whatever you *think* powers the adapter. Check its `uptime` field
  afterwards — if it did not reset, you cycled something else.

### Repopulation is slow, and that is normal

After recovery, only a handful of entities will refresh in the first minutes.
ebusd polls messages round-robin across hundreds of definitions and only publishes
on change, so live quantities (flow temperatures, compressor state) come back
first, while `ctls2` configuration parameters trickle in over hours. Judge recovery
by whether the *moving* values move, not by an entity count.

## The log is full of `read timeout` / `SYN received`, and Diagnostics says "No bus scan result yet"

Passive telemetry (what the heat pump broadcasts on its own) keeps arriving,
but everything ebusd **asks** for — the bus scan, active reads and, if you
enable full control, writes — fails:

```
[bus error] send to 15: ERR: read timeout
[main error] scan config 08: ERR: SYN received
```

**Cause:** the network link to the adapter is too slow. On eBUS a slave has
to answer within a few milliseconds and ebusd waits 25 ms by default; if the
adapter sits on congested WiFi, its answers arrive late and are discarded.
Check it from any machine on your LAN:

```
ping -c 20 <adapter-ip>
```

Anything above ~10 ms on average, or spikes into the hundreds, is enough to
cause this. The adapter's own `info` (ebusd TCP command, or the add-on log at
start-up) shows `max symbol latency` — the same story.

**Fix, best first:**

1. Give the adapter a better link: Ethernet if your adapter has it, or a WiFi
   access point close to it on a less crowded channel.
2. Set `ebusd_latency_ms` (e.g. `50`) in the add-on configuration and
   restart it. This tells ebusd to wait longer for answers. It does not make a
   bad link good, but it gets the bus scan and most reads through.

## `ebus.connected` flickers True/false every 10-20s, but real telemetry keeps flowing

If the bus is otherwise healthy — good WiFi signal, no congestion — and the
log shows a steady trickle of `read timeout` / `SYN received` for `ctls2 z2*`
or `ctls2 z3*` registers while `z1*` traffic looks fine, the cause is **not**
the network link: `zone_count` in the add-on configuration doesn't match how
many zones your installation actually has.

ebusd auto-polls every zone register up to `zone_count` on a recurring cycle.
Vaillant's own `ctls2` definitions are generic for up to 3 zones; if
`zone_count` is set higher than what's physically wired, ebusd polls a zone
that will never answer, forever. Each failed poll burns real bus arbitration
time competing with legitimate zone-1 traffic — on one single-zone
installation this produced ~600 failed polls every 4-5 hours, enough to make
`ebus.connected` flap even though the bus itself was fine.

**Fix:** set `zone_count` in the add-on configuration to the real number of
zones your installation has (most residential installs are `1`). The add-on
filters the polling config to match — zones above `zone_count` are no longer
auto-polled. If you don't know the number offhand, check the heat pump's own
installation paperwork or its physical panel (Settings → Heating circuits).

The add-on doesn't yet auto-detect this — it trusts whatever `zone_count` is
set to, which is easy to leave at the wrong value without noticing, since
nothing in the UI flags the mismatch. Auto-detecting it (e.g. probing each
zone's registers once at start-up and dropping any that don't answer from the
poll set) is a reasonable future improvement; it hasn't been built yet.

## Prevention: turn off `auto_update` for the add-on

Supervisor's automatic add-on update is what kills ebusd mid-connection in the
first place. In *Settings → Apps → Vaillant Genia Air*, switch **Auto update**
off and update deliberately instead. The add-on being a few days behind is a much
smaller problem than eighteen hours of silently stale telemetry.

If you can, also put the adapter on a remotely switchable socket as a last-resort
recovery path.

> ⚠️ If that socket also powers the appliance, **never cut it while the compressor
> is running.** Check that compressor modulation is `0` first. Losing telemetry for
> a while is an inconvenience; an abrupt power cut on a loaded compressor is damage.
