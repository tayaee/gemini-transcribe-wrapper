./1-code-quality-gate.sh
./2-version.sh bump
./3-build.sh
git add -u
./5-push-tag-to-github.sh
./6-publish-to-pypi.sh
