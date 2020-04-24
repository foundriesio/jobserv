#!/bin/sh -e

if [ ! -d "/data/workers" ] ; then
  mkdir -p /data/workers
fi

exec flask monitor-workers
