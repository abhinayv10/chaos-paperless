# Minimal image used only as a short-lived sidecar that joins a sandbox
# container's network namespace (tc netem delay / loss, iptables reject) or
# mounts the sandbox scratch volume (disk fill). Built locally by
# `python -m chaos up`; never pushed anywhere.
FROM docker.io/library/alpine:3.20
RUN apk add --no-cache iproute2 iptables
