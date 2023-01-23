TESTING PRs
The JobServ component can be thought of at a high level as something similar to
Jenkins. However, several design decisions have been made to make sure the
service can be highly available and horizontally scalable.

The main implementation of JobServ is at https://ci.foundries.io. This service
does lots of interesting CI including:

 * bare-metal testing for both Linux and Zephyr class devices
 * tradition CI building of code
 * building of containers using docker-in-docker
 * release deployment logic
 * handling about 20 different CI workers with no perceptible downtime
 * live updates of the system (including the k8s node updates of the backend
   MySQL cluster) while CI jobs are in progress.

## Yet Another CI Server?

We had a few seemingly simple requirements for a CI server. After looking at
several __open source__ implementations, none seemed to able to tick off all
these items:

 * *Horizontal scaleabilty* - The server must be stateless and scale out
   horizontally.

 * *No VPNs* - Rather than a model where the server pushes to workers, the
   workers should be able to sit in a private lab somewhere and pull work
   from the server.

 * *Containers* - These should be a first-class item. All builds should take
   place inside a container. There's even Docker-In-Docker these days in the
   event you need to build a container.

 * *Matrix Builds* - Modern projects rarely have a single "build". It will
   need to be built for different targets and/or toolchains and this should be
   directly built into the system in some similar fashion to Matrix Style
   builds in Jenkins.

 * *Continuous* - It should be capable of being upgraded in production w/o
    scheduling downtime.

 * *Screw the GUI* - Not totally true, but if you are spending much time
   inside your CI system than its failing you. The thing should just work in
   the background and point you to the failure when you need to know. A GUI
   is really a must, but getting everything else nailed down makes you care a
   whole lot less. That said we've built one:
   [gavel-ci](https://ci.foundries.io).

 * *Test Locally* - If everything important is happening inside a container,
   then it should be really simple to recreate a build at home in some
   simulated mode or develop a new project without having to push things into
   production and iterate dozens of times until a build passes.

 * *Simplicity* - We really aren't asking for that much here.


## Requirements

 * **Server** - The primary things are Python3, Flask, and SQLAlchemy.
 * **Worker** - Python3, python3-requests, Docker

The JobServ can be tested out for evaluation and development purposes in a
few minutes by user [docker-compose](docs/docker-compose.md).

## Project

The fundamental unit and driver in the JobServ is a "Project". These are
defined in simple, but really flexible YAML format. A Project will have one
or more "triggers" which the input/stimulus that "trigger" a "Build" of the
Project. Triggers can be something like a GitHub Pull Request or detecting
a change on a branch in a Git repository. A Build of a project will consist
of multiple Runs. This builds up a directory like model of data. eg:
~~~
  ProjectFoo/
    1/            # The first build of a project
      flake8/     # A flake8 Run in Build 1
      unit-test/  # A "./setup.py test" run for Build 1
    2/
      flake8/
      unit-test
  ProjectBar/
    1/             # Build 1 of ProjectBar
      checkpatch/  # A Run of checkpatch against the change
      compile/     # A Run that compiles the code
~~~

When a Build is created for a Project each Run will have "run definition". The
run definition basically takes the information from the Project.yml and fills
in what needs to take place for a single Run. The definition is a simple JSON
file that explains what needs to be done on the Worker.

## Runner / Simulator

When a Run is queued in the JobServ a "run definition" is created. This
definition is a simple JSON file that the worker can use to execute the Run.

One of the most powerful concepts of the JobServ is its "runner". The Runner is
a very simple Python3 application that can process a run definition. The neat
thing with the Runner is that you can set a "simulated=True" flag in the
run definition and it becomes a "simulator". This means it does the exact set
of operations that would happen in production, but it skips the steps of
communicating with the server for streaming the log files and uploading
artifacts.

Every Run's console.log includes a stanza near the top with instructions for
re-creating the run locally. eg:
~~~
  == 2017-08-15 14:03:07.362887: Steps to recreate inside simulator

      mkdir /tmp/sim-run
      cd /tmp/sim-run
      wget -O runner https://api.linarotechnologies.org/runner
      wget -O rundef.json https://jobserv.example.com/projects/Foo/builds/1/runs/compile-linux/.rundef.json
      # open rundef.json and update values for secrets
      PYTHONPATH=./runner python3 -m jobserv_runner.simulator -w `pwd` rundef.json
~~~

There's also a built-in [simulator](/docs/tutorial.md) to help develop new
Projects.


## Workers

Workers are sort of like a Jenkins slave. These should ideally be bare-metal
servers that can access the JobServ via HTTPS. A worker will register itself
with the JobServ. An administrator can then mark the worker as "enlisted"
which will enable it to handle Runs when it checks in. The worker periodically
checks in with the JobServ. This check-in lets the JobServ know the worker is
online and gives the JobServ the chance to schedule on Run on the worker. The
worker is a fairly simple Python3 script that knows how to update itself so
that managing workers in production is simple.

## Data Model

The data model is fairly trivial. At the root it has multiple Projects. A
Project has Builds. Builds are sequentially numbered starting with 1. Each
Build has one or more Runs. Runs can optionally have Tests that can optionally
have TestResults.


## Project Example

Here is a simple definition that expects to be triggered by a GitHub Pull
Request. (A webhook would be registered with a GitHub project that will then
trigger the JobServ when a pull-request occurs).

~~~
timeout: 5   # each run has 5 minutes to complete before being killed
triggers:
  - name: Python Style Project on GitHub
    type: github_pr
    runs:
      - name: unit-test
        container: linarotechnologies/python-builder
        script: unit-test
      - name: flake8
        container: linarotechnologies/python-builder
        script: flake8

scripts:
  flake8: |
    #!/bin/sh -ex
    pip3 install flake8
    flake8 ./

  unit-test: |
    #!/bin/sh -ex
    ./unit-test.sh
~~~


When triggered the JobServ will create a new "Build" containing two "Runs",
"unit-test" and "flake8". The Build will pass if both runs pass, otherwise it
will be marked as a failure. The Runs are each marked as "QUEUED", so that the
JobServ will know to schedule them when a worker is available.


## Deployment Diagram

This service is deployed into Kubernetes as follows:
~~~
                              inbound traffic
                                    +
                                    |
                                    |
                          +---------v-----------+
                          |                     |
                          |  load balancer      |
                          |                     |
                          +---------------------+
                            /       |         \
                           /        |          \
                          /         |           \
                         /          |            \
          +-------------v-+  +------v--------+  +-v-------------+
          |               |  |               |  |               |
          |  jobserv api  |  |  jobserv api  |  |  jobserv api  |
          |               |  |               |  |               |
          +---------------+  +---------------+  +---------------+

 +-------------------------+  +--------------------+  +-----------------------+
 |                         |  |                    |  |                       |
 | NFS Server (not HA)     |  |  MySQL             |  | Storage               |
 |  (for in progress runs) |  |                    |  |  (for build artifacts)|
 +-------------------------+  +--------------------+  +-----------------------+
~~~
