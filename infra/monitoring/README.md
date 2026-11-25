# Monitoring skeleton (Phase 1 placeholder).
#
# Provisioning of Prometheus/Grafana/OTel collectors belongs to a later phase.
# No scrape targets, endpoints, tokens, or credentials are committed here.

# Later-phase wiring outline:
#   * api:    expose /metrics on the private network only
#   * worker: expose metrics via a private exporter sidecar or push metrics
#   * litellm: scrape the container's private metrics endpoint
#   * postgres/redis: use the platform-managed exporters
#   * alerts: route to the on-call tool configured in the secrets manager
