#!/bin/sh
set -eu
mkdir -p out
cc -Wall -Wextra -Werror -O2 -o out/sum sum.c
exec ./out/sum "$@"
