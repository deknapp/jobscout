# jobscout as a running service. `make up` goes from an empty machine to a
# cluster that looks up careers boards on a schedule.

SHELL := /bin/bash
.DEFAULT_GOAL := help

CLUSTER  ?= jobscout
IMAGE    ?= jobscout
TAG      ?= dev
NS       ?= jobscout
OVERLAY  ?= deploy/overlays/kind
WEB      ?= http://localhost:8765
KEDA_VER ?= 2.17.2

export DOCKER_BUILDKIT := 1

.PHONY: help
help: ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

# --- local, no cluster ----------------------------------------------------

.PHONY: test
test: ## the test suite
	.venv/bin/python -m pytest -q

.PHONY: lint
lint: ## manifests and chart must render
	kubectl kustomize $(OVERLAY) > /dev/null
	@echo "manifests render"

# --- cluster --------------------------------------------------------------

.PHONY: up
up: cluster keda build load deploy secret ## everything
	@echo
	@echo "  web       $(WEB)"
	@echo "  status    make status"
	@echo "  try it    make discover"
	@echo

.PHONY: cluster
cluster: ## create the kind cluster
	@kind get clusters 2>/dev/null | grep -qx $(CLUSTER) \
	  || kind create cluster --name $(CLUSTER) --config deploy/kind/cluster.yaml --wait 120s
	kubectl config use-context kind-$(CLUSTER)

.PHONY: keda
keda: ## install KEDA, which owns the worker replica count
	@helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
	@helm repo update kedacore >/dev/null
	helm upgrade --install keda kedacore/keda \
	  --namespace keda --create-namespace --version $(KEDA_VER) --wait --timeout 5m

.PHONY: build
build: ## build the image
	docker build -t $(IMAGE):$(TAG) .

.PHONY: load
load: ## push the image into the kind node
	kind load docker-image $(IMAGE):$(TAG) --name $(CLUSTER)

.PHONY: deploy
deploy: ## apply the manifests
	kubectl apply -k $(OVERLAY)
	kubectl -n $(NS) rollout status statefulset/redis --timeout=180s
	kubectl -n $(NS) rollout status deployment/web --timeout=180s

.PHONY: secret
secret: ## put your own API key in the cluster, without writing it to a file
	@# One shell, not four. Make runs each recipe line in its own shell, so an
	@# `exit 0` on the guard line ends that shell and Make cheerfully carries
	@# on to the next -- which is how this printed "not set" and then created
	@# the secret with an empty value anyway.
	@if [ -z "$$ANTHROPIC_API_KEY" ]; then \
	  echo "ANTHROPIC_API_KEY is not set — leaving the key empty."; \
	  echo "The free stages (board discovery, reading ATS boards) work without it."; \
	  echo "The model stages do not. Export it and re-run 'make secret'."; \
	else \
	  kubectl -n $(NS) create secret generic jobscout-secrets \
	    --from-literal=ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY" \
	    --dry-run=client -o yaml | kubectl apply -f - ; \
	  kubectl -n $(NS) rollout restart deployment/web ; \
	  echo "key installed"; \
	fi

.PHONY: seed
seed: ## copy your existing ~/.jobscout into the cluster's volume
	@test -d $$HOME/.jobscout || { echo "no ~/.jobscout to copy"; exit 1; }
	kubectl -n $(NS) wait --for=condition=ready pod -l app.kubernetes.io/component=web --timeout=120s
	@# Named entries rather than ".", so the archive has no entry for the
	@# mount point itself. Extracting one makes tar try to chmod and utime
	@# /data, which the unprivileged user in the container cannot do -- and
	@# that failure alone would fail the whole copy, for nothing.
	@# spend.json is skipped: in the cluster the ledger lives in Redis, and a
	@# copied file would be a stale second answer to the same question.
	@cd $$HOME/.jobscout && tar -cf - $$(ls -A | grep -v '^spend.json$$') \
	  | kubectl -n $(NS) exec -i deploy/web -- \
	      tar -C /data --no-same-owner --no-same-permissions -xf -
	@echo "state copied into the volume"

.PHONY: down
down: ## delete the cluster
	kind delete cluster --name $(CLUSTER)

# --- driving it -----------------------------------------------------------

.PHONY: discover
discover: ## queue a discovery run now and watch the pool scale
	kubectl -n $(NS) create job --from=cronjob/queue-discovery queue-now-$$(date +%s)
	@echo "watching the worker pool — ctrl-C when you have seen enough"
	@$(MAKE) --no-print-directory watch

.PHONY: watch
watch: ## live: queue depth against worker count
	@scripts/watch.sh $(NS)

.PHONY: status
status: ## pods, jobs, autoscaler
	@kubectl -n $(NS) get pods,cronjobs,scaledobject 2>/dev/null

.PHONY: logs
logs: ## follow the worker logs
	kubectl -n $(NS) logs -l app.kubernetes.io/component=worker -f --tail=20 --max-log-requests 10
