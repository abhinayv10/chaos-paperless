# Minimal image used only as a short-lived sidecar that joins a sandbox
# container's network namespace to add / remove a tc netem qdisc.
# Built locally by `python -m chaos up`; never pushed anywhere.
FROM docker.io/library/alpine:3.20
RUN apk add --no-cache iproute2
