PY ?= python

.PHONY: setup test lint demo backtest simulate cost serve validate-infra bundle deploy-primary deploy-standby

setup:            ## virtualenv with dev dependencies
	$(PY) -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

test:             ## 63 tests, including the Lambda handlers against mocked AWS
	$(PY) -m pytest -q

lint:
	ruff check .

demo:             ## replay the first hours of 25 April 2015 through the real writer and aggregator
	$(PY) -m surakshit demo

backtest:         ## activation rule over every M5+ quake near Nepal since 2015
	$(PY) -m surakshit activation-backtest

simulate:         ## surge simulation, six designs x three surge sizes (about 90 seconds)
	$(PY) -m surakshit simulate

cost:             ## idle and per-event cost model
	$(PY) -m surakshit cost

serve:            ## local API and web page on http://localhost:8000
	$(PY) -m surakshit serve

validate-infra:   ## lint the CloudFormation/SAM templates
	cfn-lint infra/template.yaml infra/waf.yaml

bundle:           ## Lambda code: only the package, no numpy/matplotlib (boto3 is in the runtime)
	rm -rf build/lambda && mkdir -p build/lambda
	cp -r surakshit build/lambda/ && rm -rf build/lambda/surakshit/__pycache__ build/lambda/surakshit/aws/__pycache__
	rm build/lambda/surakshit/simulate.py build/lambda/surakshit/report.py build/lambda/surakshit/api.py \
	   build/lambda/surakshit/local.py build/lambda/surakshit/cost.py build/lambda/surakshit/__main__.py
	touch build/lambda/requirements.txt

deploy-primary: bundle   ## see infra/README.md for the parameters
	cd infra && sam build && sam deploy --guided --region ap-south-1 --stack-name surakshit-primary

deploy-standby: bundle
	cd infra && sam build && sam deploy --guided --region ap-southeast-1 --stack-name surakshit-standby
