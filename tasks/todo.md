# Duepi v1.3.0 implementation

- [x] Workstream A: automatic device discovery
- [x] Workstream B: alarms, presets, last seen, and accumulating sensors
- [x] Workstream C: translations, HACS compliance, release workflow, and README
- [x] Push the branch and verify GitHub Actions
- [x] Generate release notes automatically and display the original brand icon

# Backlog

- [ ] **Local server transport (DNS redirect, no Duepi cloud)** — What: HA listens on TCP :3000; a local DNS A record sends the module's `duepiwebserverN.com` to HA; the module logs in as `ESC slave:<code>#` and answers the same AES frames. Why: works with no internet and no Duepi relay. Cons: MyDPRemote and dpremoteiot stop working (one module connection at a time) unless HA also proxies to the real relay. Context: proven read-only on 2026-11-06 with a throwaway fake relay script; reuse the frame/crypto code from `relay.py`. Depends on: relay transport (#16) shipped.
- [ ] **Relay reconfigure / reauth step** — What: let users change the relay server, port or device code of an existing relay entry, and raise a reauth after repeated decrypt failures (wrong code). Why: today a changed code only gives "unavailable" until the entry is deleted and re-added. Context: raised by the v2.0.0 ship review (adversarial pass). Depends on: v2.0.0.
- [ ] **Relay hardening left from the v2.0.0 ship review** — What: (1) let a turn-off skip the 20 s `LOCK_WAIT`, or make waiting power/setpoint writes give way to it, so a slow relay cannot drop the safety command; (2) drop a setpoint held for an HA start when the stove is seen off again after that start, so a later panel restart keeps its own target; (3) during the first minute after loading, report "integration just loaded" instead of "the stove is not heating" for power changes. Why: informational findings, not blocking. Depends on: v2.0.0.
