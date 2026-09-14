RELEASE_ID ?= $(shell git rev-parse --short HEAD)
STACK ?= s3-zip-archiver

.PHONY: test build check-clean deploy smoke rollback clean

test:
	python -m pytest -q

build:
	sam build

# A Lambda version is only traceable to a commit if the deployed tree is that
# commit. Version 2 of the first stack was deployed from an uncommitted
# template, which is exactly what this prevents.
check-clean:
	@test -z "$$(git status --porcelain)" || \
		(echo "working tree has uncommitted changes; commit or stash them before deploying" && exit 1)

# The commit id is passed as ReleaseId, which the template uses as
# AutoPublishCodeSha256, so every deployed commit publishes a new version.
# ReleaseId has no default, so a plain `sam deploy` without it fails instead
# of silently skipping the version.
deploy: check-clean build
	sam deploy --parameter-overrides ReleaseId=$(RELEASE_ID)

smoke:
	STACK=$(STACK) scripts/smoke_test.sh

# make rollback VERSION=3
rollback:
	@test -n "$(VERSION)" || (echo "usage: make rollback VERSION=<n>" && exit 1)
	aws lambda update-alias \
		--function-name $$(aws cloudformation describe-stacks --stack-name $(STACK) \
			--query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text) \
		--name live --function-version $(VERSION)

clean:
	rm -rf .aws-sam
