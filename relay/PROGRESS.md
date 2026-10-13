# Wave-3 relay agent progress (feat/simkit-w3-relay-snowyowl)

Owner ruling: container_route_ruling_2026-10-13.live_roles — live role calls go from the
container to a host-side Kong relay; the Kong key never enters the container.

## Milestones
- [x] M0 read brief, ruling, Kong skill rules, KongRoles, runner role attributes
- [x] M0 probe: Rancher Desktop container DNS: host.docker.internal = 192.168.127.254,
      gateway.docker.internal = 192.168.127.1; host has no 192.168.127.x interface
      (networking tunnel forwards to host loopback) => bind 127.0.0.1 only
- [x] M1 relay_roles.py + kong_relay_host.py + offline tests (40 pass) + README
- [ ] M2 ruff + full test suite
- [ ] M3 live end-to-end smoke, RELAY-RESULT.json
