#!/bin/sh
# apt-get update on a CI runner, as root, that gives up on a mirror that hangs and tries again,
# instead of waiting until the job times out (a runner's Ubuntu mirror once hung it for 20 minutes).
# The timeouts stay set for the rest of the job, for every later apt-get (Playwright's too).
# SPDX-License-Identifier: Apache-2.0
set -u
printf '%s\n' 'Acquire::http::Timeout "20";' 'Acquire::https::Timeout "20";' 'Acquire::Retries "3";' \
    > /etc/apt/apt.conf.d/99-ci-timeouts || exit 1
for try in 1 2 3; do
    if timeout 150 apt-get update -qq; then
        exit 0
    fi
    echo "::warning::apt-get update failed or hung (try $try of 3)"
    sleep 15
done
echo "::error::apt-get update failed three times"
exit 1
