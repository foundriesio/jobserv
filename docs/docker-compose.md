# Quick Start with docker-compose

docker-compose provides a quick way to evaluate and test the JobServ without
having to worry about Kubernetes. The following steps should get you going:

## Build a local docker image:
~~~
  docker build --build-arg APP_VERSION=local -t jobserv ./
~~~

## Launch the system
~~~
  # The first time will take about a minute to for the DB and HAProxy services
  # to initialize
  docker-compose up
~~~

## Registering a Worker
From another x-term or machine you need to verify the host is addressable. This
can be done by IPv4 or the system's host name. A quick check would look like:
~~~
  curl http://doanac-laptop/health/runs/
  curl http://192.168.0.104/health/runs/
~~~

Assuming a $JOBSERV_HOST is set to the instance you can register a host with:
~~~
  # pick a directory to host the runner in
  mkdir $HOME/jobserv-worker
  cd $HOME/jobserv-worker
  curl http://$JOBSERV_HOST/worker > jobserv_worker.py
  chmod +x jobserv_worker.py
  ./jobserv_worker.py register http://$JOBSERV_HOST amd64

  # the worker is now registered but not *enlisted*, so it won't be able to
  # handle Runs. From the JobServ's host, view you host and then enlist it
  docker exec -it $(docker ps --filter name=jobserv_api -q) flask worker list
  docker exec -it $(docker ps --filter name=jobserv_api -q) flask worker enlist <host>
~~~

The worker is now ready, and you can have it manually check in with the JobServ
by running "./jobserv_worker.py check" or just run it in a loop with
"./jobserv_worker.py loop".


## Setting up a Project
Setting up a project can be done from the JobServ container. A quick example
project could be:
~~~
  # Create a Project
  docker exec -it $(docker ps --filter name=api -q) \
    flask project create home-poller

  # Set up a Trigger that will kick off builds
  docker exec -it $(docker ps --filter name=api -q) \
    flask project add-trigger \
      -u doanac \
      -t git_poller \
      -r https://github.com/foundriesio/jobserv \
      -f .jobserv.yml \
      home-poller
~~~

## Forcing a Build
You can force the git poller to trigger a build by doing the following:
~~~
  # Get a shell inside the git-poller:
  docker exec -it $(docker ps --filter name=git-poller -q) /bin/sh

  # Look at the current SHA the poller sees the project at:
  cat /data/artifacts/git_poller_cache.json

  # Edit the current SHA with an older SHA from the project
  # The next time the poller runs, it will detect a change and trigger a build.
~~~
