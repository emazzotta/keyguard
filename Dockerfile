FROM python:3.15.2-alpine3.24@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd

ENV PYTHONPATH=/opt/keyguard-passkey \
    PYTHONSAFEPATH=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KEYGUARD_PASSKEY_BIND=0.0.0.0 \
    KEYGUARD_PASSKEY_PORT=8080 \
    KEYGUARD_PASSKEY_STORE=/store

COPY LICENSE /opt/keyguard-passkey/
COPY keyguard_passkey /opt/keyguard-passkey/keyguard_passkey

USER 1001:1001
WORKDIR /store
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --start-interval=1s \
    CMD wget -q -O /dev/null "http://127.0.0.1:${KEYGUARD_PASSKEY_PORT}/healthz"

ENTRYPOINT ["python", "-m", "keyguard_passkey"]
CMD ["serve"]
