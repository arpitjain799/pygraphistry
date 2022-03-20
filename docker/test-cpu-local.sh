#!/bin/bash
set -ex

echo "CONFIG"

PYTHON_VERSION=${PYTHON_VERSION:-3.6}
PIP_DEPS=${PIP_DEPS:--e .[dev]}
WITH_NEO4J=${WITH_NEO4J:-0}
WITH_LINT=${WITH_LINT:-1}
WITH_TYPECHECK=${WITH_TYPECHECK:-1}
WITH_BUILD=${WITH_BUILD:-1}
TEST_CPU_VERSION=${TEST_CPU_VERSION:-latest}

NETWORK=""
if [ "$WITH_NEO4J" == "1" ]
then
    NETWORK="--net grph_net"
fi

if [ "$WITH_NEO4J" == "1" ]
then
    echo "WITH_NEO4J"
    ( cd ../test/db/neo4j && ./launch.sh )
fi

if [ "$WITH_BUILD" == "1" ]
then
    echo "WITH_BUILD"
    docker-compose build \
        --build-arg PYTHON_VERSION=${PYTHON_VERSION} \
        --build-arg PIP_DEPS="${PIP_DEPS}" \
        test-cpu
fi

echo "RUN"
docker run \
    --security-opt seccomp=unconfined \
    -e PYTEST_CURRENT_TEST=TRUE \
    -e WITH_NEO4J=$WITH_NEO4J \
    -e WITH_LINT=$WITH_LINT \
    -e WITH_TYPECHECK=$WITH_TYPECHECK \
    -e WITH_BUILD=$WITH_BUILD \
    --rm \
    ${NETWORK} \
    graphistry/test-cpu:${TEST_CPU_VERSION} \
        --maxfail=1 $@
