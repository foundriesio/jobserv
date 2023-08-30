#!/bin/bash -ex
set -ex

HERE=$(dirname $(readlink -f $0))
cd $HERE

VENV=$(mktemp -d)
trap "[ -z $MYSQL ] || docker kill jobserv-db; rm -rf $VENV" EXIT

if [ -n "$MYSQL" ] ; then
	echo "INFO: Using mysql database, test execution will be slower"
	$HERE/run-mysqld.sh
	export SQLALCHEMY_DATABASE_URI='mysql+pymysql://root@localhost:3306/jobserv'
fi

if [ -z $SQLALCHEMY_DATABASE_URI ] ; then
	echo "WARNING: Using sqlite database - work queue testing will be skipped"
	export SQLALCHEMY_DATABASE_URI='sqlite://'
fi

python3 -m venv $VENV
$VENV/bin/pip3 install -U pip
$VENV/bin/pip3 install -U setuptools
$VENV/bin/pip3 install -r requirements.txt

$VENV/bin/pip3 install junitxml==0.7 python-subunit==1.3.0

set -o pipefail
PYTHONPATH=./ $VENV/bin/python3 -m subunit.run ${TEST-discover} \
	| $VENV/bin/subunit2junitxml --no-passthrough \
	| tee /archive/junit.xml
