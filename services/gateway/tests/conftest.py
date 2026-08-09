import os

os.environ["SERVICE_TOKEN"] = "test-service-token"
os.environ["OPERATOR_TOKEN"] = "test-operator-token"
# Tests call the ASGI app in-process, the same as local development: nothing sits in
# front of the gateway to append a trustworthy entry, so trusting one -- the default,
# which assumes the one hop Render puts in front of production -- would let a caller's
# own X-Forwarded-For value pick its own rate-limit key. See config.py's trusted_proxy_hops.
os.environ["TRUSTED_PROXY_HOPS"] = "0"
