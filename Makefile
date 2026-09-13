RELEASE_ID ?= $(shell git rev-parse --short HEAD)

.PHONY: test build deploy rollback clean

test:
	python -m pytest -q

build:
	sam build

# The release id ends up in the function's environment, so every commit that
# gets deployed publishes a new Lambda version even if the image is identical.
deploy: build
	sam deploy --parameter-overrides ReleaseId=$(RELEASE_ID)

# make rollback VERSION=3
rollback:
	@test -n "$(VERSION)" || (echo "usage: make rollback VERSION=<n>" && exit 1)
	aws lambda update-alias \
		--function-name $$(aws cloudformation describe-stacks --stack-name s3-zip-archiver \
			--query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text) \
		--name live --function-version $(VERSION)

clean:
	rm -rf .aws-sam
