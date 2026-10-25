PY ?= python3
REF = $(PY) harness/referee.py
# The gateway these targets drive: this repo's own gateway/ if it has one (the template clone, or an
# --with-gateway install), else the shared one named in .devteam-home (written by install.sh).
DEVTEAM_HOME ?= $(shell cat .devteam-home 2>/dev/null || pwd)
GW ?= $(if $(wildcard gateway/tiers.toml),gateway,$(DEVTEAM_HOME)/gateway)
PORT ?= $(or $(shell grep -s '^GATEWAY_PORT=' $(GW)/.env | cut -d= -f2 | cut -d' ' -f1),4000)
PROFILE_ENV = PROFILE=$${PROFILE:-$$(grep -s '^PROFILE=' $(GW)/.env | cut -d= -f2 | cut -d' ' -f1)}

.PHONY: doctor verify status ledger reap tokens harness-test gateway gateway-stop gateway-status probe \
        probe-identity profile tiers routes service service-uninstall standalone

doctor:            ## preflight: gateway (identity, token, profile), deps, ports, credentials, git state
	$(REF) --strict doctor

verify:            ## every gate on the current tree (the definition of done)
	$(REF) --strict --inline gate

status:            ## task states, small JSON
	$(REF) status

ledger:            ## regenerate work/ledger.md
	$(REF) ledger

reap:              ## remove merged worktrees, branches and logs
	$(REF) reap

tokens:            ## this repo's token/cost telemetry from Claude Code transcripts
	$(PY) harness/tokens.py

gateway:           ## start the shared gateway in the background (log: <gateway home>/.loop/gateway.log)
	@curl -fs http://127.0.0.1:$(PORT)/health/liveliness >/dev/null 2>&1 && echo "gateway already up on :$(PORT)" || $(GW)/start.sh --daemon

gateway-stop:      ## stop the gateway started by `make gateway`
	@f=$(GW)/../.loop/gateway.pid; test -f $$f && kill $$(cat $$f) && rm -f $$f && echo stopped || echo "not running (or run by the macOS service: make service-uninstall)"

gateway-status:    ## is the gateway up?
	@curl -fs http://127.0.0.1:$(PORT)/health/liveliness >/dev/null && echo "up on :$(PORT) ($(GW))" || (echo "down: run make gateway"; exit 1)

tiers:             ## validate tiers.toml and every selectable profile, and render the gateway config
	$(PY) $(GW)/render.py

profile:           ## role -> tier -> model -> data note (PROFILE=<name> make profile to preview another)
	@cd $(GW) && $(PROFILE_ENV) $(PY) -c "import hooks,tiers;t=tiers.load_tiers();n,r=hooks.active_profile();print('profile',n);[print(f'  role-{k:16} -> {v:13} {t[v][\"model\"]:52} {t[v][\"data\"]}') for k,v in sorted(r.items())]"

probe:             ## live check of every tier with your real keys (< 1 cent); `make probe T=<tier>` for one
	$(PY) $(GW)/probe.py $(T)

probe-identity:    ## is the advanced tier really a different model from the cheap one? (a few cents)
	$(PY) $(GW)/probe.py --identity

routes:            ## per-profile requests, fallbacks, failures, tokens, cost, latency (all gateway clients)
	$(PY) $(GW)/routes.py

service:           ## macOS: run the gateway as a LaunchAgent (starts at login, restarts on crash)
	$(GW)/service/macos.sh install

service-uninstall: ## macOS: remove the LaunchAgent
	$(GW)/service/macos.sh uninstall

standalone:        ## put claude-custom on your PATH (dry run; add APPLY=1 to do it)
	$(DEVTEAM_HOME)/standalone/install.sh $(if $(APPLY),--apply)

harness-test:      ## test the harness (and the gateway, where this repo has one) — no API keys needed
	$(PY) -m pytest -q -p no:cacheprovider harness/tests $(wildcard gateway/tests)
