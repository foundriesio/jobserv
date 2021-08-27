#!/usr/bin/python3
# Copyright (C) 2017 Linaro Limited
# Author: Andy Doan <andy.doan@linaro.org>

import base64
import sys
import urllib.request

if len(sys.argv) != 2:
    sys.exit("Usage: %s <internal url>" % sys.argv[0])
url = sys.argv[1]


class NoRedirection(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):
        return response

    https_response = http_response


# Find the auth token. Python's netrc implementation has a bug forcing files
# to have a password entry. Our github entries only use "login". This is a
# minimal parser that depends on simple.py to produce a sane entry
token = None
with open("/root/.netrc") as f:
    matched = False
    for line in f:
        key, val = line.strip().split(" ", 2)
        if key == "machine" and val in url:
            matched = True
        if matched and key == "password":
            token = val
            break

if not token:
    sys.exit(".netrc missing entry for " + url)

# return the 302 redirect value for the url
opener = urllib.request.build_opener(NoRedirection)
token = base64.encodebytes(token.encode()).decode().strip()
headers = {
    "X-EXPIRATION": "7200",  # make the link valid for 2 hours
    "Authorization": "Basic " + token,
}
req = urllib.request.Request(url, headers=headers)
resp = opener.open(req)
if resp.status != 302:
    raise RuntimeError(
        "Expected a 302 redirect from URL(%s):\n%s"
        % (resp.status, resp.read().decode())
    )
print(resp.headers["location"])
