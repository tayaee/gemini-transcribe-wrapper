#!/bin/bash -x
# Developer tool: install the local repo and run gtw.
uv -q tool install -e .
gtw $*
